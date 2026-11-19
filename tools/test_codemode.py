"""CodeMode session, revision, source and exact-promotion regression boundaries."""

import asyncio
import hashlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import codemode as cm
from core import codemode_cli as cli
from core import development as dev
from core import development_submission as submission
from core.cancel_registry import REGISTRY
from core.input_envelope import InputEnvelope
from core.llm.types import LlmError


@pytest.fixture(autouse=True)
def codemode_switched_on(monkeypatch):
    """PF-050: CodeMode is off by default; these cases test how it behaves when ON."""
    monkeypatch.setenv("ERA_CODEMODE", "on")


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "JOBS", tmp_path / "runtime/development")
    monkeypatch.setattr(dev, "_active", None)
    monkeypatch.setattr(dev, "_cancel_events", {})
    monkeypatch.setattr(cm, "ROOT", tmp_path / "runtime/codemode")
    monkeypatch.setattr(cm, "configured_profile", lambda: (str(tmp_path / "profile"), "sonnet"))
    (tmp_path / "core").mkdir()
    (tmp_path / "core/example.py").write_text("VALUE = 1\n")
    return tmp_path


def empty_context(request):
    return {"files": [], "checkout": "fixture", "revision": "fixture"}


class Advisory:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def generate(self, prompt, text):
        self.calls.append(prompt)
        await text("A current-source explanation.")
        return {"reply": "A current-source explanation.", "candidate": None}


@pytest.mark.parametrize(
    "value", ["Hello world.", 'An escaped "quote" and \\ path', "سواد الغيم", "emoji \U0001f680"]
)
def test_incremental_reply_never_leaks_candidate(value):
    raw = json.dumps({"reply": value, "candidate": {"files": [{"content": "secret code"}]}})
    previous = ""
    for size in range(1, len(raw) + 1):
        current = cli.reply_prefix(raw[:size])
        assert value.startswith(current) and current.startswith(previous)
        assert "secret code" not in current
        previous = current
    assert previous == value


def test_subscription_environment_cannot_silently_use_gateway(monkeypatch):
    for key in (
        *cli.GATEWAY_ENV_VARS,
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_SIMPLE",
    ):
        monkeypatch.setenv(key, "wrong-provider")
    monkeypatch.setenv("ERA_CLAUDE_KEEP_API_ENV", "1")
    env = cli.subscription_env("selected")
    assert env["CLAUDE_CONFIG_DIR"] == "selected"
    assert not any(
        key in env
        for key in (
            *cli.GATEWAY_ENV_VARS,
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_SIMPLE",
        )
    )


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: sys.executable)
    return cli.ClaudeSession("selected", str(uuid.uuid4()), tmp_path)


def test_cli_command_disables_machine_tools_and_keeps_exact_session(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    command = instance.command()
    assert "--safe-mode" in command and "--bare" not in command
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert command[command.index("--session-id") + 1] == instance.session_id
    instance.resumable = True
    command = instance.command()
    assert command[command.index("--resume") + 1] == instance.session_id
    assert "--continue" not in command and "--dangerously-skip-permissions" not in command


class FakeProcess:
    def __init__(self, frames):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.stdin = SimpleNamespace(write=lambda _: None, drain=self._drain, close=lambda: None)
        self.returncode = 0

    async def _drain(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", [None, "session", "no_result", "invalid_json", "auth_error", "oversize"]
)
async def test_stream_requires_bounded_matching_successful_result(tmp_path, monkeypatch, fault):
    instance = client(tmp_path, monkeypatch)
    text = json.dumps({"reply": "Verified stream.", "candidate": None})
    if fault == "invalid_json":
        text = "not json"
    if fault == "oversize":
        text = "x" * 500001
    frames = [
        {"type": "system", "session_id": "wrong" if fault == "session" else instance.session_id},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": text}}},
    ]
    if fault != "no_result":
        frames.append(
            {
                "type": "result",
                "is_error": fault == "auth_error",
                "result": "/login" if fault == "auth_error" else text,
            }
        )
    proc = FakeProcess(frames)

    async def spawn(args):
        return proc

    async def auth():
        pass

    monkeypatch.setattr(instance, "_spawn", spawn)
    monkeypatch.setattr(instance, "authenticate", auth)
    chunks = []

    async def emit(text):
        chunks.append(text)

    if fault:
        with pytest.raises(LlmError):
            await instance.generate("request", emit)
    else:
        result = await instance.generate("request", emit)
        assert result["reply"] == "".join(chunks) == "Verified stream."
        assert instance.resumable


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "third_party", "none", "oauth_token", "claude.ai"])
async def test_authentication_refuses_non_subscription_methods(tmp_path, monkeypatch, auth):
    instance = client(tmp_path, monkeypatch)
    proc = FakeProcess([])
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(json.dumps({"loggedIn": True, "authMethod": auth}).encode())
    proc.stdout.feed_eof()

    async def spawn(args):
        return proc

    monkeypatch.setattr(instance, "_spawn", spawn)
    if auth == "claude.ai":
        await instance.authenticate()
    else:
        with pytest.raises(LlmError, match="subscription login"):
            await instance.authenticate()


