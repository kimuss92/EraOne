"""Tests for core/llm/adapters/gemini_generate.py (phase 3).

The adapter WRAPS core.ai_provider rather than replacing it, so the properties
that matter are:

  * TRANSLATION IS FAITHFUL — the alias resolves to the right model id, the
    system prompt goes to system_instruction (not concatenated into the
    prompt), and a JSON request sets the json mime type.
  * ERRORS ARE NORMALISED, NOT SWALLOWED — core.ai_provider.generate_json
    returns a fallback on any failure because its callers have no try/except.
    The adapter is the opposite: it RAISES a typed LlmError, because the
    gateway above it needs to know whether to retry, cool down, or route
    elsewhere. Both behaviours are correct for their own callers; conflating
    them would either crash hermes_router or blind the router.
  * QUOTA IS DISTINGUISHED FROM RATE LIMIT — cooling down for an exhausted
    daily allowance and retrying a per-minute burst are different responses,
    and Era already has a growing-cooldown machine that depends on the
    difference.
  * NO PROVIDER LEAKAGE — the adapter must not reach into actions/, and must
    not require google-genai to be importable in order to be tested.
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from core.llm import registry
from core.llm.adapters.gemini_generate import (
    GeminiGenerateProvider,
    classify_gemini_error,
)
from core.llm.types import LlmError, LlmMessage, LlmRequest, PromptBlock

REGISTRY = """
version: 1
default: era.fast
aliases:
  era.fast:
    provider: gemini
    model: gemini-2.5-flash-lite
    capabilities:
      json_schema: true
      streaming: true
  era.priced:
    provider: gemini
    model: gemini-priced
    capabilities:
      json_schema: true
    price:
      input_per_mtok: 1.0
      output_per_mtok: 4.0
  era.notools:
    provider: gemini
    model: gemini-notools
    capabilities:
      json_schema: false
  era.reason:
    provider: claude_cli
    model: sonnet
    capabilities:
      json_schema: true
