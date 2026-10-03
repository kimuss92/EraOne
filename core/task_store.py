"""core/task_store.py — storage, lookup and safety helpers for the user's scheduled tasks.

The lifecycle (create/run/approve) lives in core/task_scheduler.py and the hive +
report wording in core/task_reports.py; this module holds everything those two
share and that needs no scheduler state. See task_scheduler's docstring for the
invariants the whole feature keeps.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from core.confirmation_preview import CARRIED_KEYS

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Tools a task may never call: confirmation plumbing (a task could pre-confirm
# itself), standing waivers/preferences, the voice lock, memory writes whose gate
# is the user's live words, and this tool itself (a task scheduling tasks).
UNSCHEDULABLE_TOOLS = frozenset(
    {
        "scheduled_task",
        "confirm_pending_confirmation",
        "cancel_pending_confirmation",
        "manage_preference",
        "user_preference",
        "voice_lock",
        "save_memory",
        "record_feedback",
        "set_wake_word",
    }
)
# tool -> the action whose dispatcher demands the user's spoken command. Such a
# task is accepted only when the caller confirms the user named it when scheduling.
VOICE_GATED_ACTIONS = {"protocol_one": "launch", "protocol_zero": "execute"}
# A stored task must never carry a pre-made confirmation — nor a pre-made
# *plan*. `_approved_plan` (CARRIED_KEYS) is set only by a preview builder and
# means "the user was shown these exact steps"; a scheduled task's args come
# from the model, so one supplied here would be executed verbatim at every fire
# with no preview and no planner call at all.
STRIPPED_ARGS = ("confirmed", "confirmation_id", "auto_confirm", *sorted(CARRIED_KEYS))
# Rated LOW for a LIVE call because the user is there to watch and stop it. Run
# unattended on a repeat — open-ended goals, code, other agents, publishing — they
# need the same approval-or-waiver as a HIGH action. {tool: read-only actions exempt}
UNATTENDED_REVIEW_TOOLS = {
    "autonomous_development": ("status",),
    "agent_task": (),
    "agent_browser": (),
    "code_helper": ("explain",),
    "dev_agent": (),
    "community_manager": (),
    "self_improve": ("propose", "list_backups", "find", ""),
    "self_heal": ("status", "scan", ""),
    "file_controller": (
        "open",
        "list",
        "read",
        "info",
        "search",
        "find",
        "largest",
        "disk_usage",
        "last_download",
    ),
    "browser_control": (
        "search",
        "go_to",
        "get_text",
        "get_url",
        "list_tabs",
        "active_tab",
        "screenshot",
    ),
    "computer_control": ("running_apps", "system_snapshot", "active_window"),
    # Launching these edits Era's own code and system prompt. classify_action rates
    # them LOW-with-no-confirmation because the user's spoken "launch Protocol One"
    # IS the confirmation — true in a live turn, not true weeks later on a timer,
    # where the only thing left is a stored fingerprint. Same reasoning as
    # self_improve/dev_agent above; the user can still waive it per task.
    "protocol_one": ("status", ""),
    "protocol_zero": ("status", ""),
}
NOTIFY_LEVELS = ("all", "failures", "silent")
# What a CHAIN does when one of its steps fails. "stop" is the default because a
# routine is usually ordered for a reason: the later links may assume the earlier
# ones worked. Either way the failure is reported before anything else runs.
ON_ERROR_MODES = ("stop", "continue")
# Conditional tasks ("check every 5 min UNTIL the download is done"). A condition
# runs a READ-ONLY check each fire; how its result is judged is the operator, and
# what happens once met is on_met. "changed" needs a stored baseline, so its first
# evaluation is never "met".
CONDITION_OPERATORS = ("truthy", "falsy", "contains", "not_contains", "equals", "changed")
# stop = terminate the recurring task once the condition is first met ("until X");
# continue = keep the schedule, running the action every fire the condition holds
# ("each day at 9, IF it is raining"). continue is the default: it never silently
# retires a recurring schedule, which is the more surprising failure.
CONDITION_ON_MET = ("stop", "continue")
# A chain is gated, approved and reported as ONE unit, so it is bounded: an
# unbounded chain would be an unattended script with a single yes behind it.
MAX_CHAIN_STEPS = 8
_RISK_ORDER = {"SAFE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "BLOCKED": 4}


class TaskError(ValueError):
    """A refusal or a bad request; str(e) is meant for the user."""


class ApprovalRequired(TaskError):
    """The approval gate HELD: the task needs the user's own words before it runs.

    A subclass, not a message pattern, so callers can tell "the gate worked" from
    "the scheduler broke" without matching on words — the classification mistake
    that made every refusal an ERROR and fed the auto-report loop (2026-09-21).
    """


# ── storage ──────────────────────────────────────────────────────────────────
def tasks_dir() -> Path:
    """Overridable so tests never touch the real runtime/scheduled_tasks."""
    override = os.environ.get("ERA_SCHEDULED_TASKS_DIR", "").strip()
    return Path(override) if override else _REPO_ROOT / "runtime" / "scheduled_tasks"


def load() -> dict:
    try:
        data = json.loads((tasks_dir() / "tasks.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    """Atomic (temp file + os.replace): a torn store would lose every task."""
    d = tasks_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="tasks", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, d / "tasks.json")
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def build_trigger(spec: dict):
    """APScheduler trigger for a core/schedule_parser spec (imported lazily)."""
    kind = spec["type"]
    if kind == "date":
        from apscheduler.triggers.date import DateTrigger

        return DateTrigger(run_date=datetime.fromisoformat(spec["run_at"]))
    if kind == "interval":
        from apscheduler.triggers.interval import IntervalTrigger

        start = datetime.fromisoformat(spec["start_at"]) if spec.get("start_at") else None
        return IntervalTrigger(seconds=int(spec["seconds"]), start_date=start)
    from apscheduler.triggers.cron import CronTrigger

    fields = ("minute", "hour", "day", "month", "day_of_week")
    return CronTrigger(**{f: str(spec.get(f, "*")) for f in fields})


def strip_args(args: dict | None) -> dict:
    return {k: v for k, v in dict(args or {}).items() if k not in STRIPPED_ARGS}


def norm_action(args: dict | None) -> str:
    """The ONE way an action name is normalised, everywhere it is compared.

    The dispatchers that run a tool normalise with .lower().strip(); anything that
    DECIDES whether that call is allowed must normalise identically or the two
    disagree. They did: a step with action="execute " was invisible to the voice
    gate here and a live Protocol Zero execute at dispatch, so a chain the user
    authorised for Protocol One carried an unauthorised Protocol Zero. Never
    compare a raw args["action"] again — call this.
    """
    return str((args or {}).get("action") or "").strip().lower()


# ── chains ───────────────────────────────────────────────────────────────────
def normalize_steps(raw) -> list[dict]:
    """Validate a chain's steps into [{tool, args, label}]. Every step is cleaned
    exactly like a single task's args: no link may carry a pre-made confirmation."""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, (list, tuple)):
        raise TaskError("steps must be a list of {tool, args} objects.")
    if len(raw) > MAX_CHAIN_STEPS:
        raise TaskError(
            f"A chained task runs at most {MAX_CHAIN_STEPS} steps; that one has {len(raw)}."
        )
    steps = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise TaskError(f"Step {i} must be an object with a 'tool' and its 'args'.")
        tool = str(item.get("tool") or "").strip()
        if not tool:
            raise TaskError(f"Step {i} has no tool. Name the Era tool that link calls.")
        args = item.get("args") or {}
        if not isinstance(args, dict):
            raise TaskError(f"Step {i}'s args must be a JSON object.")
        step = {
            "tool": tool,
            "args": strip_args(args),
            "label": str(item.get("label") or "").strip()[:60],
        }
        try:
            run_if = normalize_run_if(item.get("run_if"))
        except TaskError as exc:
            raise TaskError(f"Step {i} run_if: {exc}") from exc
        if run_if:  # stored only when present, so pre-run_if chains keep their fingerprint
            step["run_if"] = run_if
        steps.append(step)
    return steps


