"""Claude voice mode — Era answers through the Claude subscription CLI when
Gemini Live is not answering.

WHY IT EXISTS
  2026-10-03, Karim's screen recording: the HUD said "Connected" and "Listening",
  the mic waveform moved, and Era never answered. Nothing was logged because the
  socket was fine; 20 s later Google sent 1011 "service unavailable". Sixteen
  1011/1008 drops had already been logged that day. Era only reacted to a
  disconnect, so a connected-but-dead session looked like a quiet room.

WHAT IS PROVEN HERE, AND WHAT IS NOT
  Proven: the verdict logic, the order matcher, that the adapter can never give
  the CLI a tool, that a JSON tool call comes back out as a gated tool call, the
  mode's state machine, and that the pieces are wired into the run loop.
  NOT proven: a real Gemini failure switching to Claude on Karim's machine. That
  needs a live session; the logs (`engine.claude_voice.*`) are how to see it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.era_live.claude_voice import ClaudeVoiceMixin, claude_voice_order
from core.live_health import VOICE_RMS, LiveHealth
from core.llm.adapters.claude_voice import (
    ClaudeVoiceProvider,
    render_history,
    render_tool_catalog,
)
from core.llm.types import LlmRequest, PromptBlock


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


LOUD = VOICE_RMS * 4


def speak(health, clock, seconds, rms=LOUD):
    """Feed `seconds` of speech in 100 ms blocks, advancing the clock with it."""
    for _ in range(int(seconds * 10)):
        health.note_voice(rms, 0.1)
        clock.advance(0.1)


# ── LiveHealth: the three failure shapes ────────────────────────────────────


def test_a_quiet_session_has_no_verdict():
    c = Clock()
    h = LiveHealth(c)
    c.advance(600)
    assert h.verdict() is None


def test_speech_gemini_never_transcribed_is_deaf():
    """THE recording: the user talks, nothing comes back, no error."""
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 4.0)  # one long unanswered episode
    assert h.verdict() is None, "must not judge before hear_timeout_s"
    c.advance(13)  # silence AFTER the user stopped
    v = h.verdict()
    assert v is not None and v.kind == "deaf"


def test_a_long_sentence_is_never_judged_while_it_is_still_being_spoken():
    """THE live false positive (2026-10-03 17:07, 17:11, 17:16): a 9-second sentence
    was called "deaf" while the user was still talking, because the clock started
    when they BEGAN. Gemini sends the transcript after the user stops."""
    c = Clock()
    h = LiveHealth(c)
    for _ in range(4):  # 4 x 9 s of talking, never pausing for a full second
        speak(h, c, 9.0)
        assert h.verdict() is None, "judged a user who is still speaking"


def test_the_deaf_clock_starts_when_the_user_stops_not_when_they_start():
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 9.0)
    c.advance(11)  # 20 s since they began, but only 11 s since they stopped
    assert h.verdict() is None
    c.advance(2)
    assert h.verdict() is not None and h.verdict().kind == "deaf"


def test_two_short_unanswered_attempts_are_deaf_too():
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 1.5)
    c.advance(2.0)  # a real pause ends the episode
    speak(h, c, 1.5)
    c.advance(13)
    assert h.verdict() is not None and h.verdict().kind == "deaf"


def test_one_short_burst_is_never_deaf():
    """A cough, a door, a single word: not evidence of a dead session."""
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 1.5)
    c.advance(120)
    assert h.verdict() is None


def test_a_burst_shorter_than_the_minimum_is_ignored():
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 0.8)
    c.advance(120)
    assert h.verdict() is None


def test_room_noise_below_the_voice_level_is_ignored():
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 10.0, rms=VOICE_RMS / 3)
    c.advance(60)
    assert h.verdict() is None


def test_a_transcript_clears_the_deaf_suspicion():
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 4.0)
    c.advance(5)
    h.note_heard()  # Gemini transcribed it after all — just slowly
    h.note_reply()
    c.advance(60)
    assert h.verdict() is None


def test_heard_but_never_answered_is_mute():
    c = Clock()
    h = LiveHealth(c)
    speak(h, c, 3.0)
    h.note_heard()
    c.advance(24)
    assert h.verdict() is None
    c.advance(2)
    v = h.verdict()
    assert v is not None and v.kind == "mute"


def test_an_answer_clears_mute():
    c = Clock()
    h = LiveHealth(c)
    h.note_heard()
    c.advance(5)
    h.note_reply()
    c.advance(60)
    assert h.verdict() is None


def test_repeated_upstream_errors_are_flaky():
    c = Clock()
    h = LiveHealth(c)
    for _ in range(2):
        h.note_upstream_error()
        c.advance(60)
    assert h.verdict() is None
    h.note_upstream_error()
    v = h.verdict()
    assert v is not None and v.kind == "flaky"


def test_old_upstream_errors_age_out():
    c = Clock()
    h = LiveHealth(c)
    for _ in range(3):
        h.note_upstream_error()
    c.advance(601)
    assert h.verdict() is None


def test_reset_keeps_errors_but_clears_the_session_state():
    """Errors describe the pattern ACROSS connections, so a reconnect must not
    erase them — but a new session starts with no pending speech."""
    c = Clock()
    h = LiveHealth(c)
    for _ in range(3):
        h.note_upstream_error()
    speak(h, c, 4.0)
    h.reset()
    c.advance(30)
    v = h.verdict()
    assert v is not None and v.kind == "flaky"  # the errors survived, the speech did not


def test_clear_errors_gives_gemini_a_fresh_start():
    c = Clock()
    h = LiveHealth(c)
    for _ in range(3):
        h.note_upstream_error()
    h.clear_errors()
    assert h.verdict() is None


# ── the spoken / typed order ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "use Claude",
        "Era, use Claude",
        "switch to Claude",
        "please switch to claude voice",
        "go to the backup brain",
        "answer with Claude",
        "use your Claude brain.",
        "passe sur Claude",
        "bascule sur Claude",
    ],
)
def test_orders_that_switch_to_claude(text):
    assert claude_voice_order(text), text


@pytest.mark.parametrize(
    "text",
    [
        "ask Claude about this",
        "I used Claude yesterday to write this",
        "what is Claude",
        "tell Claude to fix the bug",
        "use Claude to summarise this email",
        "reconnect",
        "",
    ],
)
def test_sentences_that_merely_mention_claude_do_not_switch(text):
    """The matcher is anchored to a WHOLE order, or any mention would flip the voice."""
    assert not claude_voice_order(text), text


# ── the adapter: tools can never reach the CLI ──────────────────────────────

TOOLS = (
    {
        "type": "function",
        "function": {
            "name": "audio_control",
            "description": "Audio DEVICES and volume.",
            "parameters": {
                "type": "object",
                "properties": {"action": {"type": "string"}, "value": {"type": "integer"}},
                "required": ["action"],
            },
        },
    },
)

OK_PAYLOAD = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "terminal_reason": "completed",
    "stop_reason": "end_turn",
    "api_error_status": None,
    "result": "Bonjour, sir.",
    "total_cost_usd": 0.001,
    "duration_ms": 4300,
    "ttft_ms": 1700,
    "usage": {"input_tokens": 740, "output_tokens": 12},
    "modelUsage": {"claude-haiku-4-5-20251001": {"costUSD": 0.001}},
}


class FakeRunner:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cmd, prompt, env, cwd, timeout, rid):
        self.calls.append({"cmd": cmd, "prompt": prompt, "cwd": cwd})
        text = self.replies.pop(0) if self.replies else "ok"
        return 0, json.dumps({**OK_PAYLOAD, "result": text}), ""


def request(user="Bonjour Era", history=None, tools=TOOLS):
    return LlmRequest(
        alias="era.cheap",
        system=(PromptBlock(text="You are ERA ONE.", cache_hint=True),),
        tools=tools,
        provider_options={
            "ollama": {"extra_messages": history or [{"role": "user", "content": user}]}
        },
    )


def test_the_cli_is_never_given_a_tool():
    """The safety property. Tools inside the CLI would execute OUTSIDE Era's gates."""
    runner = FakeRunner(["hi"])
    ClaudeVoiceProvider(runner=runner).generate(request())
    cmd = runner.calls[0]["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == ""
    assert cmd[cmd.index("--tools") + 1] == "", "native tool definitions must be removed"


def test_the_harness_is_stripped_to_keep_it_fast_and_cheap():
    """Measured: 70,508 tokens loaded by default, 740 with these flags."""
    runner = FakeRunner(["hi"])
    ClaudeVoiceProvider(runner=runner).generate(request())
    cmd = runner.calls[0]["cmd"]
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--system-prompt"):
        assert flag in cmd, flag
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_the_tool_catalog_and_protocol_reach_the_model_as_text():
    runner = FakeRunner(["hi"])
    ClaudeVoiceProvider(runner=runner).generate(request())
    prompt = runner.calls[0]["prompt"]
    assert "audio_control(action*, value)" in prompt, "required args are starred"
    assert '"name": "<tool>"' in prompt, "the JSON-only protocol must be stated"
    assert "You are ERA ONE." in prompt, "the system prompt must still be there"


def test_the_history_reaches_the_model_including_tool_calls():
    """Dropping the calls is what taught a small model to answer 'Done, sir.' with
    nothing done (core/local_brain.py)."""
    history = [
        {"role": "user", "content": "volume 30"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "audio_control", "arguments": {"action": "set", "value": 30}}}
            ],
        },
        {"role": "tool", "tool_name": "audio_control", "content": '{"success": true}'},
        {"role": "assistant", "content": "Done, sir."},
        {"role": "user", "content": "what did I ask?"},
    ]
    runner = FakeRunner(["you asked for volume"])
    ClaudeVoiceProvider(runner=runner).generate(request(history=history))
    prompt = runner.calls[0]["prompt"]
    assert "User: volume 30" in prompt
    assert "You (tool call):" in prompt and '"audio_control"' in prompt
    assert "Tool result (audio_control):" in prompt
    assert prompt.rindex("User: what did I ask?") > prompt.rindex("Done, sir.")


