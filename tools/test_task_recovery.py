"""Read recovery cannot replay mutations, approvals, cancellation or long hangs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.cancel_registry import CURRENT_TOKEN, CancelledTask, CancelRegistry
from core.task_recovery import execute_read_recovery
from core.tool_result import ToolResult


@pytest.fixture(autouse=True)
def diagnostics(monkeypatch):
    events = []
    monkeypatch.setattr("core.task_recovery.log_event", lambda *a, **kw: events.append((a[1], kw)))
    monkeypatch.setattr("core.task_trace.trace", lambda *a, **kw: None)
    return events


def test_transient_read_retries_once_and_preserves_actual_content(diagnostics):
    action = AsyncMock(side_effect=[TimeoutError("fixture"), "Actual file content"])
    result = asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
    assert action.await_count == 2 and result.message == "Actual file content"
    assert result.success and result.verification_status == "NOT_VERIFIED"
    assert result.data["recovery"]["attempts"] == 2
    assert diagnostics[-1][1]["outcome"] == "returned_success"


def test_failed_retry_is_not_reported_as_done(diagnostics):
    action = AsyncMock(
        side_effect=[TimeoutError(), ToolResult.fail("Not found", error_type="ActionError")]
    )
    result = asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
    assert not result.success and result.message == "Not found" and action.await_count == 2
    assert result.data["recovery"]["outcome"] == "failed"


@pytest.mark.parametrize(
    "tool,action",
    [
        ("file_controller", "move"),
        ("file_controller", "write"),
        ("file_controller", "delete"),
        ("spotify_control", "play"),
        ("spotify_control", "next"),
        ("browser_control", "click"),
        ("browser_control", "go_to"),
        ("send_message", "send"),
        ("web_search", "search"),
    ],
)
def test_mutations_and_unknown_or_internally_retried_tools_never_replay(tool, action):
    execute = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        asyncio.run(execute_read_recovery(tool, {"action": action}, execute))
    assert execute.await_count == 1


@pytest.mark.parametrize("extra", [{"confirmed": True}, {"confirmation_id": "approved-id"}])
def test_approved_operation_is_never_replayed(extra):
    action = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        asyncio.run(execute_read_recovery("file_controller", {"action": "read", **extra}, action))
    assert action.await_count == 1


@pytest.mark.parametrize(
    "error_type", ["PermissionError", "ConfirmationRequired", "BlockedAction", "Cancelled"]
)
def test_nontransient_or_held_failures_are_preserved(error_type):
    expected = ToolResult.fail("Read failed", error_type=error_type)
    action = AsyncMock(return_value=expected)
    assert (
        asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
        is expected
    )
    assert action.await_count == 1


def test_success_content_mentioning_timeout_is_not_a_failure():
    action = AsyncMock(
        return_value="This guide discusses timeout settings and error handling. " * 8
    )
    result = asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
    assert isinstance(result, str) and action.await_count == 1


def test_retry_uses_original_parameters_even_if_first_action_mutates_its_copy():
    calls = []

    async def action(parameters):
        calls.append(dict(parameters))
        parameters["path"] = "wrong"
        if len(calls) == 1:
            raise TimeoutError()
        return "Actual content"

    original = {"action": "read", "path": "requested"}
    asyncio.run(execute_read_recovery("file_controller", original, action))
    assert calls == [original, original] and original["path"] == "requested"


def test_cancel_during_retry_delay_prevents_second_read(monkeypatch, diagnostics):
    token = CancelRegistry().register("read-cancel", "file_controller")
    action = AsyncMock(side_effect=TimeoutError())

    async def sleep(_):
        token.event.set()

    monkeypatch.setattr("core.task_recovery.asyncio.sleep", sleep)
    reset = CURRENT_TOKEN.set(token)
    try:
        with pytest.raises(CancelledTask):
            asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
    finally:
        CURRENT_TOKEN.reset(reset)
    assert action.await_count == 1 and diagnostics[-1][1]["outcome"] == "cancelled"


def test_long_read_timeout_is_not_restarted():
    clock = iter([0.0, 6.0])
    action = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        asyncio.run(
            execute_read_recovery(
                "file_controller", {"action": "read"}, action, clock=lambda: next(clock)
            )
        )
    assert action.await_count == 1


def test_delay_exhausting_retry_budget_preserves_initial_failure():
    clock = iter([0.0, 0.1, 0.2, 6.0])
    action = AsyncMock(side_effect=TimeoutError())
    result = asyncio.run(
        execute_read_recovery(
            "file_controller", {"action": "read"}, action, clock=lambda: next(clock)
        )
    )
    assert not result.success and action.await_count == 1
    assert result.data["recovery"]["outcome"] == "budget_exhausted"


def test_final_exception_is_logged_as_recovery_failure_and_propagated(diagnostics):
    action = AsyncMock(side_effect=[TimeoutError(), ValueError("fixture")])
    with pytest.raises(ValueError):
        asyncio.run(execute_read_recovery("browser_control", {"action": "get_url"}, action))
    assert action.await_count == 2 and diagnostics[-1][1]["outcome"] == "exception"


def test_dispatch_verification_still_rejects_retry_result(monkeypatch):
    from app.era_live import tool_dispatch as dispatch

    monkeypatch.setattr(
        dispatch,
        "classify_action",
        lambda *a, **kw: {"risk_level": "LOW", "requires_confirmation": False, "reason": "fixture"},
    )
    action = AsyncMock(side_effect=[TimeoutError(), "Raw operation returned"])
    era = SimpleNamespace(_execute_tool_raw=action, _log_caught_exception=lambda *a: None)

    def verify(result, **kw):
        result.success, result.verification_status = False, "FAILED"
        result.error_type = "VerificationFailed"
        return result

    monkeypatch.setattr(dispatch, "verify_tool_result", verify)
    fc = SimpleNamespace(name="file_controller", args={"action": "read"})
    result = asyncio.run(dispatch.ToolDispatchMixin._execute_tool_result_inner(era, fc))
    assert action.await_count == 2 and not result.success and result.verification_status == "FAILED"
    assert result.data["recovery"]["outcome"] == "verification_failed"


def test_permission_failure_with_timeout_word_is_not_retried():
    action = AsyncMock(
        return_value=ToolResult.fail("Permission denied after timeout", error_type="ActionError")
    )
    result = asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
    assert not result.success and action.await_count == 1


def test_non_dict_success_data_remains_compatible():
    expected = ToolResult.ok("Actual content")
    expected.data = ["original payload"]
    action = AsyncMock(side_effect=[TimeoutError(), expected])
    result = asyncio.run(execute_read_recovery("browser_control", {"action": "get_text"}, action))
    assert result.data == ["original payload"] and action.await_count == 2


def test_hud_reports_retry_without_replacing_failure_status():
    from app.hud.events import HudBus

    bus = HudBus()
    cid = bus.tool_started("recovery-test", "file_controller", {"action": "read"})
    result = ToolResult.fail(
        "Read failed",
        data={"recovery": {"strategy": "same_read_once", "attempts": 2, "outcome": "failed"}},
    )
    bus.tool_finished(cid, "file_controller", {"action": "read"}, result)
    row = bus._tools[cid]
    assert row["status"] == "failed" and "Read retry did not complete" in row["summary"]
    assert row["recovery"]["attempts"] == 2
    view = bus._public_run(row)
    view["recovery"]["attempts"] = 999
    assert row["recovery"]["attempts"] == 2


def test_raised_permission_timeout_is_not_retried():
    action = AsyncMock(side_effect=TimeoutError("permission denied after timeout"))
    with pytest.raises(TimeoutError):
        asyncio.run(execute_read_recovery("file_controller", {"action": "read"}, action))
    assert action.await_count == 1
