"""The task brains (Claude via OmniRoute, OmniRoute, local): telemetry, spoken choice,
auto-pick with fail-over, and the CM route order they drive. No network: probes are
injected."""

import pytest

import era_dispatch as ed
from app.text_intents import _model_intent
from core.llm import brains
from core.llm.brains import BrainState


def _s(name, ok=True, ms=1000, reason="", model=""):
    return BrainState(name, ok, ms, reason, model, {}, 1.0)


ALL_UP = {
    "claude": _s("claude", model="claude-sonnet-5"),
    "omniroute": _s("omniroute", model="nvidia/nemotron-3-ultra"),
    "local": _s("local", ms=20, model="qwen3.5:9b"),
}


def _down(name, why):
    return {**ALL_UP, name: _s(name, False, None, why)}


def test_light_work_goes_local_heavy_work_goes_to_claude_when_all_are_up():
    assert brains.pick("light", ALL_UP, "auto").brain == "local"
    assert brains.pick("heavy", ALL_UP, "auto").brain == "claude"


def test_auto_skips_a_down_brain_and_says_why():
    d = brains.pick("heavy", _down("claude", "login expired"), "auto")
    assert d.brain == "omniroute" and d.failed_over
    assert "Claude (through OmniRoute) is down (login expired)" in d.reason


def test_an_explicit_choice_wins_while_up_and_fails_over_when_down():
    assert brains.pick("light", ALL_UP, "omniroute").brain == "omniroute"
    d = brains.pick("light", _down("omniroute", "out of credits"), "omniroute")
    assert d.brain == "claude" and d.failed_over and "out of credits" in d.reason
    assert d.requested == "omniroute"
    # a pinned Claude that is down goes to the strongest remaining brain, not the weak one
    both = {**_down("claude", "login expired")}
    assert brains.pick("normal", both, "claude").brain == "omniroute"


def test_everything_down_returns_no_brain_and_says_so():
    states = {n: _s(n, False, None, "off") for n in brains.BRAINS}
    d = brains.pick("normal", states, "auto")
    assert d.brain is None and "No brain is available" in d.reason


def test_a_paid_model_is_named_so_spend_is_never_silent():
    states = {**ALL_UP, "omniroute": _s("omniroute", model="openai/gpt-5.4-mini")}
    d = brains.pick("normal", {**states, "local": _s("local", False, None, "gpu busy")}, "auto")
    assert d.brain == "omniroute" and "PAID model" in d.reason


def test_choice_is_session_state_and_unknown_words_change_nothing():
    assert "Using the local model" in brains.set_choice("local")
    assert brains.get_choice() == "local"
    assert "don't know" in brains.set_choice("gpt")
    assert brains.get_choice() == "local"
    assert "automatic" in brains.set_choice("automatic")


def test_snapshot_reprobes_stale_entries_and_a_crashing_probe_is_a_down_brain():
    calls = []

    def ok():
        calls.append(1)
        return _s("claude")

    def boom():
        raise RuntimeError("x")

    out = brains.snapshot(0, {"claude": ok, "omniroute": boom, "local": ok})
    assert out["claude"].available and not out["omniroute"].available
    assert "probe failed" in out["omniroute"].reason


def test_plain_reasons_for_the_failures_seen_live():
    pr = brains._plain_reason
    assert pr(401, "OAuth access token has expired") == "login expired"
    assert pr(429, "All credentials are cooling down") == "rate limited"
    assert pr(402, "Payment required") == "out of credits"
    assert pr(504, "") == "gateway could not reach a provider"


def test_status_line_says_what_is_up_and_what_is_down():
    line = brains.status_line(_down("claude", "login expired"))
    assert "Claude (through OmniRoute) down (login expired)" in line and "OmniRoute up" in line


@pytest.mark.parametrize(
    "text,token",
    [
        ("use OmniRoute", "brain:omniroute"),
        ("Era, switch to omni route", "brain:omniroute"),
        ("go to the gateway", "brain:omniroute"),
        ("which brain are you using", "brain:status"),
        ("check your brains", "brain:status"),
        ("use Claude", "era.reason"),
        ("use the local model", "era.classify"),
    ],
)
def test_spoken_words_reach_the_brain_tokens(text, token):
    assert _model_intent(text) == token


def test_conversation_that_mentions_omniroute_is_not_an_order():
    assert _model_intent("omniroute is down again, can you check the logs") is None


def test_cm_route_order_follows_the_pinned_brain(monkeypatch):
    monkeypatch.delenv("ERA_CM_DIRECT_ROUTES", raising=False)
    accounts = [r"C:\p\.claude-freelance"]
    env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128", "ANTHROPIC_API_KEY": "k"}

    def names():
        return [r[0].split(":")[0] for r in ed._routes_for(accounts, dict(env))]

    assert names() == ["local", "gateway"]  # automatic: the owner's order
    brains.set_choice("omniroute")
    assert names() == ["gateway", "local"]
    brains.set_choice("claude")
    routes = ed._routes_for(accounts, dict(env))
    assert [r[0].split(":")[0] for r in routes] == ["gateway-claude", "gateway", "local"]
    assert routes[0][1]["ANTHROPIC_MODEL"] == brains.CLAUDE_MODEL == "claude/claude-sonnet-5"
    assert routes[1][1]["ANTHROPIC_MODEL"] == ed._GATEWAY_COMBO


def test_applying_a_spoken_order_sets_the_brain_without_probing():
    from app.era_live.model_route import ModelRouteMixin

    class Fake(ModelRouteMixin):
        def _log_override(self, *a, **k):
            pass

        def publish_route(self, *a, **k):
            pass

        def publish_catalog(self, *a, **k):
            pass

        def current_route(self):
            return None

    said = Fake().apply_model_override("brain:omniroute")
    assert brains.get_choice() == "omniroute" and "OmniRoute" in said
    assert "Checking all three" in Fake().apply_model_override("brain:status")


def test_a_claude_probe_that_was_answered_by_another_model_is_down(monkeypatch):
    """Live 2026-11-19: the claude combo fell through to a free model and looked 'up'."""
    import io
    import json as _json

    class Resp(io.BytesIO):
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = {"model": "nvidia/nemotron-3-ultra", "content": [{"type": "tool_use"}]}
    monkeypatch.setattr(brains.urllib.request, "urlopen", lambda *a, **k: Resp(_json.dumps(body).encode()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    state = brains.probe_claude()
    assert not state.available and "fell back to nvidia/nemotron-3-ultra" in state.reason
    body["model"] = "claude-sonnet-5"
    assert brains.probe_claude().available
