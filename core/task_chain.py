"""core/task_chain.py — running one scheduled task's steps in order.

A chained task ("every weekday at 9, run my morning routine") is ONE scheduled
rule holding several tool calls, approved once and reported once. This module is
the sequencing half of that: it decides what happens between link N and link N+1.
It lives apart from core/task_scheduler.py because those rules are about failure,
not about storage or APScheduler — and because that file is at its size ceiling.

Rules — do not undo:
  * Steps run strictly in order, one at a time, never concurrently.
  * A step may carry a `run_if` condition (a read-only check). It is evaluated
    immediately before the step, in order: not met → the step is SKIPPED and the
    chain moves on to the next. A conditional skip is NOT a failure — it never
    triggers on_error and never stops the chain. If the check itself cannot be run
    (errored, or no longer read-only) the step is skipped too, the safe direction:
    an unresolvable condition never runs an action the user made conditional.
  * A failing link is REPORTED BEFORE the next link runs. With the default
    on_error="stop" the chain stops there and the run record carries the failure;
    with on_error="continue" the user hears about the failed link first and the
    remaining links then run, so nothing later acts on a silent failure.
  * Every step is re-classified by the safety guard immediately before it runs.
    The chain was approved as a whole, but a step that has become BLOCKED since
    (a path that is now protected) does not run, and always stops the chain —
    whatever on_error says, because something changed after the user's yes.
  * Steps that never ran are recorded as "skipped", not silently dropped: a
    half-run routine must be visible as a half-run routine.
  * A chain that did some of its work never reports plain "ok" — it reports
    "partial", naming the link that stopped it.
  * A single-step task is summarised exactly as before chains existed (its own
    status and its tool's own message), so nothing about it changes.
  * Nothing here touches the store, the scheduler or the reporter, and nothing
    raises: it returns a result for the caller to record.
"""

from __future__ import annotations

from collections.abc import Callable

_STEP_MESSAGE_MAX = 400


def run(
    steps: list[dict],
    *,
    execute: Callable[[int, str, dict], object],
    assess_step: Callable[[str, dict], dict],
    announce: Callable[[dict, int], None] | None = None,
    on_error: str = "stop",
    run_if: Callable[[dict], tuple[bool, str]] | None = None,
) -> dict:
    """Run `steps` in order. Returns {"status", "message", "steps"}.

    `execute(n, tool, args)` performs one step (the caller's safety-gated runtime);
    `assess_step(tool, args)` re-classifies it first; `announce(entry, total)` tells
    the user about a failure the chain is about to run PAST — it is not called when
    the chain stops, because the run record already reports that. `run_if(step)`, if
    given, evaluates a step's condition just before it would run and returns
    (should_run, note); a step it declines is skipped, not failed.
    """
    total, entries, stopped_at = len(steps), [], 0
    for i, step in enumerate(steps, 1):
        if stopped_at:
            entries.append(
                {
                    "n": i,
                    "tool": step["tool"],
                    "status": "skipped",
                    "message": f"not run: step {stopped_at} stopped the chain",
                }
            )
            continue
        if run_if is not None and step.get("run_if"):
            should, note = run_if(step)
            if not should:
                entries.append(
                    {
                        "n": i,
                        "tool": step["tool"],
                        "status": "skipped",
                        "skipped_by": "condition",
                        "message": note or "run_if condition not met",
                    }
                )
                continue
        entry = _run_one(i, step, execute, assess_step)
        entries.append(entry)
        if entry["status"] == "ok":
            continue
        # A block means the ground moved after the user's approval: never carry on.
        carry_on = on_error == "continue" and entry["status"] != "blocked"
        if not carry_on:
            stopped_at = i
        elif announce:
            announce(entry, total)
    return _summarise(entries, total)


def _run_one(i: int, step: dict, execute, assess_step) -> dict:
    entry = {"n": i, "tool": step["tool"], "status": "failed", "message": ""}
    try:
        verdict = assess_step(step["tool"], step["args"])
    except Exception as exc:  # a guard that cannot answer is not a green light
        entry["status"] = "blocked"
        entry["message"] = f"the safety guard could not classify this step: {exc}"
        return entry
    if verdict.get("risk_level") == "BLOCKED":
        entry["status"] = "blocked"
        entry["message"] = str(verdict.get("reason") or "blocked by the safety guard")
        return entry
    try:
        out = execute(i, step["tool"], dict(step.get("args") or {}))
    except Exception as exc:
        entry["message"] = f"{type(exc).__name__}: {exc}"
        return entry
    if not isinstance(out, dict):
        out = {"success": True, "message": str(out)}
    entry["status"] = "ok" if out.get("success") else "failed"
    entry["message"] = str(out.get("message") or "Done.")[:_STEP_MESSAGE_MAX]
    return entry


def _summarise(entries: list[dict], total: int) -> dict:
    if total == 1:
        # Not a chain: report it exactly as the scheduler always has.
        one = entries[0]
        return {"status": one["status"], "message": one["message"], "steps": []}
    ok = [e for e in entries if e["status"] == "ok"]
    bad = [e for e in entries if e["status"] in ("failed", "blocked")]
    cond_skipped = [e for e in entries if e.get("skipped_by") == "condition"]
    if not bad:
        joined = "; ".join(f"{e['n']}. {e['tool']}: {e['message']}" for e in entries)
        if cond_skipped:
            head = f"{len(ok)} of {total} steps ran, {len(cond_skipped)} skipped by condition"
            return {"status": "ok", "message": f"{head} — {joined}", "steps": entries}
        return {"status": "ok", "message": f"all {total} steps ran — {joined}", "steps": entries}
    first = bad[0]
    head = f"step {first['n']} of {total} ({first['tool']}) {first['status']}: {first['message']}"
    # Only steps a STOP left unrun are "not run"; a condition skip is deliberate.
    skipped = [
        str(e["n"])
        for e in entries
        if e["status"] == "skipped" and e.get("skipped_by") != "condition"
    ]
    if skipped:
        head += f"; step(s) {', '.join(skipped)} not run"
    later = bad[1:]
    if later:
        head += "; also " + "; ".join(f"step {e['n']} ({e['tool']}) {e['status']}" for e in later)
    if ok:
        return {
            "status": "partial",
            "message": f"{len(ok)} of {total} steps ran — {head}",
            "steps": entries,
        }
    return {"status": "failed", "message": head, "steps": entries}
