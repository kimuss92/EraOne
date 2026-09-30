"""core/task_scheduler.py — the user's own scheduled tasks ("run X every monday at 9").

era_scheduler.py runs the BRAND jobs defined in brands.yaml; this runs the tasks the
user creates by voice or text. They are deliberately separate schedulers:
era_scheduler.reload() calls remove_all_jobs(), which would silently erase every
user task on each brand-schedule edit.

A task is a reminder (a message), ONE Era tool call (`tool` + `args`), or a CHAIN
of tool calls run in order under a single rule (`kind="chain"` + `steps`, e.g. "every
weekday at 9, run my morning routine"). All three store a concrete action the model
resolved from the user's words at creation; storing that, not free text, is what
makes the risk classifiable up front AND again at run time with the same
core/safety_guard.classify_action the live session uses.

Invariants — do not undo:
  * The safety gate runs at FIRE time, not only at creation. A task whose action
    needs confirmation does not run: it parks in `pending` and asks the user,
    who approves or skips it. Nothing waits on a timer and proceeds by default.
  * A chain is ONE unit for that gate: it is classified at its worst step and
    parks if ANY step needs confirmation, so a harmless first link can never
    carry a risky later one past the approval. Each step is re-classified again
    just before it runs (core/task_chain.py), and a step that has BECOME blocked
    stops the chain. A failed link is reported before any later link acts.
  * A waiver is per task and bound to a fingerprint of the exact action, timing
    and notify level: any edit to those drops it, so "don't ask for the temp-folder
    cleanup" never carries over to a task later edited to delete Documents or to
    run every minute. BLOCKED (protected system paths) is never waivable.
  * A task may carry a CONDITION ("check every 5 min UNTIL the download is done").
    Its read-only check runs BEFORE the action's safety gate each scheduled fire:
    if the check is not met the action does not run (and never parks for approval);
    if it is met the action runs through the normal gate, and with on_met="stop"
    the task then retires. A poll that is not met writes no history and no report.
    The condition scopes the waiver like timing does — editing it re-asks.
  * Approving, waiving and authorising a Protocol launch are decided by the
    CALLER from the user's own recorded words (tool_dispatch._scheduled_task_authority
    — negation-aware, time-bound, tied to the task); approve() refuses without it.
    A Protocol grant records WHICH tools the user named (voice_authorized_tools)
    and is checked against the STEP in flight, so a chain's single authorization
    can never answer for a Protocol sitting beside the one that was named. Every
    action name is compared through task_store.norm_action — the dispatchers strip
    and lowercase, and a gate that does not will disagree with what actually runs.
  * A run marker is saved before a tool executes; after a crash the run is
    reported, never repeated.
  * Every run is recorded (history, unread flag), reported to the user, and fed
    into the hive (core/task_reports.py) so Protocol One and briefings see it.
  * Nothing here raises into APScheduler or the caller's thread except TaskError,
    which carries a user-facing message.

Layout: core/task_store.py (storage, lookup, safety helpers), core/task_chain.py
(running a chain's steps in order + its failure policy), core/task_reports.py
(hive feed + report wording), this file (lifecycle, running, APScheduler).
"""

from __future__ import annotations

import contextvars
import json
import secrets
import threading
import time
from datetime import datetime, timedelta

from core import hive, task_catchup, task_chain, task_reports
from core.schedule_parser import ScheduleParseError, parse_when
from core.task_reports import AGENT
from core.task_store import (  # noqa: F401  (re-exported: public API of this module)
    MAX_CHAIN_STEPS,
    NOTIFY_LEVELS,
    ON_ERROR_MODES,
    UNSCHEDULABLE_TOOLS,
    VOICE_GATED_ACTIONS,
    ApprovalRequired,
    TaskError,
    assess,
    assess_step,
    build_trigger,
    condition_met,
    describe_action,
    describe_condition,
    describe_step,
    fingerprint,
    granted_tools,
    make_waiver,
    norm_action,
    normalize_condition,
    normalize_steps,
    steps_of,
    strip_args,
    tasks_dir,
    validate_action,
    voice_authorized,
    voice_authorized_for,
    waiver_fingerprint,
    waiver_valid,
)
from core.task_store import find as _find
from core.task_store import load as _load
from core.task_store import now as _now
from core.task_store import save as _save

_HISTORY_MAX = 20
_APPROVAL_TTL = timedelta(hours=24)
_CATCHUP_GRACE = timedelta(hours=6)
_FINISHED_RETENTION = timedelta(days=30)