"""


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path):
    p = tmp_path / "llm.yaml"
    p.write_text(textwrap.dedent(REGISTRY), encoding="utf-8")
    monkeypatch.setenv("ERA_LLM_CONFIG", str(p))
    monkeypatch.setattr(registry, "_CACHE", None)


class FakeGeminiModel:
    """Stands in for core.ai_provider.GeminiModel."""

    instances: list[FakeGeminiModel] = []

    def __init__(self, model_name=None, system_instruction=None, **kw):
        self.model_name = model_name
        self.system_instruction = system_instruction
        self.calls = []
        FakeGeminiModel.instances.append(self)

    def generate_content(self, contents, *, response_mime_type=None):
        self.calls.append({"contents": contents, "response_mime_type": response_mime_type})
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply

    _reply: object = None


def make_ai(reply, *, usage=None):
    """A fake core.ai_provider module exposing just what the adapter uses."""
    if not isinstance(reply, BaseException):
        reply = SimpleNamespace(text=reply, usage_metadata=usage)

    class Model(FakeGeminiModel):
        _reply = reply

    FakeGeminiModel.instances = []
    return SimpleNamespace(GeminiModel=Model)


def provider(reply, *, usage=None):
    return GeminiGenerateProvider(ai_provider_module=make_ai(reply, usage=usage))


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def test_alias_resolves_to_the_configured_model_id():
    p = provider("hello")
    resp = p.generate(LlmRequest(alias="era.fast", messages=(LlmMessage.text("user", "hi"),)))
    assert resp.model_id == "gemini-2.5-flash-lite"
    assert resp.provider == "gemini"
    assert resp.alias == "era.fast"
    assert resp.text == "hello"


def test_system_blocks_go_to_system_instruction_not_the_prompt():
    """Concatenating the system prompt into the user turn would change model
    behaviour AND destroy any chance of prefix caching later."""
    p = provider("ok")
    p.generate(
        LlmRequest(
            alias="era.fast",
            system=(PromptBlock("FROZEN", cache_hint=True), PromptBlock("VOLATILE")),
            messages=(LlmMessage.text("user", "the question"),),
        )
    )
    model = FakeGeminiModel.instances[-1]
    assert model.system_instruction == "FROZEN\nVOLATILE"
    assert model.calls[0]["contents"] == "the question"


def test_no_system_blocks_passes_none():
    p = provider("ok")
    p.generate(LlmRequest(alias="era.fast", messages=(LlmMessage.text("user", "q"),)))
    assert FakeGeminiModel.instances[-1].system_instruction is None


def test_json_request_sets_the_mime_type_and_strips_fences():
    p = provider('```json\n{"a": 1}\n```')
    resp = p.generate(
        LlmRequest(
            alias="era.fast",
            messages=(LlmMessage.text("user", "q"),),
            json_schema={"type": "object"},
        )
    )
    assert FakeGeminiModel.instances[-1].calls[0]["response_mime_type"] == "application/json"
    assert resp.text == '{"a": 1}'


def test_plain_request_does_not_set_the_json_mime_type():
    p = provider("prose")
    p.generate(LlmRequest(alias="era.fast", messages=(LlmMessage.text("user", "q"),)))
    assert FakeGeminiModel.instances[-1].calls[0]["response_mime_type"] is None


def test_multi_turn_prompts_are_role_labelled():
    """A bare concatenation reads to the model as one run-on user message."""
    p = provider("ok")
    p.generate(
        LlmRequest(
            alias="era.fast",
            messages=(
                LlmMessage.text("user", "first"),
                LlmMessage.text("assistant", "reply"),
                LlmMessage.text("user", "second"),
            ),
        )
    )
    contents = FakeGeminiModel.instances[-1].calls[0]["contents"]
    assert contents == "user: first\n\nassistant: reply\n\nuser: second"


def test_single_turn_prompt_is_not_labelled():
    p = provider("ok")
    p.generate(LlmRequest(alias="era.fast", messages=(LlmMessage.text("user", "just this"),)))
    assert FakeGeminiModel.instances[-1].calls[0]["contents"] == "just this"


def test_empty_messages_produce_an_empty_prompt():
    p = provider("ok")
    p.generate(LlmRequest(alias="era.fast"))
    assert FakeGeminiModel.instances[-1].calls[0]["contents"] == ""


# ---------------------------------------------------------------------------
# Usage — accounting Era has never had
# ---------------------------------------------------------------------------


def test_usage_metadata_is_read():
    usage = SimpleNamespace(
        prompt_token_count=120, candidates_token_count=45, cached_content_token_count=1000
    )
    resp = provider("hi", usage=usage).generate(LlmRequest(alias="era.fast"))
    assert resp.usage.input_tokens == 120
    assert resp.usage.output_tokens == 45
    assert resp.usage.cache_read_tokens == 1000
    assert resp.usage.total_tokens == 1165


def test_missing_usage_metadata_costs_a_metric_not_the_call():
    resp = provider("hi", usage=None).generate(LlmRequest(alias="era.fast"))
    assert resp.usage.input_tokens == 0
    assert resp.text == "hi"


def test_broken_usage_metadata_does_not_raise():
    class Hostile:
        @property
        def prompt_token_count(self):
            raise RuntimeError("exploding field")

    resp = provider("hi", usage=Hostile()).generate(LlmRequest(alias="era.fast"))
    assert resp.usage.input_tokens == 0


def test_cost_is_zero_and_unreported_without_configured_prices():
    """Inventing a per-token price would be worse than admitting we have none —
    a cost dashboard has to know which numbers were measured."""
    usage = SimpleNamespace(
        prompt_token_count=1000, candidates_token_count=1000, cached_content_token_count=0
    )
    resp = provider("hi", usage=usage).generate(LlmRequest(alias="era.fast"))
    assert resp.usage.est_cost_usd == 0.0
    assert resp.usage.reported_cost is False


def test_cost_is_computed_when_prices_are_configured():
    usage = SimpleNamespace(
        prompt_token_count=1_000_000, candidates_token_count=500_000, cached_content_token_count=0
    )
    resp = provider("hi", usage=usage).generate(LlmRequest(alias="era.priced"))
    assert resp.usage.est_cost_usd == pytest.approx(1.0 + 2.0)
    assert resp.usage.reported_cost is True


def test_latency_is_recorded():
    resp = provider("hi").generate(LlmRequest(alias="era.fast"))
    assert resp.usage.latency_ms >= 0


# ---------------------------------------------------------------------------
# Error normalisation — the adapter RAISES where ai_provider swallows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("429 RESOURCE_EXHAUSTED: daily quota", "quota"),
        ("Error 429: too many requests", "rate_limit"),
        ("quota exceeded for this project", "quota"),
        ("401 API key not valid", "auth"),
        ("PERMISSION_DENIED", "auth"),
        ("503 UNAVAILABLE", "transient"),
        ("input token count exceeds the maximum", "context_overflow"),
        ("connection reset by peer", "transient"),
    ],
)
def test_error_classification(message, kind):
    assert classify_gemini_error(RuntimeError(message)) == kind


def test_quota_outranks_rate_limit():
    """An exhausted daily allowance wants a growing cooldown; a per-minute
    burst wants a short retry. Era already has both machines — they must not be
    fed the same signal."""
    assert classify_gemini_error(RuntimeError("429 RESOURCE_EXHAUSTED")) == "quota"


def test_generate_raises_a_typed_error():
    p = provider(RuntimeError("429 RESOURCE_EXHAUSTED"))
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.fast"))
    assert exc.value.kind == "quota"
    assert exc.value.provider == "gemini"
    assert exc.value.alias == "era.fast"
    assert exc.value.retryable is False  # quota: cool down, do not hammer


def test_transient_errors_are_retryable():
    p = provider(RuntimeError("503 UNAVAILABLE"))
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.fast"))
    assert exc.value.retryable is True


def test_unparseable_json_is_invalid_response_and_retryable():
    """Distinct from a transport failure: the call worked, the output did not
    conform, so a reprompt is the right next move."""
    p = provider("this is not json")
    with pytest.raises(LlmError) as exc:
        p.generate(LlmRequest(alias="era.fast", json_schema={"type": "object"}))
    assert exc.value.kind == "invalid_response"
    assert exc.value.retryable is True


def test_non_json_request_does_not_validate_json():
    """Prose must not be rejected for failing to be JSON."""
    assert provider("just prose").generate(LlmRequest(alias="era.fast")).text == "just prose"


# ---------------------------------------------------------------------------
# Alias and capability guarding — before anything is spent
# ---------------------------------------------------------------------------


def test_a_claude_alias_is_refused():
    with pytest.raises(LlmError) as exc:
        provider("x").generate(LlmRequest(alias="era.reason"))
    assert exc.value.kind == "unsupported"
    assert "claude_cli" in exc.value.message


def test_unknown_alias_is_unsupported_not_a_crash():
    with pytest.raises(LlmError) as exc:
        provider("x").generate(LlmRequest(alias="era.nope"))
    assert exc.value.kind == "unsupported"


def test_json_request_to_an_alias_without_json_support_is_refused():
    with pytest.raises(LlmError, match="json schema"):
        provider("x").generate(LlmRequest(alias="era.notools", json_schema={"type": "object"}))


def test_tool_request_is_refused_on_this_path():
    """Tools belong to the Live session, not generateContent."""
    with pytest.raises(LlmError, match="tools"):
        provider("x").generate(LlmRequest(alias="era.fast", tools=({"name": "t"},)))


def test_capabilities_reports_the_registry_entry():
    caps = provider("x").capabilities("era.fast")
    assert caps.json_schema is True
    assert caps.realtime_audio is False


def test_capabilities_refuses_another_providers_alias():
    with pytest.raises(LlmError, match="not 'gemini'"):
        provider("x").capabilities("era.reason")


# ---------------------------------------------------------------------------
# Streaming shim and cancel
# ---------------------------------------------------------------------------


def test_stream_emits_text_then_usage_then_turn_complete():
    events = list(provider("hello").stream(LlmRequest(alias="era.fast")))
    assert [e.kind for e in events] == ["text_delta", "usage", "turn_complete"]
    assert events[0].text == "hello"
    assert events[-1].detail["stop_reason"] == "end_turn"


def test_stream_omits_an_empty_text_delta():
    events = list(provider("").stream(LlmRequest(alias="era.fast")))
    assert [e.kind for e in events] == ["usage", "turn_complete"]


def test_stream_propagates_errors():
    with pytest.raises(LlmError):
        list(provider(RuntimeError("503 UNAVAILABLE")).stream(LlmRequest(alias="era.fast")))


def test_cancel_is_safe_for_unknown_ids():
    assert provider("x").cancel("never-seen") is None


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_adapter_does_not_import_actions():
    """An adapter reaching into actions/ has broken the safety boundary: tool
    execution must stay behind classify_action and the confirmation gate."""
    import sys

    before = set(sys.modules)
    import core.llm.adapters.gemini_generate  # noqa: F401

    pulled = {m for m in set(sys.modules) - before if m.startswith(("actions", "app", "main"))}
    assert not pulled, f"adapter pulled in {pulled}"


def test_provider_name_is_stable():
    """registry entries key off this string."""
    assert GeminiGenerateProvider.name == "gemini"


# ---------------------------------------------------------------------------
# Against the REAL core.ai_provider. Every test above injects a fake module, so
# none of them would notice if the adapter called a method core.ai_provider
# does not actually have. These drive the real class with only the genai client
# faked — the seam where the network would be.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_ai_provider(monkeypatch):
    """The genuine core.ai_provider, with its client stubbed."""
    from core import ai_provider

    recorded: list[dict] = []

    def install(text="ok", usage=None, raises=None):
        class Models:
            def generate_content(self, *, model, contents, config=None):
                recorded.append({"model": model, "contents": contents, "config": config})
                if raises is not None:
                    raise raises
                return SimpleNamespace(text=text, usage_metadata=usage)

        client = SimpleNamespace(models=Models())
        ai_provider._CLIENT_CACHE.clear()
        monkeypatch.setattr(ai_provider, "get_client", lambda api_key=None: client)
        return recorded

    return install


def test_real_module_round_trip(real_ai_provider):
    recorded = real_ai_provider(
        text="  real path works  ",
        usage=SimpleNamespace(
            prompt_token_count=11, candidates_token_count=7, cached_content_token_count=3
        ),
    )
    resp = GeminiGenerateProvider().generate(
        LlmRequest(
            alias="era.fast",
            system=(PromptBlock("SYS"),),
            messages=(LlmMessage.text("user", "hello"),),
        )
    )
    assert resp.text == "real path works"
    assert resp.model_id == "gemini-2.5-flash-lite"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (11, 7)
    assert recorded[0]["model"] == "gemini-2.5-flash-lite"
    assert recorded[0]["contents"] == "hello"
    assert recorded[0]["config"].system_instruction == "SYS"


def test_real_module_json_path(real_ai_provider):
    recorded = real_ai_provider(text='```json\n{"routed": true}\n```')
    resp = GeminiGenerateProvider().generate(
        LlmRequest(alias="era.fast", json_schema={"type": "object"})
    )
    assert resp.text == '{"routed": true}'
    assert recorded[0]["config"].response_mime_type == "application/json"


def test_real_module_errors_are_normalised(real_ai_provider):
    """core.ai_provider.generate_json would swallow this and return a fallback;
    the adapter must surface it typed so the gateway can react."""
    real_ai_provider(raises=RuntimeError("429 RESOURCE_EXHAUSTED"))
    with pytest.raises(LlmError) as exc:
        GeminiGenerateProvider().generate(LlmRequest(alias="era.fast"))
    assert exc.value.kind == "quota"


def test_adapter_uses_only_public_ai_provider_surface():
    """Pinned so a refactor of core.ai_provider's internals cannot silently
    break the adapter: it may rely on GeminiModel and nothing private."""
    import inspect

    from core.llm.adapters import gemini_generate

    src = inspect.getsource(gemini_generate)
    assert "self._ai.GeminiModel" in src
    for private in ("_CLIENT_CACHE", "_ai.get_client", "self._ai._"):
        assert private not in src, f"adapter reaches into {private}"
