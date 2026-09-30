"""Voicebox backend supervisor: storage routing and restart backoff.

The storage assertions are the important ones. Voicebox defaults its data dir to
``Path("data").resolve()`` under the working directory, and its models dir feeds
straight into HF_HUB_CACHE — both landing on C:, the drive with the least room
on this machine. Passing those knobs only at install time would work until the
first supervisor-initiated restart silently reverted to the defaults, so they
must be on EVERY spawn.

No backend is launched: subprocess.Popen is stubbed and the spawn arguments are
inspected.
"""

import asyncio
from pathlib import Path

import pytest

import core.tts_supervisor as sup
from app.reconnect import grow_cooldown
from core.tts_supervisor import RESTART_BASE_S, RESTART_CAP_S, VoiceboxSupervisor


class _FakePopen:
    """Records how the backend would have been launched."""

    last: dict = {}

    def __init__(self, argv, env=None, **kwargs):
        _FakePopen.last = {"argv": argv, "env": env or {}, "kwargs": kwargs}
        self.returncode = None

    def poll(self):
        return None

    def terminate(self):
        self.returncode = 0


@pytest.fixture
def spawned(tmp_path, monkeypatch):
    """Spawn the backend against throwaway dirs; return the recorded launch."""
    models = tmp_path / "models"
    data = tmp_path / "data"
    monkeypatch.setattr(
        sup,
        "get_voicebox_config",
        lambda: {
            "host": "127.0.0.1",
            "port": 8000,
            "models_dir": str(models),
            "data_dir": str(data),
            "backend_cmd": str(tmp_path / "voicebox-backend.exe"),
        },
    )
    monkeypatch.setattr(sup.subprocess, "Popen", _FakePopen)

    s = VoiceboxSupervisor()
    assert s._spawn() is True
    return _FakePopen.last, models, data


# ── Storage routing ──────────────────────────────────────────────────────────
def test_data_dir_is_passed_on_every_spawn(spawned):
    launch, _models, data = spawned
    argv = launch["argv"]
    assert "--data-dir" in argv
    assert argv[argv.index("--data-dir") + 1] == str(data)


def test_models_dir_is_in_the_child_env(spawned):
    """VOICEBOX_MODELS_DIR is read at backend import time, so it must be
    inherited by the child — it cannot be applied after the process is up."""
    launch, models, _data = spawned
    assert launch["env"]["VOICEBOX_MODELS_DIR"] == str(models)


def test_huggingface_cache_is_redirected(spawned):
    """The multi-GB model pull must not land in the C: user profile."""
    launch, models, _data = spawned
    assert launch["env"]["HF_HUB_CACHE"] == str(models)
    assert launch["env"]["HF_HOME"] == str(models)


def test_storage_dirs_are_created(spawned):
    _launch, models, data = spawned
    assert models.is_dir() and data.is_dir()


def test_host_and_port_are_passed(spawned):
    launch, _m, _d = spawned
    argv = launch["argv"]
    assert argv[argv.index("--port") + 1] == "8000"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"


def test_refuses_to_spawn_if_storage_is_unusable(tmp_path, monkeypatch):
    """Better no local voice than a silent multi-GB download onto the full drive."""
    monkeypatch.setattr(
        sup,
        "get_voicebox_config",
        lambda: {
            "host": "127.0.0.1",
            "port": 8000,
            "models_dir": str(tmp_path / "m"),
            "data_dir": str(tmp_path / "d"),
            "backend_cmd": str(tmp_path / "voicebox-backend.exe"),
        },
    )
    monkeypatch.setattr(sup.subprocess, "Popen", _FakePopen)

    def _boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert VoiceboxSupervisor()._spawn() is False


def test_missing_backend_does_not_raise(tmp_path, monkeypatch):
    """A missing install costs the local voice, never Era's process."""
    monkeypatch.setattr(
        sup,
        "get_voicebox_config",
        lambda: {
            "host": "127.0.0.1",
            "port": 8000,
            "models_dir": str(tmp_path / "m"),
            "data_dir": str(tmp_path / "d"),
            "backend_cmd": "",
        },
    )
    monkeypatch.setattr(sup, "discover_backend_cmd", lambda: None)
    assert VoiceboxSupervisor()._spawn() is False