# Set by the live dispatcher while it executes a scheduled run, so the tool
# router can tell a scheduled call from the model acting on its own.
current_task: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "era_current_scheduled_task", default=None
)
# The ONE step of that task being executed right now ({"tool", "args"}). A chain
# holds several tool calls under one authorization, so "what did the user allow"
# must be answered against the step in flight, never against the chain as a whole.
current_step: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "era_current_scheduled_step", default=None
)

_lock = threading.RLock()
_scheduler = None
_executor = None  # (tool, args, task) -> {"success": bool, "message": str}
_reporter = None  # (text, urgent) -> None


def set_executor(fn) -> None:
    global _executor
    _executor = fn


def set_reporter(fn) -> None:
    global _reporter
    _reporter = fn


# ── queries ──────────────────────────────────────────────────────────────────
def get_task(ref: str) -> dict:
    with _lock:
        return _find(_load(), ref)


def list_tasks(include_finished: bool = False) -> list[dict]:
    with _lock:
        tasks = [t for t in _load().values() if include_finished or not t.get("finished")]
    return sorted(tasks, key=lambda t: (t.get("finished", False), t.get("next_run") or "~"))


def pending_approvals() -> list[dict]:
    with _lock:
        now = _now()
        for t in list(_load().values()):
            p = t.get("pending")
            if p and now - datetime.fromisoformat(p["since"]) > _APPROVAL_TTL:
                _record(
                    t["id"], "skipped", "The approval request expired after 24 hours unanswered."
                )
        return [t for t in _load().values() if t.get("pending")]


def results(ref: str | None = None, mark_read: bool = True) -> list[dict]:
    with _lock:
        data = _load()
        tasks = [_find(data, ref)] if ref else [t for t in data.values() if t.get("last_run")]
        out = [
            {"name": t["name"], "unread": t.get("unread"), "history": t.get("history", [])[-5:]}
            for t in tasks
        ]
        if mark_read:
            for t in tasks:
                t["unread"] = False
            _save(data)
    return out


def briefing_line() -> str:
    return task_reports.briefing_line(list_tasks())


def live_next_run(task_id: str):
    """The scheduler's OWN next fire time for a task (aware datetime), or None.

    Read-only, for the HUD: the stored ``next_run`` is only refreshed when a run is
    recorded, so a not-met condition poll can leave it behind. Includes a pending
    catch-up job, which may fire before the recurring one."""
    if not (_scheduler and _scheduler.running):
        return None
    times = []
    for job_id in (f"user:{task_id}", f"user:{task_id}:catchup"):
        job = _scheduler.get_job(job_id)
        nrt = getattr(job, "next_run_time", None) if job else None
        if nrt:
            times.append(nrt)
    return min(times) if times else None


def is_running() -> bool:
    return bool(_scheduler and _scheduler.running)


def _condition_key(task: dict) -> str:
    """A stable string for a task's condition, so an edit to it clears a parked
    approval and drops a waiver: a yes for "when the download finishes" is not a
    yes for the same action running unconditionally."""
    return json.dumps(task.get("condition"), sort_keys=True)


# ── create / modify / cancel ─────────────────────────────────────────────────
def _parse(when: str) -> dict:
    try:
        return {"when": when, **parse_when(when)}
    except ScheduleParseError as exc:
        raise TaskError(str(exc)) from exc


