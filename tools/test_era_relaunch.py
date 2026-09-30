"""'reboot Era One': phrase match, tool diversion, relaunch flag, and the
supervisor's relaunch / crash / hang handling (end-to-end with a stand-in Era)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from core import era_relaunch
from core.self_healing_router import divert_era_reboot

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR = REPO / "era_supervisor.py"


# ── phrase ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "reboot Era One",
        "Reboot Era One.",
        "reboot era 1",
        "reboot Era-One!",
        "Era, reboot Era One",
        "Hey Era, reboot Era One.",
        "Shazam, reboot Era One",
        "  reboot   era one  ",
    ],
)
def test_reboot_phrase_matches(text):
    assert era_relaunch.is_reboot_command(text, ["shazam"])


@pytest.mark.parametrize(
    "text",
    [
        "",
        "reboot",
        "reboot the computer",
        "don't reboot Era One",
        "why did you reboot Era One",
        "reboot Era One tomorrow",
        "restart the laptop",
        "shut down Era One",
        "reboot Era Two",
    ],
)
def test_reboot_phrase_rejects(text):
    assert not era_relaunch.is_reboot_command(text, ["shazam"])


# ── the model's tool choice is diverted, never the machine ─────────────────


def test_computer_restart_becomes_era_relaunch():
    tool, params = divert_era_reboot("computer_settings", {"action": "restart"}, "reboot Era One")
    assert (tool, params) == ("shutdown_era", {"relaunch": True})


def test_plain_shutdown_becomes_relaunch():
    tool, params = divert_era_reboot("shutdown_era", {}, "reboot era one")
    assert tool == "shutdown_era" and params["relaunch"] is True


def test_machine_restart_is_left_alone():
    tool, params = divert_era_reboot(
        "computer_settings", {"action": "restart"}, "Era, reboot the computer"
    )
    assert (tool, params) == ("computer_settings", {"action": "restart"})


def test_unrelated_tool_untouched():
    assert divert_era_reboot("web_search", {"q": "x"}, "reboot Era One") == (
        "web_search",
        {"q": "x"},
    )


# ── relaunch request ────────────────────────────────────────────────────────


def test_request_relaunch_writes_flag_clears_stop_and_audits(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "home").mkdir()
    era_relaunch.stop_flag().write_text("stop", encoding="utf-8")

    assert era_relaunch.request_relaunch("user said it", "voice")

    assert not era_relaunch.stop_flag().exists()
    flag = json.loads(era_relaunch.relaunch_flag().read_text(encoding="utf-8"))
    assert flag["source"] == "voice" and flag["requested_at"] > 0
    events = [json.loads(x) for x in era_relaunch.lifecycle_log().read_text().splitlines()]
    assert events[-1]["event"] == "relaunch.requested"


def test_supervisor_and_era_share_the_audit_log(tmp_path):
    env = {**os.environ, "ERA_LOG_DIR": str(tmp_path), "ERA_HOME": str(tmp_path)}
    code = (
        "import era_supervisor as s; from core import era_relaunch as r;"
        "print(s.LIFECYCLE_LOG.resolve() == r.lifecycle_log().resolve(),"
        " s.RELAUNCH_FLAG == r.relaunch_flag(), s.HEARTBEAT == r.heartbeat_file())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True
    )
    assert out.stdout.split() == ["True", "True", "True"], out.stderr


# ── dispatcher: relaunch path writes era.relaunch, never era.stop ───────────


def test_shutdown_era_relaunch_writes_relaunch_not_stop(tmp_path, monkeypatch):
    from app.era_live import tool_dispatch

    monkeypatch.setenv("ERA_HOME", str(tmp_path))
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ERA_SUPERVISED", "1")

    class _NoThread:  # the real thread would os._exit the test process
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(tool_dispatch.threading, "Thread", _NoThread)

    class _UI:
        def write_log(self, *_):
            pass

    class _Era(tool_dispatch.ToolDispatchMixin):
        def __init__(self):
            self.ui = _UI()
            self.spoken = []
            self._shutdown_requested = False
            self._shutdown_awaiting_confirm = False
            self._is_speaking = False

        def speak(self, text):
            self.spoken.append(text)

    import era_dispatch

    monkeypatch.setattr(era_dispatch, "current_job", lambda: None)
    era = _Era()
    assert era._shutdown_era({"relaunch": True}) == "Relaunching..."
    assert (tmp_path / "era.relaunch").exists()
    assert not (tmp_path / "era.stop").exists()
    assert era._shutdown_era({"relaunch": True}).endswith("already in progress.")


@pytest.mark.parametrize("relaunch, closes", [(False, 1), (True, 0)])
def test_shutdown_closes_the_hud_window_but_a_reboot_keeps_it(
    tmp_path, monkeypatch, relaunch, closes
):
    import os

    from app.era_live import tool_dispatch
    from app.hud import browser

    monkeypatch.setenv("ERA_HOME", str(tmp_path))
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ERA_SUPERVISED", "1")
    targets = []

    class _CaptureThread:
        def __init__(self, target=None, **_k):
            targets.append(target)

        def start(self):
            pass

    class _Exit(Exception):
        pass

    def _exit(_code):
        raise _Exit

    closed = []
    monkeypatch.setattr(tool_dispatch.threading, "Thread", _CaptureThread)
    monkeypatch.setattr(tool_dispatch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(os, "_exit", _exit)
    monkeypatch.setattr(browser, "close_hud_windows", lambda: closed.append(1) or 1)

    class _UI:
        def write_log(self, *_):
            pass

    class _Era(tool_dispatch.ToolDispatchMixin):
        def __init__(self):
            self.ui = _UI()
            self._shutdown_requested = False
            self._shutdown_awaiting_confirm = False
            self._is_speaking = False

        def speak(self, _text):
            pass

    import era_dispatch

    monkeypatch.setattr(era_dispatch, "current_job", lambda: None)
    _Era()._shutdown_era({"relaunch": relaunch})
    with pytest.raises(_Exit):
        targets[-1]()
    assert len(closed) == closes


def test_close_hud_windows_is_inert_under_pytest():
    from app.hud import browser

    assert browser.close_hud_windows() == 0  # never touches the user's live HUD


def test_hud_process_match_is_the_dedicated_profile_only(monkeypatch):
    import psutil

    from app.hud import browser

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            if self.pid == 1:
                return ["msedge.exe", f"--user-data-dir={browser.profile_dir()}", "--app=x"]
            return ["msedge.exe", "--profile-directory=Default"]

    monkeypatch.setattr(psutil, "Process", _Proc)
    assert browser._is_hud_process(1) is True
    assert browser._is_hud_process(2) is False


# ── supervisor end-to-end with a stand-in Era ──────────────────────────────

_STANDIN = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path
    home = Path(os.environ["ERA_HOME"]); home.mkdir(parents=True, exist_ok=True)
    runs = home / "runs.txt"
    n = int(runs.read_text()) + 1 if runs.exists() else 1
    runs.write_text(str(n))
    mode = os.environ["STANDIN_MODE"]
    if n >= 2:
        (home / "era.stop").write_text("done")
        sys.exit(0)
    if mode == "relaunch":
        (home / "era.relaunch").write_text(json.dumps(
            {"requested_at": time.time(), "reason": "test", "source": "voice"}))
        sys.exit(0)
    if mode == "stuck_relaunch":
        (home / "era.relaunch").write_text(json.dumps(
            {"requested_at": time.time(), "reason": "test", "source": "voice"}))
        time.sleep(600)
    if mode == "hang":
        (home / "era.heartbeat").write_text("beat")
        time.sleep(600)
    if mode == "crash":
        sys.exit(3)
    """
)


