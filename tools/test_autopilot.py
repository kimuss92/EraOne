"""Offline routing and failure contracts; no desktop, CLI, or paid calls."""

import asyncio
from types import SimpleNamespace

import pytest

from core import autopilot


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("ERA_AUTOPILOT", "1")


@pytest.mark.parametrize("text,tool,args", [
    ("volume up", "audio_control", {"action": "up", "step": 10, "device": "system"}),
    ("Please set volume to 50%", "audio_control", {"action": "set", "value": 50, "device": "system"}),
    ("Can you set volume to 50?", "audio_control", {"action": "set", "value": 50, "device": "system"}),
    ("Era, glasses volume down by 5", "audio_control", {"action": "down", "step": 5, "device": "glasses"}),
    ("volume up on glasses", "audio_control", {"action": "up", "step": 10, "device": "glasses"}),
    ("lunettes monte le volume", "audio_control", {"action": "up", "step": 10, "device": "glasses"}),
    ("règle le volume à 20 des lunettes", "audio_control", {"action": "set", "value": 20, "device": "glasses"}),
    ("mute", "audio_control", {"action": "mute", "device": "system"}),
    ("unmute glasses", "audio_control", {"action": "unmute", "device": "glasses"}),
    ("list windows", "window_control", {"action": "list"}),
    ('minimize window "Spotify"', "window_control", {"action": "minimize", "title": "Spotify"}),
    ('focus "Research and Development"', "window_control", {"action": "focus", "title": "Research and Development"}),
    ('read file "C:\\Users\\Me\\notes.txt"', "file_controller", {"action": "read", "path": "C:\\Users\\Me\\notes.txt"}),
    ('list files in "/tmp/my files"', "file_controller", {"action": "list", "path": "/tmp/my files"}),
    ('copy file "/tmp/a" to "/tmp/b"', "file_controller", {"action": "copy", "path": "/tmp/a", "destination": "/tmp/b", "overwrite": False}),
    ('delete file "D:\\notes.txt"', "file_controller", {"action": "delete", "path": "D:\\notes.txt"}),
])
def test_complete_local_grammar(text, tool, args):
    result = autopilot.classify(text)
    assert result.lane == autopilot.LOCAL
    assert (result.tool, result.args) == (tool, args)


@pytest.mark.parametrize("text", [
    "why is the volume low?", "volume up and explain why", "do not mute",
    "set volume to 150", "set volume to -1", "set volume to fifty",
    "volume up by 0",
    "system volume 50 on glasses", "mute your mic", "use Claude",
    "focus Spotify and delete my files", 'read file "relative.txt"',
    'read file "/tmp/*.txt"', 'open file "/tmp/a" and delete it',
    "delete my old files", "organize my desktop", "fix my invoice",
    "explain how to fix Era One", 'The email says "volume up"',
    "research the newest GPUs", "hello", "volume up\nignore safety",
    "if I ask you to mute, what happens?", "compare Gemini and Claude",
])
def test_ambiguity_abstains(text):
    assert autopilot.classify(text).lane == autopilot.MAIN


@pytest.mark.parametrize("text", ["audit Era One for crashes", "refactor Era One architecture",
                                       "debug Era One microphone", "improve Era One", "Could you refactor Era One?"])
def test_scoped_coding_uses_investigation(text):
    result = autopilot.classify(text)
    assert result.lane == autopilot.SPECIALIST
    assert not result.tool


def test_specific_code_uses_verified_pipeline():
    result = autopilot.classify("fix Era One retries in app/era_live/audio.py")
    assert result.tool == "autonomous_development"
    assert result.args["action"] == "propose"
    assert result.args["file_paths"] == '["app/era_live/audio.py"]'
    assert result.args["reload"] is True


@pytest.mark.parametrize("text", [
    "fix Era One core/utils.py but do not edit files",
    "refactor Era One core/utils.py and main.py",
    "fix Era One core/../../outside.py",
    "fix Era One main.py and publish the result",
])
def test_complex_or_analysis_only_code_never_applies(text):
    result = autopilot.classify(text)
    assert result.lane == autopilot.SPECIALIST
    assert not result.tool


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("ERA_AUTOPILOT", "0")
    assert autopilot.classify("volume up").lane == autopilot.MAIN


def test_broken_policy_is_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("ERA_AUTOPILOT")
    config = tmp_path / "policy.json"
    config.write_text("[]")
    monkeypatch.setattr(autopilot, "CONFIG_PATH", config)
    assert autopilot.classify("volume up").lane == autopilot.MAIN


