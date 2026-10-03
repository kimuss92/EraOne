# tools/test_confirmation_flow.py
from __future__ import annotations

import json
import threading
import time

import pytest

from core.main_tool_bridge import (
    _build_suggested_confirmation_question,
    tool_result_to_response_payload,
)
from core.pending_confirmation import (
    PendingConfirmation,
    PendingConfirmationBusy,
    PendingConfirmationStore,
)
from core.safety_guard import classify_action
from core.tool_result import ToolResult


@pytest.fixture(autouse=True)
def _clear_shared_pending_store():
    """The executor and EraLive share ONE process-wide slot, and it no longer
    silently overwrites. A test that leaves a confirmation armed would block
    the next test's store() — so clear it either side."""
    from core.pending_confirmation import get_shared_store

    get_shared_store().clear()
    yield
    get_shared_store().clear()


# ── PendingConfirmationStore Tests ──


def test_store_returns_confirmation_id():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"to": "John"}, "HIGH", "Destructive action")
    assert isinstance(cid, str)
    assert len(cid) == 8  # 4 bytes hex = 8 chars


def test_get_with_valid_id():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"to": "John"}, "HIGH", "Destructive action")
    pending = store.get(cid)
    assert pending is not None
    assert pending.tool == "send_message"
    assert pending.parameters == {"to": "John"}
    assert pending.risk_level == "HIGH"
    assert pending.reason == "Destructive action"
    assert pending.confirmation_id == cid


def test_get_with_wrong_id():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "reason")
    assert store.get("wrong_id") is None


def test_get_with_no_pending():
    store = PendingConfirmationStore()
    assert store.get("abc12345") is None


def test_confirm_returns_pending_and_clears():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"to": "John"}, "HIGH", "Destructive action")
    pending = store.confirm(cid)
    assert pending is not None
    assert pending.tool == "send_message"
    assert store.is_pending() is False


def test_confirm_with_wrong_id():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "reason")
    assert store.confirm("wrong_id") is None
    assert store.is_pending() is True


def test_confirm_with_no_pending():
    store = PendingConfirmationStore()
    assert store.confirm("abc12345") is None


def test_confirm_with_expired():
    store = PendingConfirmationStore(ttl=0)
    cid = store.store("send_message", {}, "HIGH", "reason")
    time.sleep(0.01)
    assert store.confirm(cid) is None
    assert store.is_pending() is False


def test_cancel_returns_true_and_clears():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {}, "HIGH", "reason")
    assert store.cancel(cid) is True
    assert store.is_pending() is False


def test_cancel_with_wrong_id():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "reason")
    assert store.cancel("wrong_id") is False
    assert store.is_pending() is True


def test_cancel_with_no_pending():
    store = PendingConfirmationStore()
    assert store.cancel("abc12345") is False


def test_is_pending_returns_true_when_active():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "reason")
    assert store.is_pending() is True


def test_is_pending_returns_false_when_expired():
    store = PendingConfirmationStore(ttl=0)
    store.store("send_message", {}, "HIGH", "reason")
    time.sleep(0.01)
    assert store.is_pending() is False


def test_clear_removes_pending():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "reason")
    store.clear()
    assert store.is_pending() is False


def test_expire_all_removes_expired():
    store = PendingConfirmationStore(ttl=0)
    store.store("send_message", {}, "HIGH", "reason")
    time.sleep(0.01)
    assert store.expire_all() == 1
    assert store.is_pending() is False


def test_expire_all_noop_when_none_expired():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "reason")
    assert store.expire_all() == 0
    assert store.is_pending() is True


def test_store_refuses_to_displace_a_live_confirmation():
    """The slot must keep the question the user is answering.

    It used to overwrite: two risky calls in ONE model turn both stored, the
    user heard the first question, and a bare "yes" (allow_sole) consumed the
    second. `allow_sole` only makes sense while the slot cannot be swapped.
    """
    store = PendingConfirmationStore()
    cid1 = store.store("send_message", {"to": "John"}, "HIGH", "reason1")
    with pytest.raises(PendingConfirmationBusy):
        store.store("send_message", {"to": "Jane"}, "HIGH", "reason2")
    assert store.get(cid1) is not None, "the original question was lost"
    assert store.confirm("", allow_sole=True).parameters["to"] == "John"


def test_store_replaces_only_when_asked_explicitly():
    store = PendingConfirmationStore()
    cid1 = store.store("send_message", {"to": "John"}, "HIGH", "reason1")
    cid2 = store.store("send_message", {"to": "Jane"}, "HIGH", "reason2", replace=True)
    assert store.get(cid1) is None
    assert store.get(cid2) is not None


def test_an_expired_confirmation_does_not_block_a_new_one():
    store = PendingConfirmationStore(ttl=-1)
    store.store("send_message", {"to": "John"}, "HIGH", "reason1")
    # No exception: the slot is stale, so nobody is waiting on an answer.
    assert store.store("send_message", {"to": "Jane"}, "HIGH", "reason2")


