"""HUD event bus + tool adapter: the state machine must only ever reflect real
signals, expire instead of sticking, and never leak secrets or message bodies."""

import time

import pytest

from app.hud import tool_adapter
from app.hud.bus_const import PHASE_GUARD_S, classify_connection
from app.hud.events import HudBus
from core.tool_result import ToolResult


@pytest.fixture
def bus():
    b = HudBus()
    sent: list[dict] = []
    b.attach(sent.append)
    b.sent = sent  # type: ignore[attr-defined]
    b.connection("Connected")
    return b


def events(bus, etype=None):
    evs = [m["event"] for m in bus.sent if m.get("type") == "event"]
    return [e for e in evs if etype is None or e["type"] == etype]


def test_starts_offline_until_the_model_connects():
    b = HudBus()
    assert b.state()["state"] == "offline"
    b.connection("Reconnecting")
    assert b.state() == {"state": "offline", "label": "Reconnecting"}
    b.connection("Connected")
    assert b.state() == {"state": "idle", "label": "Ready"}


def test_connection_status_strings_are_classified():
    assert classify_connection("Connected") == "connected"
    assert classify_connection("Cooling down (quota) — retrying in ~15 min") == "cooldown"
    assert classify_connection("Quota exhausted — trying backup API key") == "cooldown"
    assert classify_connection("API key invalid — check config/api_keys.json") == "auth"
    assert classify_connection("Local mode — Gemini unavailable (quota)") == "local"
    assert classify_connection("Offline — retrying in background") == "offline"
    assert classify_connection("Reconnecting") == "reconnecting"


def test_mic_off_is_reported_as_such(bus):
    bus.observe_legacy({"type": "browser_mic", "active": False})
    assert bus.state() == {"state": "idle", "label": "Mic off"}


def test_voice_turn_walks_the_real_states(bus):
    bus.transcript_partial("what can")
    assert bus.state()["state"] == "transcribing"
    bus.user_speech_finished("what can you do")
    assert bus.state()["state"] == "thinking"
    bus.response_delta("I can")
    assert bus.state()["state"] == "synthesizing"
    bus.observe_legacy({"type": "state", "state": "SPEAKING"})
    assert bus.state()["state"] == "speaking"
    bus.response_completed("I can do a lot.")
    bus.observe_legacy({"type": "state", "state": "LISTENING"})
    assert bus.state()["state"] == "complete"  # the one-shot completion pulse
    types = [e["type"] for e in events(bus)]
    for t in ("voice.transcript.final", "agent.response.completed", "tts.started", "tts.completed"):
        assert t in types


def test_latency_is_measured_not_invented(bus):
    assert bus.latency()["ttftMs"] is None
    bus.request_sent("typed")
    time.sleep(0.05)
    bus.response_delta("hi")
    assert bus.latency()["ttftMs"] >= 40


def test_barge_in_marks_the_turn_interrupted_and_suppresses_completion(bus):
    bus.request_sent()
    bus.observe_legacy({"type": "state", "state": "SPEAKING"})
    bus.barge_in("voice")
    bus.observe_legacy({"type": "state", "state": "LISTENING"})
    assert bus.state()["state"] == "interrupted"
    assert not events(bus, "tts.completed")
    bus.response_completed("half an answer")
    done = events(bus, "agent.response.completed")[-1]
    assert done["metadata"]["interrupted"] is True
    # the next turn starts clean: a second interruption is reported again
    bus.request_sent()
    bus.observe_legacy({"type": "state", "state": "SPEAKING"})
    bus.barge_in("voice")
    assert len(events(bus, "tts.interrupted")) == 2


def test_standby_outranks_everything(bus):
    bus.observe_legacy({"type": "state", "state": "STANDBY"})
    bus.transcript_partial("ignored while asleep")
    assert bus.state()["state"] == "standby"
    bus.observe_legacy({"type": "mute", "muted": False})
    bus.observe_legacy({"type": "state", "state": "LISTENING"})
    assert bus.state()["state"] == "idle"


def test_a_phase_never_sticks(bus, monkeypatch):
    bus.request_sent()
    assert bus.state()["state"] == "thinking"
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + PHASE_GUARD_S["thinking"] + 1)
    bus._expire(time.monotonic())
    assert bus.state()["state"] == "idle"


def test_tool_run_lifecycle_is_reported_with_real_duration(bus):
    cid = bus.tool_started("fc-1", "spotify_control", {"action": "pause"})
    assert bus.state()["state"] == "tool_running"
    assert "SPOTIFY" in bus.state()["label"]
    bus.tool_finished(
        cid,
        "spotify_control",
        {"action": "pause"},
        ToolResult(success=True, message="Paused.", duration_ms=321),
    )
    run = bus.activity()["tools"][-1]
    assert run["status"] == "success" and run["duration"] == 321
    assert bus.state()["state"] != "tool_running"
    assert events(bus, "tool.completed")