# ── Restart backoff ──────────────────────────────────────────────────────────
def test_backoff_grows_and_caps():
    """Reuses grow_cooldown rather than reimplementing backoff maths."""
    delay = RESTART_BASE_S
    seen = []
    for _ in range(12):
        seen.append(delay)
        delay = grow_cooldown(delay, RESTART_CAP_S)

    assert seen[0] == RESTART_BASE_S
    assert seen == sorted(seen), "backoff must never shrink"
    assert delay == RESTART_CAP_S
    assert max(seen) <= RESTART_CAP_S


def test_local_restart_is_far_faster_than_the_quota_cooldown():
    """A local process that died should return in seconds, not minutes — it is
    the thing Era falls back TO when the cloud is already gone."""
    assert RESTART_BASE_S < 60.0
    assert RESTART_CAP_S <= 300.0


# ── Readiness ────────────────────────────────────────────────────────────────
def test_model_not_ready_before_preload():
    """model_ready gates the switch INTO the local voice, so it must start False
    — otherwise Era could switch to a voice that cannot speak."""
    assert VoiceboxSupervisor().model_ready is False


def test_preload_reports_failure_when_backend_is_down(monkeypatch):
    async def _no(*a, **k):
        return False

    monkeypatch.setattr(sup.VOICEBOX, "health", _no)
    s = VoiceboxSupervisor()
    assert asyncio.run(s.preload()) is False
    assert s.model_ready is False


def test_watchdog_retries_a_stuck_model_load_on_a_healthy_backend(monkeypatch):
    """A backend that answers /health but never loaded the model (a one-off
    preload failure, e.g. a transient WinError 64 at boot) must not leave
    model_ready stuck False forever just because the process never crashed."""

    async def _healthy(*a, **k):
        return True

    preload_calls = []

    async def _preload():
        preload_calls.append(1)
        s._model_ready = True
        return True

    monkeypatch.setattr(sup.VOICEBOX, "health", _healthy)
    s = VoiceboxSupervisor()
    s.preload = _preload
    s._model_ready = False

    async def _run_one_cycle():
        s._stopping = False
        calls = []

        async def _sleep(_):
            calls.append(1)
            if len(calls) > 1:  # let exactly one loop body run before stopping
                s._stopping = True

        monkeypatch.setattr(sup.asyncio, "sleep", _sleep)
        await s._watch()

    asyncio.run(_run_one_cycle())
    assert preload_calls == [1]
    assert s.model_ready is True


def test_watchdog_does_not_reload_an_already_ready_model(monkeypatch):
    """The common case (healthy + already loaded) must not re-hit /models/load
    every poll — that would be wasted work on every 20s tick forever."""

    async def _healthy(*a, **k):
        return True

    preload_calls = []

    async def _preload():
        preload_calls.append(1)
        return True

    monkeypatch.setattr(sup.VOICEBOX, "health", _healthy)
    s = VoiceboxSupervisor()
    s.preload = _preload
    s._model_ready = True

    async def _run_one_cycle():
        s._stopping = False
        calls = []

        async def _sleep(_):
            calls.append(1)
            if len(calls) > 1:
                s._stopping = True

        monkeypatch.setattr(sup.asyncio, "sleep", _sleep)
        await s._watch()

    asyncio.run(_run_one_cycle())
    assert preload_calls == []


def test_no_watchdog_when_voicebox_is_not_installed(monkeypatch):
    """Nothing to supervise means no watchdog — otherwise Era logs the same
    'backend down' line forever on a machine that simply has no Voicebox."""

    async def _no(*a, **k):
        return False

    monkeypatch.setattr(sup.VOICEBOX, "health", _no)
    monkeypatch.setattr(sup, "discover_backend_cmd", lambda: None)

    s = VoiceboxSupervisor()
    asyncio.run(s.start())
    assert s._task is None
    assert s.model_ready is False