def test_peek_without_id_returns_active():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"to": "John"}, "HIGH", "test")
    p = store.peek()
    assert p is not None
    assert p.tool == "send_message"
    assert p.confirmation_id == cid


def test_peek_with_valid_id():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"to": "John"}, "HIGH", "test")
    p = store.peek(cid)
    assert p is not None
    assert p.tool == "send_message"


def test_peek_with_wrong_id():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "test")
    assert store.peek("wrong_id") is None


def test_peek_does_not_consume():
    store = PendingConfirmationStore()
    store.store("send_message", {}, "HIGH", "test")
    assert store.peek() is not None
    assert store.is_pending() is True


def test_peek_with_no_pending():
    store = PendingConfirmationStore()
    assert store.peek() is None


def test_peek_expired():
    store = PendingConfirmationStore(ttl=0)
    store.store("send_message", {}, "HIGH", "test")
    time.sleep(0.01)
    assert store.peek() is None
    assert store.is_pending() is False


def test_confirm_pending_without_id():
    store = PendingConfirmationStore()
    store.store("send_message", {"to": "John"}, "HIGH", "test")
    p = store.peek()
    assert p is not None
    confirmed = store.confirm(p.confirmation_id)
    assert confirmed is not None
    assert confirmed.tool == "send_message"
    assert store.is_pending() is False


def test_confirm_pending_no_active_returns_none():
    store = PendingConfirmationStore()
    assert store.peek() is None


def test_simulated_confirm_pending_tool_flow():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John", "message": "Hello"}, "HIGH", "test")
    peek = store.peek(cid)
    assert peek is not None
    stored_params = dict(peek.parameters)
    stored_params["confirmed"] = True
    stored_params["confirmation_id"] = peek.confirmation_id
    assert stored_params["receiver"] == "John"
    assert stored_params["confirmed"] is True
    assert stored_params["confirmation_id"] == cid


def test_confirm_pending_executes_stored_tool():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    peek = store.peek(cid)
    assert peek is not None
    assert peek.tool == "send_message"
    stored_params = dict(peek.parameters)
    stored_params["confirmed"] = True
    stored_params["confirmation_id"] = cid
    confirmed = store.confirm(cid)
    assert confirmed is not None
    assert confirmed.tool == "send_message"
    assert confirmed.parameters["receiver"] == "John"


def test_confirm_pending_wrong_tool_mismatch():
    store = PendingConfirmationStore()
    store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    p = store.peek()
    assert p is not None
    assert p.tool != "shutdown"


def test_cancel_pending_confirmation_clears():
    store = PendingConfirmationStore()
    store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    assert store.is_pending() is True
    store.clear()
    assert store.is_pending() is False


def test_confirm_pending_low_risk():
    from core.safety_guard import classify_action

    safety = classify_action("confirm_pending_confirmation", {})
    assert safety["risk_level"] in ("SAFE", "LOW")
    assert safety["requires_confirmation"] is False


def test_cancel_pending_low_risk():
    from core.safety_guard import classify_action

    safety = classify_action("cancel_pending_confirmation", {})
    assert safety["risk_level"] in ("SAFE", "LOW")
    assert safety["requires_confirmation"] is False


def test_thread_safety():
    store = PendingConfirmationStore()
    errors = []
    winners = []
    busy = []
    barrier = threading.Barrier(10)
    results_lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=5)
            cid = store.store("test", {}, "LOW", "test")
            with results_lock:
                winners.append(cid)
        except PendingConfirmationBusy:
            with results_lock:
                busy.append(True)
        except Exception as exc:
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    # Exactly one caller owns the approval slot; competitors cannot replace it.
    assert len(winners) == 1
    assert len(busy) == 9
    assert store.peek(winners[0]) is not None


# ── PendingConfirmation Dataclass Tests ──


def test_pending_confirmation_defaults():
    pc = PendingConfirmation(tool="send_message", parameters={}, risk_level="HIGH", reason="test")
    assert pc.created_at > 0
    assert pc.expires_at > pc.created_at
    assert len(pc.confirmation_id) == 8


def test_pending_confirmation_custom_id():
    pc = PendingConfirmation(
        tool="t", parameters={}, risk_level="LOW", reason="r", confirmation_id="custom1"
    )
    assert pc.confirmation_id == "custom1"


# ── ToolResult confirmation_id Tests ──


def test_toolresult_confirmation_id_default():
    result = ToolResult.ok("Done.")
    assert result.confirmation_id is None


def test_toolresult_confirmation_id_ok():
    result = ToolResult.ok("Done.", confirmation_id="abc123")
    assert result.confirmation_id == "abc123"


def test_toolresult_confirmation_id_fail():
    result = ToolResult.fail("Failed.", error_type="ConfirmationRequired", confirmation_id="abc123")
    assert result.confirmation_id == "abc123"
    assert result.error_type == "ConfirmationRequired"