def test_it_runs_from_a_scratch_directory_not_the_repo():
    """Measured: running from the repo loads CLAUDE.md and the skills."""
    runner = FakeRunner(["hi"])
    ClaudeVoiceProvider(runner=runner).generate(request())
    assert "EraOne" not in runner.calls[0]["cwd"]


def test_a_tool_free_request_still_works():
    runner = FakeRunner(["salut"])
    resp = ClaudeVoiceProvider(runner=runner).generate(request(tools=()))
    assert resp.text == "salut"
    assert "TOOLS:" not in runner.calls[0]["prompt"]


def test_render_helpers_ignore_malformed_entries():
    assert render_tool_catalog(({"nope": 1}, {"function": {}})) == ""
    assert render_history([{"role": "assistant", "tool_calls": ["junk"], "content": ""}]) == ""


# ── the brain end to end: a JSON reply becomes a GATED tool call ────────────


def test_a_json_reply_becomes_a_tool_call_through_the_gate():
    from core.local_brain import CASCADE_SYSTEM_PROMPT, LocalBrain

    runner = FakeRunner(
        [
            '{"name": "audio_control", "arguments": {"action": "set_volume", "value": 30}}',
            "Done, sir.",
        ]
    )
    brain = LocalBrain(
        provider=ClaudeVoiceProvider(runner=runner),
        alias="era.cheap",
        tools=list(TOOLS),
        system_prompt=CASCADE_SYSTEM_PROMPT,
    )
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"success": True}

    async def run():
        return [s async for s in brain.respond("set the volume to 30", execute)]

    said = asyncio.run(run())
    assert executed == [("audio_control", {"action": "set_volume", "value": 30})]
    assert said == ["Done, sir."]
    assert len(runner.calls) == 2, "one pass to call the tool, one to phrase the result"


