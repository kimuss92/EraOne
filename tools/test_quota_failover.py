"""Tests for core/llm/quota_failover.py — Gemini quota -> spare key -> Claude.

Nothing here spawns the Claude CLI or touches the network: the SDK original,
the key list, the spend ledger and the Claude provider are all injected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.llm import quota_failover as qf
from core.llm.types import LlmError

QUOTA = "429 RESOURCE_EXHAUSTED. You exceeded your current quota"


class FakeSdk:
    """Stands in for the unwrapped Models.generate_content, per API key."""

    def __init__(self, behaviour: dict[str, object]):
        self.behaviour = behaviour  # key -> reply text | Exception
        self.calls: list[str] = []

    def models(self, key: str):
        return SimpleNamespace(_api_client=SimpleNamespace(api_key=key))

    def __call__(self, models, *, model, contents, config=None):
        key = models._api_client.api_key
        self.calls.append(key)
        outcome = self.behaviour[key]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


class FakeLedger:
    def __init__(self, affordable=True):
        self.affordable = affordable
        self.recorded: list[float] = []

    def can_afford(self, estimate):
        return (True, "") if self.affordable else (False, "the daily model budget is spent")

    def record(self, amount, *, alias=""):
        self.recorded.append(amount)


class FakeClaude:
    def __init__(self, text="claude answer", raises=None):
        self.text = text
        self.raises = raises
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return SimpleNamespace(
            text=self.text,
            model_id="claude-haiku-4-5-20251001",
            usage=SimpleNamespace(est_cost_usd=0.012),
        )


@pytest.fixture
def rig(monkeypatch):
    pol = {
        "enabled": True,
        "alias": "era.cheap",
        "max_per_day": 80,
        "timeout_s": 45.0,
        "latch_seconds": 300,
        "rotate_keys": True,
    }
    sdk = FakeSdk({"k1": Exception(QUOTA), "k2": Exception(QUOTA)})
    ledger = FakeLedger()
    claude = FakeClaude()

    monkeypatch.setattr(qf, "policy", lambda: pol)
    monkeypatch.setattr(qf, "_ORIGINAL", sdk)
    monkeypatch.setattr(qf, "_keys_source", lambda: ["k1", "k2"])
    monkeypatch.setattr(qf, "_models_for_key", sdk.models)
    monkeypatch.setattr(qf, "_ledger_factory", lambda: ledger)
    monkeypatch.setattr(qf, "_claude_provider_factory", lambda: claude)
    qf._LATCH.clear()
    qf._CLAUDE_CALLS.clear()
    yield SimpleNamespace(pol=pol, sdk=sdk, ledger=ledger, claude=claude)
    qf._LATCH.clear()
    qf._CLAUDE_CALLS.clear()


def call(rig, contents="hello", config=None, key="k1"):
    return qf.generate_with_failover(
        rig.sdk.models(key), model="gemini-2.5-flash", contents=contents, config=config
    )


def test_success_passes_straight_through(rig):
    rig.sdk.behaviour["k1"] = "gemini answer"
    assert call(rig).text == "gemini answer"
    assert rig.sdk.calls == ["k1"]
    assert rig.claude.requests == []


def test_non_quota_error_is_not_failed_over(rig):
    rig.sdk.behaviour["k1"] = ValueError("400 INVALID_ARGUMENT")
    with pytest.raises(ValueError):
        call(rig)
    assert rig.claude.requests == []


def test_spare_gemini_key_is_tried_before_claude(rig):
    rig.sdk.behaviour["k2"] = "spare key answer"
    assert call(rig).text == "spare key answer"
    assert rig.sdk.calls == ["k1", "k2"]
    assert rig.claude.requests == []


def test_all_keys_exhausted_fails_over_to_claude(rig):
    response = call(rig, contents="what time is it in Tokyo?")
    assert response.text == "claude answer"
    # Callers that read candidates directly (web_search style) must keep working.
    assert response.candidates[0].content.parts[0].text == "claude answer"
    assert rig.ledger.recorded == [0.012]
    request = rig.claude.requests[0]
    assert request.alias == "era.cheap"
    assert request.messages[0].as_text == "what time is it in Tokyo?"
    assert not request.tools


def test_exhausted_pair_is_latched_and_skipped(rig):
    call(rig)
    rig.sdk.calls.clear()
    call(rig)
    assert rig.sdk.calls == []  # straight to Claude, no wasted 429 round-trips
    assert len(rig.claude.requests) == 2


def test_latch_expires_and_gemini_is_primary_again(rig, monkeypatch):
    call(rig)
    for pair in list(qf._LATCH):
        qf._LATCH[pair] = 0.0
    rig.sdk.behaviour["k1"] = "gemini is back"
    assert call(rig).text == "gemini is back"


def test_image_input_reraises_original_quota_error(rig):
    image = {"inline_data": {"mime_type": "image/png", "data": b"x"}}
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig, contents=["describe this", image])
    assert rig.claude.requests == []


def test_non_text_object_reraises(rig):
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig, contents=["describe", object()])
    assert rig.claude.requests == []


def test_search_grounded_call_is_not_failed_over(rig):
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig, config={"tools": [{"google_search": {}}]})
    assert rig.claude.requests == []


def test_budget_spent_reraises_original(rig):
    rig.ledger.affordable = False
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig)
    assert rig.claude.requests == []


def test_daily_cap_reraises_original(rig):
    rig.pol["max_per_day"] = 1
    call(rig)
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig)
    assert len(rig.claude.requests) == 1


def test_claude_failure_reraises_original_gemini_error(rig):
    rig.claude.raises = LlmError("timeout", "slow", provider="claude_cli")
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig)


def test_disabled_is_a_pure_passthrough(rig):
    rig.pol["enabled"] = False
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig)
    assert rig.sdk.calls == ["k1"]
    assert rig.claude.requests == []


def test_json_and_system_instruction_are_carried(rig):
    from google.genai import types as gtypes

    config = gtypes.GenerateContentConfig(
        system_instruction="You are a router.",
        response_mime_type="application/json",
    )
    rig.claude.text = '{"category": "media"}'
    assert call(rig, contents="play jazz", config=config).text == '{"category": "media"}'
    request = rig.claude.requests[0]
    assert request.json_schema == {}
    assert request.system_text() == "You are a router."


def test_content_objects_keep_turn_roles(rig):
    from google.genai import types as gtypes

    contents = [
        gtypes.Content(role="user", parts=[gtypes.Part(text="hi")]),
        gtypes.Content(role="model", parts=[gtypes.Part(text="hello")]),
        gtypes.Content(role="user", parts=[gtypes.Part(text="summarise that")]),
    ]
    call(rig, contents=contents)
    roles = [m.role for m in rig.claude.requests[0].messages]
    assert roles == ["user", "assistant", "user"]


def test_install_is_idempotent_and_skipped_under_pytest(monkeypatch):
    from google.genai import models as genai_models

    monkeypatch.setattr(qf, "_ORIGINAL", None)
    original = genai_models.Models.generate_content
    assert qf.install() is False  # pytest guard: never auto-wraps in tests
    assert genai_models.Models.generate_content is original
    try:
        assert qf.install(force=True) is True
        wrapped = genai_models.Models.generate_content
        assert getattr(wrapped, "_era_quota_failover", False)
        assert qf.install(force=True) is True
        assert genai_models.Models.generate_content is wrapped
    finally:
        qf.uninstall()
    assert genai_models.Models.generate_content is original


def test_policy_fails_closed_when_config_unreadable(monkeypatch):
    from core.llm import router

    def boom():
        raise RuntimeError("bad yaml")

    monkeypatch.setattr(router, "routing_policy", boom)
    assert router.quota_failover_policy()["enabled"] is False


# ── step 4: local model (added 2026-09-23 for 24/7 operation) ─────────────────


class FakeLocal:
    def __init__(self, text="local answer", raises=None):
        self.text = text
        self.raises = raises
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return SimpleNamespace(text=self.text, model_id="qwen2.5-coder:7b")


@pytest.fixture
def local(rig, monkeypatch):
    fake = FakeLocal()
    rig.pol["local_alias"] = "era.local"
    monkeypatch.setattr(qf, "_local_provider_factory", lambda: fake)
    return fake


def test_local_serves_when_claude_fails(rig, local):
    rig.claude.raises = RuntimeError("claude down")
    assert call(rig).text == "local answer"
    assert local.requests[0].alias == "era.local"


def test_local_serves_when_claude_budget_spent(rig, local):
    rig.ledger.affordable = False
    assert call(rig).text == "local answer"
    assert rig.claude.requests == []


def test_claude_still_preferred_over_local(rig, local):
    assert call(rig).text == "claude answer"
    assert local.requests == []


def test_local_skipped_without_alias_reraises_quota(rig, monkeypatch):
    rig.claude.raises = RuntimeError("claude down")
    fake = FakeLocal()
    monkeypatch.setattr(qf, "_local_provider_factory", lambda: fake)
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig)
    assert fake.requests == []


def test_local_failure_reraises_original_quota_error(rig, local):
    rig.claude.raises = RuntimeError("claude down")
    local.raises = RuntimeError("ollama down")
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig)


def test_offline_error_goes_straight_to_local(rig, local):
    rig.pol["offline_failover"] = True
    rig.sdk.behaviour["k1"] = ConnectionError("[Errno 11001] getaddrinfo failed")
    assert call(rig).text == "local answer"
    assert rig.sdk.calls == ["k1"]  # no spare keys: they need the network too
    assert rig.claude.requests == []


def test_offline_failover_off_reraises(rig, local):
    rig.sdk.behaviour["k1"] = ConnectionError("[Errno 11001] getaddrinfo failed")
    with pytest.raises(ConnectionError):
        call(rig)
    assert local.requests == []


def test_bug_is_not_mistaken_for_offline(rig, local):
    rig.pol["offline_failover"] = True
    rig.sdk.behaviour["k1"] = ValueError("400 INVALID_ARGUMENT")
    with pytest.raises(ValueError):
        call(rig)
    assert local.requests == []


def test_offline_image_request_is_not_served_locally(rig, local):
    rig.pol["offline_failover"] = True
    rig.sdk.behaviour["k1"] = ConnectionError("getaddrinfo failed")
    image = {"inline_data": {"data": b"x", "mime_type": "image/png"}}
    with pytest.raises(ConnectionError):
        call(rig, contents=[{"role": "user", "parts": [image, {"text": "what is this"}]}])
    assert local.requests == []


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ConnectionError("x"), True),
        (TimeoutError("x"), True),
        (OSError("[WinError 10061] actively refused"), True),
        (ValueError("400 INVALID_ARGUMENT"), False),
        (RuntimeError("500 INTERNAL"), False),
    ],
)
def test_is_offline_error(exc, expected):
    assert qf.is_offline_error(exc) is expected


# ── CodeCraft step (3a, before Claude) ───────────────────────────────────────


class FakeCodeCraft:
    def __init__(self, text="codecraft answer", raises=None):
        self.text, self.raises, self.requests = text, raises, []

    def generate(self, request):
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return SimpleNamespace(text=self.text, model_id="claude-sonnet-5")


@pytest.fixture
def cc(rig, monkeypatch):
    from core.llm import codecraft_failover as cf

    fake = FakeCodeCraft()
    rig.pol["codecraft"] = True
    monkeypatch.setattr(cf, "_provider_factory", lambda: fake)
    monkeypatch.setattr(cf, "available", lambda: (True, ""))
    return fake


def test_codecraft_serves_before_claude(rig, cc):
    out = call(rig)
    assert out.text == "codecraft answer"
    assert rig.sdk.calls == ["k1", "k2"]  # spare key still tried first
    assert len(cc.requests) == 1 and rig.claude.requests == []
    assert rig.ledger.recorded == []  # subscription: nothing charged to the budget


def test_codecraft_failure_falls_through_to_claude(rig, cc):
    cc.raises = RuntimeError("CodeCraft HTTP 503")
    assert call(rig).text == "claude answer"
    assert len(rig.claude.requests) == 1


def test_codecraft_bad_json_falls_through_to_claude(rig, cc):
    cc.text = "Sure! Here is the data you asked for."
    rig.claude.text = '{"ok": true}'
    out = call(rig, config={"response_mime_type": "application/json"})
    assert out.text == '{"ok": true}'
    assert "ONLY valid JSON" in cc.requests[0].system_text()


def test_codecraft_fenced_json_is_unwrapped(rig, cc):
    cc.text = '```json\n{"a": 1}\n```'
    out = call(rig, config={"response_mime_type": "application/json"})
    assert out.text == '{"a": 1}' and rig.claude.requests == []


def test_codecraft_never_sees_images(rig, cc):
    image = {"parts": [{"inline_data": {"mime_type": "image/png", "data": b"x"}}]}
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        call(rig, contents=image)
    assert cc.requests == [] and rig.claude.requests == []


def test_codecraft_unconfigured_is_skipped(rig, cc, monkeypatch):
    from core.llm import codecraft_failover as cf

    monkeypatch.setattr(cf, "available", lambda: (False, "CodeCraft not configured"))
    assert call(rig).text == "claude answer"
    assert cc.requests == []
