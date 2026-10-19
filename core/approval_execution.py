"""Per-call binding between a consumed approval and its verified result."""

from contextvars import ContextVar

from core.tool_result import ToolResult

APPROVED_CONFIRMATION: ContextVar[str | None] = ContextVar("approved_confirmation", default=None)
CURRENT_TASK_ID: ContextVar[str | None] = ContextVar("approval_task_id", default=None)
# Set only by the authenticated remote adapter, never by a tool argument/model.
# Contains the detached pending snapshot whose exact generation was approved.
REMOTE_APPROVAL: ContextVar[object | None] = ContextVar("remote_approval", default=None)


def remote_approves(pending) -> bool:
    grant = REMOTE_APPROVAL.get()
    return bool(
        grant is not None
        and pending is not None
        and grant.confirmation_id == pending.confirmation_id
        and grant.created_at == pending.created_at
        and grant.tool == pending.tool
        and grant.parameters == pending.parameters
    )


def publish_approved_result(confirmation_id: str, result: ToolResult) -> None:
    try:
        from agent.task_queue import get_queue

        get_queue().resolve_approval(confirmation_id, result)
    except Exception as exc:
        print(f"[TaskQueue] approval continuation failed: {type(exc).__name__}: {exc}")
        result.data = {**result.data, "task_continuation_error": str(exc)}
        result.message += " The background job could not resume; see the task logs."