def steps_of(task: dict) -> list[dict]:
    """Any task's action as an ordered list: a chain's steps, or its one tool call.
    Everything downstream (risk, running, reporting) works off this, so a chain and
    a single call travel the same code path."""
    if task.get("kind") == "reminder":
        return []
    steps = task.get("steps") or []
    if steps:
        out = []
        for s in steps:
            step = {
                "tool": s["tool"],
                "args": dict(s.get("args") or {}),
                "label": s.get("label", ""),
            }
            if s.get("run_if"):
                step["run_if"] = s["run_if"]
            out.append(step)
        return out
    if task.get("tool"):
        return [{"tool": task["tool"], "args": dict(task.get("args") or {}), "label": ""}]
    return []


# ── safety ───────────────────────────────────────────────────────────────────
def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def fingerprint(task: dict) -> str:
    """Identity of WHAT a task does (action only). `steps` joins the digest only when
    the task HAS steps, so a single-call task written before chains existed keeps the
    fingerprint its waiver and Protocol authorization were bound to."""
    payload = {k: task.get(k) for k in ("kind", "tool", "args", "message")}
    if task.get("steps"):
        payload["steps"] = task["steps"]
    return _digest(payload)


def waiver_fingerprint(task: dict) -> str:
    """What a waiver covers: the action AND how often / how quietly it runs. A waived
    weekly report re-timed to every minute with notify=failures is a different grant."""
    sched = {
        k: v for k, v in (task.get("schedule") or {}).items() if k not in ("when", "description")
    }
    payload = {"action": fingerprint(task), "schedule": sched, "notify": task.get("notify")}
    # Only a non-default failure policy joins the digest: "stop" is what a task
    # without the field already does, so existing waivers survive this feature.
    if (task.get("on_error") or "stop") != "stop":
        payload["on_error"] = task["on_error"]
    # Same rule for the catch-up window: it lets a waived task run at launch time.
    if task.get("catch_up_until"):
        payload["catch_up_until"] = task["catch_up_until"]
    # A condition changes WHEN the action runs, so it scopes the grant: a yes for
    # "email me when the download finishes" is not a yes for emailing every hour.
    # Joins only when present, so tasks written before conditions keep their waiver.
    if task.get("condition"):
        payload["condition"] = task["condition"]
    return _digest(payload)