@pytest.mark.asyncio
async def test_explicit_mode_and_single_owned_advisory(repository, monkeypatch):
    manager = cm.CodeMode(cli_factory=Advisory, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)
    owner = InputEnvelope.create("Explain the coding workflow", "typed")
    events = []

    async def emit(kind, text, envelope):
        events.append((kind, text, envelope.id))

    with pytest.raises(ValueError, match="explicitly"):
        manager.start(owner, emit)
    manager.enter()
    assert manager.start(owner, emit)
    assert not manager.start(owner, emit)
    await manager._task
    assert manager.phase == "ready" and len(manager._cli.calls) == 1
    assert {event[2] for event in events} == {owner.id}
    assert REGISTRY.get(manager.work_id) is None
    stored = json.loads((cm.ROOT / manager.session_id / "state.json").read_text())
    assert "Explain the coding workflow" not in json.dumps(stored)
    manager.cancel(exit_mode=True)
    assert not manager.enabled


@pytest.mark.asyncio
async def test_correction_and_cancel_discard_old_output(repository, monkeypatch):
    started = asyncio.Event()

    class Slow(Advisory):
        async def generate(self, prompt, emit):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                started.set()
                try:
                    await asyncio.sleep(20)
                except asyncio.CancelledError:
                    await emit("Late text must be discarded")
                    raise
            return {"reply": "Corrected", "candidate": None}

    manager = cm.CodeMode(cli_factory=Slow, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    events = []

    async def emit(kind, text, owner):
        events.append(text)

    manager.start(InputEnvelope.create("Original", "typed"), emit)
    await started.wait()
    manager.start(InputEnvelope.create("Correction", "typed"), emit)
    await manager._task
    assert "Late text must be discarded" not in events and manager.phase == "ready"


def pending(manager, repository):
    job_id = "a" * 16
    live = repository / "core/example.py"
    stage = dev.JOBS / job_id / "workspace/core/example.py"
    stage.parent.mkdir(parents=True)
    stage.write_text("VALUE = 2\n")
    job = {
        "id": job_id,
        "state": "awaiting_approval",
        "source": "codemode",
        "session_id": manager.session_id,
        "turn_revision": manager.revision,
        "input_source": "typed",
        "approval_expires_at": manager._clock() + 900,
        "checks": {"passed": True},
        "files": [
            {
                "path": "core/example.py",
                "base": dev._hash(live),
                "candidate": dev._hash(stage),
                "new": False,
                "diff": "change",
            }
        ],
    }
    job["approval"] = hashlib.sha256(json.dumps(job["files"], sort_keys=True).encode()).hexdigest()
    dev._save(job)
    manager.job_id, manager.phase = job_id, "review"
    manager._approval_revision = manager.revision
    manager._token = REGISTRY.register("codemode-test-approval", "codemode")
    REGISTRY.finish(manager._token.cid)
    return job


@pytest.mark.parametrize("fault", ["expiry", "revision", "session", "cancel", "restart"])
def test_exact_approval_refuses_expiry_revision_cancel_restart(repository, monkeypatch, fault):
    manager = cm.CodeMode(cli_factory=Advisory, clock=lambda: 100)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    job = pending(manager, repository)
    if fault == "expiry":
        job["approval_expires_at"] = 100
        dev._save(job)
    elif fault == "revision":
        manager.revision += 1
    elif fault == "session":
        manager.session_id = "new-session"
    elif fault == "cancel":
        manager.cancel()
    else:
        monkeypatch.setattr(cm, "_instance", cm.CodeMode())
    with pytest.raises(ValueError):
        dev.activate(job["id"], job["approval"], confirmed=True)
    assert (repository / "core/example.py").read_text() == "VALUE = 1\n"


def test_hud_approval_is_sufficient_and_exact_once(repository, monkeypatch):
    from tools import era_manifest

    monkeypatch.setattr(era_manifest, "main", lambda _: 0)
    manager = cm.CodeMode(cli_factory=Advisory)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    job = pending(manager, repository)
    activated = dev.activate(job["id"], job["approval"], confirmed=True)
    assert activated["state"] == "activated"
    assert (repository / "core/example.py").read_text() == "VALUE = 2\n"
    with pytest.raises(ValueError):
        dev.activate(job["id"], job["approval"], confirmed=True)


def test_exit_keeps_verified_review_but_new_session_invalidates_it(repository, monkeypatch):
    manager = cm.CodeMode(cli_factory=Advisory)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    job = pending(manager, repository)
    manager.cancel(exit_mode=True)
    assert not manager.enabled
    manager.authorize(job)
    manager.enter()
    with pytest.raises(ValueError):
        manager.authorize(job)


def test_generic_retry_cannot_switch_codemode_provider(repository, monkeypatch):
    manager = cm.CodeMode(cli_factory=Advisory)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    job = pending(manager, repository)
    job["state"] = "failed"
    dev._save(job)
    called = []
    monkeypatch.setattr(dev, "propose", lambda *a, **kw: called.append(True))
    with pytest.raises(ValueError, match="pinned subscription"):
        dev.retry(job["id"])
    assert not called


@pytest.mark.parametrize(
    "path", ["core/CodeMode.py", "core/DEVELOPMENT.py", "crew/Source.py", "core/file:stream.py"]
)
def test_windows_case_and_stream_aliases_cannot_bypass_protected_source(repository, path):
    with pytest.raises(ValueError):
        dev._path(path)


def test_verifier_cancellation_stops_real_owned_process_tree(tmp_path):
    import psutil

    from core.codemode_process import run_verifier

    parent_pid, child_pid = tmp_path / "parent.txt", tmp_path / "child.txt"
    script = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        f"Path({str(parent_pid)!r}).write_text(str(os.getpid())); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"Path({str(child_pid)!r}).write_text(str(child.pid)); time.sleep(30)"
    )
    stop = threading.Event()

    def cancel_started():
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()

    timer = threading.Thread(target=cancel_started, daemon=True)
    timer.start()
    with pytest.raises(ValueError, match="cancelled"):
        run_verifier([sys.executable, "-c", script], tmp_path, dict(__import__("os").environ), stop)
    timer.join(5)
    assert parent_pid.exists() and child_pid.exists()
    assert not psutil.pid_exists(int(parent_pid.read_text()))
    assert not psutil.pid_exists(int(child_pid.read_text()))


