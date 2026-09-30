"""Tests for core/llm/types.py and core/llm/registry.py (phase 2).

The properties under test are the ones whose failure is expensive and silent:

  * NON-VACUITY — an empty or unreadable registry must RAISE, never resolve to
    "no aliases known". A validator that reads 0 of 0 entries and reports
    health has already bitten this project once; here it would make every
    lookup fail with "unknown alias" and read like a typo instead of a missing
    config file.
  * ISOLATION — these tests must never read the user's real config/llm.yaml.
    ERA_LLM_CONFIG is pointed at a fixture in an autouse fixture, for the same
    reason test_media_host now clears CLOUDINARY_* : a test that reads real
    configuration is testing the machine, not the code.
  * CAPABILITY GATING BEFORE SPEND — validate() is what stops Era routing voice
    at a model that cannot hear, or paying for a call that was always going to
    fail. An unavailable alias must explain itself in words that can be spoken.
  * MTIME CACHE HONESTY — the cache is keyed on (mtime, path); repointing
    ERA_LLM_CONFIG must not serve the previous file's contents.
"""

from __future__ import annotations

import textwrap

import pytest

from core.llm import registry
from core.llm.types import (
    Capability,
    ContentPart,
    LlmError,
    LlmMessage,
    LlmRequest,
    PromptBlock,
    Usage,
)

MINIMAL = """
version: 1
default: era.voice
voice_default: era.voice
providers:
  gemini:
    reports_cost: false
  claude_cli:
    reports_cost: true
aliases:
  era.voice:
    provider: gemini
    model: models/gemini-live
    role: the voice loop
    capabilities:
      realtime_audio: true
      tools: true
      streaming: true
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
      long_context: true
      cost_reporting: true
    measured:
      call_overhead_usd: 0.058
      total_ms: 2756
  era.cheap:
    provider: claude_cli
    model: haiku
    available: false
    unavailable_reason: the proxy it points at is unreachable
    capabilities:
      json_schema: true
routing:
  availability_fallback: era.voice
  daily_budget_usd: 2.0
"""


def _write(tmp_path, text: str):
    p = tmp_path / "llm.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path):
    """Never read the user's real registry, and never serve a stale cache
    between tests."""
    monkeypatch.setattr(registry, "_CACHE", None)
    monkeypatch.setenv("ERA_LLM_CONFIG", str(_write(tmp_path, MINIMAL)))
    return tmp_path


# ---------------------------------------------------------------------------
# Isolation and non-vacuity
# ---------------------------------------------------------------------------


def test_fixture_isolates_from_the_real_registry(tmp_path):
    assert registry._config_path() == tmp_path / "llm.yaml"
    assert registry._config_path() != registry.CONFIG_PATH


def test_registry_is_not_empty():
    """Non-vacuity guard: every assertion below is worthless if this is 0."""
    assert len(registry.all_aliases()) == 3


def test_missing_file_raises_rather_than_returning_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("ERA_LLM_CONFIG", str(tmp_path / "does_not_exist.yaml"))
    monkeypatch.setattr(registry, "_CACHE", None)
    with pytest.raises(registry.RegistryError, match="cannot read"):
        registry.load_config()


def test_registry_with_no_aliases_raises(monkeypatch, tmp_path):
    """The exact silent-blindness failure: a parseable file with nothing in it
    must not read as a healthy empty registry."""
    monkeypatch.setenv("ERA_LLM_CONFIG", str(_write(tmp_path, "version: 1\naliases: {}\n")))
    monkeypatch.setattr(registry, "_CACHE", None)
    with pytest.raises(registry.RegistryError, match="declares no aliases"):
        registry.load_config()


def test_malformed_yaml_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("ERA_LLM_CONFIG", str(_write(tmp_path, "aliases: [unclosed\n")))
    monkeypatch.setattr(registry, "_CACHE", None)
    with pytest.raises(registry.RegistryError, match="not valid YAML"):
        registry.load_config()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_returns_provider_and_model():
    e = registry.resolve("era.voice")
    assert (e.provider, e.model) == ("gemini", "models/gemini-live")
    assert e.capabilities.realtime_audio is True
    assert e.provider_config == {"reports_cost": False}


