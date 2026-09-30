"""actions/scheduled_task.py — the `scheduled_task` tool: create, list, modify, cancel,
approve and waive the user's scheduled tasks. The engine is core/task_scheduler.py.

A task is a reminder, one tool call (tool + tool_args), or a CHAIN: `steps`, a JSON
array of tool calls run in order under a single rule ("every weekday at 9, run my
morning routine"). A chain is approved, waived and reported as ONE unit, classified
at its riskiest step, and a failed link is reported before any later link acts.

Authority comes from an `Authority` the dispatcher builds over the user's OWN
recorded words (never the model's arguments), and each check is about ONE task:
  approves(task, focused)  a non-negated "yes / approve / always" said AFTER the task
                        parked — bare when it is the task announced last (focused),
                        naming it otherwise
  waives(name, focused, since)  a short "always / don't ask again" as the reply
                        to the ask about the focused task, or a waiver naming it
  waives_new(name)      "…without asking" said while scheduling this task
  names_protocol(tool)  "launch Protocol One" / "launch Protocol Zero" said now
A model that sets waive_confirmation=true on its own gets the task created WITH
the confirmation gate intact, and is told so. The default Authority grants nothing.

Returns {"success": bool, "message": str} — explicit, because ToolResult.from_any
judges plain strings by words like "failed", and a run report is made of them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from core import task_scheduler as ts


@dataclass
class Authority:
    """What the user's own words authorise. Built by the dispatcher; default: nothing."""

    approves: Callable[[dict, bool], bool] = lambda task, focused: False
    waives: Callable[..., bool] = lambda name, focused=False, since=0.0: False
    waives_new: Callable[[str], bool] = lambda name: False
    names_protocol: Callable[[str], bool] = lambda tool: False


_ACTION_ALIASES = {
    "schedule": "create",
    "add": "create",
    "new": "create",
    "show": "list",
    "get": "status",
    "info": "status",
    "modify": "update",
    "edit": "update",
    "reschedule": "update",
    "delete": "cancel",
    "remove": "cancel",
    "run": "run_now",
    "confirm": "approve",
    "yes": "approve",
    "always": "waive_confirmation",
    "deny": "skip",
    "reject": "skip",
    "waive": "waive_confirmation",
    "report": "results",
    "chain": "steps",
    "sequence": "steps",
}


def _ok(message: str) -> dict:
    return {"success": True, "message": message}


def _fail(message: str) -> dict:
    return {"success": False, "message": message}


def _gated(message: str) -> dict:
    """A refusal by the approval gate — the system WORKING, not a fault.

    error_type is what tells the two apart downstream: without it the dict lands in
    ToolResult with error_type=None, the error router collapses that to "Error",
    and BENIGN_ERROR_TYPES (which already lists ConfirmationRequired) can never
    match — so three held gates in 15 minutes mailed Claude Code a bug report and
    self_diagnosis counted them as real faults (2026-09-21).
    """
    return {"success": False, "message": message, "error_type": "ConfirmationRequired"}


