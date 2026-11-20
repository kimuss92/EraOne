"""PF-044: deep_think hands hard questions to local Qwen ONLY on request or real need.

Owner 2026-11-18: normal conversation must stay as fast as before (Gemini answers
itself); Qwen is used only when asked ("think deeply", "ask Qwen") or when the
question needs long reasoning. The local model gets NO tools, so it can never act
on the machine; the full answer is spoken, not cut to one sentence.
"""

from __future__ import annotations

import inspect

import pytest

from app.tool_declarations import TOOL_DECLARATIONS
from core import deep_think
from core.llm.types import LlmError, LlmResponse, Usage


class _Provider:
    def __init__(self, script=None, text="Take the train. It is cheaper and greener."):
        self.script = script or {}
        self.text = text
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        outcome = self.script.get(request.alias)
        if isinstance(outcome, Exception):
            raise outcome
        return LlmResponse(
            request_id=request.request_id,
            provider="ollama",
            model_id="qwen3.5:9b" if request.alias == "era.primary" else "gemma4:e4b",
            alias=request.alias,
            text=outcome if isinstance(outcome, str) else self.text,
            usage=Usage(),
        )


@pytest.fixture(autouse=True)
def _logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ZORO_TASK_LOG_DIR", raising=False)


# ── the reasoning path ──────────────────────────────────────────────────────


def _thinker(script=None, notes="weighing cost, time, emissions"):
    """Phase 1 stand-in: per alias -> (notes, answer) or an exception."""
    calls = []

    def run(alias, system, user):
        calls.append(alias)
        outcome = (script or {}).get(alias, (notes, ""))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    run.calls = calls
    return run


def test_budgeted_thinking_then_answer_from_notes_with_no_tools():
    provider, thinker = _Provider(), _thinker()
    ok, text, route = deep_think.think("Train or plane?", provider=provider, thinker=thinker)
    assert ok and text.startswith("Take the train")
    assert route["alias"] == "era.primary" and route["phase"] == "answer"
    request = provider.requests[0]
    assert request.provider_options["ollama"]["think"] is False  # phase 2 never re-thinks
    assert "weighing cost" in request.messages[0].as_text  # it answers from its own notes
    assert request.tools == ()  # it can never act on the machine


def test_an_answer_finished_inside_the_budget_is_used_directly():
    provider = _Provider()
    thinker = _thinker({"era.primary": ("notes", "Train. Cheaper and greener.")})
    ok, text, route = deep_think.think("q", provider=provider, thinker=thinker)
    assert ok and text == "Train. Cheaper and greener."
    assert route["phase"] == "think" and provider.requests == []


def test_thinking_is_bounded():
    # Measured 2026-11-18: unbounded qwen3.5:9b thought 129 s and never answered.
    assert deep_think.THINK_BUDGET_S <= 60
    assert deep_think.THINK_BUDGET_TOKENS <= 2000


def test_gemma_takes_over_when_qwen_fails():
    provider = _Provider()
    thinker = _thinker({"era.primary": LlmError("timeout", "slow")})
    ok, _text, route = deep_think.think("hard question", provider=provider, thinker=thinker)
    assert ok and route["alias"] == "era.local"
    assert thinker.calls == ["era.primary", "era.local"]


def test_a_refusal_is_not_retried_on_another_model():
    thinker = _thinker({"era.primary": LlmError("refusal", "no")})
    ok, _text, _route = deep_think.think("q", provider=_Provider(), thinker=thinker)
    assert not ok and thinker.calls == ["era.primary"]


def test_only_local_models_are_in_the_chain():
    assert deep_think.CHAIN == ("era.primary", "era.local")


def test_everything_failing_is_reported_honestly():
    thinker = _thinker(
        {"era.primary": LlmError("transient", "x"), "era.local": LlmError("timeout", "y")}
    )
    ok, text, _route = deep_think.think("q", provider=_Provider(), thinker=thinker)
    assert not ok and "unavailable" in text


def test_an_empty_question_starts_nothing():
    provider, thinker = _Provider(), _thinker()
    ok, _text, _route = deep_think.think("   ", provider=provider, thinker=thinker)
    assert not ok and provider.requests == [] and thinker.calls == []


def test_the_answer_is_speakable_and_complete():
    raw = (
        "## Verdict\n**Take the train.**\n- It is cheaper.\n- It is greener.\nSee [docs](http://x)."
    )
    ok, text, _ = deep_think.think(
        "q", provider=_Provider(script={"era.primary": raw}), thinker=_thinker()
    )
    assert ok
    assert "#" not in text and "**" not in text and "http" not in text
    assert "cheaper" in text and "greener" in text  # not cut to one sentence


# ── the tool surface ────────────────────────────────────────────────────────


def _declaration():
    return next(t for t in TOOL_DECLARATIONS if t["name"] == "deep_think")


def test_the_declaration_limits_it_to_requests_or_real_need():
    decl = _declaration()
    desc = decl["description"]
    assert "ONLY when" in desc and "think deeply" in desc and "NEVER" in desc
    assert decl["parameters"]["required"] == ["question"]


def test_it_is_low_risk_and_informational():
    from core.completion_contract import INFORMATIONAL_TOOLS
    from core.safety_guard import classify_action

    assert "deep_think" in INFORMATIONAL_TOOLS
    risk = classify_action("deep_think", {"question": "q"})
    assert risk["risk_level"] == "LOW" and risk["requires_confirmation"] is False


def test_the_handler_runs_in_the_background_and_speaks_the_full_answer():
    from app.era_live import tool_dispatch

    src = inspect.getsource(tool_dispatch)
    start = src.index('elif tool_name == "deep_think":')
    branch = src[start : start + 2500]
    assert "_spawn_background_task" in branch
    assert "_deliver_or_queue_cm_report" in branch and "verbatim=True" in branch
    assert "STILL RUNNING" in branch and "Do NOT answer" in branch