def test_toolresult_to_dict_includes_confirmation_id():
    result = ToolResult.fail(
        "Need confirm", error_type="ConfirmationRequired", confirmation_id="abc123"
    )
    d = result.to_dict()
    assert d["confirmation_id"] == "abc123"


def test_toolresult_to_dict_confirmation_id_none():
    result = ToolResult.ok("Done.")
    d = result.to_dict()
    assert d["confirmation_id"] is None


# ── tool_result_to_response_payload Tests ──


def test_payload_includes_confirmation_id():
    result = ToolResult.fail(
        "Need confirm", error_type="ConfirmationRequired", confirmation_id="abc123"
    )
    payload = tool_result_to_response_payload(result)
    assert payload["confirmation_id"] == "abc123"


def test_payload_confirmation_id_none():
    result = ToolResult.ok("Done.")
    payload = tool_result_to_response_payload(result)
    assert payload["confirmation_id"] is None


def test_payload_no_suggested_question_when_no_confirmation_id():
    result = ToolResult.ok("Done.")
    payload = tool_result_to_response_payload(result)
    assert "suggested_confirmation_question" not in payload


def test_payload_suggested_question_for_confirmation_required():
    result = ToolResult.fail(
        "Need confirm",
        error_type="ConfirmationRequired",
        confirmation_id="abc123",
        tool_name="send_message",
        action_name="send_message",
    )
    payload = tool_result_to_response_payload(result)
    assert "suggested_confirmation_question" in payload
    assert "do you want me to send a message" in payload["suggested_confirmation_question"]


def test_payload_suggested_question_uses_action_name():
    result = ToolResult.fail(
        "Need confirm",
        error_type="ConfirmationRequired",
        confirmation_id="abc123",
        action_name="send a message to John",
    )
    payload = tool_result_to_response_payload(result)
    assert "send a message to John" in payload["suggested_confirmation_question"]


# ── _build_suggested_confirmation_question Tests ──


def test_build_suggested_question_with_confirmation_required():
    result = ToolResult.fail("test", error_type="ConfirmationRequired", action_name="send_message")
    q = _build_suggested_confirmation_question(result)
    assert "do you want to" in q.lower()


def test_build_suggested_question_with_success():
    result = ToolResult.ok("Done.")
    q = _build_suggested_confirmation_question(result)
    assert q == ""


def test_build_suggested_question_with_blocked():
    result = ToolResult.fail("Blocked", error_type="BlockedAction")
    q = _build_suggested_confirmation_question(result)
    assert q == ""


# ── Simulated Full Confirmation Flow Tests ──


def test_simulated_first_call_stores_pending():
    store = PendingConfirmationStore()
    safety = classify_action("send_message", {"receiver": "John"})
    assert safety["requires_confirmation"] is True
    cid = store.store("send_message", {"receiver": "John"}, safety["risk_level"], safety["reason"])
    assert store.is_pending() is True
    assert len(cid) == 8


def test_simulated_confirm_with_valid_id():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    pending = store.confirm(cid)
    assert pending is not None
    assert pending.tool == "send_message"
    assert store.is_pending() is False


def test_simulated_confirm_with_wrong_id():
    store = PendingConfirmationStore()
    store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    assert store.confirm("badbadbad") is None
    assert store.is_pending() is True


def test_simulated_confirm_with_expired_id():
    store = PendingConfirmationStore(ttl=0)
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    time.sleep(0.01)
    assert store.confirm(cid) is None
    assert store.is_pending() is False


def test_simulated_confirmed_true_no_confirmation_id():
    safety = classify_action("send_message", {"receiver": "John", "confirmed": True})
    assert safety["requires_confirmation"] is True
    # The parameters dict includes confirmed=True but no confirmation_id
    confirmation_id = safety.get("confirmation_id", "")
    assert confirmation_id == ""


def test_simulated_param_mismatch():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John", "message": "Hello"}, "HIGH", "test")
    stored_params = {
        k: v
        for k, v in store.get(cid).parameters.items()
        if k not in ("confirmed", "confirmation_id", "auto_confirm")
    }
    call_params = {"receiver": "Jane", "message": "Hello"}
    assert stored_params != call_params


def test_simulated_param_match():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John", "message": "Hello"}, "HIGH", "test")
    stored_params = {
        k: v
        for k, v in store.get(cid).parameters.items()
        if k not in ("confirmed", "confirmation_id", "auto_confirm")
    }
    call_params = {"receiver": "John", "message": "Hello"}
    assert stored_params == call_params


def test_simulated_no_pending_confirmation():
    store = PendingConfirmationStore()
    assert store.is_pending() is False
    assert store.confirm("nonexistent") is None


def test_simulated_cancel_pending():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    assert store.is_pending() is True
    assert store.cancel(cid) is True
    assert store.is_pending() is False