def test_unknown_alias_lists_the_known_ones():
    """An unknown alias is nearly always a typo; the list is the fix."""
    with pytest.raises(registry.RegistryError) as exc:
        registry.resolve("era.nonexistent")
    msg = str(exc.value)
    assert "era.nonexistent" in msg
    for known in ("era.voice", "era.reason", "era.cheap"):
        assert known in msg


def test_alias_missing_provider_or_model_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "ERA_LLM_CONFIG",
        str(_write(tmp_path, "aliases:\n  broken:\n    role: no provider\n")),
    )
    monkeypatch.setattr(registry, "_CACHE", None)
    with pytest.raises(registry.RegistryError, match="missing a provider or model"):
        registry.resolve("broken")


def test_defaults_are_read():
    assert registry.default_alias() == "era.voice"
    assert registry.voice_alias() == "era.voice"


def test_voice_alias_falls_back_to_default(monkeypatch, tmp_path):
    text = MINIMAL.replace("voice_default: era.voice\n", "")
    monkeypatch.setenv("ERA_LLM_CONFIG", str(_write(tmp_path, text)))
    monkeypatch.setattr(registry, "_CACHE", None)
    assert registry.voice_alias() == "era.voice"


def test_measured_numbers_surface():
    e = registry.resolve("era.reason")
    assert e.call_overhead_usd == pytest.approx(0.058)
    assert e.floor_latency_ms == 2756
    # An alias with no measured block must report 0, not raise.
    assert registry.resolve("era.voice").call_overhead_usd == 0.0


# ---------------------------------------------------------------------------
# Capability gating — the check that happens BEFORE anything is spent
# ---------------------------------------------------------------------------


def test_validate_passes_when_capable():
    assert registry.validate("era.voice", realtime_audio=True).model == "models/gemini-live"


def test_validate_rejects_a_missing_capability():
    """Routing voice at a model that cannot hear is the failure this prevents."""
    with pytest.raises(registry.RegistryError, match="does not support realtime audio"):
        registry.validate("era.reason", realtime_audio=True)


def test_validate_rejects_an_unavailable_alias_with_a_speakable_reason():
    with pytest.raises(registry.RegistryError) as exc:
        registry.validate("era.cheap", json_schema=True)
    assert "the proxy it points at is unreachable" in str(exc.value)


def test_unavailable_alias_is_capable_but_not_usable():
    """available is a fact about the environment, not about the model — the two
    must not be conflated, or a temporarily-unreachable model looks incapable
    forever."""
    caps = registry.resolve("era.cheap").capabilities
    assert caps.json_schema is True
    assert caps.available is False


def test_available_aliases_filters_on_capability_and_availability():
    assert registry.available_aliases(realtime_audio=True) == ["era.voice"]
    assert registry.available_aliases(json_schema=True) == ["era.reason"]  # era.cheap is out
    assert "era.cheap" not in registry.available_aliases()


def test_describe_is_human_readable():
    assert "gemini/models/gemini-live" in registry.describe("era.voice")
    assert "$0.058/call" in registry.describe("era.reason")
    assert "UNAVAILABLE" in registry.describe("era.cheap")


def test_routing_policy_is_exposed():
    policy = registry.routing_policy()
    assert policy["availability_fallback"] == "era.voice"
    assert policy["daily_budget_usd"] == 2.0


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_cache_does_not_leak_across_config_paths(monkeypatch, tmp_path):
    """Repointing ERA_LLM_CONFIG must not serve the previous file's contents —
    otherwise a test (or a runtime reconfigure) reads the wrong policy."""
    assert registry.default_alias() == "era.voice"
    other = tmp_path / "other.yaml"
    other.write_text(
        textwrap.dedent("""
        default: era.other
        aliases:
          era.other:
            provider: gemini
            model: m
        """),
        encoding="utf-8",
    )
    monkeypatch.setenv("ERA_LLM_CONFIG", str(other))
    assert registry.default_alias() == "era.other"
    assert registry.all_aliases() == ["era.other"]


