"""core/task_reports.py — how a scheduled-task run reaches the hive and the user.

Hive (core/hive.py) is the command repository Protocol One and Era brief from:
every run is a `task_run` message; a failure, block or miss is an open insight
until a later run succeeds; a run parked for approval is its own insight until
the user approves or skips it. Report wording lives here so the scheduler only
decides WHAT happened.
"""

from __future__ import annotations

from core import hive
from core.task_store import describe_action

AGENT = "scheduler"


def insight_signature(task_id: str) -> str:
    return f"scheduler:{task_id}"


def feed_hive(task: dict, entry: dict, safety: dict | None) -> None:
    sig, status = insight_signature(task["id"]), entry["status"]
    hive.send_message(AGENT, "*", "task_run", {"id": task["id"], "name": task["name"], **entry})
    if status in ("failed", "blocked", "missed", "partial"):
        hive.post_insight(
            AGENT,
            sig,
            severity="high" if status == "blocked" else "medium",
            category="scheduled_task",
            title=f"Scheduled task '{task['name']}' {status}",
            evidence=entry["message"],
            location=f"scheduled task {task['id']}",
        )
    elif status == "ok":
        # The re-check that justifies "resolved": the task actually ran fine.
        hive.resolve_insight(sig, by=AGENT, note="a later run succeeded")
    if status == "awaiting_confirmation":
        hive.post_insight(
            AGENT,
            sig + ":approval",
            severity="medium",
            category="needs_approval",
            title=f"Scheduled task '{task['name']}' is waiting for your approval",
            evidence=(safety or {}).get("reason", entry["message"]),
            location=f"scheduled task {task['id']}",
        )
    else:
        hive.resolve_insight(sig + ":approval", by=AGENT, note=f"run {status}")


def resolve_all(task: dict, note: str) -> None:
    sig = insight_signature(task["id"])
    for s in (sig, sig + ":approval"):
        hive.resolve_insight(s, by=AGENT, note=note)


def step_failure_text(task: dict, entry: dict, total: int) -> str | None:
    """A chain link that failed while the chain will CARRY ON. Said before the next
    link acts, so the user never learns of a broken step only after the rest ran on
    top of it. Suppressed only by notify='silent', like any other failure report."""
    if task.get("notify") == "silent":
        return None
    return (
        f"Scheduled task '{task['name']}' step {entry['n']} of {total} ({entry['tool']}) "
        f"{entry['status']}: {entry['message']} — continuing with the remaining steps."
    )


def report_text(task: dict, entry: dict) -> str | None:
    """What to tell the user about one run, or None when nothing should be said."""
    status, msg, name = entry["status"], entry["message"], task["name"]
    if entry["trigger"] in ("manual", "approved"):
        return None  # asked for in the live turn: the tool result already carries it back
    level = task.get("notify", "all")
    if status == "awaiting_confirmation":
        # Never suppressed, whatever `notify` says: a safety prompt must reach the user.
        return (
            f"Scheduled task '{name}' is due: {describe_action(task)}. It needs your approval "
            f"({msg}). Say 'yes' to run it, 'always' to run it and stop asking, or 'skip'."
        )
    if task["kind"] == "reminder" and status == "ok":
        return f"Reminder: {msg}"
    if status == "ok":
        return f"Scheduled task '{name}' finished: {msg}" if level == "all" else None
    if level == "silent":
        return None
    if status == "partial":
        # A chain that did part of its work is never announced as a plain failure:
        # the user needs to know what DID run before they retry it.
        return f"Scheduled task '{name}' only partly ran — {msg}"
    # skipped / failed / blocked / missed
    return f"Scheduled task '{name}' {status}: {msg}"


def status_detail(tasks: list[dict]) -> str:
    active = sum(1 for t in tasks if t.get("enabled") and not t.get("finished"))
    pending = sum(1 for t in tasks if t.get("pending"))
    failing = sum(
        1
        for t in tasks
        if (t.get("last_run") or {}).get("status") in ("failed", "blocked", "partial")
    )
    return f"{active} active task(s), {pending} awaiting approval, {failing} failing"


def briefing_line(tasks: list[dict]) -> str:
    if not tasks:
        return "No scheduled tasks."
    pending = [t["name"] for t in tasks if t.get("pending")]
    unread = [t["name"] for t in tasks if t.get("unread")]
    parts = [f"{len(tasks)} scheduled task(s)."]
    if pending:
        parts.append("Awaiting your approval: " + ", ".join(pending) + ".")
    if unread:
        parts.append("New results: " + ", ".join(unread) + ".")
    return " ".join(parts)