def _parse_args(raw) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
    except ValueError as exc:
        raise ts.TaskError(f"tool_args must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ts.TaskError("tool_args must be a JSON object.")
    return parsed


def _parse_steps(raw) -> list[dict]:
    """The chain, as the model sent it: a JSON array string or an already-decoded list.
    Shape and safety are checked by core/task_store.normalize_steps, not here."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        parsed = json.loads(str(raw))
    except ValueError as exc:
        raise ts.TaskError(f"steps must be a JSON array of tool calls: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ts.TaskError("steps must be a JSON array of {tool, args} objects.")
    return parsed


def _voice_authorized(steps: list[dict], authority: Authority) -> list[str]:
    """The Protocol tools the user named — and only if they named EVERY one the task
    would launch. A partial grant is no grant, so one spoken "launch Protocol One"
    can never carry an unspoken Protocol Zero in the same chain."""
    gated = [
        s["tool"]
        for s in steps
        if ts.VOICE_GATED_ACTIONS.get(s["tool"]) == ts.norm_action(s["args"])
    ]
    if not gated or not all(authority.names_protocol(t) for t in gated):
        return []
    return sorted(set(gated))


def _as_bool(value) -> bool | None:
    if isinstance(value, bool) or value is None:
        return value
    return {"true": True, "yes": True, "false": False, "no": False}.get(str(value).lower())


def format_task(task: dict) -> str:
    flags = []
    if task.get("pending"):
        flags.append("AWAITING YOUR APPROVAL")
    if not task.get("enabled") and not task.get("finished"):
        flags.append("paused")
    if task.get("finished"):
        flags.append("finished")
    if ts.waiver_valid(task):
        flags.append("confirmation waived")
    if task.get("kind") == "chain" and task.get("on_error") == "continue":
        flags.append("continues past a failed step")
    if task.get("condition"):
        flags.append(ts.describe_condition(task["condition"]))
        state = task.get("condition_state") or {}
        if state.get("checked_at"):
            flags.append(
                f"last checked {state['checked_at'][:16].replace('T', ' ')}: "
                + ("met" if state.get("met") else "not yet")
            )
    if task.get("catch_up_until"):
        flags.append(f"made up at launch if missed, until {task['catch_up_until']}")
    last = task.get("last_run") or {}
    parts = [
        f"'{task['name']}' [{task['id']}]: {ts.describe_action(task)}",
        task["schedule"].get("description", task["schedule"].get("when", "")),
    ]
    if task.get("next_run"):
        parts.append(f"next {task['next_run']}")
    if last:
        parts.append(f"last run {last['at'][:16].replace('T', ' ')} {last['status']}")
    if flags:
        parts.append("(" + ", ".join(flags) + ")")
    return " — ".join(parts)


def _run_message(out: dict) -> str:
    task, run = out["task"], out.get("run") or {}
    status = run.get("status", "not run")
    if status == "awaiting_confirmation":
        return (
            f"'{task['name']}' needs approval before it runs ({run.get('message')}). Ask the "
            "user once: 'yes' runs it, 'always' runs it and stops asking. On yes call "
            f"scheduled_task(action='approve', task='{task['name']}')."
        )
    text = f"'{task['name']}' ran: {status} — {run.get('message', '')}"
    steps = run.get("steps") or []
    if steps:
        # Per-link outcomes, so the model reports WHICH step of the routine broke
        # rather than flattening a half-run chain into one word.
        text += "\n" + "\n".join(
            f"  {e['n']}. {e['tool']}: {e['status']} — {e['message'][:200]}" for e in steps
        )
    return text


def scheduled_task(parameters: dict, authority: Authority | None = None) -> dict:
    p = dict(parameters or {})
    action = str(p.get("action") or "list").lower().strip()
    action = _ACTION_ALIASES.get(action, action)
    ref = str(p.get("task") or p.get("name") or "").strip()
    try:
        return _dispatch(action, ref, p, authority or Authority())
    except ts.TaskError as exc:
        return _fail(f"{exc} Tell the user in one sentence.")
    except Exception as exc:  # never let a scheduler bug take the live turn down
        print(f"[TASKS] scheduled_task({action}) crashed: {type(exc).__name__}: {exc}")
        return _fail(f"The scheduler hit an internal error ({type(exc).__name__}).")


def _dispatch(action, ref, p, authority: Authority) -> dict:
    if action == "create":
        want_waiver = bool(_as_bool(p.get("waive_confirmation")))
        name, tool_name = str(p.get("name") or ref), str(p.get("tool") or "").strip()
        args = _parse_args(p.get("tool_args"))
        steps = ts.normalize_steps(_parse_steps(p.get("steps")))
        has_action = bool(steps or tool_name)
        task = ts.create_task(
            name,
            p.get("when") or "",
            kind=p.get("kind") or ("tool" if has_action else "reminder"),
            tool=tool_name,
            args=args,
            steps=steps,
            message=str(p.get("message") or ""),
            notify=str(p.get("notify") or "all").lower(),
            on_error=str(p.get("on_error") or "stop").lower(),
            catch_up_until=str(p.get("catch_up_until") or ""),
            condition=p.get("condition"),
            # The task being created is what the user is talking about right now, so
            # "…without asking" needs no name (the name is often one the model made up).
            waive=want_waiver and authority.waives_new(name),
            voice_authorized=_voice_authorized(
                steps or ([{"tool": tool_name, "args": args}] if tool_name else []), authority
            ),
        )
        risk = task["risk"]
        msg = f"Scheduled {format_task(task)}."
        if task.get("condition"):
            msg += " Each time it is due it runs the check first and " + (
                "acts, then stops, only when the condition is met."
                if task["condition"].get("on_met") == "stop"
                else "acts only when the condition is met, otherwise waits for the next check."
            )
        if task["kind"] == "chain":
            msg += f" The {len(task['steps'])} steps run in order as one task; if one fails " + (
                "the failure is reported and the rest still run."
                if task.get("on_error") == "continue"
                else "it is reported and the remaining steps do NOT run."
            )
        if risk["requires_confirmation"]:
            if ts.waiver_valid(task):
                msg += " You waived confirmation, so it will run without asking."
            else:
                msg += (
                    f" It is {risk['risk_level']} risk ({risk['reason']}), so each time it is "
                    "due I will ask for your approval before running it."
                )
                if want_waiver:
                    msg += (
                        " Confirmation was NOT waived: the user did not say so themself. "
                        "They can say 'always' when it asks, or 'don't ask again'."
                    )
        return _ok(msg + " Confirm to the user in one or two sentences.")

    if action == "list":
        include = bool(_as_bool(p.get("include_finished")))
        tasks = ts.list_tasks(include_finished=include)
        if not tasks:
            return _ok("There are no scheduled tasks.")
        lines = "\n".join(f"- {format_task(t)}" for t in tasks)
        return _ok(f"{len(tasks)} scheduled task(s):\n{lines}\nSummarise briefly for the user.")

    if action == "status":
        if not ref:
            return _ok(ts.briefing_line())
        task = ts.get_task(ref)
        history = task.get("history", [])[-3:]
        runs = "; ".join(f"{h['at'][:16]} {h['status']}: {h['message'][:120]}" for h in history)
        return _ok(format_task(task) + (f"\nRecent runs: {runs}" if runs else ""))

    if action in ("update", "pause", "resume"):
        changes = {}
        if action == "update":
            changes = {k: p.get(k) for k in ("when", "tool", "message") if p.get(k)}
            if p.get("new_name"):
                changes["name"] = p["new_name"]
            if p.get("notify"):
                changes["notify"] = str(p["notify"]).lower()
            if p.get("on_error"):
                changes["on_error"] = str(p["on_error"]).lower()
            if p.get("catch_up_until") not in (None, ""):
                changes["catch_up_until"] = str(p["catch_up_until"])
            if p.get("condition") is not None:
                changes["condition"] = p["condition"]
            if p.get("tool_args") not in (None, ""):
                changes["args"] = _parse_args(p.get("tool_args"))
            if p.get("steps") not in (None, ""):
                changes["steps"] = _parse_steps(p.get("steps"))
            if _as_bool(p.get("enabled")) is not None:
                changes["enabled"] = _as_bool(p.get("enabled"))
            if not changes:
                return _fail("Nothing to change. Ask the user what to modify.")
        else:
            changes["enabled"] = action == "resume"
        current = ts.get_task(ref)
        # What the task will DO after this edit is what the user must have authorised —
        # a fresh Protocol command is required for the new action, never the old one.
        after = {**current, **{k: v for k, v in changes.items() if k in ("tool", "args", "steps")}}
        if changes.get("steps") is not None:
            after["steps"] = ts.normalize_steps(changes["steps"])
        elif changes.get("tool"):
            after["kind"], after["steps"] = "tool", []
        authorized = _voice_authorized(ts.steps_of(after), authority)
        task = ts.update_task(current["id"], voice_authorized=authorized, **changes)
        msg = f"Updated {format_task(task)}."
        if task.get("waiver_dropped"):
            msg += (
                " Its action or timing changed, so its confirmation waiver was removed — it "
                "will ask again unless the user waives it for the new version."
            )
        return _ok(msg)

    if action == "cancel":
        task = ts.cancel_task(ref)
        return _ok(f"Cancelled and removed '{task['name']}'.")

    if action == "run_now":
        out = ts.run_now(ref)
        if (out.get("run") or {}).get("status") == "awaiting_confirmation":
            ts.mark_announced(out["task"]["id"])  # the result tells the model to ask
        return _ok(_run_message(out))

    if action == "approve":
        try:
            out = ts.approve(
                ref or None,
                authorize=authority.approves,
                # "yes, always" / "don't ask again" approves THIS run and every later one.
                always=lambda t, focused: authority.waives(t["name"], focused, ts.asked_at(t)),
            )
        except ts.ApprovalRequired as exc:
            return _gated(f"{exc} Ask the user once; do not retry on your own.")
        except ts.TaskError as exc:
            return _fail(f"{exc} Ask the user once; do not retry on your own.")
        msg = _run_message(out)
        if out.get("waived"):
            msg += " Confirmation is now waived: it will run without asking from now on."
        return _ok(msg)

    if action == "skip":
        out = ts.skip(ref or None)
        return _ok(f"Skipped this run of '{out['task']['name']}'. It stays scheduled.")

    if action in ("waive_confirmation", "require_confirmation"):
        waived = action == "waive_confirmation"
        if waived and not ref:
            # "always" with no name answers the task the user was asked about last.
            focus = next((t for t in ts.pending_approvals() if ts.is_focused(t)), None)
            ref = focus["id"] if focus else ref
        current = ts.get_task(ref)
        waiting = bool(current.get("pending"))
        if waived:
            # A waiting task: the words must come after the user was asked about it.
            since = ts.asked_at(current) if waiting else 0.0
            if not authority.waives(current["name"], waiting and ts.is_focused(current), since):
                if waiting:
                    ts.mark_announced(current["id"])  # the model now asks about it
                return _gated(
                    "Waiver NOT granted: it needs the user's own words — 'always' or 'don't "
                    f"ask again' as the answer when '{current['name']}' is asked about, or "
                    f"naming it ('never ask for {current['name']}'). Ask the user once; do "
                    "not retry on your own."
                )
        # Waiving a waiting task also answers it, so it runs now (below) and must not
        # stay parked — fire() skips a parked task at every due time.
        task = ts.set_waiver(current["id"], waived, clear_pending=waived and waiting)
        if not waived:
            return _ok(f"'{task['name']}' will ask for approval again before risky runs.")
        msg = (
            f"'{task['name']}' will now run without asking for confirmation. The waiver "
            "covers this exact action only; editing the task removes it."
        )
        if waiting:
            run = ts.fire(task["id"], trigger="approved")
            msg += " " + _run_message({"task": task, "run": run})
        return _ok(msg)

    if action == "results":
        rows = ts.results(ref or None)
        if not rows:
            return _ok("No scheduled task has run yet.")
        out = []
        for row in rows:
            for h in row["history"]:
                out.append(f"- {row['name']} {h['at'][:16]} {h['status']}: {h['message'][:200]}")
        return _ok("Recent scheduled-task results:\n" + "\n".join(out[-15:]))

    if action == "pending":
        items = ts.pending_approvals()
        if not items:
            return _ok("No scheduled task is waiting for approval.")
        for t in items:  # the model reads these to the user: that is the ask
            ts.mark_announced(t["id"])
        return _ok("Awaiting approval:\n" + "\n".join(f"- {format_task(t)}" for t in items))

    if action == "steps":
        task = ts.get_task(ref)
        steps = ts.steps_of(task)
        if len(steps) < 2:
            return _ok(f"'{task['name']}' is a single action: {ts.describe_action(task)}.")
        lines = "\n".join(f"  {i}. {ts.describe_step(s)}" for i, s in enumerate(steps, 1))
        policy = (
            "a failed step stops the rest"
            if (task.get("on_error") or "stop") == "stop"
            else "a failed step is reported and the rest still run"
        )
        return _ok(f"'{task['name']}' runs {len(steps)} steps in order ({policy}):\n{lines}")

    return _fail(
        f"Unknown action '{action}'. Use create | list | status | update | pause | resume | "
        "cancel | run_now | approve | skip | waive_confirmation | require_confirmation | "
        "results | pending | steps."
    )