def test_edits_take_effect_via_mtime(monkeypatch, tmp_path):
    import os
    import time

    assert registry.default_alias() == "era.voice"
    path = tmp_path / "llm.yaml"
    path.write_text(
        textwrap.dedent("""
        default: era.edited
        aliases:
          era.edited:
            provider: gemini
            model: m
        """),
        encoding="utf-8",
    )
    # Force a distinct mtime: a same-second rewrite can otherwise land on the
    # same timestamp and be served from cache.
    future = time.time() + 10
    os.utime(path, (future, future))
    assert registry.default_alias() == "era.edited"


# ---------------------------------------------------------------------------
# types.py — the neutral shapes
# ---------------------------------------------------------------------------


def test_capability_satisfies_reports_the_missing_one_by_name():
    caps = Capability(tools=True, streaming=True)
    assert caps.satisfies(tools=True) == (True, "")
    ok, reason = caps.satisfies(tools=True, vision=True)
    assert ok is False and "vision" in reason


def test_capability_ignores_unrequested_capabilities():
    ok, _ = Capability(tools=True).satisfies(tools=True, vision=False)
    assert ok is True


def test_message_helpers():
    m = LlmMessage.text("user", "pause the music")
    assert m.role == "user"
    assert m.as_text == "pause the music"
    mixed = LlmMessage(
        role="user",
        parts=(ContentPart(text="look at "), ContentPart(kind="image", mime_type="image/png")),
    )
    assert mixed.as_text == "look at "  # non-text parts contribute nothing


def test_system_text_preserves_block_order():
    """Order is load-bearing: prefix caching only works if the frozen blocks
    come first and the volatile ones last."""
    req = LlmRequest(
        alias="era.reason",
        system=(
            PromptBlock("FROZEN", cache_hint=True, label="prompt.txt"),
            PromptBlock("VOLATILE", label="os_vision"),
        ),
    )
    assert req.system_text() == "FROZEN\nVOLATILE"


def test_usage_totals():
    u = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=100, cache_write_tokens=20)
    assert u.total_tokens == 135
    assert Usage().total_tokens == 0


def test_reported_cost_distinguishes_measured_from_derived():
    """The Claude CLI reports a real cost; Gemini's is derived from prices. A
    dashboard has to be able to say which is which."""
    assert Usage(est_cost_usd=0.058, reported_cost=True).reported_cost is True
    assert Usage(est_cost_usd=0.01).reported_cost is False


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        ("auth", False),
        ("quota", False),
        ("unsupported", False),
        ("refusal", False),
        ("rate_limit", True),
        ("transient", True),
        ("timeout", True),
    ],
)
def test_error_retryability_defaults(kind, retryable):
    """Retrying a bad key or a policy refusal burns time and quota for a
    guaranteed second failure — those must default to terminal."""
    assert LlmError(kind, "x").retryable is retryable


def test_error_retryable_can_be_overridden_and_reads_well():
    e = LlmError("auth", "key rejected", provider="gemini", alias="era.voice", retryable=True)
    assert e.retryable is True
    assert "gemini/era.voice" in str(e)
    assert "key rejected" in str(e)


def test_request_defaults_are_safe():
    req = LlmRequest(alias="era.reason")
    assert req.max_output_tokens > 0
    assert req.tools == ()
    assert req.provider_options == {}
    assert req.system_text() == ""


# ---------------------------------------------------------------------------
# The SHIPPED registry. Everything above runs against a fixture, which means
# none of it would notice a typo in the real config/llm.yaml. These tests point
# deliberately at the actual file — isolation by default, one explicit check of
# the real artifact.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_registry(monkeypatch):
    monkeypatch.delenv("ERA_LLM_CONFIG", raising=False)
    monkeypatch.setattr(registry, "_CACHE", None)
    return registry.CONFIG_PATH