@pytest.mark.asyncio
async def test_candidate_runs_existing_verifier_and_voice_approval_exact_once(
    repository, monkeypatch
):
    from tools import era_manifest
    from tools.test_phase8_coordination import candidate, prepare_fixture

    prepare_fixture(repository, monkeypatch)
    monkeypatch.setattr(era_manifest, "main", lambda _: 0)

    class Candidate(Advisory):
        async def generate(self, prompt, emit):
            await emit("I have a candidate, pending verification.")
            return {"reply": "I have a candidate, pending verification.", "candidate": candidate()}

    def context(request):
        return {
            "files": [
                {
                    "path": "core/example.py",
                    "sha256": dev._hash(repository / "core/example.py"),
                    "start_line": 1,
                    "truncated": False,
                }
            ]
        }

    manager = cm.CodeMode(cli_factory=Candidate, context_builder=context)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    events = []

    async def emit(kind, text, owner):
        events.append((kind, text))

    manager.start(InputEnvelope.create("Change the value and test it", "typed"), emit)
    await asyncio.wait_for(manager._task, 5)
    assert manager.phase == "review", manager.error
    job = dev._read(manager.job_id)
    assert job["source"] == "codemode" and job["work_id"] == manager.work_id
    assert job["approval_expires_at"] > time.time()
    assert (repository / "core/example.py").read_text() == "VALUE = 1\n"
    with pytest.raises(ValueError, match="original channel"):
        await manager.decide("yes", InputEnvelope.create("yes", "telegram"), emit)
    assert await manager.decide("yes", InputEnvelope.create("yes", "typed"), emit)
    assert (repository / "core/example.py").read_text() == "VALUE = 2\n"
    assert manager.snapshot()["phase"] == "applied"
    assert not await manager.decide("yes", InputEnvelope.create("yes", "typed"), emit)
    with pytest.raises(ValueError):
        await manager.decide(
            f"approve coding change {job['id']}", InputEnvelope.create("approve", "typed"), emit
        )