def test_background_jobs_do_not_hold_the_agent_state(bus):
    bus.tool_started(None, "community_manager", {"task_type": "weekly"}, background=True)
    assert bus.state()["state"] == "idle"


def test_a_failed_tool_is_an_error_and_a_recorded_failure(bus):
    cid = bus.tool_started("fc-2", "spotify_control", {"action": "pause"})
    bus.tool_finished(
        cid, "spotify_control", {}, ToolResult(success=False, error="Spotify is not running")
    )
    assert bus.state()["state"] == "error"
    assert bus.failures()[-1]["where"] == "tool:spotify_control"


def test_gate_outcomes_are_not_called_failures(bus):
    cid = bus.tool_started("fc-3", "send_message", {"message": "hi"})
    bus.tool_finished(
        cid,
        "send_message",
        {},
        ToolResult(success=False, error="needs ok", error_type="ConfirmationRequired"),
    )
    assert bus.activity()["tools"][-1]["status"] == "awaiting"
    assert events(bus, "tool.progress")
    assert not bus.failures()


def test_web_sources_are_only_urls_that_appear_in_the_result(bus):
    text = "Search results for: q\n\n1. Gemini changelog\n   notes\n   https://ai.google.dev/changelog\n"
    cid = bus.tool_started("fc-4", "web_search", {"query": "q"})
    bus.tool_finished(cid, "web_search", {"query": "q"}, ToolResult(success=True, message=text))
    srcs = bus.activity()["sources"]
    assert [s["url"] for s in srcs] == ["https://ai.google.dev/changelog"]
    assert srcs[0]["title"] == "Gemini changelog"


def test_memory_retrieval_counts_real_entries(bus):
    text = "preferences/coffee: black\nnotes/project: EraOne HUD"
    cid = bus.tool_started("fc-5", "retrieve_memory_context", {"query": "coffee"})
    bus.tool_finished(cid, "retrieve_memory_context", {}, ToolResult(success=True, message=text))
    assert bus.memory_stats()["last_count"] == 2
    none = bus.tool_started("fc-6", "retrieve_memory_context", {"query": "x"})
    bus.tool_finished(
        none,
        "retrieve_memory_context",
        {},
        ToolResult(success=True, message="No relevant memories found."),
    )
    assert bus.memory_stats()["last_count"] == 2  # nothing invented for an empty result


def test_safe_args_masks_secrets_and_bodies():
    out = tool_adapter.safe_args(
        {
            "action": "send",
            "api_key": "AIzaSECRET",
            "password": "hunter2",
            "message": "a" * 500,
            "_source": "x",
        }
    )
    assert out["api_key"] == "•••" and out["password"] == "•••"
    assert out["message"] == "<500 chars>"
    assert "_source" not in out
    assert "hunter2" not in repr(out) and "AIzaSECRET" not in repr(out)


def test_unknown_tools_still_get_a_generic_row():
    d = tool_adapter.describe("brand_new_tool", {"action": "do_it"})
    assert d["label"] == "BRAND NEW TOOL" and d["activity"] == "do it"


def test_typed_input_is_not_recorded_twice(bus):
    bus.user_input("pause spotify")
    bus.observe_legacy({"type": "log", "text": "You: pause spotify"})
    users = [e for e in bus.conversation() if e["role"] == "user"]
    assert len(users) == 1
    bus.user_input("__mute__")  # sentinels are plumbing, not conversation
    assert len([e for e in bus.conversation() if e["role"] == "user"]) == 1


def test_a_broken_sink_never_raises_into_the_engine():
    b = HudBus()

    def boom(_msg):
        raise RuntimeError("socket gone")

    b.attach(boom)
    b.connection("Connected")
    b.tool_started("x", "web_search", {})
    b.voice_level(0.5)  # nothing propagates


def test_voice_level_needs_a_sustained_signal(bus):
    bus.voice_level(0.5)
    assert bus.state()["state"] == "idle"  # one loud block is not speech
    bus.voice_level(0.5)
    assert bus.state()["state"] == "listening"
    assert any(m.get("type") == "voice_level" for m in bus.sent)


def test_first_audio_is_measured_even_when_text_finishes_first(bus):
    """Fish/Kokoro: the reply TEXT completes before its synthesized audio plays."""
    bus.request_sent("typed")
    bus.response_delta("Right now")
    bus.response_completed("Right now it is 21 degrees.")
    time.sleep(0.03)
    bus.observe_legacy({"type": "state", "state": "SPEAKING"})
    assert bus.latency()["firstAudioMs"] >= 25