def _run_supervisor(tmp_path: Path, mode: str, **env_extra: str) -> tuple[list[dict], float]:
    standin = tmp_path / "standin_main.py"
    standin.write_text(_STANDIN, encoding="utf-8")
    env = {
        **os.environ,
        "ERA_MAIN": str(standin),
        "ERA_HOME": str(tmp_path / "home"),
        "ERA_LOG_DIR": str(tmp_path / "logs"),
        "ERA_LOCK_FILE": str(tmp_path / "home" / "era.lock"),
        "ERA_STATUS_FILE": str(tmp_path / "home" / "status.json"),
        "ERA_RESTART_DELAY": "30",  # a crash restart would blow the time budget
        "ERA_RELAUNCH_DELAY": "0.2",
        "ERA_POLL": "0.2",
        "STANDIN_MODE": mode,
        **env_extra,
    }
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(SUPERVISOR)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.time() - t0
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = tmp_path / "logs" / "era_lifecycle.jsonl"
    events = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    assert (tmp_path / "home" / "runs.txt").read_text() == "2"
    return events, elapsed


def test_supervisor_relaunches_immediately_on_request(tmp_path):
    events, elapsed = _run_supervisor(tmp_path, "relaunch")
    assert [e["event"] for e in events] == ["relaunch.performed"]
    assert events[0]["source"] == "voice" and events[0]["forced"] is False
    assert elapsed < 20  # skipped the 30s crash delay
    assert not (tmp_path / "home" / "era.relaunch").exists()


def test_supervisor_kills_era_that_ignores_its_own_relaunch(tmp_path):
    events, _ = _run_supervisor(tmp_path, "stuck_relaunch", ERA_RELAUNCH_GRACE="1")
    assert events[-1]["event"] == "relaunch.performed" and events[-1]["forced"] is True


def test_supervisor_restarts_a_frozen_era(tmp_path):
    events, _ = _run_supervisor(tmp_path, "hang", ERA_HANG_TIMEOUT="1", ERA_RESTART_DELAY="0.2")
    names = [e["event"] for e in events]
    assert names == ["hang.detected", "restart.hang"]


def test_supervisor_restarts_after_crash_and_audits_it(tmp_path):
    events, _ = _run_supervisor(tmp_path, "crash", ERA_RESTART_DELAY="0.2")
    assert [e["event"] for e in events] == ["restart.crash"]
    assert events[0]["exit_code"] == 3