def create_task(
    name: str,
    when: str,
    *,
    kind: str = "tool",
    tool: str = "",
    args: dict | None = None,
    steps: list[dict] | None = None,
    message: str = "",
    notify: str = "all",
    on_error: str = "stop",
    catch_up_until: str = "",
    condition=None,
    waive: bool = False,
    voice_authorized=False,
) -> dict:
    """`voice_authorized`: True/False, or the list of Protocol tools the user named."""
    name = (name or "").strip()[:80]
    if not name:
        raise TaskError("Give the task a name.")
    args, kind = strip_args(args), (kind or "tool").strip().lower()
    steps = normalize_steps(steps)
    condition = normalize_condition(condition)
    if steps and kind != "reminder":
        kind = "chain"  # `steps` is what makes it a chain; the caller need not say so
    validate_action(kind, tool, args, message, voice_authorized, steps)
    if notify not in NOTIFY_LEVELS:
        raise TaskError(f"notify must be one of {', '.join(NOTIFY_LEVELS)}.")
    if on_error not in ON_ERROR_MODES:
        raise TaskError(f"on_error must be one of {', '.join(ON_ERROR_MODES)}.")
    schedule = _parse(when)
    catch_up_until = task_catchup.normalize_until(catch_up_until)
    if catch_up_until and schedule["type"] != "cron":
        raise TaskError(
            "catch_up_until only applies to a recurring time of day (e.g. 'every weekday at 9')."
        )

    with _lock:
        data = _load()
        if any(t["name"].lower() == name.lower() and not t.get("finished") for t in data.values()):
            raise TaskError(f"There is already a task named '{name}'.")
        task = {
            "id": secrets.token_hex(3),
            "name": name,
            "kind": kind,
            "tool": tool if kind == "tool" else "",
            "args": args if kind == "tool" else {},
            # A reminder's action fields are all empty: validate_action returns early
            # for one, so steps smuggled in beside a message are never checked.
            "steps": steps if kind != "reminder" else [],
            "message": message.strip(),
            "schedule": schedule,
            "notify": notify,
            "on_error": on_error,
            "catch_up_until": catch_up_until,
            "condition": condition,
            "condition_state": None,
            "enabled": True,
            "finished": False,
            "created": _now().isoformat(),
            "voice_authorization": None,
            "voice_authorized_tools": [],
            "waiver": None,
            "pending": None,
            "last_run": None,
            "history": [],
            "unread": False,
            "next_run": schedule.get("run_at", "").replace("T", " ")[:16] or None,
        }
        safety = assess(task)
        if safety["risk_level"] == "BLOCKED":
            raise TaskError(f"Refused: that action is blocked outright ({safety['reason']}).")
        if voice_authorized:
            # Recorded per tool: a chain's single grant must not cover a Protocol the
            # user never named (core/task_store.voice_authorized_for).
            task["voice_authorized_tools"] = granted_tools(task, voice_authorized)
            task["voice_authorization"] = fingerprint(task)
        if waive:
            task["waiver"] = make_waiver(task)
        data[task["id"]] = task
        _register(task)
        _save(data)
    hive.send_message(AGENT, "*", "task_created", {"id": task["id"], "name": name})
    _update_status()
    return {**task, "risk": safety}


def update_task(ref: str, **changes) -> dict:
    """changes: name, when, tool, args, steps, message, notify, on_error,
    catch_up_until, condition, enabled, voice_authorized."""
    with _lock:
        data = _load()
        task = _find(data, ref)
        before = fingerprint(task)
        before_policy = (
            task.get("on_error"),
            task.get("catch_up_until") or "",
            _condition_key(task),
        )
        if changes.get("name"):
            task["name"] = str(changes["name"]).strip()[:80]
        if changes.get("when"):
            task["schedule"], task["finished"] = _parse(changes["when"]), False
        if changes.get("tool"):
            task["tool"] = changes["tool"]
        if changes.get("args") is not None:
            task["args"] = strip_args(changes["args"])
        if changes.get("steps") is not None:
            # Replacing the steps replaces the whole action, chain or not: an edit
            # that leaves a stale single `tool` behind would run something the user
            # never sees in the listing.
            task["steps"] = normalize_steps(changes["steps"])
            if task["steps"]:
                task["kind"], task["tool"], task["args"] = "chain", "", {}
            elif task["kind"] == "chain":
                raise TaskError("A chain needs its steps; cancel the task instead of emptying it.")
        if changes.get("tool") and changes.get("steps") is None and task["kind"] == "chain":
            task["kind"], task["steps"] = "tool", []  # back to a single call
        if changes.get("on_error"):
            if changes["on_error"] not in ON_ERROR_MODES:
                raise TaskError(f"on_error must be one of {', '.join(ON_ERROR_MODES)}.")
            task["on_error"] = changes["on_error"]
        if changes.get("catch_up_until") is not None:
            task["catch_up_until"] = task_catchup.normalize_until(changes["catch_up_until"])
        if task.get("catch_up_until") and task["schedule"]["type"] != "cron":
            raise TaskError("catch_up_until only applies to a recurring time of day.")
        if changes.get("condition") is not None:
            # Editing the check or its verdict resets the poll baseline: a stale
            # "last seen" message must not make the new 'changed' fire immediately.
            task["condition"] = normalize_condition(changes["condition"])
            task["condition_state"] = None
        if changes.get("message"):
            task["message"] = str(changes["message"]).strip()
        if changes.get("notify"):
            if changes["notify"] not in NOTIFY_LEVELS:
                raise TaskError(f"notify must be one of {', '.join(NOTIFY_LEVELS)}.")
            task["notify"] = changes["notify"]
        if changes.get("enabled") is not None:
            task["enabled"] = bool(changes["enabled"])
        # A Protocol authorization covers only the action it was given for; an edit
        # keeps it only if the user named the Protocol again for THIS change.
        if changes.get("voice_authorized"):
            task["voice_authorized_tools"] = granted_tools(task, changes["voice_authorized"])
            task["voice_authorization"] = fingerprint(task)
        elif not voice_authorized(task):
            task["voice_authorization"], task["voice_authorized_tools"] = None, []
        validate_action(
            task["kind"],
            task["tool"],
            task["args"],
            task["message"],
            task.get("voice_authorized_tools") if voice_authorized(task) else False,
            task.get("steps"),
        )
        waiver_dropped = bool(task.get("waiver")) and not waiver_valid(task)
        if waiver_dropped:
            task["waiver"] = None
        # on_error is outside the action fingerprint (it changes how the steps run,
        # not what they are), but a yes given for "stops at the first failure" is not
        # a yes for "runs all eight regardless": re-ask.
        policy = (task.get("on_error"), task.get("catch_up_until") or "", _condition_key(task))
        if fingerprint(task) != before or policy != before_policy:
            task["pending"] = None
        if fingerprint(task) != before:
            if assess(task)["risk_level"] == "BLOCKED":
                raise TaskError("Refused: the edited action is blocked by the safety guard.")
        _register(task)
        _save(data)
    _update_status()
    return {**task, "waiver_dropped": waiver_dropped}