def test_simulated_cancel_does_not_execute():
    store = PendingConfirmationStore()
    cid = store.store("shutdown", {}, "HIGH", "test")
    store.cancel(cid)
    assert store.is_pending() is False
    # After cancel, confirm returns None
    assert store.confirm(cid) is None


def test_simulated_cancel_pending_confirmation_tool():
    store = PendingConfirmationStore()
    store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    assert store.is_pending() is True
    store.clear()
    assert store.is_pending() is False


def test_simulated_confirm_different_tool():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    pending = store.get(cid)
    assert pending is not None
    assert pending.tool != "shutdown"  # Tool mismatch


def test_simulated_confirm_tool_match():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    pending = store.get(cid)
    assert pending is not None
    assert pending.tool == "send_message"  # Tool matches


# ── Safety Guard Integration Tests ──


def test_safety_guard_send_message_is_high():
    safety = classify_action("send_message", {"receiver": "John"})
    assert safety["risk_level"] == "HIGH"
    assert safety["requires_confirmation"] is True


def test_safety_guard_cancel_pending_is_low():
    safety = classify_action("cancel_pending_confirmation", {})
    assert safety["risk_level"] == "LOW"
    assert safety["requires_confirmation"] is False


def test_safety_guard_shutdown_era_is_low():
    safety = classify_action("shutdown_era", {})
    assert safety["risk_level"] == "LOW"
    assert safety["requires_confirmation"] is False


# ── Honest cancel_pending_confirmation Tests ──


def test_cancel_active_pending_clears():
    store = PendingConfirmationStore()
    store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    assert store.is_pending() is True
    store.clear()
    assert store.is_pending() is False


def test_cancel_no_pending_noop():
    store = PendingConfirmationStore()
    assert store.is_pending() is False
    # clear on empty store should not error
    store.clear()


def test_confirm_consumes_pending_then_cancel_finds_none():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    assert store.is_pending() is True
    store.confirm(cid)
    assert store.is_pending() is False
    # After confirm, cancel finds nothing
    assert store.is_pending() is False


def test_cancel_after_confirm_no_pending():
    store = PendingConfirmationStore()
    cid = store.store("send_message", {"receiver": "John"}, "HIGH", "test")
    store.confirm(cid)
    store.clear()
    # Cancel after confirm doesn't re-clear a consumed pending
    assert store.is_pending() is False


# ── desktop_control: every path into the LLM planner is confirmed ──
# The gate used to fire only on `action == "task"`, while the dispatcher also
# planned a bare `task` parameter and any unrecognised action string. Those two
# paths reached the planner — which moves and copies real files — at default LOW.


def _desktop(params):
    return classify_action("desktop_control", params)


def test_desktop_direct_actions_are_not_gated_as_planner_calls():
    from actions.desktop_ops import DIRECT_ACTIONS

    for act in sorted(DIRECT_ACTIONS):
        verdict = _desktop({"action": act})
        # The point is that a directly-handled action is never mistaken for a
        # planner call. `clean`/`organize` DO confirm, but for their own reason
        # (they move every loose file), not because anything was generated.
        assert "generated plan" not in verdict["reason"], act
        if act not in ("clean", "organize"):
            assert verdict["requires_confirmation"] is False, act


def test_desktop_explicit_task_action_still_confirms():
    verdict = _desktop({"action": "task", "task": "tidy my desktop"})
    assert verdict["risk_level"] == "HIGH"
    assert verdict["requires_confirmation"] is True


def test_desktop_bare_task_parameter_confirms():
    # No action at all — used to fall straight through to the default LOW branch.
    verdict = _desktop({"task": "move everything from Downloads into Documents"})
    assert verdict["risk_level"] == "HIGH"
    assert verdict["requires_confirmation"] is True


def test_desktop_bare_description_parameter_confirms():
    verdict = _desktop({"description": "copy my photos somewhere else"})
    assert verdict["requires_confirmation"] is True


def test_desktop_unrecognised_action_string_confirms():
    # The dispatcher's `else` branch plans this verbatim.
    verdict = _desktop({"action": "move my downloads into documents"})
    assert verdict["risk_level"] == "HIGH"
    assert verdict["requires_confirmation"] is True


def test_desktop_empty_call_is_not_gated():
    # Dispatches to "No action or task specified." — nothing runs, so no prompt.
    assert _desktop({})["requires_confirmation"] is False
    assert _desktop({"action": "", "task": ""})["requires_confirmation"] is False


def test_desktop_direct_actions_match_the_dispatch_chain():
    """DIRECT_ACTIONS is what safety_guard trusts to be planner-free, so it must
    stay equal to the set of actions `desktop_control` answers with a handler."""
    import inspect
    import re

    from actions.desktop import desktop_control
    from actions.desktop_ops import DIRECT_ACTIONS

    source = inspect.getsource(desktop_control)
    handled = set(re.findall(r'action == "([a-z_]+)"', source))
    handled.discard("task")  # "task" is the planner, not a direct action
    assert handled, "dispatch chain not found — this test has gone blind"
    assert handled == set(DIRECT_ACTIONS), (
        f"drifted: chain-only={handled - set(DIRECT_ACTIONS)}, "
        f"list-only={set(DIRECT_ACTIONS) - handled}"
    )


