"""Tests for contextual resumption (Phase 2 barge-in): turn memory + intents."""

import pytest

from app.text_intents import (
    build_resume_note,
    build_resume_offer_note,
    is_backchannel,
    is_drop_request,
    is_resume_request,
    looks_unfinished,
)
from core.turn_memory import DEFAULT_TTL_S, InterruptedTurn, TurnMemory
from core.vad import get_endpointing_config, get_resume_config

_LONG = "I was about to explain the wardrobe pricing tiers and the delivery lead time"


# ── TurnMemory ─────────────────────────────────────────────────────────────────
def test_capture_and_peek_roundtrip():
    tm = TurnMemory()
    assert tm.capture(spoken=_LONG) is True
    turn = tm.peek()
    assert isinstance(turn, InterruptedTurn)
    assert turn.spoken == _LONG
    assert turn.offered is False


def test_capture_rejects_thin_content():
    tm = TurnMemory()
    assert tm.capture(spoken="As I—") is False
    assert tm.peek() is None
    # Remainder counts toward the substance bar too.
    assert (
        tm.capture(spoken="Well,", remainder="that depends on the finish you choose here") is True
    )


def test_second_capture_overwrites_first():
    tm = TurnMemory()
    tm.capture(spoken=_LONG)
    tm.capture(spoken="A completely different second thought that is plenty long here")
    assert tm.peek().spoken.startswith("A completely different")


def test_take_consumes():
    tm = TurnMemory()
    tm.capture(spoken=_LONG)
    assert tm.take().spoken == _LONG
    assert tm.peek() is None
    assert tm.take() is None


def test_ttl_expiry_is_lazy():
    tm = TurnMemory(ttl_s=100)
    tm.capture(spoken=_LONG, now=1000.0)
    assert tm.peek(now=1099.0) is not None  # inside window
    assert tm.peek(now=1101.0) is None  # expired → dropped
    assert tm.has_pending(now=1101.0) is False


def test_mark_offered_and_clear():
    tm = TurnMemory()
    tm.capture(spoken=_LONG)
    tm.mark_offered()
    assert tm.peek().offered is True
    tm.clear()
    assert tm.peek() is None


def test_topic_label_truncates():
    turn = InterruptedTurn(spoken=" ".join(f"w{i}" for i in range(20)), remainder="", ts=0.0)
    label = turn.topic(max_words=5)
    assert label == "w0 w1 w2 w3 w4…"  # first 5 words, ellipsis appended to the last


# ── Intent parsing ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "continue",
        "go on",
        "carry on",
        "as you were",
        "where were you",
        "finish that",
        "finish your thought",
        "what were you saying",
        "Era, continue please",
        "continue what you were saying",
    ],
)
def test_resume_intents_match(text):
    assert is_resume_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "continue playing the movie",
        "go on youtube",
        "finish the render",
        "what's the weather",
        "play something",
        "",
    ],
)
def test_resume_intents_reject_real_commands(text):
    assert is_resume_request(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "never mind",
        "nevermind",
        "forget it",
        "forget that",
        "drop it",
        "leave it",
        "don't bother",
        "no need",
        "Era, never mind",
    ],
)
def test_drop_intents_match(text):
    assert is_drop_request(text) is True


@pytest.mark.parametrize("text", ["forget the reminder", "drop the file here", "mind the gap"])
def test_drop_intents_reject_real_commands(text):
    assert is_drop_request(text) is False


# ── Note builders (must forbid tools/actions — 2026-07-21 bug) ─────────────────
def test_resume_note_is_constrained_and_contains_context():
    note = build_resume_note("explaining the three cabinet finishes", "and the glass option")
    assert note.startswith("[System note:")
    assert "explaining the three cabinet finishes" in note
    assert "and the glass option" in note
    assert "do not take any action" in note.lower()
    assert "do not use any tool" in note.lower()


def test_resume_note_truncates_long_context():
    note = build_resume_note("x" * 5000)
    assert len(note) < 1200  # context capped, note stays bounded


def test_offer_note_is_one_shot_and_constrained():
    note = build_resume_offer_note("the wardrobe pricing tiers")
    assert "more than once" in note.lower()
    assert "do not take any action" in note.lower()
    assert "the wardrobe pricing tiers" in note


# ── Config ─────────────────────────────────────────────────────────────────────
def _isolate(monkeypatch):
    monkeypatch.setattr("core.utils.load_config", lambda: {}, raising=False)