def cancel_task(ref: str) -> dict:
    with _lock:
        data = _load()
        task = _find(data, ref)
        data.pop(task["id"])
        _unregister(task["id"])
        _save(data)
    task_reports.resolve_all(task, "task cancelled by the user")
    hive.send_message(AGENT, "*", "task_cancelled", {"id": task["id"], "name": task["name"]})
    _update_status()
    return task


def set_waiver(ref: str, waived: bool, *, clear_pending: bool = False) -> dict:
    """clear_pending: the caller runs the waiting request itself right after. A task
    left parked is skipped by fire() at every due time until the 24h expiry, so a
    waiver granted while it waits must either clear it this way or approve it."""
    with _lock:
        data = _load()
        task = _find(data, ref)
        if waived and assess(task)["risk_level"] == "BLOCKED":
            raise TaskError("That action is blocked outright; a waiver can't unblock it.")
        task["waiver"] = make_waiver(task) if waived else None
        if waived and clear_pending:
            task["pending"] = None
        _save(data)
    kind = "waiver_granted" if waived else "waiver_revoked"
    hive.send_message(AGENT, "*", kind, {"name": task["name"]})
    return task


# ── running ──────────────────────────────────────────────────────────────────
# A condition check that ran and was MET: the caller falls through to the action.
_CONDITION_RUN = object()


def fire(
    task_id: str,
    trigger: str = "schedule",
    approved: bool = False,
    approved_fingerprint: str | None = None,
) -> dict | None:
    """Run one task now through the safety gate. Returns the run record."""
    with _lock:
        task = _load().get(task_id)
        if not task or (trigger in ("schedule", "late") and not task.get("enabled")):
            return None
        if approved and approved_fingerprint and fingerprint(task) != approved_fingerprint:
            # Edited between the user's yes and the run: that yes was for another action.
            return _record(task_id, "skipped", "The task changed after it was approved; not run.")
        if task.get("pending") and not approved:
            # Already waiting on the user: asking again every tick is noise.
            print(f"[TASKS] '{task['name']}' still awaiting approval; skipping this run.")
            return task.get("last_run")
    # A conditional task checks its condition FIRST — before the action's safety
    # gate — so a task whose action needs approval never asks until the condition is
    # actually met. Manual ("run it now") and approved runs are the user deciding at
    # that moment, so they bypass the gate and run the action directly.
    if task.get("condition") and trigger in ("schedule", "late"):
        gate = _condition_gate(task, trigger)
        if gate is not _CONDITION_RUN:
            return gate
    with _lock:
        task = _load().get(task_id)
        if task is None:
            return None
        safety = assess(task)
        if safety["risk_level"] == "BLOCKED":
            return _record(task_id, "blocked", safety["reason"], trigger)
        if safety["requires_confirmation"] and not approved and not waiver_valid(task):
            return _record(task_id, "awaiting_confirmation", safety["reason"], trigger, safety)
    if task["kind"] == "reminder":
        run = _record(task_id, "ok", task["message"], trigger)
    else:
        run = _execute(task, trigger)
    _maybe_finish_on_condition(task, run)
    return run