def test_shipped_registry_parses_and_is_populated(real_registry):
    assert real_registry.is_file(), f"config/llm.yaml is missing at {real_registry}"
    aliases = registry.all_aliases()
    assert len(aliases) >= 5, f"only {len(aliases)} aliases in the shipped registry"


def test_shipped_registry_every_alias_resolves(real_registry):
    """A typo'd provider or a missing model id must fail here, not at the first
    real request."""
    for alias in registry.all_aliases():
        entry = registry.resolve(alias)
        assert entry.provider, f"{alias} has no provider"
        assert entry.model, f"{alias} has no model id"
        assert entry.provider in {"gemini", "claude_cli", "ollama"}, (
            f"{alias} names an unknown provider {entry.provider!r}"
        )


def test_shipped_defaults_exist_and_are_usable(real_registry):
    assert registry.default_alias() in registry.all_aliases()
    assert registry.voice_alias() in registry.all_aliases()


def test_shipped_voice_alias_can_actually_do_voice(real_registry):
    """The one alias that must never be wrong: Gemini Live is the only thing
    that can serve Era's audio loop."""
    entry = registry.validate(registry.voice_alias(), realtime_audio=True)
    assert entry.provider == "gemini"


def test_shipped_registry_has_exactly_one_realtime_alias(real_registry):
    """Claude has no realtime audio API. If a second alias ever claims it,
    something has been copy-pasted wrong."""
    realtime = registry.available_aliases(realtime_audio=True)
    assert realtime == [registry.voice_alias()], f"unexpected realtime aliases: {realtime}"


def test_shipped_claude_aliases_have_measured_costs(real_registry):
    """The CLI's per-call overhead is the dominant routing input. An unmeasured
    Claude alias would let the router treat it as free."""
    claude = [a for a in registry.all_aliases() if registry.resolve(a).provider == "claude_cli"]
    assert claude, "no claude_cli aliases found"
    for alias in claude:
        entry = registry.resolve(alias)
        if not entry.capabilities.available:
            continue  # an unreachable alias has nothing to measure
        assert entry.call_overhead_usd > 0, f"{alias} has no measured call_overhead_usd"
        assert entry.floor_latency_ms > 0, f"{alias} has no measured total_ms"


def test_shipped_claude_aliases_expose_no_tools(real_registry):
    """Claude is a reasoning oracle on this path: it returns text, and Era's own
    gated layer executes everything. An alias advertising tools would mean tool
    execution had moved inside Claude Code, outside Era's safety layer."""
    for alias in registry.all_aliases():
        entry = registry.resolve(alias)
        if entry.provider == "claude_cli":
            assert entry.capabilities.tools is False, (
                f"{alias} advertises tools — Claude runs tool-free on the CLI path"
            )


def test_shipped_routing_keeps_fast_commands_off_claude(real_registry):
    """The agreed policy, asserted rather than trusted: media, volume,
    navigation and short commands stay on the voice session. The measured
    ~2.7s floor for any claude_cli call is the independent argument."""
    by_task = registry.routing_policy().get("by_task") or {}
    assert by_task, "routing.by_task is empty"
    for task in ("media_playback", "device_adjustment", "navigation_status", "short_command"):
        alias = by_task.get(task)
        assert alias == registry.voice_alias(), f"{task} routes to {alias}, not the voice session"
        assert registry.resolve(alias).provider == "gemini"


def test_shipped_routing_targets_are_real_aliases(real_registry):
    """Every routing target must resolve, except the explicit LANE: markers
    (community management is a separate pipeline, not a model choice)."""
    policy = registry.routing_policy()
    targets = list((policy.get("by_task") or {}).values())
    targets.append(policy.get("availability_fallback"))
    for target in targets:
        if not target or str(target).startswith("LANE:"):
            continue
        registry.resolve(target)  # raises if unknown


def test_shipped_media_bypass_is_default_off(real_registry):
    """It would skip the live session's OS-vision routing rules and the
    utterance window that authorises save_memory. Measure before enabling."""
    bypass = registry.routing_policy().get("deterministic_media_bypass") or {}
    assert bypass.get("enabled") is False
