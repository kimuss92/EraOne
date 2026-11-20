"""One bounded retry for explicit read-only operations, never mutations."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy

from core.cancel_registry import CURRENT_TOKEN, CancelledTask
from core.logger import log_event, main_logger
from core.tool_result import ToolResult

READ_ACTIONS = {
    "file_controller": frozenset(
        {"read", "list", "info", "disk_usage", "index_status", "list_favorites", "find", "largest"}
    ),
    "browser_control": frozenset(
        {"get_text", "get_url", "list_tabs", "page_snapshot", "list_browsers"}
    ),
    "computer_control": frozenset({"running_apps", "active_window", "system_snapshot"}),
}
RETRY_WINDOW_S = 5.0


def _check_cancelled():
    token = CURRENT_TOKEN.get()
    if token is not None and token.cancelled():
        raise CancelledTask("The user cancelled this task.")


def transient_failure(result):
    if result.success or result.requires_confirmation or result.confirmation_id:
        return False
    reason = str(result.error or result.message or "").casefold()
    if any(
        marker in reason
        for marker in ("permission", "denied", "unauthorized", "cancelled", "confirmation required")
    ):
        return False
    if result.error_type in {"TimeoutError", "ConnectionError", "TemporaryUnavailable"}:
        return True
    if result.error_type not in {"ActionError", "OperationalError"}:
        return False
    return any(
        marker in reason
        for marker in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "database is locked",
        )
    )


async def execute_read_recovery(tool, parameters, execute, *, clock=time.monotonic):
    from core import tool_health

    tool_health.snapshot()  # retire stale observations before accepting fresh work
    identity = tool_health.begin(tool)
    try:
        result = await _execute_read_recovery(tool, parameters, execute, clock=clock)
        outcome = ToolResult.from_any(result)
        tool_health.finish(tool, identity, outcome.success, outcome.error_type or "")
        return result
    except BaseException as exc:
        tool_health.finish(tool, identity, False, type(exc).__name__)
        raise


async def _execute_read_recovery(tool, parameters, execute, *, clock=time.monotonic):
    """Execute normally; retry only a transient, unapproved, read-only call.

    `execute` still enters the existing tool gate. No alternate tool, arguments,
    approvals or verification result are invented by this layer.
    """
    action = parameters.get("action")
    eligible = (
        isinstance(action, str)
        and action in READ_ACTIONS.get(tool, ())
        and not parameters.get("confirmation_id")
        and not parameters.get("confirmed")
    )
    if not eligible:
        return await execute(parameters)
    original = deepcopy(parameters)
    started = clock()
    _check_cancelled()
    try:
        raw = await execute(deepcopy(original))
        first = ToolResult.from_any(raw)
    except (TimeoutError, ConnectionError) as exc:
        first = ToolResult.fail(
            message="Read operation temporarily unavailable.",
            error=str(exc) or "Transient read timeout.",
            error_type=type(exc).__name__,
        )
        raw = first
        if not transient_failure(first) or clock() - started > RETRY_WINDOW_S:
            raise
    if not transient_failure(first) or clock() - started > RETRY_WINDOW_S:
        return raw

    token = CURRENT_TOKEN.get()
    details = {
        "attempts": 1,
        "strategy": "same_read_once",
        "initialErrorType": first.error_type,
        "cause": "transient_read_failure",
        "outcome": "pending",
    }
    log_event(
        main_logger,
        "tool.recovery.started",
        "WARNING",
        tool=tool,
        action=action,
        cid=token.cid if token else None,
        **details,
    )
    from core.task_trace import trace

    trace("recovery", tool=tool, action=action, cid=token.cid if token else None, **details)
    try:
        await asyncio.sleep(0.25)
        _check_cancelled()
        # The delay is inside the retry budget; never restart a long hung read.
        if clock() - started > RETRY_WINDOW_S:
            details["attempts"], details["outcome"] = 1, "budget_exhausted"
            result = first
        else:
            details["attempts"] = 2
            result = ToolResult.from_any(await execute(deepcopy(original)))
            details["outcome"] = "returned_success" if result.success else "failed"
        if isinstance(result.data, dict):
            result.data = {**result.data, "recovery": deepcopy(details)}
        return result
    except (CancelledTask, asyncio.CancelledError):
        details["outcome"] = "cancelled"
        raise
    except Exception:
        details["outcome"] = "exception"
        raise
    finally:
        log_event(
            main_logger,
            "tool.recovery.finished",
            "INFO",
            tool=tool,
            action=action,
            cid=token.cid if token else None,
            **details,
        )
        trace("recovery", tool=tool, action=action, cid=token.cid if token else None, **details)
