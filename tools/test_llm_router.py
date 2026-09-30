"""Tests for core/llm/router.py — which model serves a request, and why.

THE PROPERTIES THAT MATTER
--------------------------
  * PRECEDENCE — override > routing.by_task > default. Getting this wrong means
    "use Claude" is quietly ignored, which is indistinguishable from Claude
    being broken.
  * NO SILENT FALLBACK — an explicit requirement. A decision that displaced the
    user's choice must SAY so, in `describe()` and in the log fields. A router
    that returns a bare alias makes a fallback look like a first choice.
  * REFUSE RATHER THAN RETURN SOMETHING UNUSABLE — if neither the choice nor the
    fallback can serve the request, raising is correct. Returning an alias the
    router already knows will fail converts a clear error into a mystery
    halfway through a sentence.
  * LANES ARE NOT MODELS — community_management routes to a whole separate
    pipeline. A caller must not be able to mistake it for something to prompt.

Isolation follows test_llm_registry.py: ERA_LLM_CONFIG points at a fixture.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm import registry, router  # noqa: E402

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
      tools: true
      streaming: true
  era.fast:
    provider: gemini
    model: gemini-flash-lite
    capabilities:
      json_schema: true
      streaming: true
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
      long_context: true
  era.deep:
    provider: claude_cli
    model: opus
    capabilities:
      json_schema: true
      long_context: true
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
    creative_advanced: era.deep
    community_management: LANE:cm
  availability_fallback: era.voice
"""


def write(tmp_path, text: str) -> Path:
    path = tmp_path / "llm.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(write(tmp_path, CONFIG)))
    return tmp_path


def repoint(monkeypatch, tmp_path, text: str) -> None:
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(write(tmp_path, text)))


# ── precedence ────────────────────────────────────────────────────────────


def test_no_task_uses_the_default():
    d = router.route()
    assert d.alias == "era.voice"
    assert d.source == router.SOURCE_DEFAULT


def test_a_known_task_uses_the_policy():
    d = router.route("complex_planning")
    assert d.alias == "era.reason"
    assert d.source == router.SOURCE_TASK_POLICY
    assert "complex_planning" in d.reason


def test_an_unknown_task_falls_to_the_default():
    d = router.route("nonsense_task")
    assert d.alias == "era.voice"
    assert d.source == router.SOURCE_DEFAULT


def test_an_override_beats_the_task_policy():
    """'Use Claude' on a short command must win, or the override is decorative."""
    d = router.route("short_command", override="era.deep")
    assert d.alias == "era.deep"
    assert d.source == router.SOURCE_OVERRIDE


def test_an_override_beats_the_default():
    d = router.route(override="era.reason")
    assert d.alias == "era.reason"
    assert d.source == router.SOURCE_OVERRIDE


def test_fast_commands_stay_on_the_voice_model():
    """The stated policy: media, volume and short orders do not leave the live
    session. The measured 2.7s Claude floor is the independent argument."""
    for task in ("short_command",):
        assert router.route(task).alias == "era.voice"


# ── lanes ─────────────────────────────────────────────────────────────────


def test_a_lane_target_is_not_returned_as_a_model():
    d = router.route("community_management")
    assert d.is_lane is True
    assert d.lane == "cm"
    assert d.alias == ""


def test_a_lane_decision_describes_itself_as_a_lane():
    assert "not a model" in router.route("community_management").describe()


def test_an_override_still_wins_over_a_lane():
    """An explicit model request must not be swallowed by a lane mapping."""
    d = router.route("community_management", override="era.reason")
    assert d.is_lane is False
    assert d.alias == "era.reason"


# ── fallback: recorded, never silent ──────────────────────────────────────


def test_an_unavailable_choice_falls_back():
    d = router.route(override="era.cheap")
    assert d.alias == "era.voice"
    assert d.is_fallback is True
    assert d.fell_back_from == "era.cheap"


def test_the_fallback_carries_the_reason():
    d = router.route(override="era.cheap")
    assert "proxy" in d.fallback_reason


def test_describe_always_admits_a_fallback():
    """Hiding it is how a user believes they got the reasoning model."""
    text = router.route(override="era.cheap").describe()
    assert "fell back from" in text
    assert "era.cheap" in text


def test_a_first_choice_does_not_claim_to_be_a_fallback():
    d = router.route("complex_planning")
    assert d.is_fallback is False
    assert "fell back" not in d.describe()


def test_log_fields_expose_the_fallback():
    fields = router.route(override="era.cheap").to_log_fields()
    assert fields["fell_back_from"] == "era.cheap"
    assert fields["alias"] == "era.voice"
    assert fields["provider"] == "gemini"