@pytest.mark.asyncio
async def test_runtime_claims_mode_before_yield_and_blocks_parallel_model(repository, monkeypatch):
    from unittest.mock import AsyncMock

    from app import codemode_runtime as runtime
    from core.model_reply import send_model_text

    manager = cm.CodeMode(cli_factory=Advisory, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)
    said = []
    speech = SimpleNamespace(
        interrupt=lambda: None,
        say=lambda text, **kw: said.append(text),
        feed=lambda text, **kw: said.append(text),
        end_turn=lambda: None,
    )
    era = SimpleNamespace(
        _loop=asyncio.get_running_loop(),
        speech_out=speech,
        ui=SimpleNamespace(write_log=lambda text: None),
        session=SimpleNamespace(send_realtime_input=AsyncMock()),
    )
    assert runtime.route(era, "enter CodeMode", InputEnvelope.create("enter CodeMode", "typed"))
    assert manager.enabled  # no event-loop yield needed to claim the mouth
    assert runtime.route(era, "enter CodeMode", era._input_envelope)
    await send_model_text(era, "Gemini must not answer in parallel")
    era.session.send_realtime_input.assert_not_called()
    assert not runtime.route(era, "__standby__")
    await asyncio.sleep(0)
    assert len(said) == 1 and "ready" in said[0]
    manager.cancel(exit_mode=True)


def test_crew_contribution_preserves_root_authority(repository, monkeypatch):
    from core.codemode_team import consult
    from core.tool_result import ToolResult
    from crew import runtime
    from crew.coordination import CURRENT_WORK

    owner = InputEnvelope.create("Cody, inspect the current implementation", "typed")
    authority = {
        "source": "codemode",
        "session_id": "session",
        "turn_revision": 3,
        "input_id": owner.id,
        "input_source": owner.source,
    }
    token = REGISTRY.register("codemode-crew-fixture", "codemode")

    def run(params):
        work = CURRENT_WORK.get()
        assert work.id == token.cid and work.owner == "codemode"
        assert work.task == owner.text and work.authority == authority
        assert params["bot"] == "cody"
        return ToolResult.ok(message="Inspected")

    monkeypatch.setattr(runtime, "run_tool", run)
    try:
        result = consult(owner, token.cid, token, authority)
        assert result["work"]["owner"] == "codemode"
        assert CURRENT_WORK.get() is None
    finally:
        REGISTRY.finish(token.cid)


def test_context_is_current_bounded_and_redacted(repository, monkeypatch):
    from core import codemode_context, source_discovery
    from crew import source

    monkeypatch.setattr(source, "ROOT", repository)
    monkeypatch.setattr(
        source_discovery,
        "discover",
        lambda request, limit: {
            "candidates": [{"path": "core/example.py"}, {"path": "config/api_keys.json"}]
        },
    )
    result = codemode_context.build("Explain core/example.py")
    assert len(result["files"]) == 1 and result["bytes"] <= 100000
    assert result["files"][0]["sha256"] == dev._hash(repository / "core/example.py")
    assert "VALUE = 1" in result["files"][0]["content"]