# ── regression: a window verb carrying a task is still a planner call ──
# Found in security review. `classify_action`'s window branch matches on `action`
# alone and returns LOW, but the dispatcher plans whenever `task` is non-empty —
# so {"action": "minimize", "task": "<anything>"} ran a generated plan silently.
# The gate now runs before that branch, and the dispatcher no longer plans a
# bare window verb at all.

_NL = chr(10)
_ATTACK = "move every file from Downloads into Documents"


def test_window_verb_carrying_a_task_is_gated():
    from actions.desktop_ops import WINDOW_ACTIONS

    for act in sorted(WINDOW_ACTIONS):
        verdict = _desktop({"action": act, "task": _ATTACK})
        assert verdict["requires_confirmation"] is True, act
        assert verdict["risk_level"] == "HIGH", act


def test_bare_window_verb_is_not_planned_and_not_gated():
    # No task: the dispatcher redirects to window_control, nothing is planned,
    # so the routine-window-control classification must survive (no new friction).
    for act in ("minimize", "focus"):
        assert _desktop({"action": act})["requires_confirmation"] is False, act


def test_bare_window_verb_dispatches_without_planning(monkeypatch):
    import actions.desktop as d
    import actions.desktop_preview as dp

    monkeypatch.setattr(
        d, "_run_desktop_task", lambda *a, **k: pytest.fail("window verb must not be planned")
    )
    assert "window action" in d.desktop_control({"action": "minimize"})


def test_close_waiver_does_not_authorise_a_file_plan():
    # The user waived closing an app, not running a generated plan over their files.
    import core.user_prefs as up

    up.set_risk_confirmation_waiver("close", True)
    try:
        verdict = classify_action(
            "desktop_control",
            {"action": "close", "name": "x", "task": _ATTACK},
            honor_risk_waivers=True,
        )
        assert verdict["requires_confirmation"] is True
        # ...while a plain waived close still skips the prompt.
        plain = classify_action(
            "desktop_control", {"action": "close", "name": "x"}, honor_risk_waivers=True
        )
        assert plain["requires_confirmation"] is False
    finally:
        up.set_risk_confirmation_waiver("close", False)


def test_classify_action_survives_non_string_params():
    for params in ({"action": 5}, {"task": {"a": 1}}, {"action": None, "task": 7}):
        assert classify_action("desktop_control", params)["requires_confirmation"] is True


# ── plan-first confirmation ──
# The user used to approve a task STRING, and only then did the model choose
# which files to move — so the approval carried no information about the real
# effects. The plan is now resolved before the question is asked, described in
# the question, and executed verbatim from the store on approval.


def _desktop_sandbox(tmp_path, monkeypatch):
    """Point every safe root at a throwaway folder holding two .txt files."""
    import actions.desktop_ops as ops

    desk = tmp_path / "Desktop"
    desk.mkdir()
    for n in ("a.txt", "b.txt"):
        (desk / n).write_text("x")
    monkeypatch.setattr(ops, "_NAMED_ROOTS", {k: (lambda d=desk: d) for k in ops._NAMED_ROOTS})
    monkeypatch.setattr(ops, "get_desktop", lambda: desk)
    monkeypatch.setattr(ops, "_denied_roots", lambda: [])
    return desk


_MOVE_PLAN = [
    {"op": "move_files", "args": {"source": "desktop", "destination": "Text", "pattern": "*.txt"}}
]


def test_preview_resolves_the_plan_and_describes_it(tmp_path, monkeypatch):
    import actions.desktop as d
    import actions.desktop_preview as dp
    from core.confirmation_preview import build_confirmation_preview

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    text, carried = build_confirmation_preview(
        "desktop_control", {"action": "task", "task": "tidy up"}
    )
    assert "MOVE" in text and "*.txt" in text  # names the real effect
    assert carried["_approved_plan"] == _MOVE_PLAN


def test_question_shows_the_steps_not_the_generic_fallback(tmp_path, monkeypatch):
    import actions.desktop as d
    import actions.desktop_preview as dp
    import agent.executor as ex

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    spoken = []
    result = ex._call_tool_result(
        "desktop_control", {"action": "task", "task": "tidy up"}, spoken.append
    )
    assert result.error_type == "ConfirmationRequired"
    # The steps reach the user out-of-band (finding 5), not as a question the
    # model is free to reword — so assert on what the user is actually told.
    assert "MOVE" in spoken[0]
    question = _build_suggested_confirmation_question(result)
    assert "do you want to task" not in question  # the old uninformative prompt
    assert "Do not repeat" in question


def test_nothing_runs_before_approval(tmp_path, monkeypatch):
    import actions.desktop as d
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, None)
    assert sorted(p.name for p in desk.iterdir()) == ["a.txt", "b.txt"]