def _condition_gate(task: dict, trigger: str) -> object:
    """Run a conditional task's read-only check and judge it. Returns _CONDITION_RUN
    to let the action run, or a run record (waiting / skipped / error) otherwise."""
    if _executor is None:
        return _record(task["id"], "failed", "Era's tool runtime was not ready.", trigger)
    check = task["condition"]["check"]
    # Re-classified immediately before it runs, exactly like the action: a check that
    # has become risky or blocked since creation (a path now protected) does not run.
    try:
        verdict = assess_step(check["tool"], check["args"])
    except Exception as exc:
        return _record(task["id"], "failed", f"the condition check could not be classified: {exc}")
    if verdict["risk_level"] == "BLOCKED" or verdict["requires_confirmation"]:
        return _record(
            task["id"],
            "blocked",
            f"the condition check '{check['tool']}' is no longer a safe read-only check",
            trigger,
        )
    try:
        out = _executor(check["tool"], dict(check["args"]), task)
    except Exception as exc:
        return _record(
            task["id"],
            "failed",
            f"the condition check failed: {type(exc).__name__}: {exc}",
            trigger,
        )
    if not isinstance(out, dict):
        out = {"success": True, "message": str(out)}
    previous = (task.get("condition_state") or {}).get("last_message")
    met = condition_met(out, task["condition"], previous)
    _note_condition(task["id"], out, met)
    if met:
        return _CONDITION_RUN
    # Not met. A recurring task keeps polling quietly (no history spam, no report);
    # a one-shot date task had its single chance and finishes rather than waiting.
    if task["schedule"]["type"] == "date":
        return _record(
            task["id"], "skipped", "The condition was not met at the scheduled time.", trigger
        )
    print(f"[TASKS] '{task['name']}' condition not met; waiting for the next check.")
    return task.get("last_run")


def _note_condition(task_id: str, out: dict, met: bool) -> None:
    """Record the latest check result WITHOUT touching history — a poll that changes
    nothing is not a run, and a 5-minute poller would otherwise flush the 20 real
    entries in an hour. `last_message` is the baseline the 'changed' operator needs."""
    with _lock:
        data = _load()
        task = data.get(task_id)
        if task is None:
            return
        task["condition_state"] = {
            "checked_at": _now().isoformat(),
            "met": met,
            "last_message": str(out.get("message") or "")[:200],
        }
        _save(data)


def _maybe_finish_on_condition(task: dict, run: dict | None) -> None:
    """An "until" task (on_met="stop") retires once its condition is met and the
    follow-up actually ran. Not on a parked, skipped or failed run: the condition
    keeps holding the schedule open until the action truly happened."""
    condition = task.get("condition") or {}
    if condition.get("on_met") != "stop":
        return
    if (run or {}).get("status") not in ("ok", "partial"):
        return
    with _lock:
        data = _load()
        current = data.get(task["id"])
        if current is None or current.get("finished"):
            return
        current["finished"], current["enabled"], current["next_run"] = True, False, None
        _save(data)
        _unregister(task["id"])
    task_reports.resolve_all(task, "condition met; the task is complete")
    if task.get("notify") != "silent":
        text = f"Scheduled task '{task['name']}' is complete — its condition was met, so it won't run again."
        try:
            (_reporter or (lambda t, _u: print(f"[TASKS] {t}")))(text, False)
        except Exception as exc:
            print(f"[TASKS] report failed: {exc}")
    _update_status()


def _mark_running(task_id: str, step: int) -> None:
    """Persisted BEFORE each action: if Era dies mid-run, start() sees the marker and
    reports the run as interrupted instead of repeating it (a second email, etc.).
    For a chain it also records WHICH link was in flight, so the report can say so."""
    with _lock:
        data = _load()
        if task_id in data:
            data[task_id]["running_since"] = _now().isoformat()
            data[task_id]["running_step"] = step
            _save(data)


def _announce_step(task: dict, entry: dict, total: int) -> None:
    """Deliver a failed link's report BEFORE the chain runs the next one."""
    text = task_reports.step_failure_text(task, entry, total)
    if not text:
        return
    try:
        (_reporter or (lambda t, _u: print(f"[TASKS] {t}")))(text, True)
    except Exception as exc:
        print(f"[TASKS] report failed: {exc}")


