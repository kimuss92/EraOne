"""Tests for the model-routing override: the grammar and the mixin that applies it.

THE COLLISION THIS FILE EXISTS FOR
----------------------------------
`_VOICE_SWITCH_INTENT` already claims a bare "use gemini" / "switch to gemini",
meaning "use the cloud VOICE", and it is intercepted FIRST in
`_on_text_command`. If `_MODEL_INTENT` also matched those phrases, the model
override would be unreachable for Gemini and — worse — a user asking for the
voice could silently repin their routing.

So the two grammars are asserted DISJOINT over a corpus of both kinds of order.
That assertion is the point of this module; everything else supports it.

The other property under test is that ordinary conversation which merely
mentions a model does not repin routing. "What do you think about Claude?" is a
question, not an instruction.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.model_route import ModelRouteMixin  # noqa: E402
from app.text_intents import (  # noqa: E402
    MODEL_INTENT_AUTO,
    _model_intent,
    _voice_switch_intent,
)
from core.llm import registry  # noqa: E402

CONFIG = """
version: 1
default: era.voice
voice_default: era.voice
providers:
  gemini: {}
  claude_cli: {}
aliases:
  era.voice:
    provider: gemini
    model: gemini-live
    capabilities:
      realtime_audio: true
  era.fast:
    provider: gemini
    model: gemini-flash-lite
    capabilities:
      json_schema: true
  era.smart:
    provider: gemini
    model: gemini-flash
    capabilities:
      json_schema: true
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
  era.deep:
    provider: claude_cli
    model: opus
    capabilities:
      json_schema: true
  era.cheap:
    provider: claude_cli
    model: haiku
    available: false
    unavailable_reason: the proxy it points at is unreachable
    capabilities:
      json_schema: true
routing:
  by_task:
    short_command: era.voice
    complex_planning: era.reason
  availability_fallback: era.voice
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(path))
    return tmp_path


# Phrases that have ALWAYS meant "switch the voice". These must stay voice-only.
VOICE_ORDERS = [
    "use gemini",
    "switch to gemini",
    "go fish",
    "use fish",
    "switch to your normal voice",
    "use the cloud voice",
    "switch back to fish audio",
    "use primary",
]

# Phrases that mean "route my requests to this model".
MODEL_ORDERS = [
    "use claude",
    "ask claude",
    "switch to claude",
    "route to claude",
    "hand this to claude",
    "use claude for this",
    "use opus",
    "use sonnet",
    "think harder",
    "think deeper",
    "think about this carefully",
    "take your time",
    "use the gemini model",
    "use your fast model",
    "use the smart brain",
    "switch to the cheap model",
    "back to automatic routing",
    "go back to default routing",
]


# ── the disjointness guarantee ────────────────────────────────────────────


@pytest.mark.parametrize("phrase", VOICE_ORDERS)
def test_a_voice_order_is_never_read_as_a_model_order(phrase):
    """'use gemini' has always meant the cloud VOICE. It must keep meaning that."""
    assert _model_intent(phrase) is None, f"{phrase!r} would repin model routing"


@pytest.mark.parametrize("phrase", MODEL_ORDERS)
def test_a_model_order_is_never_read_as_a_voice_order(phrase):
    assert _voice_switch_intent(phrase) is None, f"{phrase!r} would switch the voice instead"


def test_the_two_grammars_are_disjoint():
    """Belt and braces over the whole corpus at once — a new phrase added to
    either list is checked against the other automatically."""
    overlaps = [
        p
        for p in VOICE_ORDERS + MODEL_ORDERS
        if _model_intent(p) is not None and _voice_switch_intent(p) is not None
    ]
    assert overlaps == [], f"phrases matched by BOTH grammars: {overlaps}"


def test_the_corpora_are_non_vacuous():
    """Guard against the disjointness test passing because nothing matches."""
    assert sum(_model_intent(p) is not None for p in MODEL_ORDERS) == len(MODEL_ORDERS)
    assert sum(_voice_switch_intent(p) is not None for p in VOICE_ORDERS) == len(VOICE_ORDERS)