def test_approval_runs_the_stored_plan_without_replanning(tmp_path, monkeypatch):
    import actions.desktop as d
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)
    pending = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, None)

    # Re-planning on approval would run something the user never saw.
    monkeypatch.setattr(
        dp, "_plan_desktop_task", lambda task: pytest.fail("re-planned after approval")
    )
    result = ex._call_tool_result(
        "desktop_control",
        {
            "action": "task",
            "task": "tidy up",
            "confirmed": True,
            "confirmation_id": pending.confirmation_id,
        },
        None,
    )
    assert result.success
    assert sorted(p.name for p in (desk / "Text").iterdir()) == ["a.txt", "b.txt"]


def test_model_cannot_swap_the_plan_it_echoes_back(tmp_path, monkeypatch):
    """The approved plan is re-read from the store, so a model that echoes a
    DIFFERENT plan on the confirm call cannot substitute its own."""
    import actions.desktop as d
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)
    pending = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, None)

    swapped = [{"op": "copy_files", "args": {"source": "desktop", "destination": "Exfil"}}]
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: pytest.fail("re-planned"))
    result = ex._call_tool_result(
        "desktop_control",
        {
            "action": "task",
            "task": "tidy up",
            "confirmed": True,
            "confirmation_id": pending.confirmation_id,
            "_approved_plan": swapped,
        },
        None,
    )
    assert result.success
    assert not (desk / "Exfil").exists(), "the model's substituted plan ran"
    assert sorted(p.name for p in (desk / "Text").iterdir()) == ["a.txt", "b.txt"]


def test_caller_supplied_approved_plan_is_stripped():
    from core.confirmation_preview import strip_carried

    assert strip_carried({"action": "task", "_approved_plan": [{"op": "open_path"}]}) == {
        "action": "task"
    }


def test_a_failing_preview_never_skips_the_confirmation(monkeypatch):
    import core.confirmation_preview as cp

    monkeypatch.setitem(
        cp._BUILDERS, "desktop_control", lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    text, carried = cp.build_confirmation_preview("desktop_control", {"action": "task"})
    assert text == "" and carried == {}
    # ...the call is still classified as needing confirmation...
    assert _desktop({"action": "task", "task": "x"})["requires_confirmation"] is True
    # ...and, the part this test used to stop short of, approving it cannot run
    # anything: with no carried plan, execution refuses instead of planning.
    import actions.desktop as d
    import actions.desktop_preview as dp

    assert "check that with you" in d.desktop_control({"action": "task", "task": "x"})


def test_unplannable_task_is_approved_as_a_refusal_not_a_fresh_plan(tmp_path, monkeypatch):
    import actions.desktop as d
    import actions.desktop_preview as dp
    from core.confirmation_preview import build_confirmation_preview

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: [])
    text, carried = build_confirmation_preview(
        "desktop_control", {"action": "task", "task": "email my boss"}
    )
    assert carried["_approved_plan"] == []
    assert "can't" in text.lower()
    # Confirming that yields the refusal, never a newly generated plan.
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: pytest.fail("re-planned"))
    assert "can't do that" in d.desktop_control({"action": "task", "task": "x", **carried})


# ── second security review: every way a plan could run unseen ──
# All four were reproduced against the first version of this fix. The common
# root cause was that a MISSING _approved_plan meant "plan it now", which is
# fail-open; execution now never plans.


def test_spoken_yes_path_runs_the_shown_plan(tmp_path, monkeypatch):
    """confirm_pending_confirmation is the route a spoken "yes" takes. It
    re-dispatched with the carried plan stripped, so approval re-planned —
    the original bug, still live, on the path users actually use."""
    import actions.desktop as d
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda t: _MOVE_PLAN)
    pending = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, None)

    evil = [{"op": "copy_files", "args": {"source": "desktop", "destination": "Exfil"}}]
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda t: evil)
    ex._call_tool_result(
        "confirm_pending_confirmation", {"confirmation_id": pending.confirmation_id}, None
    )
    assert not (desk / "Exfil").exists(), "approval re-planned instead of running what was shown"
    assert sorted(p.name for p in (desk / "Text").iterdir()) == ["a.txt", "b.txt"]


def test_execution_without_an_approved_plan_refuses_and_never_plans(tmp_path, monkeypatch):
    """Every planner call is HIGH and unwaivable, so reaching execution with no
    carried plan means the confirmation layer was bypassed."""
    import actions.desktop as d
    import actions.desktop_preview as dp

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    evil = [{"op": "copy_files", "args": {"source": "desktop", "destination": "Exfil"}}]
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda t: evil)

    reply = d.desktop_control({"action": "task", "task": "tidy up"})
    assert "check that with you" in reply
    assert not (desk / "Exfil").exists()