def test_a_tool_name_the_brain_does_not_know_is_refused():
    """An injected or hallucinated name must not reach the executor."""
    from core.local_brain import CASCADE_SYSTEM_PROMPT, LocalBrain

    runner = FakeRunner(['{"name": "format_disk", "arguments": {}}'])
    brain = LocalBrain(
        provider=ClaudeVoiceProvider(runner=runner),
        alias="era.cheap",
        tools=list(TOOLS),
        system_prompt=CASCADE_SYSTEM_PROMPT,
    )
    executed = []

    async def execute(name, args):
        executed.append(name)
        return {"success": True}

    async def run():
        return [s async for s in brain.respond("do it", execute)]

    asyncio.run(run())
    assert executed == []


# ── the mode's state machine ────────────────────────────────────────────────


class Stub(ClaudeVoiceMixin):
    def __init__(self, ready=True, policy=None):
        self.clock = Clock()
        self.live_health = LiveHealth(self.clock)
        self._go_away_event = asyncio.Event()
        self._standby_active = False
        self._shutdown_requested = False
        self._local_outage_reason = ""
        self._ready = ready
        self._pol = {
            "enabled": True,
            "stay_s": 180,
            "max_stay_s": 1800,
            "manual_stay_s": 3600,
            "healthy_after_s": 600,
        } | (policy or {})
        self.ran = []

    def _claude_policy(self):
        return self._pol

    def claude_voice_ready(self):
        return (True, "") if self._ready else (False, "no cli")

    def local_mode_active(self):
        return False

    async def _run_local_fallback(self, seconds, reason):
        self.ran.append((round(seconds), reason))


@pytest.fixture
def mono(monkeypatch):
    c = Clock()
    # Replace the module's `time`, NOT time.monotonic: that is the same global
    # function asyncio schedules with, and a frozen clock hangs every asyncio.sleep.
    monkeypatch.setattr("app.era_live.claude_voice.time", SimpleNamespace(monotonic=c))
    return c


def test_entering_opens_a_window_and_winds_the_session_down(mono):
    s = Stub()
    assert s.enter_claude_voice("deaf", "x") is True
    assert s._claude_until == pytest.approx(mono() + 180)
    assert s._go_away_event.is_set(), "the Gemini session must be told to end"


def test_it_does_nothing_when_unavailable(mono):
    """Degrade, never raise: no CLI means Gemini carries on exactly as before."""
    s = Stub(ready=False)
    assert s.enter_claude_voice("deaf", "x") is False
    assert not s._go_away_event.is_set()
    assert getattr(s, "_claude_until", 0.0) == 0.0


def test_a_repeat_failure_doubles_the_stay_up_to_the_cap(mono):
    s = Stub()
    stays = []
    for _ in range(6):
        s.enter_claude_voice("deaf", "x")
        stays.append(s._claude_stay_s)
        mono.t += s._claude_stay_s + 30  # window over, Gemini fails again at once
    assert stays == [180, 360, 720, 1440, 1800, 1800]


def test_a_long_healthy_gap_starts_the_stay_over(mono):
    s = Stub()
    s.enter_claude_voice("deaf", "x")
    s.enter_claude_voice("deaf", "x")
    assert s._claude_stay_s == 360
    mono.t += 5000  # Gemini was fine for a long while
    s.enter_claude_voice("deaf", "x")
    assert s._claude_stay_s == 180


def test_a_manual_switch_stays_for_the_manual_window(mono):
    s = Stub()
    s.enter_claude_voice("user", "use Claude", manual=True)
    assert s._claude_stay_s == 3600 and s._claude_manual is True


def test_entering_clears_the_errors_so_gemini_gets_a_fresh_start(mono):
    """Otherwise 'flaky' would still be true when the window ends and Era would
    walk straight back into Claude without ever trying Gemini."""
    s = Stub()
    for _ in range(3):
        s.live_health.note_upstream_error()
    s.enter_claude_voice("flaky", "x")
    assert s.live_health.verdict() is None


def test_release_closes_the_window_so_reconnect_really_reconnects(mono):
    s = Stub()
    s.enter_claude_voice("user", "x", manual=True)
    s.release_claude_voice()
    assert asyncio.run(s.serve_claude_voice()) is False, "released window must not re-enter"