def _execute(task: dict, trigger: str) -> dict:
    """Run the task's action — one tool call, or a chain's steps in order."""
    if _executor is None:
        return _record(task["id"], "failed", "Era's tool runtime was not ready.", trigger)
    steps = steps_of(task)
    if not steps:
        return _record(task["id"], "failed", "The task has no action to run.", trigger)

    def _run_step(n: int, tool: str, args: dict):
        _mark_running(task["id"], n)
        return _executor(tool, args, task)

    out = task_chain.run(
        steps,
        execute=_run_step,
        assess_step=assess_step,
        announce=lambda entry, total: _announce_step(task, entry, total),
        on_error=task.get("on_error") or "stop",
        run_if=lambda step: _eval_run_if(task, step),
    )
    return _record(
        task["id"], out["status"], out["message"][:600], trigger, steps=out["steps"] or None
    )


def _eval_run_if(task: dict, step: dict) -> tuple[bool, str]:
    """Evaluate one chain step's run_if right before it would run: (should_run, note).
    The check is a read-only tool call, re-classified first exactly like the action —
    a check that has become risky, is blocked, or errors is treated as "don't run this
    step" (the safe direction), never as a green light."""
    cond = step.get("run_if")
    if not cond:
        return True, ""
    check = cond["check"]
    try:
        verdict = assess_step(check["tool"], check["args"])
    except Exception as exc:
        return False, f"run_if skipped: its check could not be classified ({exc})"
    if verdict["risk_level"] == "BLOCKED" or verdict["requires_confirmation"]:
        return False, f"run_if skipped: the check '{check['tool']}' is no longer read-only"
    try:
        out = _executor(check["tool"], dict(check["args"]), task)
    except Exception as exc:
        return False, f"run_if skipped: the check failed ({type(exc).__name__}: {exc})"
    if not isinstance(out, dict):
        out = {"success": True, "message": str(out)}
    # A step has no persistent baseline between fires, so 'changed' is refused at
    # creation and `previous` is always None here.
    if condition_met(out, cond, None):
        return True, ""
    return False, f"run_if not met: {describe_condition(cond)}"


# Two asks this close together make a bare "yes" ambiguous: the user must name one.
_FOCUS_AMBIGUITY_S = 30.0

# A refusal clears only on the user's own words, so the same call repeated before they
# could possibly have spoken cannot succeed. Re-stamping the ask on such a retry would
# restart the bare-"yes" window the user is already inside, and — with a second task
# refused within _FOCUS_AMBIGUITY_S — blank the focus, so no bare "yes" answers
# anything at all. The model retried 736ms apart on 2026-09-21; prose never stopped it.
_RE_ASK_QUIET_S = 10.0


def mark_announced(task_id: str) -> None:
    """The user has just been ASKED about this waiting task — by voice, or by the
    model relaying a tool result that tells it to ask. A bare reply must come after.

    A stamp younger than _RE_ASK_QUIET_S is left alone: that is the model calling
    again, not the user being asked again, and moving it would restart the window
    their reply is already inside."""
    with _lock:
        data = _load()
        task = data.get(task_id)
        if task and task.get("pending"):
            if time.time() - (task["pending"].get("announced_at") or 0.0) <= _RE_ASK_QUIET_S:
                return
            task["pending"]["announced_at"] = time.time()
            _save(data)


def _focus(tasks) -> dict | None:
    """The waiting task the user was asked about LAST — what a bare "yes" answers.
    None when nothing was announced or two asks came within _FOCUS_AMBIGUITY_S."""
    asked = sorted(
        (t for t in tasks if t.get("pending") and t["pending"].get("announced_at")),
        key=lambda t: t["pending"]["announced_at"],
    )
    if not asked:
        return None
    last = asked[-1]
    if len(asked) > 1 and (
        last["pending"]["announced_at"] - asked[-2]["pending"]["announced_at"] < _FOCUS_AMBIGUITY_S
    ):
        return None
    return last


def asked_at(task: dict) -> float:
    """When the user was last asked about this waiting task (or when it parked)."""
    pending = task.get("pending") or {}
    since = datetime.fromisoformat(pending["since"]).timestamp() if pending.get("since") else 0.0
    return max(since, pending.get("announced_at") or 0.0)