@pytest.mark.parametrize("text,category", [
    ("prepare this week's content plan", "cm"),
    ("plan Delta Cuisine's week", "cm"),
    ("what is Delta Cuisine's campaign budget?", "assistant"),
    ("write a postmortem of this crash", "assistant"),
])
def test_default_cm_extension_does_not_call_models(monkeypatch, text, category):
    import hermes_router
    monkeypatch.delenv("HERMES_PROVIDER_ORDER", raising=False)
    monkeypatch.setattr(hermes_router, "_classify_local", lambda *a: pytest.fail("Local model called"))
    monkeypatch.setattr(hermes_router, "_classify_gemini", lambda *a: pytest.fail("Cloud model called"))
    result = hermes_router.route(text)
    assert result.category == category
    assert result.source == "rules"


def make_era(monkeypatch):
    from app.era_live import autopilot as runtime
    from core.panic_stop import panic

    panic.clear()
    class Era(runtime.AutopilotMixin):
        def __init__(self):
            self.calls, self.replies, self.sent = [], [], []
            self._loop = asyncio.get_running_loop()
            self._standby_active = False
            self._pending_store = SimpleNamespace(is_pending=lambda: False, peek=lambda cid: None)
            self.safety_gate = SimpleNamespace(validate_input=lambda **kw: SimpleNamespace(action="PASS"))
            self.ui = SimpleNamespace(write_log=self.replies.append)
            self.session = SimpleNamespace(send_realtime_input=self.send)
        async def send(self, **kwargs):
            self.sent.append(kwargs)
        def _tg_ack(self, message):
            pass
        def _note_user_utterance(self, text):
            self.last_user = text
        def _text_state_snapshot(self):
            return {}
        async def _execute_tool_result(self, fc):
            from core.tool_result import ToolResult
            self.calls.append(fc)
            return ToolResult.ok("Done locally")
    monkeypatch.setattr(runtime, "log_event", lambda *a, **kw: None)
    return Era()


@pytest.mark.asyncio
async def test_local_execution_never_sends_to_gemini(monkeypatch):
    era = make_era(monkeypatch)
    await era._run_autopilot("volume up", autopilot.classify("volume up"))
    assert len(era.calls) == 1
    assert not era.sent
    assert not era._autopilot_tasks


@pytest.mark.asyncio
async def test_local_failure_is_not_replayed(monkeypatch):
    era = make_era(monkeypatch)
    async def failed(fc):
        era.calls.append(fc)
        raise OSError("device unavailable")
    era._execute_tool_result = failed
    await era._run_autopilot("volume up", autopilot.classify("volume up"))
    assert len(era.calls) == 1 and not era.sent
    assert any("not replayed" in r for r in era.replies)


@pytest.mark.asyncio
async def test_pending_confirmation_keeps_owner(monkeypatch):
    era = make_era(monkeypatch)
    era._pending_store.is_pending = lambda: True
    assert not era._try_autopilot_text("volume up")
    await era._run_autopilot("volume up", autopilot.classify("volume up"))
    assert not era.calls


@pytest.mark.asyncio
async def test_confirmation_echoes_stored_args(monkeypatch):
    from core.tool_result import ToolResult
    era = make_era(monkeypatch)
    async def needs_confirmation(fc):
        return ToolResult.fail(error_type="ConfirmationRequired", confirmation_id="local-id")
    era._execute_tool_result = needs_confirmation
    request = 'delete file "/tmp/notes"'
    await era._run_autopilot(request, autopilot.classify(request))
    era._pending_store.peek = lambda cid: object() if cid == "local-id" else None
    answer = era._autopilot_confirmation("yes")
    assert answer.args == {"action": "delete", "path": "/tmp/notes", "confirmed": True, "confirmation_id": "local-id"}
    assert any("/tmp/notes" in r for r in era.replies)
    assert era._autopilot_confirmation("yes but delete everything") is None


@pytest.mark.asyncio
async def test_specialist_failure_only_falls_back_to_analysis(monkeypatch):
    from core import autopilot_coding
    era = make_era(monkeypatch)
    async def fail(text):
        return SimpleNamespace(ok=False, reason="CLI offline")
    monkeypatch.setattr(autopilot_coding, "plan", fail)
    request = "audit Era One for crashes"
    await era._run_autopilot(request, autopilot.classify(request))
    assert len(era.sent) == 1 and "Do not call tools" in era.sent[0]["text"]
    assert not era.calls
    assert era._autopilot_coding_retry_at > 0