def test_serve_runs_the_local_loop_for_the_remaining_window(mono):
    s = Stub()
    s.enter_claude_voice("deaf", "x")
    mono.t += 30
    assert asyncio.run(s.serve_claude_voice()) is True
    assert s.ran == [(150, "claude")]


def test_a_manual_window_serves_under_its_own_reason(mono):
    s = Stub()
    s.enter_claude_voice("user", "x", manual=True)
    asyncio.run(s.serve_claude_voice())
    assert s.ran[0][1] == "claude_manual"


def test_serve_returns_false_once_the_window_is_over(mono):
    s = Stub()
    s.enter_claude_voice("deaf", "x")
    mono.t += 999
    assert asyncio.run(s.serve_claude_voice()) is False


def test_a_failing_local_loop_hands_back_to_gemini_instead_of_raising(mono):
    s = Stub()

    async def boom(seconds, reason):
        raise RuntimeError("audio device vanished")

    s._run_local_fallback = boom
    s.enter_claude_voice("deaf", "x")
    assert asyncio.run(s.serve_claude_voice()) is False
    assert s._claude_until == 0.0, "a broken loop must not be re-entered forever"


# ── the watchdog ────────────────────────────────────────────────────────────


def test_the_watchdog_ends_a_session_that_is_judged_unhealthy(monkeypatch):
    s = Stub()
    for _ in range(3):
        s.live_health.note_upstream_error()  # an instant 'flaky' verdict

    async def fast_sleep(_):
        return None

    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", fast_sleep)
    asyncio.run(s._live_health_watch())
    assert s._go_away_event.is_set()
    assert s._claude_until > 0


def test_the_watchdog_leaves_standby_alone(monkeypatch):
    """In standby nobody is expected to be answered; speech there is not evidence."""
    s = Stub()
    s._standby_active = True
    for _ in range(3):
        s.live_health.note_upstream_error()
    ticks = []

    async def fast_sleep(_):
        ticks.append(1)
        if len(ticks) > 3:
            s._shutdown_requested = True

    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", fast_sleep)
    asyncio.run(s._live_health_watch())
    assert not s._go_away_event.is_set()


def test_the_watchdog_is_off_when_health_is_disabled(monkeypatch):
    monkeypatch.setenv("ERA_LIVE_HEALTH", "0")
    s = Stub()
    for _ in range(3):
        s.live_health.note_upstream_error()
    asyncio.run(s._live_health_watch())
    assert not s._go_away_event.is_set()


# ── the local loop picks the Claude brain ───────────────────────────────────


def _patch_local_models(monkeypatch):
    monkeypatch.setattr(
        "core.local_stt.LocalListener",
        lambda **_k: SimpleNamespace(load=lambda: None, reset=lambda: None),
    )


def test_the_local_loop_uses_the_claude_brain_for_the_claude_reasons(monkeypatch):
    import app.era_live.local_fallback as lf

    class Era(lf.LocalFallbackMixin, ClaudeVoiceMixin):
        def _claude_policy(self):
            return {"enabled": True, "alias": "era.cheap"}

        def claude_voice_ready(self):
            return True, ""

    _patch_local_models(monkeypatch)
    era = Era()
    for reason in ("claude", "claude_manual", "quota", "auth", "config"):
        era._local_brain = None
        asyncio.run(era._local_prepare(reason))
        assert era._local_brain is era._claude_brain_obj, reason


def test_a_network_outage_still_uses_the_local_brain(monkeypatch):
    """Claude needs the network too, so it cannot help when there is none."""
    import app.era_live.local_fallback as lf

    class Era(lf.LocalFallbackMixin, ClaudeVoiceMixin):
        def _local_policy(self):
            return {"enabled": True, "alias": "era.local", "tools": False}

        def claude_voice_ready(self):
            return True, ""

    _patch_local_models(monkeypatch)
    era = Era()
    asyncio.run(era._local_prepare("network"))
    assert getattr(era, "_claude_brain_obj", None) is None
    assert era._local_brain is not None


def test_the_fallback_mixin_still_works_without_the_claude_mixin(monkeypatch):
    """test_local_fallback.py's Stub has no ClaudeVoiceMixin; the calls into it
    must be tolerant, as `_in_cascade` is for the cascade mixin."""
    import app.era_live.local_fallback as lf

    class Bare(lf.LocalFallbackMixin):
        def _local_policy(self):
            return {"enabled": True, "alias": "era.local", "tools": False}

    _patch_local_models(monkeypatch)
    asyncio.run(Bare()._local_prepare("quota"))  # must not raise AttributeError


# ── it is wired into the run loop ───────────────────────────────────────────