def is_focused(task: dict) -> bool:
    """True when `task` is the one a bare "yes" / "always" answers. Decided here from
    the store, never from which task the model chose to name."""
    with _lock:
        focus = _focus(_load().values())
    return bool(focus and focus["id"] == task.get("id"))


def approve(ref: str | None = None, authorize=None, always=None) -> dict:
    """`authorize(task, focused) -> bool` is the caller's check of the user's own
    words for THIS parked request; `focused` = it is the one asked about last.
    `always(task, focused) -> bool`: the same words also said "don't ask again".
    Without `authorize` nothing is approved."""
    # Claim the approval under the lock (a second "yes" finds nothing pending and
    # cannot run it twice), then run OUTSIDE the lock: a tool call may take
    # minutes and must not stall every other scheduler read meanwhile.
    with _lock:
        data = _load()
        focus = _focus(data.values())
        # No name given: the user is answering the task asked about last.
        task = focus if not ref and focus else None
        task = task or _find(
            data, ref, predicate=lambda t: bool(t.get("pending")), requires="awaiting approval"
        )
        since = datetime.fromisoformat(task["pending"]["since"])
        if _now() - since > _APPROVAL_TTL:
            _record(
                task["id"], "skipped", "The approval request expired after 24 hours unanswered."
            )
            raise TaskError(f"The approval request for '{task['name']}' had expired; not run.")
        focused = bool(focus and focus["id"] == task["id"])
        if authorize is None or not authorize(task, focused):
            # The model is now told to ask the user about THIS task, so the answer
            # to that question is what the next bare "yes" means. Only a FRESH ask
            # moves the stamp: a retry the user never saw must not restart it.
            stamped = task["pending"].get("announced_at") or 0.0
            retry = time.time() - stamped <= _RE_ASK_QUIET_S
            if not retry:
                task["pending"]["announced_at"] = time.time()
                _save(data)
            raise ApprovalRequired(
                f"Not run: '{task['name']}' needs the user's own answer to the question "
                "about it — 'yes' to run it, 'always' to stop asking."
                + (" You have already asked; wait for their reply." if retry else "")
            )
        # Decided now, from the words that approved it — not after a run that may
        # take minutes, when the latest utterance is something else entirely.
        waive_too = bool(always and always(task, focused))
        task["pending"] = None
        approved_fp = fingerprint(task)
        _save(data)
    run = fire(task["id"], trigger="approved", approved=True, approved_fingerprint=approved_fp)
    waived = False
    if waive_too and (run or {}).get("status") != "skipped":
        with _lock:
            current = _load().get(task["id"])
            if current and fingerprint(current) == approved_fp:  # edited since: no waiver
                set_waiver(task["id"], True)
                waived = True
    return {"task": task, "run": run, "focused": focused, "waived": waived}


def skip(ref: str | None = None) -> dict:
    with _lock:
        task = _find(
            _load(), ref, predicate=lambda t: bool(t.get("pending")), requires="awaiting approval"
        )
        return {"task": task, "run": _record(task["id"], "skipped", "Skipped by the user.")}


def run_now(ref: str) -> dict:
    task = get_task(ref)
    if not task.get("enabled") and not task.get("finished"):
        raise TaskError(f"'{task['name']}' is paused. Resume it first to run it.")
    return {"task": task, "run": fire(task["id"], trigger="manual")}


def _record(task_id, status, message, trigger="schedule", safety=None, steps=None) -> dict | None:
    with _lock:
        data = _load()
        task = data.get(task_id)
        if task is None:
            return None
        entry = {"at": _now().isoformat(), "status": status, "trigger": trigger, "message": message}
        if steps:
            entry["steps"] = steps  # per-link outcome of a chain, for status/results
        task["history"] = (task.get("history") or [])[-(_HISTORY_MAX - 1) :] + [entry]
        task["last_run"], task["unread"] = entry, True
        task.pop("running_since", None)
        task.pop("running_step", None)
        if status == "awaiting_confirmation":
            # announced_at: set once the user has actually been ASKED (mark_announced).
            task["pending"] = {"since": entry["at"], "reason": message, "announced_at": None}
        else:
            task["pending"] = None
            if task["schedule"]["type"] == "date":
                task["finished"], task["enabled"] = True, False
        job = _scheduler.get_job(f"user:{task_id}") if _scheduler else None
        if job:
            task["next_run"] = _job_next_run(job)
        elif task["finished"]:
            task["next_run"] = None
        _save(data)
    task_reports.feed_hive(task, entry, safety)
    text = task_reports.report_text(task, entry)
    if text:
        try:
            heard = (_reporter or (lambda t, _u: print(f"[TASKS] {t}")))(text, status != "ok")
        except Exception as exc:
            heard = False
            print(f"[TASKS] report failed: {exc}")
        # The reporter returns False when the ask only reached the UI log (Era was
        # busy); None means it cannot tell, and is taken as delivered.
        if status == "awaiting_confirmation" and heard is not False:
            mark_announced(task_id)
    _update_status()
    return entry