def assess_step(tool: str, args: dict | None) -> dict:
    """One action's risk: the same classify_action the live session uses, plus the
    unattended review. Called at creation AND again immediately before the step runs."""
    from core.safety_guard import classify_action

    args = dict(args or {})
    safety = classify_action(tool, args)
    exempt = UNATTENDED_REVIEW_TOOLS.get(tool)
    action = norm_action(args)
    if (
        exempt is not None
        and action not in exempt
        and safety["risk_level"] != "BLOCKED"
        and not safety["requires_confirmation"]
    ):
        return {
            "risk_level": "MEDIUM",
            "requires_confirmation": True,
            "reason": f"{tool} runs unattended with open-ended effects",
        }
    return safety


def assess(task: dict) -> dict:
    """A whole task's risk. A CHAIN is gated as one unit at its WORST step and needs
    confirmation if ANY step does: the user approves the entire sequence or none of
    it, so a harmless first link can never carry a risky later one past the gate."""
    if task.get("kind") == "reminder":
        return {"risk_level": "LOW", "requires_confirmation": False, "reason": "Reminder."}
    steps = steps_of(task)
    if not steps:
        return {"risk_level": "LOW", "requires_confirmation": False, "reason": "Nothing to run."}
    verdicts = [assess_step(s["tool"], s["args"]) for s in steps]
    if len(verdicts) == 1:
        return verdicts[0]
    worst = max(range(len(verdicts)), key=lambda i: _RISK_ORDER.get(verdicts[i]["risk_level"], 0))
    return {
        "risk_level": verdicts[worst]["risk_level"],
        "requires_confirmation": any(v["requires_confirmation"] for v in verdicts),
        "reason": (
            f"step {worst + 1} of {len(steps)} ({steps[worst]['tool']}): "
            f"{verdicts[worst]['reason']}"
        ),
    }


def waiver_valid(task: dict) -> bool:
    waiver = task.get("waiver") or {}
    return bool(waiver) and waiver.get("fingerprint") == waiver_fingerprint(task)


def make_waiver(task: dict) -> dict:
    return {"fingerprint": waiver_fingerprint(task), "granted": now().isoformat()}


def voice_authorized(task: dict) -> bool:
    """A Protocol launch authorization covers exactly the action it was given for."""
    return bool(task.get("voice_authorization")) and task["voice_authorization"] == fingerprint(
        task
    )