def _main_tree():
    import ast
    from pathlib import Path

    return ast.parse((Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8-sig"))


def _run_source():
    import ast

    run = next(
        n for n in ast.walk(_main_tree()) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
    )
    return ast.unparse(run)


def test_eralive_inherits_the_mixin():
    import ast

    cls = next(
        n for n in ast.walk(_main_tree()) if isinstance(n, ast.ClassDef) and n.name == "EraLive"
    )
    assert "ClaudeVoiceMixin" in {b.id for b in cls.bases if isinstance(b, ast.Name)}


def test_the_run_loop_serves_claude_voice_before_connecting_gemini():
    src = _run_source()
    assert "serve_claude_voice" in src
    # run() reaches Gemini through managed_live_connection (app/era_live/
    # session_runtime.py, which owns the one `live.connect` call), so that is the
    # marker for "Gemini connects here".
    assert src.index("serve_claude_voice") < src.index("managed_live_connection"), (
        "Claude voice must be offered BEFORE Gemini connects, or the window never opens"
    )


def test_the_session_starts_a_watchdog_and_counts_upstream_errors():
    src = _run_source()
    assert "_live_health_watch" in src
    assert "note_upstream_error" in src


def test_the_audio_path_reports_what_gemini_does():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "app" / "era_live" / "audio.py").read_text(
        "utf-8-sig"
    )
    for call in ("live_health.note_voice", "live_health.note_heard", "live_health.note_reply"):
        assert call in src, call
    assert src.count("live_health.note_reply") >= 3, "audio, transcript/turn end, and tool call"


# ── 2026-10-03 evening: what the live log showed, each pinned ───────────────


def test_trailing_prose_after_a_tool_call_is_trimmed():
    """Live: `{"name": "spotify_control", ...}` + 'Music paused, sir.' in one reply
    was refused by the strict parser, so Era said she could not pause the music."""
    from core.llm.adapters.claude_voice import trim_to_tool_json

    call = '{"name": "spotify_control", "arguments": {"action": "pause"}}'
    assert trim_to_tool_json(call + "\n\nMusic paused, sir.") == call
    assert trim_to_tool_json("```json\n" + call + "\n```\nDone.") == call
    assert trim_to_tool_json("[" + call + "]\nok") == "[" + call + "]"


def test_prose_that_merely_quotes_json_is_left_for_the_parser_to_refuse():
    """The parser's strictness exists so JSON quoted from an email cannot run. Only
    a reply that STARTS with JSON may be trimmed."""
    from core.llm.adapters.claude_voice import trim_to_tool_json

    quoted = 'The email said {"name": "file_controller", "arguments": {"action": "delete"}} ok'
    assert trim_to_tool_json(quoted) == quoted
    assert trim_to_tool_json("Sure, sir.") == "Sure, sir."
    assert trim_to_tool_json('{"broken": ') == '{"broken": '


def test_a_reply_with_trailing_prose_still_runs_the_tool_once_trimmed():
    from core.local_brain import CASCADE_SYSTEM_PROMPT, LocalBrain

    runner = FakeRunner(
        [
            '{"name": "audio_control", "arguments": {"action": "pause"}}\n\nPaused, sir.',
            "Music paused.",
        ]
    )
    brain = LocalBrain(
        provider=ClaudeVoiceProvider(runner=runner),
        alias="era.cheap",
        tools=list(TOOLS),
        system_prompt=CASCADE_SYSTEM_PROMPT,
    )
    executed = []

    async def execute(name, args):
        executed.append(name)
        return {"success": True}

    async def run():
        return [s async for s in brain.respond("pause", execute)]

    said = asyncio.run(run())
    assert executed == ["audio_control"]
    assert said == ["Music paused."], "the premature 'Paused, sir.' must never be spoken"


def test_plain_messages_are_not_dropped():
    """The adapter used to rebuild the prompt from LocalBrain's side channel only,
    so a caller using `messages` sent an EMPTY prompt."""
    from core.llm.types import LlmMessage

    runner = FakeRunner(["ok"])
    req = LlmRequest(alias="era.cheap", messages=(LlmMessage.text("user", "reply with ok"),))
    ClaudeVoiceProvider(runner=runner).generate(req)
    assert "User: reply with ok" in runner.calls[0]["prompt"]


def test_a_cli_failure_is_an_error_event_not_a_thread_exception():
    """Live: the CLI exited 1, LlmError was raised on LocalBrain's worker thread
    ([UNCAUGHT]), the stream ended with no words and Era said NOTHING."""

    def failing(cmd, prompt, env, cwd, timeout, rid):
        return 1, "", "claude.ai connectors are disabled because ANTHROPIC_API_KEY is set"

    events = list(ClaudeVoiceProvider(runner=failing).stream(request()))
    kinds = [e.kind for e in events]
    assert kinds == ["error"], kinds
    assert "no output" in str(events[0].error)


def test_a_provider_bug_is_also_a_spoken_failure():
    def exploding(cmd, prompt, env, cwd, timeout, rid):
        raise RuntimeError("boom")

    events = list(ClaudeVoiceProvider(runner=exploding).stream(request()))
    assert [e.kind for e in events] == ["error"]


def test_the_brain_speaks_a_failure_instead_of_going_silent():
    from core.local_brain import CASCADE_SYSTEM_PROMPT, LocalBrain

    def failing(cmd, prompt, env, cwd, timeout, rid):
        return 1, "", "boom"

    brain = LocalBrain(
        provider=ClaudeVoiceProvider(runner=failing),
        alias="era.cheap",
        tools=list(TOOLS),
        system_prompt=CASCADE_SYSTEM_PROMPT,
    )

    async def run():
        return [s async for s in brain.respond("hello", None)]

    with pytest.raises(Exception, match="no output"):  # raised, so _local_turn can speak it
        asyncio.run(run())


# ── the environment that broke the CLI inside Era ───────────────────────────


def test_the_gateway_environment_is_stripped_so_the_subscription_is_used(monkeypatch):
    """Karim's Windows USER environment sets ANTHROPIC_API_KEY / BASE_URL / MODEL for
    OmniRoute; Era inherits them and the CLI then ignores the subscription login
    (exit 1, no output). Reproduced for real: 60 s timeout before, 4-7 s after."""
    from core.llm import registry
    from core.llm.adapters.claude_cli import GATEWAY_ENV_VARS

    for name in GATEWAY_ENV_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("ERA_CLAUDE_KEEP_API_ENV", raising=False)
    env = ClaudeVoiceProvider()._build_env(registry.resolve("era.cheap"))
    assert not [n for n in GATEWAY_ENV_VARS if n in env]


def test_the_escape_hatch_keeps_the_environment(monkeypatch):
    from core.llm import registry

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ERA_CLAUDE_KEEP_API_ENV", "1")
    assert "ANTHROPIC_API_KEY" in ClaudeVoiceProvider()._build_env(registry.resolve("era.cheap"))


def test_the_strip_list_matches_the_one_the_cm_lane_uses():
    """Two copies of "which variables point Claude at the gateway" must not drift."""
    import re
    from pathlib import Path

    from core.llm.adapters.claude_cli import GATEWAY_ENV_VARS

    src = (Path(__file__).resolve().parent.parent / "era_dispatch.py").read_text("utf-8-sig")
    block = re.search(r"_GATEWAY_ENV_VARS = \((.*?)\)", src, re.S).group(1)
    assert set(re.findall(r'"(\w+)"', block)) == set(GATEWAY_ENV_VARS)


# ── ending the Gemini session NOW, not when Gemini next speaks ──────────────


def test_entering_claude_cancels_the_session_tasks_immediately(mono):
    """Live: "Switching to Claude" at 17:07:42, but the local loop only started at
    17:10:21 — after Gemini rotated the session itself — with the window nearly
    spent. setting the go-away event alone does not wake a task parked on receive()."""

    async def go():
        s = Stub()
        s._loop = asyncio.get_running_loop()
        parked = asyncio.create_task(asyncio.sleep(3600))  # stands in for receive()
        other = asyncio.create_task(asyncio.sleep(3600))
        s._session_tasks = [parked, other]
        s.enter_claude_voice("deaf", "x")
        await asyncio.sleep(0.05)
        return parked.cancelled(), other.cancelled()

    assert asyncio.run(go()) == (True, True)


def test_the_watchdog_does_not_cancel_itself(mono):
    async def go():
        s = Stub()
        s._loop = asyncio.get_running_loop()
        parked = asyncio.create_task(asyncio.sleep(3600))
        me = asyncio.current_task()
        s._session_tasks = [parked, me]
        s._end_gemini_session()
        await asyncio.sleep(0.05)
        return parked.cancelled(), me.cancelled()

    assert asyncio.run(go()) == (True, False)


def test_ending_the_session_from_another_thread_is_safe(mono):
    """The typed "use Claude" arrives on the HUD's thread, not the event loop's."""
    import threading

    async def go():
        s = Stub()
        s._loop = asyncio.get_running_loop()
        parked = asyncio.create_task(asyncio.sleep(3600))
        s._session_tasks = [parked]
        t = threading.Thread(target=s._end_gemini_session)
        t.start()
        t.join()
        await asyncio.sleep(0.05)
        return parked.cancelled()

    assert asyncio.run(go()) is True


def test_the_session_task_group_ends_normally_when_its_children_are_cancelled():
    """The premise the whole switch rests on: a cancelled child does not abort the
    group, so the run loop falls through to the top (and serve_claude_voice)."""

    async def go():
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(asyncio.sleep(3600)) for _ in range(3)]
            await asyncio.sleep(0.01)
            for t in tasks:
                t.cancel()
        return "group exited normally"

    assert asyncio.run(go()) == "group exited normally"