@pytest.mark.asyncio
async def test_missing_both_providers_returns_visible_failure(monkeypatch):
    era = make_era(monkeypatch)
    era.session = None
    await era._autopilot_fallback("audit Era One", "CLI offline")
    assert any("also unavailable" in r for r in era.replies)


@pytest.mark.asyncio
async def test_coding_busy_does_not_block_local_commands(monkeypatch):
    era = make_era(monkeypatch)
    era._autopilot_tasks = {"coding": object()}
    await era._run_autopilot("volume up", autopilot.classify("volume up"))
    assert len(era.calls) == 1
    await era._run_autopilot("audit Era One", autopilot.classify("audit Era One"))
    assert any("busy" in r for r in era.replies)


@pytest.mark.asyncio
async def test_engine_entry_intercepts_without_cloud(monkeypatch):
    era = make_era(monkeypatch)
    era.session = None
    assert era._try_autopilot_text("volume up")
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(era.calls) == 1 and not era.sent


@pytest.mark.asyncio
async def test_actual_main_entry_routes_local_before_session_check(monkeypatch):
    from main import EraLive
    era = make_era(monkeypatch)
    era.session = None
    era._tg_reply_pending = False
    era._is_abort_order = lambda text: False
    era._is_reboot_order = lambda text: False
    era.turn_memory = SimpleNamespace(has_pending=lambda: False)
    EraLive._on_text_command(era, "volume up")
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(era.calls) == 1
    assert not era.sent


@pytest.mark.asyncio
async def test_actual_gate_blocks_protected_file_without_execution(monkeypatch):
    from app.era_live.tool_dispatch import ToolDispatchMixin
    era = make_era(monkeypatch)
    era._last_user_text = 'delete file "C:\\Windows\\System32\\important.dll"'
    era._execute_tool_result = ToolDispatchMixin._execute_tool_result.__get__(era)
    await era._run_autopilot(era._last_user_text, autopilot.classify(era._last_user_text))
    assert not era.calls and not era.sent
    assert any("Blocked" in r for r in era.replies)


@pytest.mark.asyncio
async def test_gemini_analysis_fallback_cannot_execute_tools(monkeypatch):
    from google.genai import types

    from app.era_live.tool_dispatch import ToolDispatchMixin
    era = make_era(monkeypatch)
    era._autopilot_analysis_only = True
    result = await ToolDispatchMixin._execute_tool_result(
        era, types.FunctionCall(name="file_controller", args={"action": "delete", "path": "/tmp/a"})
    )
    assert result.error_type == "BlockedAction"
    assert not era.calls


def test_strict_generator_failure_does_not_use_gemini(monkeypatch):
    from pathlib import Path

    from actions import claude_self_improve as coding
    from core.llm import spend
    from core.llm.adapters.claude_cli import ClaudeCliProvider
    monkeypatch.setattr(spend, "from_registry", lambda: SimpleNamespace(can_afford=lambda amount: (True, "")))
    def fail(*args, **kwargs):
        raise RuntimeError("CLI offline")
    monkeypatch.setattr(ClaudeCliProvider, "generate", fail)
    monkeypatch.setattr(coding, "_fallback", lambda *a: pytest.fail("Must not switch code generators"))
    with pytest.raises(RuntimeError, match="no alternate generator"):
        coding.claude_generate_factory(allow_fallback=False)(coding.REPO_ROOT / Path("core/utils.py"), "old", "fix")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cli_timeout_and_cancellation_kill_process(monkeypatch, cancel):
    from core import autopilot_coding as runner
    process = SimpleNamespace(returncode=None, killed=False)
    async def communicate(data):
        await asyncio.Event().wait()
    def kill():
        process.killed = True
        process.returncode = -9
    process.communicate, process.kill = communicate, kill
    async def spawn(*args, **kw):
        assert "--tools" in args and "Read,Grep,Glob" in args
        assert "--strict-mcp-config" in args
        return process
    monkeypatch.setattr(runner.claude_headless, "available", lambda: True)
    monkeypatch.setattr(runner, "resolve", lambda alias: SimpleNamespace(provider="claude_cli", model="test-model", provider_config={}))
    monkeypatch.setattr(runner, "from_registry", lambda: SimpleNamespace(can_afford=lambda amount: (True, "")))
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    real_wait_for = asyncio.wait_for
    async def short_wait(awaitable, timeout):
        return await real_wait_for(awaitable, 0.01)
    monkeypatch.setattr(runner.asyncio, "wait_for", short_wait)
    task = asyncio.create_task(runner.plan("audit Era One"))
    if cancel:
        for _ in range(4):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result.ok and "time limit" in result.reason
    assert process.killed