def test_resume_config_defaults(monkeypatch):
    _isolate(monkeypatch)
    for v in ("ERA_BARGE_IN_RESUME_OFFER", "ERA_BARGE_IN_RESUME_TTL_S"):
        monkeypatch.delenv(v, raising=False)
    cfg = get_resume_config()
    assert cfg["offer"] is False  # autonomous offer off by default
    assert cfg["ttl_s"] == DEFAULT_TTL_S


def test_resume_config_env_override(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("ERA_BARGE_IN_RESUME_OFFER", "true")
    monkeypatch.setenv("ERA_BARGE_IN_RESUME_TTL_S", "45")
    cfg = get_resume_config()
    assert cfg["offer"] is True
    assert cfg["ttl_s"] == 45.0


def test_resume_config_bad_ttl_falls_back(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("ERA_BARGE_IN_RESUME_TTL_S", "garbage")
    assert get_resume_config()["ttl_s"] == 120.0


# ── Phase 3: implicit turn-taking ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "mm",
        "mhm",
        "hmm",
        "uh huh",
        "uh-huh",
        "yeah",
        "yep",
        "okay",
        "ok",
        "right",
        "sure",
        "go on",
        "keep going",
        "and?",
        "and then?",
        "i see",
        "gotcha",
        "cool",
        "exactly",
        "Era, ok",
    ],
)
def test_backchannel_matches(text):
    assert is_backchannel(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "okay open the browser",
        "yes send it now",
        "play music",
        "what's the weather",
        "right, cancel that",
        "sure thing boss do it",
        "",
    ],
)
def test_backchannel_rejects_real_turns(text):
    assert is_backchannel(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "so I was thinking and",
        "let me check the,",
        "well",
        "tell me about the",
        "can you also",
        "it's because",
        "",
        "hold on, um",
    ],
)
def test_looks_unfinished_true(text):
    assert looks_unfinished(text) is True


@pytest.mark.parametrize(
    "text",
    ["open the browser", "what's the weather?", "play some music.", "turn it off", "call mom now"],
)
def test_looks_unfinished_false(text):
    assert looks_unfinished(text) is False


def test_endpointing_defaults(monkeypatch):
    _isolate(monkeypatch)
    for v in (
        "ERA_ENDPOINTING",
        "ERA_ENDPOINTING_SILENCE_MS",
        "ERA_ENDPOINTING_PREFIX_MS",
        "ERA_ENDPOINTING_START",
        "ERA_ENDPOINTING_END",
    ):
        monkeypatch.delenv(v, raising=False)
    ep = get_endpointing_config()
    assert ep["enabled"] is True
    assert ep["silence_ms"] == 800
    assert ep["prefix_ms"] == 300
    assert ep["start_sensitivity"] == "high"  # quick to notice speech onset
    assert ep["end_sensitivity"] == "low"  # patient about declaring the end


def test_endpointing_env_override(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("ERA_ENDPOINTING", "false")
    monkeypatch.setenv("ERA_ENDPOINTING_SILENCE_MS", "1200")
    monkeypatch.setenv("ERA_ENDPOINTING_END", "high")
    ep = get_endpointing_config()
    assert ep["enabled"] is False
    assert ep["silence_ms"] == 1200
    assert ep["end_sensitivity"] == "high"


def test_endpointing_bad_values_fall_back(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("ERA_ENDPOINTING_SILENCE_MS", "soon")
    monkeypatch.setenv("ERA_ENDPOINTING_END", "medium")  # not low/high
    ep = get_endpointing_config()
    assert ep["silence_ms"] == 800
    assert ep["end_sensitivity"] == "low"


def test_realtime_input_config_builds_or_disables(monkeypatch):
    """The genai enum mapping in tool_dispatch must construct a valid object when
    enabled and return None when disabled (guards against a genai API change)."""
    from app.era_live.tool_dispatch import ToolDispatchMixin

    monkeypatch.setattr("core.utils.load_config", lambda: {}, raising=False)
    monkeypatch.delenv("ERA_ENDPOINTING", raising=False)
    ric = ToolDispatchMixin._realtime_input_config(object())
    assert ric is not None
    aad = ric.automatic_activity_detection
    assert aad.silence_duration_ms == 800
    assert aad.prefix_padding_ms == 300

    monkeypatch.setenv("ERA_ENDPOINTING", "false")
    assert ToolDispatchMixin._realtime_input_config(object()) is None