def test_main_records_the_session_tasks():
    assert "_session_tasks" in _run_source()


# ── a Claude that cannot answer must not stay in charge ─────────────────────


class LoopStub(Stub):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._local_exit = asyncio.Event()

    def local_mode_active(self):
        return True

    def _in_claude(self):
        return True


def test_two_failed_turns_hand_back_to_gemini(mono):
    s = LoopStub()
    s.enter_claude_voice("deaf", "x")

    async def go():
        first = await s.claude_turn_result(False)
        second = await s.claude_turn_result(False)
        return first, second

    assert asyncio.run(go()) == (False, True)
    assert s._claude_until == 0.0 and s._local_exit.is_set()


def test_a_success_resets_the_failure_count(mono):
    s = LoopStub()

    async def go():
        await s.claude_turn_result(False)
        await s.claude_turn_result(True)
        return await s.claude_turn_result(False)

    assert asyncio.run(go()) is False


def test_after_giving_up_claude_is_not_re_entered_automatically(mono):
    """Otherwise a Gemini hiccup bounces the user between two silent brains."""
    s = LoopStub()
    s.enter_claude_voice("deaf", "x")

    async def fail_twice():
        await s.claude_turn_result(False)
        await s.claude_turn_result(False)

    asyncio.run(fail_twice())
    assert s.enter_claude_voice("deaf", "again") is False
    assert s.enter_claude_voice("user", "use Claude", manual=True) is True, "the user may insist"
    mono.t += 301
    assert s.enter_claude_voice("deaf", "later") is True


# ── the local turn never leaves the user in silence ─────────────────────────