# ── the grammar ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("use claude", "era.reason"),
        ("ask claude", "era.reason"),
        ("use sonnet", "era.reason"),
        ("use opus", "era.deep"),
        ("think harder", "era.reason"),
        ("take your time", "era.reason"),
        ("use the gemini model", "era.voice"),
        ("use your fast model", "era.fast"),
        ("use the smart brain", "era.smart"),
        ("switch to the cheap engine", "era.cheap"),
        ("back to automatic routing", MODEL_INTENT_AUTO),
        ("automatic mode", MODEL_INTENT_AUTO),
    ],
)
def test_phrase_maps_to_the_right_token(phrase, expected):
    assert _model_intent(phrase) == expected


@pytest.mark.parametrize("prefix", ["", "era, ", "Era: ", "please ", "era, please "])
def test_address_and_politeness_prefixes_are_accepted(prefix):
    assert _model_intent(f"{prefix}use claude") == "era.reason"


@pytest.mark.parametrize("phrase", ["USE CLAUDE", "Use Claude.", "  use claude  ", "use claude!"])
def test_case_and_punctuation_do_not_matter(phrase):
    assert _model_intent(phrase) == "era.reason"


# ── conversation must not repin routing ───────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "what do you think about claude",
        "I was reading about gemini yesterday",
        "claude is a large language model",
        "do you think claude would be better here",
        "tell me about opus",
        # The adverb is what makes "think ..." an order. Without it these are
        # ordinary sentences, and the grammar was widened to accept
        # "think about this carefully" — so the boundary needs pinning.
        "think about this",
        "let me think about it",
        "think about what I said",
        "think it over",
        "play some music",
        "what is the weather",
        "",
        "   ",
    ],
)
def test_conversation_does_not_trigger_an_override(phrase):
    """A mention is not an instruction. Repinning routing on 'what do you think
    about Claude?' would send every later request somewhere expensive."""
    assert _model_intent(phrase) is None


def test_none_input_is_safe():
    assert _model_intent(None) is None


# ── the mixin that applies it ─────────────────────────────────────────────


class FakeEra(ModelRouteMixin):
    pass


def test_override_starts_unset():
    assert FakeEra().model_override is None


def test_applying_an_override_pins_it():
    era = FakeEra()
    message = era.apply_model_override("era.reason")
    assert era.model_override == "era.reason"
    assert "era.reason" in message


def test_auto_clears_the_override():
    era = FakeEra()
    era.apply_model_override("era.reason")
    message = era.apply_model_override(MODEL_INTENT_AUTO)
    assert era.model_override is None
    assert "automatic" in message.lower()


def test_auto_when_nothing_is_pinned_says_so():
    assert "already" in FakeEra().apply_model_override(MODEL_INTENT_AUTO).lower()


def test_an_unknown_model_leaves_routing_unchanged():
    era = FakeEra()
    era.apply_model_override("era.reason")
    message = era.apply_model_override("gpt-4")
    assert era.model_override == "era.reason", "an unknown word must not clear the pin"
    assert "unchanged" in message


def test_an_unavailable_model_is_reported_at_the_moment_it_is_asked_for():
    """Reporting it now beats degrading silently on the next request, when the
    user has forgotten they set it."""
    era = FakeEra()
    message = era.apply_model_override("era.cheap")
    assert "isn't usable" in message
    assert "proxy" in message


def test_applying_an_override_never_raises():
    """This runs from a text-command handler; a bad word must not cost the handler."""
    era = FakeEra()
    for token in ["", "  ", "!!!", "era.nope", None]:
        era.apply_model_override(token)


# ── the decision the override feeds ───────────────────────────────────────


def test_current_route_honours_the_override():
    era = FakeEra()
    era.apply_model_override("era.reason")
    assert era.current_route("short_command").alias == "era.reason"


def test_current_route_without_an_override_uses_the_policy():
    assert FakeEra().current_route("complex_planning").alias == "era.reason"


def test_current_route_returns_none_instead_of_raising(monkeypatch, tmp_path):
    """Degrade, never raise — this is reachable from the live audio loop."""
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    assert FakeEra().current_route("complex_planning") is None


