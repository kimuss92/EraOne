"""core/task_catchup.py — make up a recurring run Era was not running for.

APScheduler only knows about fire times from launch onward, so a weekday-9:00
routine silently vanished whenever Era started at 10:59 (2026-09-18). A recurring
cron task may carry `catch_up_until` ("HH:MM"): if today's first scheduled run
passed while Era was off, start() runs it ONCE shortly after launch — as long as
it is still before that time — and otherwise records the day as "missed" so the
user hears about it instead of nothing.

Invariants:
  * At most one catch-up (or one "missed" record) per scheduled day: any history
    entry at or after today's first fire time counts as "already handled".
  * Opt-in per task. Without `catch_up_until` nothing here changes behaviour.
  * Cron only: an interval task fires again soon anyway, and a date task has its
    own grace handling in task_scheduler.start().
"""

from __future__ import annotations

import re
from datetime import datetime, time

from core.task_store import TaskError, build_trigger

_HHMM = re.compile(r"^\s*(\d{1,2})(?:\s*[:h]\s*(\d{2}))?\s*$", re.IGNORECASE)


def normalize_until(value) -> str:
    """'11', '11:00', '11h', '9h30' -> 'HH:MM'. '' / 'none' / 'off' clears it."""
    text = str(value or "").strip().lower()
    if text in ("", "none", "off", "no", "false"):
        return ""
    m = _HHMM.match(text.rstrip("h"))
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise TaskError("catch_up_until must be a time of day like 11:00.")
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def first_fire_today(schedule: dict, now: datetime) -> datetime | None:
    """Today's first scheduled fire time (naive local), or None if there is none."""
    if (schedule or {}).get("type") != "cron":
        return None
    trigger = build_trigger(schedule)
    midnight = datetime.combine(now.date(), time()).astimezone(trigger.timezone)
    nxt = trigger.get_next_fire_time(None, midnight)
    if nxt is None:
        return None
    first = nxt.astimezone().replace(tzinfo=None)
    return first if first.date() == now.date() else None


def startup_decision(task: dict, now: datetime) -> str | None:
    """'catch_up' | 'missed' | None for one task at Era launch."""
    until = task.get("catch_up_until")
    if not until or not task.get("enabled") or task.get("finished") or task.get("pending"):
        return None
    try:
        first = first_fire_today(task.get("schedule") or {}, now)
    except Exception:
        return None
    if first is None or first > now:
        return None  # nothing due yet today: the normal job will fire
    for entry in task.get("history") or []:
        try:
            if datetime.fromisoformat(entry["at"]) >= first:
                return None  # today's run already happened (or was already reported)
        except (KeyError, TypeError, ValueError):
            continue
    hh, mm = (int(x) for x in until.split(":"))
    deadline = datetime.combine(now.date(), time(hh, mm))
    return "catch_up" if now <= deadline else "missed"
