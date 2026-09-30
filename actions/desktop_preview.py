"""actions/desktop_preview.py — planning a desktop task, and describing the plan.

Split out of `actions/desktop.py` and `actions/desktop_ops.py` because after the
plan-first confirmation fix these are one concern: planning happens ONLY to
build the preview the user approves. `_run_desktop_task` never plans, so nothing
on the execution path imports this module.

`core/confirmation_preview.py` calls `plan_desktop_preview()` before the pending
confirmation is stored; the plan it returns is carried through the confirmation
and executed verbatim, so the steps rendered here are the steps that run.
"""

from __future__ import annotations

import json

from actions.desktop_ops import (
    DIRECT_ACTIONS,
    MAX_STEPS,
    OP_SPECS,
    WINDOW_ACTIONS,
    OpError,
    _is_unsafe_in_step,
)
from core.ai_provider import GeminiModel


def _plan_desktop_task(task: str) -> list:
    """Ask Gemini which vetted operations to run. Returns a list of steps.

    The model chooses from `DESKTOP_OPS`; it does not write code. An empty list
    means "not doable with these operations", which the caller reports plainly.
    """
    prompt = f"""You are a desktop automation planner.
You cannot write code. You may only choose from the operations below.

Operations:
{OP_SPECS}

Paths: use one of the folder names desktop, documents, downloads, pictures,
music, videos — or a path inside one of them. A relative path resolves inside
the Desktop. Anything outside those folders is rejected, so never try it.
There is no operation that deletes, renames or writes file contents, and none
that moves the mouse or types. Do not invent one.

Return ONLY a JSON array of steps, at most {MAX_STEPS}.
Each step is {{"op": "<operation name>", "args": {{...}}}}.
If the task needs anything not listed above, return exactly: []

Task: {task}"""

    try:
        # Construction inside the try: a client-init failure (bad/absent key,
        # SDK change) is just as much "the planner could not be reached", and
        # outside it escaped as a non-OpError past every handler above.
        model = GeminiModel("gemini-2.5-flash")  # genai.configure() is gone in google-genai 2.x
        raw = (model.generate_content(prompt).text or "").strip()
    except Exception as e:
        raise OpError(f"the planner could not be reached ({e})") from e

    if raw.startswith("```"):
        body = raw.splitlines()[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        raw = "\n".join(body).strip()

    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OpError(f"the planner did not return valid JSON ({e})") from e
    if not isinstance(plan, list):
        raise OpError("the planner did not return a list of steps")
    return plan


# Distinguishes "no plan was carried" (confirmation layer bypassed -> refuse)
# from "planning failed" (None) and "nothing to do" ([]). None of these may
# fall back to planning at execution time.


# ── rendering a plan for the user to approve ─────────────────────────────────
# The point of showing the plan is that the user can check it against what is
# about to happen to their files, so the mutating steps say MOVE/COPY in caps
# and always name both folders. A step whose op is unknown is printed raw
# rather than prettified — it will be rejected by run_plan anyway, and hiding
# it behind a friendly sentence would be the opposite of informing the user.
_STEP_TEMPLATES = {
    "list_dir": "list what is in {path}",
    "find_files": "search {path} for files matching {pattern}",
    "count_files": "count the files matching {pattern} in {path}",
    "folder_stats": "measure {path} (file count and size)",
    "disk_usage": "check free space on the drive holding {path}",
    "make_folder": "CREATE the folder {path}",
    "move_files": "MOVE files matching {pattern} from {source} to {destination}",
    "copy_files": "COPY files matching {pattern} from {source} to {destination}",
    "open_path": "open {path}",
}
_STEP_DEFAULTS = {"path": "the Desktop", "pattern": "everything", "source": "?", "destination": "?"}
# Steps that change the disk. Counted separately so the question can lead with
# the fact that this is not a read-only plan.
MUTATING_OPS = frozenset({"move_files", "copy_files", "make_folder"})


# The confirmation sentence is assembled from MODEL-CHOSEN args, and it is the
# one thing the user checks before saying yes. Unsanitised, a crafted pattern
# could close the plan early and append forged steps — e.g. a `count_files`
# whose pattern contained a newline, "Should I proceed?", and a fake step
# number, so the real MOVE arrived after what looked like the end of the list.
# Spoken aloud that is worse than on screen: the fake terminator is read out
# and the user answers before the real step is reached.
_STEP_ARG_MAX = 60


def _clean_arg(value: object) -> str:
    """Flatten one model-chosen argument to a single short line.

    Line breaks and control characters are what let an argument impersonate the
    surrounding prose, so they go; the cap stops one long argument from pushing
    a real step past the user's attention.
    """
    text = "".join(" " if _is_unsafe_in_step(c) else c for c in str(value))
    text = " ".join(text.split())
    if len(text) > _STEP_ARG_MAX:
        text = text[: _STEP_ARG_MAX - 1] + "…"
    return text or "?"


class _StepArgs(dict):
    def __missing__(self, key):
        return _STEP_DEFAULTS.get(key, "?")

    def __getitem__(self, key):
        return _clean_arg(super().__getitem__(key)) if key in self else self.__missing__(key)


def describe_plan(plan: object) -> str:
    """Render a plan as numbered steps in plain language. Never raises: this is
    used to build a confirmation prompt, and a rendering bug must not become a
    reason the user is asked to approve something with no description."""
    if not isinstance(plan, list) or not plan:
        return ""
    lines = []
    for i, step in enumerate(plan, 1):
        if not isinstance(step, dict):
            lines.append(f"{i}. (malformed step: {step!r})")
            continue
        op = step.get("op")
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        template = _STEP_TEMPLATES.get(op) if isinstance(op, str) else None
        if template is None:
            lines.append(f"{i}. (unrecognised step: {op!r} {args!r})")
            continue
        try:
            lines.append(f"{i}. {template.format_map(_StepArgs(args))}")
        except Exception:
            lines.append(f"{i}. {op} {args!r}")
    return "\n".join(lines)


def plan_touches_disk(plan: object) -> bool:
    """True when any step writes. Read-only plans still need approval, but the
    question should not claim files are about to move when none are."""
    if not isinstance(plan, list):
        return False
    return any(isinstance(s, dict) and s.get("op") in MUTATING_OPS for s in plan)


BULK_FILE_ACTIONS = frozenset({"clean", "organize"})


def _bulk_preview(action: str, parameters: dict) -> tuple[str, dict]:
    """Describe what `clean`/`organize` would move, without moving anything.

    These are `DIRECT_ACTIONS`, so they never reach the planner and carry no
    approved plan — the handler is fixed code. What they DO need is the same
    honesty the planner path now has: they relocate every loose file on the
    Desktop, and were classified LOW with no prompt at all.
    """
    from actions.desktop import clean_archive_name, movable_desktop_items, organize_target_name

    items = movable_desktop_items()
    if not items:
        return ("Sir, there is nothing loose on your Desktop to move.", {})

    n = len(items)
    plural = "file" if n == 1 else "files"
    if action == "clean":
        return (
            f"Sir, I would move {n} {plural} off your Desktop into a new folder "
            f"called '{clean_archive_name()}'. Should I proceed?",
            {},
        )

    mode = str(parameters.get("mode") or "by_type").strip().lower()
    folders = sorted({organize_target_name(i, mode) for i in items})
    shown = ", ".join(folders[:6]) + (f" and {len(folders) - 6} more" if len(folders) > 6 else "")
    fplural = "folder" if len(folders) == 1 else "folders"
    return (
        f"Sir, I would move {n} {plural} on your Desktop into {len(folders)} "
        f"{fplural} ({shown}). Should I proceed?",
        {},
    )


def plan_desktop_preview(parameters: dict) -> tuple[str, dict]:
    """Resolve a `desktop_control` call into a concrete plan BEFORE the user is
    asked to approve it, and describe that plan in words.

    Returns (sentence, {"_approved_plan": plan}). The plan is carried through
    the confirmation and executed verbatim on approval, so what runs is what
    was shown. Planning failures come back as an empty plan with a sentence
    saying so — confirming then produces a refusal, never a fresh plan the user
    never saw.
    """
    action = str(parameters.get("action") or "").lower().strip()
    if action in BULK_FILE_ACTIONS:
        return _bulk_preview(action, parameters)

    task = _task_from(parameters)
    if not task:
        return "", {}

    try:
        plan = _plan_desktop_task(task)
    except Exception as e:
        # Deliberately broad. Anything that escapes here (a quota error, an SDK
        # change, a missing key) used to leave the stored call with no plan at
        # all, and approval then ran a freshly generated one. Carry None so the
        # approved execution refuses instead.
        print(f"[Desktop] Preview planning failed: {e}")
        return (f"Sir, I could not work out how to do that safely: {e}.", {"_approved_plan": None})

    if not plan:
        return (
            "Sir, I can't do that with the desktop operations I'm allowed to run.",
            {"_approved_plan": []},
        )

    steps = describe_plan(plan)
    lead = (
        "Sir, to do that I would make these changes to your files:"
        if plan_touches_disk(plan)
        else "Sir, to answer that I would look at these things:"
    )
    return (f"{lead}\n{steps}\nShould I proceed?", {"_approved_plan": plan})


def _task_from(parameters: dict) -> str:
    """The task string `desktop_control` would plan for these parameters, or ""
    when this call does not reach the planner. Mirrors the dispatch chain."""
    action = str(parameters.get("action") or "").lower().strip()
    task = str(parameters.get("task") or "").strip()
    if action in DIRECT_ACTIONS:
        return ""
    if task:
        return task
    description = str(parameters.get("description") or "").strip()
    if action == "task":
        return description
    if action in WINDOW_ACTIONS:
        return ""
    return action