def test_a_brain_that_returns_nothing_is_still_answered(monkeypatch):
    """Live symptom: "hears me and ignores me". No exception AND no words used to be
    total silence."""
    import app.era_live.local_fallback as lf

    class SilentBrain:
        alias = "era.cheap"

        async def respond(self, text, execute):
            return
            yield  # pragma: no cover - makes this an async generator

        def cancel(self):
            pass

    played = []

    class Era(lf.LocalFallbackMixin, ClaudeVoiceMixin):
        def __init__(self):
            self.ui = SimpleNamespace(
                muted=False, write_log=lambda t: None, set_state=lambda s: None
            )
            self._standby_active = False
            self._local_brain = SilentBrain()
            self._go_away_event = asyncio.Event()
            self._local_exit = asyncio.Event()
            self._local_outage_reason = "claude"
            self._local_mode_active = True

        def _local_fish_ok(self):
            return True

        async def _local_play(self, pcm):
            played.append(pcm)

        def _tg_ack(self, m):
            pass

        def _note_era_question(self, s):
            pass

        def _flush_failure_notices(self):
            pass

        async def _local_execute_tool(self, name, args):
            return {}

    async def fake_synth(text, lang, allow_fish=True):
        return text.encode(), "fake"

    monkeypatch.setattr("core.local_tts.synthesize_best", fake_synth)
    spoken = asyncio.run(Era()._local_turn("hello"))
    assert spoken, "the user spoke and Era said nothing"
    assert played, "the failure was never played"


# ── Phase 2.5 (2026-10-04): the policy function, and what made the mode fire ─────
# claude_voice_policy was MISSING from core.llm.router; the bare `except Exception` in
# _claude_policy turned that ImportError into "disabled", so the whole mode was dead
# and nothing said so. These pin the function, the loud failure, and the wiring the
# switch needs (verdicts -> enter_claude_voice -> the Claude brain -> hand back).


def test_the_claude_voice_policy_exists_and_refuses_by_default(monkeypatch, tmp_path):
    # The CODE default refuses. (config/llm.yaml turns it on since 2026-10-09 —
    # Karim's decision — so this reads an empty config, not the real one.)
    from pathlib import Path

    from core.llm import registry
    from core.llm.router import claude_voice_policy

    real = (Path(__file__).resolve().parent.parent / "config" / "llm.yaml").read_text("utf-8")
    off = real.replace("  claude_voice:\n    enabled: true", "  claude_voice:\n    enabled: false")
    assert off != real, "config/llm.yaml no longer has the claude_voice block this test edits"
    cfg = tmp_path / "llm.yaml"
    cfg.write_text(off, encoding="utf-8")
    monkeypatch.setenv("ERA_LLM_CONFIG", str(cfg))
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.delenv("ERA_CLAUDE_VOICE", raising=False)
    pol = claude_voice_policy()
    assert pol["enabled"] is False, "voice must not reach a paid CLI unless the user says so"
    assert pol["alias"] == "era.cheap"
    for key in (
        "stt_threads",
        "history_turns",
        "max_tool_rounds",
        "timeout_s",
        "probe_gemini_every_s",
        "max_turns_per_day",
        "min_verdict_hold_s",
        # read by ClaudeVoiceMixin.enter_claude_voice, so they must be configurable
        "stay_s",
        "manual_stay_s",
        "healthy_after_s",
        "max_stay_s",
    ):
        assert key in pol, key
    monkeypatch.setenv("ERA_CLAUDE_VOICE", "1")
    assert claude_voice_policy()["enabled"] is True
    monkeypatch.setenv("ERA_CLAUDE_VOICE", "0")
    assert claude_voice_policy()["enabled"] is False


def test_enabled_claude_voice_is_ready_when_its_pieces_exist(monkeypatch):
    monkeypatch.setenv("ERA_CLAUDE_VOICE", "1")
    monkeypatch.setattr("core.claude_headless.available", lambda: True)
    monkeypatch.setattr("core.local_stt.available", lambda: (True, ""))
    monkeypatch.setattr("core.local_tts.available", lambda: (True, ""))
    assert ClaudeVoiceMixin().claude_voice_ready() == (True, "")


def test_a_missing_policy_function_is_logged_as_a_code_bug_not_swallowed(monkeypatch):
    import core.llm.router as router

    seen = []
    monkeypatch.setattr(
        "app.era_live.claude_voice.log_event", lambda _logger, event, *a, **k: seen.append(event)
    )
    monkeypatch.delattr(router, "claude_voice_policy")
    assert ClaudeVoiceMixin()._claude_policy() == {"enabled": False}
    assert seen == ["engine.claude_voice.policy_missing"]


def test_a_policy_that_raises_is_logged_and_disables(monkeypatch):
    import core.llm.router as router

    def boom():
        raise RuntimeError("bad yaml")

    seen = []
    monkeypatch.setattr(
        "app.era_live.claude_voice.log_event", lambda _logger, event, *a, **k: seen.append(event)
    )
    monkeypatch.setattr(router, "claude_voice_policy", boom)
    assert ClaudeVoiceMixin()._claude_policy() == {"enabled": False}
    assert seen == ["engine.claude_voice.policy_raised"]


def test_live_health_is_on_unless_it_is_switched_off(monkeypatch):
    """No launch config needs ERA_LIVE_HEALTH=1: absent means ON."""
    from core import live_health

    monkeypatch.delenv("ERA_LIVE_HEALTH", raising=False)
    assert live_health.enabled() is True
    monkeypatch.setenv("ERA_LIVE_HEALTH", "0")
    assert live_health.enabled() is False