def test_log_fields_are_flat_strings():
    """log_event takes scalars; a nested value would break the emitter."""
    for value in router.route("complex_planning").to_log_fields().values():
        assert isinstance(value, str)


def test_a_capability_the_alias_lacks_causes_a_recorded_fallback():
    d = router.route("complex_planning", realtime_audio=True)
    assert d.alias == "era.voice"
    assert d.fell_back_from == "era.reason"
    assert "realtime audio" in d.fallback_reason


def test_an_unknown_override_alias_falls_back_rather_than_crashing():
    d = router.route(override="era.doesnotexist")
    assert d.is_fallback is True
    assert d.alias == "era.voice"


# ── refusing, rather than returning something unusable ────────────────────


def test_raises_when_neither_choice_nor_fallback_can_serve(monkeypatch, tmp_path):
    """Both out: raising beats returning an alias already known to fail."""
    repoint(monkeypatch, tmp_path, CONFIG.replace("      realtime_audio: true\n", ""))
    with pytest.raises(router.RoutingError) as exc:
        router.route("complex_planning", realtime_audio=True)
    assert "era.reason" in str(exc.value)
    assert "era.voice" in str(exc.value)


def test_raises_when_the_choice_is_itself_the_fallback(monkeypatch, tmp_path):
    repoint(monkeypatch, tmp_path, CONFIG.replace("      realtime_audio: true\n", ""))
    with pytest.raises(router.RoutingError) as exc:
        router.route(override="era.voice", realtime_audio=True)
    assert "itself the fallback" in str(exc.value)


def test_raises_when_there_is_no_default_and_no_task(monkeypatch, tmp_path):
    repoint(monkeypatch, tmp_path, CONFIG.replace("default: era.voice\n", "", 1))
    with pytest.raises(router.RoutingError):
        router.route()


def test_the_error_names_what_was_tried(monkeypatch, tmp_path):
    repoint(monkeypatch, tmp_path, CONFIG.replace("      realtime_audio: true\n", ""))
    with pytest.raises(router.RoutingError) as exc:
        router.route("complex_planning", realtime_audio=True)
    assert "cannot serve" in str(exc.value)


# ── resolve_override ──────────────────────────────────────────────────────


def test_resolve_override_accepts_an_alias():
    assert router.resolve_override("era.reason") == "era.reason"


def test_resolve_override_accepts_a_model_id():
    assert router.resolve_override("opus") == "era.deep"


def test_resolve_override_accepts_the_alias_suffix():
    assert router.resolve_override("reason") == "era.reason"


def test_resolve_override_is_case_insensitive():
    assert router.resolve_override("  ERA.Reason  ") == "era.reason"


@pytest.mark.parametrize("word", [None, "", "  ", "auto", "automatic", "default", "normal"])
def test_resolve_override_treats_auto_words_as_no_override(word):
    assert router.resolve_override(word) is None


def test_resolve_override_refuses_to_guess():
    """A wrong guess routes the request to a model the user did not ask for."""
    assert router.resolve_override("gpt-4") is None
    assert router.resolve_override("something") is None


def test_resolve_override_resolves_an_unavailable_alias():
    """Resolution and availability are different questions — the caller needs
    the alias back so it can explain WHY it is unusable."""
    assert router.resolve_override("era.cheap") == "era.cheap"


# ── available_for ─────────────────────────────────────────────────────────


def test_available_for_excludes_the_unavailable():
    assert "era.cheap" not in router.available_for()


def test_available_for_filters_on_capability():
    assert router.available_for(realtime_audio=True) == ["era.voice"]


def test_available_for_is_non_vacuous():
    assert len(router.available_for()) == 4


# ── the SHIPPED policy ────────────────────────────────────────────────────


@pytest.fixture
def real_registry(monkeypatch):
    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    return registry.CONFIG_PATH


def test_shipped_policy_keeps_fast_commands_on_the_voice_model(real_registry):
    """A regression here would send 'pause' through a 2.7s floor."""
    for task in ("media_playback", "device_adjustment", "navigation_status", "short_command"):
        d = router.route(task)
        assert d.alias == "era.voice", f"{task} routed to {d.alias}"


def test_shipped_policy_sends_reasoning_to_claude(real_registry):
    assert router.route("complex_planning").provider == "claude_cli"


def test_shipped_default_route_is_usable(real_registry):
    d = router.route()
    assert d.alias
    assert d.is_fallback is False


def test_shipped_voice_route_can_actually_hear(real_registry):
    d = router.route(realtime_audio=True)
    assert d.is_fallback is False
    assert d.entry.capabilities.realtime_audio is True
