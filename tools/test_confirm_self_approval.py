"""A pending HIGH-risk action is approved by the USER's own "yes" — never the model.

Security review 2026-09-23: `confirm_pending_confirmation` is LOW risk and its
handler used to consume the slot and run the armed tool unconditionally, so a
model could emit [file_controller(delete), confirm_pending_confirmation] in one
batch — or confirm in a later round, or re-call the tool with confirmed=true and
the id it was shown — and approve its own deletion, including when steered by an
injected email body. Covered here:

  tool_batch       a confirm in the same batch as the call that armed it
  confirm handler  no utterance / said before arming / yes / yes-then-no / typed,
                   spoken in-flight, French
  confirmed=true   re-calling the armed tool with its id needs the same yes
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.tool_batch import ToolBatchMixin  # noqa: E402
from core.llm.exec_ledger import ExecLedger  # noqa: E402
from core.pending_confirmation import (  # noqa: E402
    NOT_USER_APPROVED_MESSAGE,
    PendingConfirmationStore,
)
from core.tool_result import ToolResult  # noqa: E402

td = pytest.importorskip("app.era_live.tool_dispatch")

DELETE = {"action": "delete", "path": r"C:\scratch\report.txt"}


# ── tool_batch: same-batch self-confirmation ────────────────────────────────


class FakeCall:
    def __init__(self, name, args=None):
        self.name, self.args, self.id = name, args or {}, f"call-{name}-{id(self)}"


class BatchEra(ToolBatchMixin):
    """Arms the real store the way _execute_tool_result does for a HIGH call."""

    def __init__(self):
        self.session_manager = SimpleNamespace(get_state_snapshot=lambda: {})
        self._tool_ledger = ExecLedger()
        self._pending_store = PendingConfirmationStore()
        self.executed: list[str] = []

    async def _execute_tool(self, fc):
        self.executed.append(fc.name)
        if fc.name == "file_controller":
            self._pending_store.store("file_controller", dict(fc.args), "HIGH", "delete")
        return {"name": fc.name, "id": fc.id}


def _body(response) -> dict:
    return response.response if hasattr(response, "response") else response


def test_self_confirm_in_the_arming_batch_is_refused():
    era = BatchEra()
    calls = [FakeCall("file_controller", DELETE), FakeCall("confirm_pending_confirmation")]
    responses = asyncio.run(era._run_tool_call_batch(calls))

    assert len(responses) == 2
    assert era.executed == ["file_controller"]  # the confirm never reached a handler
    body = _body(responses[1])
    assert body["error_type"] == "BlockedAction"
    assert body["data"] == {"executed": False, "reason": "confirm_same_batch"}
    assert NOT_USER_APPROVED_MESSAGE in body["error"]
    assert era._pending_store.is_pending()  # still there for the user's real answer


def test_confirm_of_an_action_armed_before_the_batch_reaches_the_handler():
    """The batch check only catches same-batch arming; the handler judges the rest."""
    era = BatchEra()
    era._pending_store.store("file_controller", DELETE, "HIGH", "delete")
    asyncio.run(era._run_tool_call_batch([FakeCall("confirm_pending_confirmation")]))
    assert era.executed == ["confirm_pending_confirmation"]


# ── the confirm handler (_execute_tool_raw) ─────────────────────────────────


def _handler_era():
    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        _pending_store=PendingConfirmationStore(),
        _recent_user_texts=[],
        _inflight_user_text="",
        _inflight_started_at=0.0,
        ran=[],
    )
    real = td.ToolDispatchMixin._execute_tool_raw.__get__(era)

    async def raw(tool_name, args, fc=None):
        if tool_name == "confirm_pending_confirmation":
            return await real(tool_name, args, fc)
        era.ran.append((tool_name, dict(args)))
        return "Deleted."

    era._execute_tool_raw = raw
    return era


def _arm(era):
    era._pending_store.store("file_controller", dict(DELETE), "HIGH", "delete")
    return era._pending_store.peek().created_at


def _confirm(era):
    return asyncio.run(era._execute_tool_raw("confirm_pending_confirmation", {}))


def _refused(era, out) -> bool:
    return (
        isinstance(out, ToolResult)
        and out.error_type == "BlockedAction"
        and era.ran == []
        and era._pending_store.is_pending()
    )


def test_confirm_without_any_user_utterance_is_refused():
    era = _handler_era()
    _arm(era)
    assert _refused(era, _confirm(era))


def test_yes_said_before_arming_does_not_count():
    """The injection shape: "read my email" → the model arms a delete and confirms."""
    era = _handler_era()
    era._recent_user_texts.append((time.time() - 5, "yes"))
    _arm(era)
    assert _refused(era, _confirm(era))


def test_typed_yes_after_arming_runs_the_armed_action():
    era = _handler_era()
    armed_at = _arm(era)
    era._recent_user_texts.append((armed_at + 1, "Yes."))
    out = _confirm(era)
    assert out == "Deleted."
    assert era.ran == [
        (
            "file_controller",
            {**DELETE, "confirmed": True, "confirmation_id": era.ran[0][1]["confirmation_id"]},
        )
    ]
    assert not era._pending_store.is_pending()


@pytest.mark.parametrize(
    "said", ["go ahead", "yes please", "oui", "Oui, vas-y.", "of course", "yes sir", "confirm"]
)
def test_spoken_yes_still_in_flight_confirms(said):
    """Gemini calls confirm before the voice turn completes: the reply is only in
    the in-flight buffer, stamped with when the user started saying it."""
    era = _handler_era()
    armed_at = _arm(era)
    era._recent_user_texts.append((armed_at - 3, "delete the report"))
    era._inflight_user_text, era._inflight_started_at = said, armed_at + 2
    assert _confirm(era) == "Deleted."


def test_in_flight_utterance_begun_before_arming_does_not_count():
    era = _handler_era()
    armed_at = _arm(era)
    era._inflight_user_text, era._inflight_started_at = "delete the report yes", armed_at - 1
    assert _refused(era, _confirm(era))


def test_yes_then_no_dont_is_refused():
    era = _handler_era()
    armed_at = _arm(era)
    era._recent_user_texts += [(armed_at + 1, "yes"), (armed_at + 2, "no, don't")]
    assert _refused(era, _confirm(era))


@pytest.mark.parametrize(
    "said",
    [
        "okay, delete everything in Documents",  # approval word inside a new order
        "can I approve it later?",
        "yes wait",
        "always",  # a bare waiver is not an answer to "delete this?"
        "stop asking",
    ],
)
def test_non_answers_are_refused(said):
    era = _handler_era()
    armed_at = _arm(era)
    era._recent_user_texts.append((armed_at + 1, said))
    assert _refused(era, _confirm(era))


def test_local_guard_uses_the_same_rule():
    from app.era_live.local_guards import user_approved

    era = _handler_era()
    armed_at = _arm(era)
    assert user_approved(era) is False
    era._inflight_user_text, era._inflight_started_at = "go ahead", armed_at + 1
    assert user_approved(era) is True


# ── confirmed=true + confirmation_id re-call (_execute_tool_result) ──────────


def _result_era(monkeypatch):
    import core.vision_context as vc

    async def _no_capture(*a, **k):
        return None

    monkeypatch.setattr(vc, "jit_vision_capture", _no_capture)
    era = SimpleNamespace(
        _last_user_text="",
        _inflight_user_text="",
        _inflight_started_at=0.0,
        _recent_user_texts=[],
        _pending_store=PendingConfirmationStore(),
        speak=lambda *a, **k: None,
        ran=[],
    )

    async def raw(tool_name, args, fc=None):
        era.ran.append(tool_name)
        return "Deleted."

    era._execute_tool_raw = raw
    era._execute_tool_result = td.ToolDispatchMixin._execute_tool_result.__get__(era)
    return era


def _recall(era, cid):
    fc = SimpleNamespace(
        name="file_controller", args={**DELETE, "confirmed": True, "confirmation_id": cid}
    )
    return asyncio.run(era._execute_tool_result(fc))


def test_confirmed_recall_with_the_shown_id_is_refused_without_a_yes(monkeypatch):
    era = _result_era(monkeypatch)
    cid = era._pending_store.store("file_controller", dict(DELETE), "HIGH", "delete")
    out = _recall(era, cid)
    assert out.error_type == "BlockedAction"
    assert era.ran == [] and era._pending_store.is_pending()


def test_confirmed_recall_after_the_users_yes_runs(monkeypatch):
    era = _result_era(monkeypatch)
    cid = era._pending_store.store("file_controller", dict(DELETE), "HIGH", "delete")
    era._recent_user_texts.append((era._pending_store.peek().created_at + 1, "yes"))
    _recall(era, cid)
    assert era.ran == ["file_controller"]
    assert not era._pending_store.is_pending()