def authorizes(grant, tool: str) -> bool:
    """Read a voice grant, which is either a bool (every gated tool in the task) or the
    list of tools the user actually named."""
    if isinstance(grant, bool) or grant is None:
        return bool(grant)
    return tool in grant


def gated_tools(task: dict) -> list[str]:
    """The tools in this task whose action needs the user to have named the Protocol."""
    return [
        s["tool"]
        for s in steps_of(task)
        if VOICE_GATED_ACTIONS.get(s["tool"]) == norm_action(s["args"])
    ]


def granted_tools(task: dict, grant) -> list[str]:
    return [t for t in gated_tools(task) if authorizes(grant, t)]


def voice_authorized_for(task: dict, tool: str) -> bool:
    """Does the user's recorded command cover THIS tool's gated action in THIS task?

    A chain holds several tool calls behind one grant, and "launch Protocol One" must
    never answer for a Protocol Zero link beside it — so the grant records WHICH tools
    were named, not just that something was. A task stored before per-tool grants
    existed has exactly one call, where the grant is unambiguous.
    """
    if not voice_authorized(task):
        return False
    named = task.get("voice_authorized_tools")
    if named is None:
        return len(steps_of(task)) == 1
    return tool in named


# ── conditions ─────────────────────────────────────────────────────────────
def _normalize_check(raw, label: str, *, allow_changed: bool) -> dict | None:
    """Validate the shared part of a condition — {check:{tool,args}, operator, value}.

    The check is a tool call run unattended each fire, so it must be READ-ONLY:
    anything the safety guard would gate, block or rate above LOW is refused here,
    at creation, rather than silently parking on every poll. `label` names the field
    in error messages ('condition' vs 'run_if'). A step run_if has no persistent
    baseline between fires, so its 'changed' operator is refused (allow_changed).
    """
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        if raw.strip().lower() in ("off", "none", "clear", "no", "false"):
            return None
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise TaskError(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaskError(f"{label} must be a JSON object with a 'check'.")
    check = raw.get("check")
    if not isinstance(check, dict):
        raise TaskError(f"{label}.check must be an object with a 'tool' and its 'args'.")
    tool = str(check.get("tool") or "").strip()
    if not tool:
        raise TaskError(f"{label}.check needs a 'tool' — the read-only check to run each time.")
    if tool in UNSCHEDULABLE_TOOLS:
        raise TaskError(f"'{tool}' can't be a {label} check.")
    if not isinstance(check.get("args") or {}, dict):
        raise TaskError(f"{label}.check.args must be a JSON object.")
    args = strip_args(check.get("args"))
    operator = str(raw.get("operator") or "truthy").strip().lower()
    if operator not in CONDITION_OPERATORS:
        raise TaskError(f"{label}.operator must be one of {', '.join(CONDITION_OPERATORS)}.")
    if operator == "changed" and not allow_changed:
        raise TaskError(
            "'changed' needs a persistent baseline; not available for a per-step run_if."
        )
    value = str(raw.get("value") or "").strip()
    if operator in ("contains", "not_contains", "equals") and not value:
        raise TaskError(f"{label}.operator '{operator}' needs a 'value' to compare against.")
    verdict = assess_step(tool, args)
    if (
        verdict["risk_level"] == "BLOCKED"
        or verdict["requires_confirmation"]
        or _RISK_ORDER.get(verdict["risk_level"], 0) > _RISK_ORDER["LOW"]
    ):
        raise TaskError(
            f"A {label} check must be a read-only action; '{tool}' is not "
            f"({verdict.get('reason', 'it changes something or needs confirmation')})."
        )
    return {"check": {"tool": tool, "args": args}, "operator": operator, "value": value}


def normalize_condition(raw) -> dict | None:
    """A whole-task condition: {check, operator, value, on_met}. Its check runs each
    fire BEFORE the action's safety gate; the action runs only when met, and with
    on_met="stop" the task then retires ("check every 5 min UNTIL the download is done")."""
    core = _normalize_check(raw, "condition", allow_changed=True)
    if core is None:
        return None
    raw_dict = json.loads(raw) if isinstance(raw, str) else raw
    on_met = str((raw_dict or {}).get("on_met") or "continue").strip().lower()
    if on_met not in CONDITION_ON_MET:
        raise TaskError(f"condition.on_met must be one of {', '.join(CONDITION_ON_MET)}.")
    return {**core, "on_met": on_met}


def normalize_run_if(raw) -> dict | None:
    """A per-step run_if: {check, operator, value}. Its check runs immediately before
    that ONE chain step; not met → the step is SKIPPED and the chain moves on (a
    conditional skip is never a failure, so it never triggers on_error). No on_met — a
    step gates only itself, it cannot retire the task."""
    return _normalize_check(raw, "run_if", allow_changed=False)


def condition_met(output: dict, condition: dict, previous: str | None) -> bool:
    """Judge one check result against the condition. Pure, so the comparison is
    testable without a live tool runtime. `previous` is the last check message the
    task saw, used only by the 'changed' operator (which needs a baseline first)."""
    success = bool((output or {}).get("success"))
    message = str((output or {}).get("message") or "")
    op = condition["operator"]
    val = condition.get("value", "")
    if op == "truthy":
        return success
    if op == "falsy":
        return not success
    low = message.lower()
    if op == "contains":
        return val.lower() in low
    if op == "not_contains":
        return val.lower() not in low
    if op == "equals":
        return low.strip() == val.lower().strip()
    if op == "changed":
        return previous is not None and message != previous
    return False


def describe_condition(condition: dict | None) -> str:
    """A short human phrase for a condition, for confirmations and listings."""
    if not condition:
        return ""
    check = condition.get("check") or {}
    op, val = condition["operator"], condition.get("value", "")
    what = describe_step(check) if check.get("tool") else "the check"
    phrase = {
        "truthy": f"{what} succeeds",
        "falsy": f"{what} fails",
        "contains": f"{what} reports '{val}'",
        "not_contains": f"{what} stops reporting '{val}'",
        "equals": f"{what} is exactly '{val}'",
        "changed": f"{what} changes",
    }.get(op, what)
    tail = ", then stop" if condition.get("on_met") == "stop" else ""
    return f"only when {phrase}{tail}"


def _validate_one(tool: str, args: dict, grant) -> None:
    if not tool:
        raise TaskError("Which tool should the task run?")
    if tool in UNSCHEDULABLE_TOOLS:
        raise TaskError(f"'{tool}' can't be scheduled: it needs you present when it runs.")
    gated = VOICE_GATED_ACTIONS.get(tool)
    if gated and norm_action(args) == gated and not authorizes(grant, tool):
        name = tool.replace("_", " ").title()
        raise TaskError(f"{name} can only be scheduled when you name it yourself when scheduling.")


def validate_action(
    kind: str,
    tool: str,
    args: dict,
    message: str,
    voice_authorized,
    steps: list[dict] | None = None,
):
    """`voice_authorized` is a bool or the list of Protocol tools the user named."""
    if kind == "reminder":
        if not message.strip():
            raise TaskError("A reminder needs a message.")
        return
    if kind == "chain":
        steps = steps or []
        if len(steps) < 2:
            raise TaskError(
                "A chain runs at least two steps in order; schedule a single tool call otherwise."
            )
        for i, step in enumerate(steps, 1):
            try:
                _validate_one(step["tool"], step["args"], voice_authorized)
            except TaskError as exc:
                raise TaskError(f"Step {i}: {exc}") from exc
        return
    if kind != "tool":
        raise TaskError("kind must be 'tool', 'chain' or 'reminder'.")
    _validate_one(tool, args, voice_authorized)


# Arguments that decide whether an action is safe. They are shown FIRST and are
# never the ones dropped: dict order is whatever the model emitted, so padding a
# delete with four filler args used to push `path` out of the approval prompt and
# leave the user approving a deletion without hearing what was being deleted.
_DECISIVE_ARGS = (
    "path",
    "destination",
    "target",
    "to",
    "recipient",
    "url",
    "query",
    "value",
    "command",
    "goal",
)
_SHOWN_ARGS = 4


def describe_step(step: dict) -> str:
    args = dict(step.get("args") or {})
    action = str(args.pop("action", "") or "")
    keys = [k for k in _DECISIVE_ARGS if k in args] + [k for k in args if k not in _DECISIVE_ARGS]
    shown = ", ".join(f"{k}={args[k]}" for k in keys[:_SHOWN_ARGS])
    if len(keys) > _SHOWN_ARGS:
        shown += f", +{len(keys) - _SHOWN_ARGS} more"
    base = f"{step['tool']}{'.' + action if action else ''}({shown})"
    if step.get("run_if"):
        base += f" [{describe_condition(step['run_if'])}]"
    return base


def describe_action(task: dict) -> str:
    if task.get("kind") == "reminder":
        return f"remind you: {task.get('message', '')}"
    steps = steps_of(task)
    if not steps:
        return "nothing"
    if len(steps) == 1:
        return describe_step(steps[0])
    return f"{len(steps)} steps in order: " + " then ".join(describe_step(s) for s in steps)


# ── lookup ───────────────────────────────────────────────────────────────────
def _describe_state(task: dict) -> str:
    """Why a task is not the one being asked for — the fact that answers 'so what
    now?', so the caller has something true to tell the user instead of retrying."""
    if task.get("finished"):
        return "it has already finished"
    if not task.get("enabled"):
        return "it is paused"
    if task.get("next_run"):
        return f"it next runs {task['next_run']}"
    return "it is not scheduled to run"


_NAME_STOPWORDS = frozenset({"a", "an", "and", "the", "of", "my", "task"})


def _name_key(text: str) -> str:
    """Spelling-insensitive form of a task name: "morning routine", "morning_routine"
    and "MorningRoutine" all become "morningroutine". Tasks are stored snake_case or
    CamelCase but spoken with spaces, so a raw substring test missed every one of
    them and the model burned retries guessing ids (2026-09-27 "morning routine",
    "ResearchAndInnovation", "research innovation" vs `research_innovation`)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    words = re.findall(r"[a-z0-9]+", spaced.lower())
    return "".join(w for w in words if w not in _NAME_STOPWORDS)


def _match(pool: list[dict], ref: str) -> list[dict]:
    """Tasks in `pool` that `ref` names, strictest tier first; [] when none do."""
    low, key = ref.lower(), _name_key(ref)
    tiers = (
        lambda t: t["name"].lower() == low,
        lambda t: bool(key) and _name_key(t["name"]) == key,
        lambda t: low in t["name"].lower(),
        lambda t: bool(key) and key in _name_key(t["name"]),
    )
    partial: list[dict] = []
    for i, tier in enumerate(tiers):
        hits = [t for t in pool if tier(t)]
        if len(hits) == 1 or (hits and i < 2):
            return hits
        partial = partial or hits
    return partial


def _rejected_match(data: dict, ref: str, predicate) -> dict | None:
    """The task `ref` names when the predicate is IGNORED: a real task that is merely
    in the wrong state, resolved exactly as `find` would resolve it."""
    if predicate is None:
        return None
    if ref in data:
        return data[ref]
    hits = _match(list(data.values()), ref)
    return hits[0] if len(hits) == 1 else None


def find(data: dict, ref: str | None, *, predicate=None, requires: str = "") -> dict:
    """By id, exact name, or a unique partial name. Ambiguity is asked, never guessed.

    `predicate` narrows the search and `requires` names what it demands ("awaiting
    approval"). A ref that matches a REAL task the predicate rejected is reported as
    exactly that, never as "no scheduled task matches" — that falsehood sent the model
    hunting by id, then by name, then to waive_confirmation, turning one task that was
    simply not parked yet into a three-deep failure streak (2026-09-21 14:17).
    """
    pool = [t for t in data.values() if predicate is None or predicate(t)]
    ref = (ref or "").strip()
    if not ref:
        if len(pool) == 1:
            return pool[0]
        if not pool:
            raise TaskError(
                f"No scheduled task is {requires}." if requires else "There are no scheduled tasks."
            )
        names = ", ".join(f"'{t['name']}'" for t in pool)
        raise TaskError(f"Which task? Candidates: {names}.")
    if ref in data and (predicate is None or predicate(data[ref])):
        return data[ref]
    hits = _match(pool, ref)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        rejected = _rejected_match(data, ref, predicate)
        if rejected is not None:
            need = requires or "available"
            raise TaskError(
                f"'{rejected['name']}' exists but is not {need}: {_describe_state(rejected)}."
            )
        # Name the real tasks so the caller can pick one instead of guessing again.
        known = ", ".join(f"'{t['name']}'" for t in pool) or "none"
        raise TaskError(f"No scheduled task matches '{ref}'. Existing tasks: {known}.")
    names = ", ".join(f"'{t['name']}'" for t in hits)
    raise TaskError(f"'{ref}' matches several tasks: {names}. Which one?")
