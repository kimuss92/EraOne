"""Authenticated HUD controls over the existing scheduler, retaining its gates."""

import hashlib
import json

from core import task_scheduler as scheduler


def version(task: dict) -> str:
    # The approval preview is bound to action, schedule AND this pending run.
    fields = {
        k: task.get(k)
        for k in (
            "id",
            "name",
            "kind",
            "tool",
            "args",
            "steps",
            "message",
            "schedule",
            "enabled",
            "pending",
            "condition",
            "notify",
            "on_error",
            "catch_up_until",
        )
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def preview(task: dict) -> str:
    from core.logger import redact_secrets

    fields = {
        key: task.get(key)
        for key in ("kind", "tool", "args", "steps", "message", "condition", "on_error")
    }
    return redact_secrets(json.dumps(fields, indent=2, ensure_ascii=False, default=str))


def control(body: dict) -> dict:
    if set(body) - {"id", "action", "version", "when", "confirmed", "name", "message"}:
        raise ValueError("Unsupported task property")
    action = body.get("action")
    if action == "create_reminder":
        name, message, when = (body.get(k) for k in ("name", "message", "when"))
        if not all(isinstance(s, str) and 0 < len(s) <= 500 for s in (name, message, when)):
            raise ValueError("Reminder requires name, message, and schedule")
        task = scheduler.create_task(name, when, kind="reminder", message=message)
        return {"ok": True, "id": task["id"], "message": "Reminder created"}
    if action not in {"pause", "resume", "cancel", "reschedule", "approve", "skip", "run_now"}:
        raise ValueError("Unsupported scheduler action")
    task_id = body.get("id")
    if not isinstance(task_id, str):
        raise ValueError("Task id required")
    task = scheduler.get_task(task_id)
    if task["id"] != task_id or body.get("version") != version(task):
        raise ValueError("Task changed; refresh its preview")
    if action in {"approve", "cancel"} and body.get("confirmed") is not True:
        raise ValueError("Explicit approval required")
    if action == "approve":
        if len(preview(task)) > 12000:
            raise ValueError(
                "Task preview is too large for HUD approval; inspect it through Era first"
            )
        expected = body["version"]
        result = scheduler.approve(
            task_id, authorize=lambda current, focused: version(current) == expected
        )
    elif action == "skip":
        result = scheduler.skip(task_id)
    elif action == "cancel":
        result = scheduler.cancel_task(task_id)
    elif action == "run_now":
        result = scheduler.run_now(task_id)  # existing risk gate may park approval
    elif action == "reschedule":
        if not isinstance(body.get("when"), str) or not 0 < len(body["when"]) <= 200:
            raise ValueError("A schedule of at most 200 characters is required")
        result = scheduler.update_task(task_id, when=body["when"])
    else:
        result = scheduler.update_task(task_id, enabled=action == "resume")
    return {"ok": True, "message": f"Task {action} processed", "result": result}