def test_the_default_thresholds_are_the_documented_ones():
    """mute at reply_timeout_s=25 after the user was heard; flaky at 3 upstream errors."""
    c = Clock()
    h = LiveHealth(c)
    h.note_heard()
    c.advance(24.0)
    assert h.verdict() is None
    c.advance(1.0)
    assert h.verdict().kind == "mute"

    h2 = LiveHealth(Clock())
    h2.note_upstream_error()
    h2.note_upstream_error()
    assert h2.verdict() is None
    h2.note_upstream_error()
    assert h2.verdict().kind == "flaky"


@pytest.mark.parametrize("kind", ["deaf", "mute", "flaky"])
def test_the_watchdog_enters_claude_on_every_kind_of_verdict(kind, monkeypatch):
    s = Stub()
    h, c = s.live_health, s.clock
    if kind == "deaf":
        speak(h, c, 3.5)  # a long utterance Gemini never transcribed
        c.advance(12)
    elif kind == "mute":
        h.note_heard()
        c.advance(25)
    else:
        for _ in range(3):
            h.note_upstream_error()
    entered = []
    s.enter_claude_voice = lambda k, detail="", **kw: entered.append(k) or True

    async def fast_sleep(_):
        return None

    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", fast_sleep)
    asyncio.run(s._live_health_watch())
    assert entered == [kind]


def _claude_era(monkeypatch):
    """A local loop serving Claude whose brain says nothing; returns (era, played)."""
    import app.era_live.local_fallback as lf

    class SilentBrain:
        alias = "era.cheap"

        async def respond(self, text, execute):
            return
            yield  # pragma: no cover - makes this an async generator

        def cancel(self):
            pass

    played = []

    class Era(lf.LocalFallbackMixin, ClaudeVoiceMixin):
        def __init__(self):
            self.ui = SimpleNamespace(
                muted=False, write_log=lambda t: None, set_state=lambda s: None
            )
            self._standby_active = False
            self._local_brain = SilentBrain()
            self._go_away_event = asyncio.Event()
            self._local_exit = asyncio.Event()
            self._local_outage_reason = "claude"
            self._local_mode_active = True
            self._claude_until = 10**9
            self._local_turn_task = None

        def _local_fish_ok(self):
            return True

        async def _local_play(self, pcm):
            played.append(pcm)

        def _tg_ack(self, m):
            pass

        def _note_era_question(self, s):
            pass

        def _is_abort_order(self, t):
            return False

        def _is_reboot_order(self, t):
            return False

    async def fake_synth(text, lang, allow_fish=True):
        return text.encode(), "fake"

    monkeypatch.setattr("core.local_tts.synthesize_best", fake_synth)
    return Era(), played


def test_two_failed_claude_turns_end_the_window_and_say_so(monkeypatch):
    era, played = _claude_era(monkeypatch)
    asyncio.run(era._local_turn("hello"))
    assert not era._local_exit.is_set(), "one failure must not end the window"
    asyncio.run(era._local_turn("hello"))
    assert era._local_exit.is_set() and era._claude_until == 0.0
    assert "Gemini" in b" ".join(played).decode(), "the user must be told why it changed"


def test_the_reconnect_order_closes_the_claude_window(monkeypatch):
    era, _played = _claude_era(monkeypatch)
    asyncio.run(era._local_on_utterance("reconnect", "en", 0.0))
    assert era._claude_until == 0.0, "left open, 'reconnect' would re-enter Claude at once"
    assert era._local_exit.is_set()


def test_the_claude_reasons_have_their_own_announcement():
    import app.era_live.local_fallback as lf

    for reason in ("claude", "claude_manual"):
        assert reason in lf._ANNOUNCE
        assert "internet" not in lf._ANNOUNCE[reason].lower(), "not the network-outage line"


def test_claude_is_the_brain_only_when_enabled_and_ready():
    import app.era_live.local_fallback as lf

    class NotReady(lf.LocalFallbackMixin, ClaudeVoiceMixin):
        def claude_voice_ready(self):
            return False, "disabled"

    class Ready(lf.LocalFallbackMixin, ClaudeVoiceMixin):
        def claude_voice_ready(self):
            return True, ""

    assert asyncio.run(NotReady()._claude_brain_wanted("quota")) is False
    assert asyncio.run(Ready()._claude_brain_wanted("quota")) is True
    assert asyncio.run(Ready()._claude_brain_wanted("network")) is False


# ── 2026-10-09: no verdict flood, and the "switch to Claude" call sites ─────


def test_an_unavailable_rescue_clears_the_error_history(monkeypatch):
    s = Stub()
    s.claude_voice_ready = lambda: (False, "off")
    for _ in range(3):
        s.live_health.note_upstream_error()
    ticks = []

    async def fast_sleep(_):
        ticks.append(1)
        if len(ticks) > 3:
            s._shutdown_requested = True

    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", fast_sleep)
    asyncio.run(s._live_health_watch())
    # Without clear_errors() the same 'flaky' verdict re-fired every second
    # (437 log lines on 2026-10-08).
    assert s.live_health.verdict() is None


def test_the_claude_order_is_intercepted_by_voice_and_by_text():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert "claude_voice_order(full_in)" in (root / "app/era_live/audio.py").read_text("utf-8")
    assert "claude_voice_order(text)" in (root / "main.py").read_text("utf-8")
