"""Authenticated HUD decisions bind to the exact displayed confirmation."""

from __future__ import annotations

import asyncio
import threading

from core.tool_result import ToolResult

_DECISION_LOCK = threading.Lock()


def submit_decision(engine, confirmation_id: str, created_at: float, decision: str) -> bool:
    store = getattr(engine, "_pending_store", None)
    if store is None or decision not in {"approve", "cancel"}:
        return False
    pending = store.peek(confirmation_id)
    if pending is None or pending.created_at != created_at:
        return False
    if decision == "cancel":
        cancelled = store.cancel(confirmation_id, expected_created_at=created_at)
        if cancelled:
            # Speak confirmation so user hears the cancel worked (mirrors typed-text path)
            try:
                engine.ui.write_log("SYS: Action cancelled by user.")
                speak = getattr(engine, "speak", None)
                if speak:
                    speak("Action cancelled.")
            except Exception:
                pass
        return cancelled
    loop = getattr(engine, "_loop", None)
    if loop is None or not loop.is_running():
        return False
    key = (confirmation_id, created_at)
    with _DECISION_LOCK:
        claims = getattr(engine, "_hud_approval_claims", None)
        if claims is None:
            claims = engine._hud_approval_claims = set()
        if key in claims:
            return True
        claims.add(key)

    async def execute():
        from app.hud.events import hud
        from core.telegram_approvals import execute_approved

        try:
            try:
                result = await execute_approved(engine, pending)
            except Exception:
                result = ToolResult.fail(
                    "HUD approval execution failed. See the task logs.",
                    error_type="ApprovalExecutionFailed",
                )
            report = getattr(engine, "_deliver_tool_outcome", None)
            if callable(report):
                report(pending.tool, result)
            if not result.success:
                hud.error(result.message or result.error or "Approval failed.", where="approval")
        finally:
            with _DECISION_LOCK:
                claims.discard(key)

    coroutine = execute()
    try:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception:
        coroutine.close()
        with _DECISION_LOCK:
            claims.discard(key)
        return False
    return True
