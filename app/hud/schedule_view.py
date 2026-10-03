"""Read-only view of the user's scheduled tasks for the HUD (GET /api/schedule).

Source of truth is core/task_scheduler + core/task_store — this module only
READS: it never creates, edits, approves or runs anything (those stay with Era,
by voice or typed command, behind their own gates). In particular it does not
call ``task_scheduler.pending_approvals()``, which expires stale approvals as a
side effect; it reads each task's ``pending`` field as stored.

The next run time comes from the live APScheduler job when the scheduler is
running (``live_next_run``); the stored ``next_run`` is only a fallback and is
flagged as such (``nextRunLive: false``), because it is refreshed only when a
run is recorded. Finished tasks are omitted. Degrade-never-raise: a broken
store yields ``{"ok": false}`` rather than an error page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_SUMMARY_MAX = 140
_TEXT_MAX = 120


def _clip(value: Any, limit: int) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _epoch(value: Any) -> float | None:
    """ISO timestamp or the store's 'YYYY-MM-DD HH:MM' (local) -> epoch seconds."""
    if not value:
        return None
    s = str(value)
    for parse in (datetime.fromisoformat, lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M")):
        try:
            return parse(s).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def _row(task: dict, running: bool, live_next_run, describe_action) -> dict:
    from app.hud.schedule_control import preview, version

    schedule = task.get("schedule") or {}
    enabled = bool(task.get("enabled"))
    live = live_next_run(task["id"]) if running and enabled else None
    if live is not None:
        next_run, next_live = live.timestamp(), True
    else:
        next_run, next_live = (_epoch(task.get("next_run")) if enabled else None), False
    pending = task.get("pending") or None
    last = task.get("last_run") or None
    try:
        summary = describe_action(task)
    except Exception:
        summary = ""
    return {
        "version": version(task),
        "preview": preview(task)[:12000],
        "previewComplete": len(preview(task)) <= 12000,
        "id": str(task.get("id", "")),
        "name": _clip(task.get("name"), 60),
        "kind": str(task.get("kind") or "tool"),
        "summary": _clip(summary, _SUMMARY_MAX),
        "schedule": _clip(schedule.get("description") or schedule.get("when"), _TEXT_MAX),
        "recurring": schedule.get("type") != "date",
        "enabled": enabled,
        "nextRun": next_run,
        "nextRunLive": next_live,
        "awaiting": (
            {
                "since": _epoch(pending.get("since")),
                "reason": _clip(pending.get("reason"), _TEXT_MAX),
            }
            if pending
            else None
        ),
        "lastRun": (
            {"at": _epoch(last.get("at")), "status": str(last.get("status") or "")}
            if last
            else None
        ),
    }


def snapshot() -> dict:
    try:
        from core import task_scheduler
        from core.task_store import describe_action

        running = task_scheduler.is_running()
        tasks = task_scheduler.list_tasks(include_finished=False)
        rows = [_row(t, running, task_scheduler.live_next_run, describe_action) for t in tasks]
        # Needs-approval first, then soonest; paused / unscheduled last.
        rows.sort(key=lambda r: (r["awaiting"] is None, r["nextRun"] is None, r["nextRun"] or 0))
        return {"ok": True, "running": running, "tasks": rows}
    except Exception as exc:
        return {"ok": False, "running": False, "tasks": [], "error": _clip(exc, _TEXT_MAX)}