def test_a_preview_that_raises_does_not_reopen_the_planner(tmp_path, monkeypatch):
    """A non-OpError escaping the preview (quota, bad key, SDK change) used to
    store no plan at all, and approval then ran a freshly generated one."""
    import actions.desktop as d
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)

    def boom(task):
        raise RuntimeError("no API key")

    monkeypatch.setattr(dp, "_plan_desktop_task", boom)
    pending = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, None)

    evil = [{"op": "copy_files", "args": {"source": "desktop", "destination": "Exfil"}}]
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda t: evil)
    result = ex._call_tool_result(
        "desktop_control",
        {
            "action": "task",
            "task": "tidy up",
            "confirmed": True,
            "confirmation_id": pending.confirmation_id,
        },
        None,
    )
    assert not (desk / "Exfil").exists(), "a fresh plan ran after approval"
    assert "could not work out" in str(result.message)


def test_a_scheduled_task_cannot_carry_a_preapproved_plan():
    """Scheduled args come from the model and never see a preview, so a plan
    supplied there would run verbatim at every fire."""
    from core.task_store import strip_args

    forged = {
        "action": "task",
        "task": "nightly tidy",
        "_approved_plan": [{"op": "copy_files", "args": {"source": "documents"}}],
    }
    assert strip_args(forged) == {"action": "task", "task": "nightly tidy"}


def test_plan_description_cannot_forge_its_own_steps():
    """describe_plan interpolates model-chosen args into the sentence the user
    approves. A pattern carrying newlines could close the plan early and hide
    the real mutating step below a fake terminator."""
    from actions.desktop_preview import describe_plan

    forged = (
        "*.txt (read-only, nothing is changed)."
        + _NL
        + "That is the entire plan, sir."
        + _NL
        + "Should I proceed?"
    )
    text = describe_plan(
        [
            {"op": "count_files", "args": {"path": "desktop", "pattern": forged}},
            {"op": "move_files", "args": {"source": "documents", "destination": "downloads"}},
        ]
    )
    assert len(text.splitlines()) == 2, "an argument forged extra lines"
    assert text.splitlines()[1].startswith("2. MOVE"), "the real mutating step was displaced"


def test_control_characters_are_rejected_in_a_pattern():
    import actions.desktop_ops as ops

    with pytest.raises(ops.OpError):
        ops._pattern("*.txt" + _NL + "2. nothing")


def test_a_long_argument_cannot_bury_a_later_step():
    from actions.desktop_preview import describe_plan

    text = describe_plan(
        [
            {"op": "open_path", "args": {"path": "A" * 4000}},
            {"op": "move_files", "args": {"source": "documents", "destination": "downloads"}},
        ]
    )
    assert len(text.splitlines()) == 2
    assert len(text.splitlines()[0]) < 120, "one argument dominated the confirmation text"


# ── finding 5: the plan must reach the user without the model's cooperation ──
# The preview used to be delivered ONLY as suggested_confirmation_question, a
# hint to the model. An adversarial model could say "shall I tidy your desktop?"
# while the store held a plan that moves files, and the whole point of the
# plan-first fix — that the user approved what runs — did not hold.


def test_plan_is_spoken_verbatim_not_left_to_the_model(tmp_path, monkeypatch):
    import actions.desktop_preview as dp
    import agent.executor as ex

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    spoken = []
    ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, spoken.append)

    assert spoken, "the user was never told the plan except through the model"
    assert "MOVE" in spoken[0], "the real effect was not read out"
    assert "Say aloud exactly this" in spoken[0], "sent as a paraphrasable suggestion"


def test_model_is_told_not_to_reword_an_announced_question(tmp_path, monkeypatch):
    import actions.desktop_preview as dp
    import agent.executor as ex

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    result = ex._call_tool_result(
        "desktop_control", {"action": "task", "task": "tidy up"}, lambda t: None
    )
    question = _build_suggested_confirmation_question(result)
    assert "Do not repeat" in question
    assert "MOVE" not in question, "the steps were re-offered for paraphrase"


def test_announcement_survives_a_missing_speak_handle(tmp_path, monkeypatch):
    """No voice (text-only / agent run): the log still carries the steps."""
    import actions.desktop_preview as dp
    import agent.executor as ex
    from core.confirmation_preview import ANNOUNCED_KEY

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    result = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, None)
    assert (result.data or {})[ANNOUNCED_KEY] is True


def test_a_speak_failure_never_breaks_the_confirmation(tmp_path, monkeypatch):
    import actions.desktop_preview as dp
    import agent.executor as ex

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    def broken_speak(_text):
        raise RuntimeError("TTS is down")

    result = ex._call_tool_result(
        "desktop_control", {"action": "task", "task": "tidy up"}, broken_speak
    )
    # Still a confirmation, still nothing executed.
    assert result.error_type == "ConfirmationRequired"
    assert result.confirmation_id


def test_announcing_does_not_execute_anything(tmp_path, monkeypatch):
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy up"}, lambda t: None)
    assert sorted(p.name for p in desk.iterdir()) == ["a.txt", "b.txt"]