@pytest.mark.asyncio
async def test_local_mouth_streams_sentences_and_interrupts_inflight_speech():
    from app.codemode_speech import LocalMouth

    started = asyncio.Event()
    stopped = asyncio.Event()
    heard = []

    async def say(text):
        heard.append(text)
        started.set()
        try:
            await asyncio.sleep(20)
        finally:
            stopped.set()

    mouth = LocalMouth(SimpleNamespace(_local_say=say))
    mouth.feed("First sentence is complete. Another sentence is pending")
    await asyncio.wait_for(started.wait(), 1)
    mouth.interrupt()
    await asyncio.wait_for(stopped.wait(), 1)
    await asyncio.gather(mouth.task, return_exceptions=True)
    assert heard == ["First sentence is complete."]


@pytest.mark.asyncio
async def test_superseded_channel_cannot_send_old_telegram_reply(repository, monkeypatch):
    from app.codemode_runtime import emit

    manager = cm.CodeMode(cli_factory=Advisory)
    monkeypatch.setattr(cm, "_instance", manager)
    replies, logs = [], []
    old = InputEnvelope.create("old coding turn", "telegram")
    era = SimpleNamespace(
        _input_envelope=InputEnvelope.create("new turn", "typed"),
        _tg_ack=replies.append,
        ui=SimpleNamespace(write_log=logs.append),
    )
    await emit(era, "end", "Old answer", old)
    assert not replies and logs == ["SYS [previous CodeMode turn]: Old answer"]


@pytest.mark.asyncio
async def test_code_stream_preserves_words_and_commits_agent_conversation(repository, monkeypatch):
    from app import codemode_runtime as runtime
    from app.hud.events import HudBus

    manager = cm.CodeMode(cli_factory=Advisory)
    monkeypatch.setattr(cm, "_instance", manager)
    bus = HudBus()
    monkeypatch.setattr(runtime, "hud", bus)
    owner = InputEnvelope.create("Explain", "typed")
    era = SimpleNamespace(
        _input_envelope=owner,
        ui=SimpleNamespace(
            write_log=lambda text: bus.observe_legacy({"type": "log", "text": text})
        ),
    )
    bus.request_sent("codemode")
    for fragment in ("Co", "de", "Mode", " ", "ready"):
        await runtime.emit(era, "text", fragment, owner)
    assert bus.snapshot()["transcript"]["response"] == "CodeMode ready"
    await runtime.emit(era, "end", "CodeMode ready", owner)
    state = bus.snapshot()
    assert not state["transcript"]["responding"]
    assert state["conversation"][-1]["role"] == "agent"
    assert state["conversation"][-1]["text"] == "[Claude / CodeMode] CodeMode ready"


@pytest.mark.asyncio
async def test_login_failure_closes_codemode_so_era_is_not_trapped(repository, monkeypatch):
    """PF-041 (live 2026-11-17): a missing Claude login left CodeMode on, and every
    later order hit the same auth error. A login failure now closes CodeMode."""

    class NoLogin(Advisory):
        async def generate(self, prompt, emit):
            raise ValueError("Selected Claude profile needs a Claude subscription login")

    manager = cm.CodeMode(cli_factory=NoLogin, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)
    events = []

    async def emit(kind, text, envelope):
        events.append((kind, text))

    manager.enter()
    assert manager.start(InputEnvelope.create("Write a Python function", "typed"), emit)
    await manager._task
    assert not manager.enabled
    assert any(kind == "notice" and "CodeMode closed" in text for kind, text in events)


@pytest.mark.asyncio
async def test_an_ordinary_failure_keeps_codemode_open(repository, monkeypatch):
    class Broken(Advisory):
        async def generate(self, prompt, emit):
            raise ValueError("the model returned an empty patch")

    manager = cm.CodeMode(cli_factory=Broken, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)

    async def emit(kind, text, envelope):
        return None

    manager.enter()
    assert manager.start(InputEnvelope.create("Fix the bug", "typed"), emit)
    await manager._task
    assert manager.enabled