def _update_status() -> None:
    running = bool(_scheduler and _scheduler.running)
    detail = task_reports.status_detail(list(_load().values()))
    hive.set_status(AGENT, "running" if running else "stopped", detail)


# ── APScheduler wiring ───────────────────────────────────────────────────────
def _job_next_run(job) -> str | None:
    nrt = getattr(job, "next_run_time", None)
    return nrt.strftime("%Y-%m-%d %H:%M") if nrt else None


def _unregister(task_id: str) -> None:
    if _scheduler is None:
        return
    for job_id in (f"user:{task_id}", f"user:{task_id}:catchup"):
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass


def _register(task: dict) -> None:
    """(Re)register one task's job. Mutates task['next_run']; caller saves."""
    if _scheduler is None or not _scheduler.running:
        return
    if not task.get("enabled") or task.get("finished"):
        _unregister(task["id"])
        task["next_run"] = None
        return
    try:
        job = _scheduler.add_job(
            fire,
            build_trigger(task["schedule"]),
            args=[task["id"]],
            id=f"user:{task['id']}",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    except Exception as exc:
        raise TaskError(f"Couldn't register that schedule: {exc}") from exc
    task["next_run"] = _job_next_run(job)


def start() -> int:
    """Start the scheduler and re-register every saved task. Returns active count."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    with _lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(job_defaults={"coalesce": True})
        if not _scheduler.running:
            _scheduler.start()
        data, now, catch_up, missed, interrupted = _load(), _now(), [], [], {}
        recurring_catch_up = []
        for tid, task in list(data.items()):
            if task.get("running_since"):
                # Never re-run: it may already have acted. For a chain, remember how
                # far it got so the report says which link was in flight.
                interrupted[tid] = (task.get("running_step") or 0, len(steps_of(task)))
            ended = (task.get("last_run") or {}).get("at") or task.get("created")
            if task.get("finished") and now - datetime.fromisoformat(ended) > _FINISHED_RETENTION:
                data.pop(tid)
                continue
            if task.get("pending"):
                continue  # still waiting on the user — never convert it to "missed"
            sched = task["schedule"]
            if sched["type"] == "date" and task.get("enabled") and not task.get("finished"):
                run_at = datetime.fromisoformat(sched["run_at"])
                if tid in interrupted:
                    continue
                if run_at <= now:
                    (catch_up if now - run_at <= _CATCHUP_GRACE else missed).append(tid)
                    continue
            if tid not in interrupted:
                # Recurring run Era was off for (core/task_catchup.py); opt-in per task.
                decision = task_catchup.startup_decision(task, now)
                if decision == "catch_up":
                    recurring_catch_up.append(tid)
                elif decision == "missed":
                    missed.append(tid)
            try:
                _register(task)
            except TaskError as exc:
                print(f"[TASKS] could not register '{task['name']}': {exc}")
        _save(data)
    for tid, (step, total) in interrupted.items():
        where = f" at step {step} of {total}" if total > 1 and step else ""
        _record(
            tid, "failed", f"Era stopped mid-run{where}; not repeated in case it already acted."
        )
    for tid in missed:
        _record(tid, "missed", "Era was not running at the scheduled time.")
    for tid in catch_up:
        # Late but within grace: run once, shortly, so startup is not blocked.
        _scheduler.add_job(
            fire,
            "date",
            run_date=now + timedelta(seconds=45),
            args=[tid],
            id=f"user:{tid}",
            replace_existing=True,
            kwargs={"trigger": "late"},
        )
    for tid in recurring_catch_up:
        # Its own job id: "user:<id>" is the recurring job, which must stay registered.
        _scheduler.add_job(
            fire,
            "date",
            run_date=now + timedelta(seconds=45),
            args=[tid],
            id=f"user:{tid}:catchup",
            replace_existing=True,
            kwargs={"trigger": "late"},
        )
    _update_status()
    return sum(1 for t in _load().values() if t.get("enabled") and not t.get("finished"))