def test_active_model_label_is_always_printable(monkeypatch, tmp_path):
    era = FakeEra()
    assert era.active_model_label() == "era.voice"
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "absent.yaml"))
    assert era.active_model_label()  # non-empty even with no registry


def test_active_model_label_reflects_the_override():
    era = FakeEra()
    era.apply_model_override("era.deep")
    assert era.active_model_label() == "era.deep"


# ── wiring: both intercept points ─────────────────────────────────────────


def test_the_typed_path_imports_the_intent():
    """A grammar nothing calls is not an override."""
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "_model_intent" in source
    assert "apply_model_override" in source


def test_the_voice_transcript_path_imports_the_intent():
    source = (Path(__file__).resolve().parents[1] / "app" / "era_live" / "audio.py").read_text(
        encoding="utf-8"
    )
    assert "_model_intent" in source
    assert "apply_model_override" in source


def test_the_typed_path_checks_the_voice_switch_first():
    """Ordering is load-bearing: a bare 'use gemini' must reach the voice switch
    before anything else looks at it."""
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert source.index("_voice_switch_intent(text)") < source.index("_model_intent(text)")


# ── "change your brain to X" ──────────────────────────────────────────────
# The natural phrasing, and unambiguous BECAUSE of the noun: "brain" carries
# the whole meaning, so every model word is safe here — including the bare
# "gemini" that the VOICE-switch grammar owns everywhere else.


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("change your brain to opus", "era.deep"),
        ("change your brain to claude", "era.reason"),
        ("change your brain to sonnet", "era.reason"),
        ("change your brain to haiku", "era.cheap"),
        ("change your brain to gemini", "era.voice"),
        ("switch your brain to opus", "era.deep"),
        ("set your brain to fast", "era.fast"),
        ("use your brain smart", "era.smart"),
        ("change the brain to local", "era.classify"),
        ("change your brain to auto", MODEL_INTENT_AUTO),
        ("change your brain to automatic", MODEL_INTENT_AUTO),
        ("switch your brain to default", MODEL_INTENT_AUTO),
        ("era, change your brain to opus", "era.deep"),
        ("Era: please change your brain to Opus.", "era.deep"),
    ],
)
def test_brain_phrasings(phrase, expected):
    assert _model_intent(phrase) == expected


@pytest.mark.parametrize(
    "phrase",
    [
        "change your brain to gemini",
        "switch your brain to gemini",
        "set your brain to local",
    ],
)
def test_a_brain_order_naming_gemini_is_not_a_voice_switch(phrase):
    """This is the whole reason the noun is required elsewhere. With "brain"
    present the meaning is unambiguous, so these must NOT reach the voice
    switch — and the voice grammar must not claim them."""
    assert _model_intent(phrase) is not None
    assert _voice_switch_intent(phrase) is None


@pytest.mark.parametrize(
    "phrase",
    [
        "what is on your brain",
        "my brain hurts",
        "the brain is a muscle",
        "brain",
    ],
)
def test_conversation_about_brains_does_not_repin(phrase):
    assert _model_intent(phrase) is None


def test_every_brain_word_resolves_to_a_real_alias(monkeypatch):
    """A phrasing that maps to a non-existent alias would report 'I don't know
    a model called...' — so the grammar and the SHIPPED registry must agree.
    Read the real file: the fixture above cannot catch a drift in what ships."""
    from core.llm.registry import all_aliases

    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    known = set(all_aliases())
    for phrase in (
        "change your brain to opus",
        "change your brain to claude",
        "change your brain to haiku",
        "change your brain to gemini",
        "set your brain to fast",
        "use your brain smart",
        "change the brain to local",
    ):
        token = _model_intent(phrase)
        assert token in known, f"{phrase!r} -> {token!r} is not a real alias"


def test_display_names_are_human_readable(monkeypatch):
    """The HUD answers "which model is this?", so it must not show era.deep.
    Checked against the SHIPPED registry — these are the names a user sees."""
    from core.llm.registry import display_name

    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)

    assert display_name("era.deep") == "Claude Opus 5"
    assert display_name("era.reason") == "Claude Sonnet 5"
    assert display_name("era.voice") == "Gemini 3.8 Live"  # moved from 3.1 on 2026-10-10
    assert "era." not in display_name("era.fast")