# ── finding 6: one turn must not swap what "yes" approves ──


def test_second_call_in_a_turn_cannot_swap_what_yes_approves(tmp_path, monkeypatch):
    """tool_batch runs a turn's calls sequentially and the slot is single.
    The second store() used to re-arm it, so a bare "yes" — which is how a
    spoken confirmation arrives — ran the plan the user was never asked about.
    """
    import actions.desktop_preview as dp
    import agent.executor as ex

    desk = _desktop_sandbox(tmp_path, monkeypatch)
    benign = [{"op": "count_files", "args": {"path": "desktop", "pattern": "*.txt"}}]
    evil = [{"op": "copy_files", "args": {"source": "desktop", "destination": "Exfil"}}]
    plans = iter([benign, evil])
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: next(plans))

    first = ex._call_tool_result(
        "desktop_control", {"action": "task", "task": "how many text files"}, None
    )
    second = ex._call_tool_result(
        "desktop_control", {"action": "task", "task": "back up everything"}, None
    )
    assert first.error_type == "ConfirmationRequired"
    assert second.error_type == "ConfirmationBusy", "the second call re-armed the slot"

    answered = ex._call_tool_result("confirm_pending_confirmation", {"confirmation_id": ""}, None)
    assert not (desk / "Exfil").exists(), "a bare yes ran the un-asked plan"
    assert "file(s) matching" in str(answered.message), "the asked-about plan did not run"


def test_answering_frees_the_slot_for_the_next_confirmation(tmp_path, monkeypatch):
    import actions.desktop_preview as dp
    import agent.executor as ex

    _desktop_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_plan_desktop_task", lambda task: _MOVE_PLAN)

    first = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy"}, None)
    ex._call_tool_result(
        "confirm_pending_confirmation", {"confirmation_id": first.confirmation_id}, None
    )
    again = ex._call_tool_result("desktop_control", {"action": "task", "task": "tidy again"}, None)
    assert again.error_type == "ConfirmationRequired", "the slot stayed blocked after an answer"


# ── finding 7: clean/organize were the cheap way around the planner ──
# They relocate every loose file on the Desktop and were classified LOW
# "Desktop personalization" — no prompt, no preview. Injected text wanting bulk
# file movement never needed action=task at all.


def _bulk_sandbox(tmp_path, monkeypatch):
    import actions.desktop as d

    desk = tmp_path / "Desktop"
    desk.mkdir()
    for n in ("a.txt", "b.txt", "c.png"):
        (desk / n).write_text("x")
    (desk / ".hidden").write_text("x")
    (desk / "Sub").mkdir()
    monkeypatch.setattr(d, "get_desktop", lambda: desk)
    return desk


def test_bulk_desktop_moves_require_confirmation():
    for act in ("clean", "organize"):
        verdict = _desktop({"action": act})
        assert verdict["requires_confirmation"] is True, act
        assert verdict["risk_level"] == "MEDIUM", act


def test_readonly_desktop_actions_gain_no_new_friction():
    for act in ("wallpaper", "list", "stats", "current_wallpaper"):
        assert _desktop({"action": act})["requires_confirmation"] is False, act


def test_bulk_preview_states_the_real_count_and_destination(tmp_path, monkeypatch):
    from core.confirmation_preview import build_confirmation_preview

    _bulk_sandbox(tmp_path, monkeypatch)
    text, carried = build_confirmation_preview("desktop_control", {"action": "clean"})
    assert "3 files" in text, "the count must match the movable files"
    assert "Desktop Archive" in text
    assert carried == {}, "a direct action carries no plan"


def test_bulk_preview_does_not_touch_the_disk(tmp_path, monkeypatch):
    """clean_desktop() mkdirs its archive folder — the preview must not."""
    from core.confirmation_preview import build_confirmation_preview

    desk = _bulk_sandbox(tmp_path, monkeypatch)
    before = sorted(p.name for p in desk.iterdir())
    build_confirmation_preview("desktop_control", {"action": "clean"})
    build_confirmation_preview("desktop_control", {"action": "organize"})
    assert sorted(p.name for p in desk.iterdir()) == before


def test_preview_count_matches_what_the_handler_moves(tmp_path, monkeypatch):
    """The preview and the handler share one selection rule, so the number the
    user approves is the number that moves."""
    import actions.desktop as d

    _bulk_sandbox(tmp_path, monkeypatch)
    predicted = len(d.movable_desktop_items())
    assert f"{predicted} files archived" in d.clean_desktop()


def test_bulk_preview_is_honest_about_an_empty_desktop(tmp_path, monkeypatch):
    import actions.desktop as d
    from core.confirmation_preview import build_confirmation_preview

    desk = tmp_path / "Desktop"
    desk.mkdir()
    monkeypatch.setattr(d, "get_desktop", lambda: desk)
    text, _ = build_confirmation_preview("desktop_control", {"action": "clean"})
    assert "nothing loose" in text
