"""crew/capabilities.py — the named, scoped operations a bot may be granted.

A bot's yaml lists capability NAMES; this module is the only place they are
implemented, once. There is deliberately no `vault.delete` and no capability that
approves anything: a staged replacement is ARMED here in the shared
PendingConfirmationStore, and only the user's own "yes" (checked in the dispatcher by
`_user_approved_pending`) can apply it.

Every function takes (CapabilityContext, args) and returns a ToolResult. Failures are
observations the bot's model reads — they are not transport errors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import vault_bridge
from core.tool_result import ToolResult
from core.vault_bridge import Caller
from crew.transport import BotSpec, Turn

REPO = Path(__file__).resolve().parent.parent
MAX_HANDOFF_DEPTH = 3
_CHECK_TIMEOUT_S = 180
_MAX_CHECK_FILES = 5
_TEST_PATH = re.compile(r"^tools/test_[A-Za-z0-9_]+\.py$")


@dataclass(frozen=True)
class CapabilityContext:
    spec: BotSpec
    turn: Turn
    caller: Caller
    dispatch: Callable[[str, Turn], ToolResult] | None = None
    review: Callable[[str, str, dict], object] | None = None  # -> NabarReport


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    args: str
    fn: Callable[[CapabilityContext, dict], ToolResult]


CAPABILITIES: dict[str, Capability] = {}


def capability(name: str, description: str, args: str = ""):
    def register(fn):
        CAPABILITIES[name] = Capability(name, description, args, fn)
        return fn

    return register


def caller_for(spec: BotSpec) -> Caller:
    return Caller(
        actor=spec.id,
        brands=None if "*" in spec.brands else frozenset(spec.brands),
        write_zones=frozenset(spec.write_zones),
    )


def describe(names) -> str:
    """The catalog a bot's model is shown: only the capabilities it was granted."""
    lines = [f"- {n}({CAPABILITIES[n].args}): {CAPABILITIES[n].description}" for n in names]
    return "\n".join(lines) if lines else "(none — answer from the task alone)"


def _pick(args: dict, *keys: str) -> dict:
    return {k: args[k] for k in keys if k in args and args[k] not in (None, "")}


# ── vault ───────────────────────────────────────────────────────────────────
@capability("vault.read", "Read one note.", "path|title, brand?")
def _vault_read(ctx, args):
    return vault_bridge.run(
        {"action": "read", **_pick(args, "path", "title", "brand")}, caller=ctx.caller
    )


@capability("vault.search", "Search notes; returns ranked paths with snippets.", "query, limit?")
def _vault_search(ctx, args):
    return vault_bridge.run(
        {"action": "search", **_pick(args, "query", "limit")}, caller=ctx.caller
    )


@capability(
    "vault.create",
    "Create a new note (lands in the Inbox unless your zones allow more).",
    "title, content, brand?, note_type?",
)
def _vault_create(ctx, args):
    return vault_bridge.run(
        {"action": "create", **_pick(args, "title", "content", "brand", "note_type")},
        caller=ctx.caller,
    )


@capability("vault.append", "Append to an existing note.", "path|title, content")
def _vault_append(ctx, args):
    return vault_bridge.run(
        {"action": "append", **_pick(args, "path", "title", "content")}, caller=ctx.caller
    )


@capability(
    "vault.replace_staged",
    "Propose replacing a note's body or one '## section'. Nothing is written: Nabar "
    "reviews the diff and the USER must approve it.",
    "path|title, content, section?",
)
def _vault_replace_staged(ctx, args):
    from core.pending_confirmation import PendingConfirmationBusy, get_shared_store
    from crew.transport import fail

    staged = vault_bridge.stage(
        _pick(args, "path", "title", "content", "section"), caller=ctx.caller
    )
    if not staged.success:
        return staged
    d = staged.data
    # `current` is read lazily, AFTER Nabar's (slow) turn, so a human edit made while he
    # reviews is caught by the base-unchanged check instead of being missed.
    extra = {**d, "current": lambda: vault_bridge.read_text_now(d["rel"])}
    report = ctx.review("vault_replace", d["diff"], extra) if ctx.review else None
    if report is not None:
        status = report.status_for(d["diff"])
        if status in ("paused", "incomplete"):
            vault_bridge.discard_staged(d["plan_id"])
            held = [a.text for a in report.unresolved()] or ["the review is incomplete"]
            return fail(
                "ReviewPaused",
                f"Nabar paused this change ({status}): {held[0]}.",
                review=report.to_dict(),
            )
    try:
        confirmation_id = get_shared_store().store(
            tool="vault_bridge",
            parameters={
                "action": "replace",
                "path": d["rel"],
                "_approved_plan": {"plan_id": d["plan_id"]},
            },
            risk_level="MEDIUM",
            reason=f"{ctx.spec.id} proposes replacing content in {d['rel']}",
        )
    except PendingConfirmationBusy:
        vault_bridge.discard_staged(d["plan_id"])
        return fail("ConfirmationBusy", "Something else is already waiting for the user's yes.")
    spoken = d["summary"] + (" " + report.spoken() if report is not None else "")
    result = fail(
        "ConfirmationRequired",
        spoken,
        diff=d["diff"],
        rel=d["rel"],
        review=report.to_dict() if report is not None else None,
    )
    result.confirmation_id = confirmation_id
    return result


# ── code (Cody proposes and checks; he never writes the repo) ───────────────
@capability(
    "code.propose",
    "Describe how a change to one of Era's own files would be made (read-only).",
    "file_path?, description",
)
def _code_propose(ctx, args):
    try:
        from actions.self_improve import self_improve
    except Exception as exc:  # noqa: BLE001
        return ToolResult.fail(message=f"self_improve unavailable: {exc}", error_type="Unavailable")
    text = self_improve({"action": "propose", **_pick(args, "file_path", "description")})
    return ToolResult.from_any(text)


@capability(
    "code.check",
    "Run up to 5 test files (tools/test_*.py) and report pass/fail. Read-only.",
    "tests: [tools/test_x.py, ...]",
)
def _code_check(ctx, args):
    raw = args.get("tests")
    tests = [raw] if isinstance(raw, str) else list(raw or [])
    if not tests or len(tests) > _MAX_CHECK_FILES:
        return ToolResult.fail(
            message=f"Give 1-{_MAX_CHECK_FILES} test files.", error_type="BadArguments"
        )
    paths = []
    for t in tests:
        rel = str(t).replace("\\", "/").strip()
        if not _TEST_PATH.match(rel) or not (REPO / rel).is_file():
            return ToolResult.fail(
                message=f"Only existing tools/test_*.py files may be run: {rel!r}",
                error_type="PathRefused",
            )
        paths.append(rel)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *paths, "-q", "-x", "-p", "no:cacheprovider"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            # Spawned from the voice app: no console window may flash (see test_no_window).
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ToolResult.fail(
            message=f"Tests exceeded {_CHECK_TIMEOUT_S}s and were stopped.",
            error_type="CheckTimeout",
        )
    tail = [ln for ln in (proc.stdout or "").strip().splitlines()[-8:] if ln.strip()]
    passed = proc.returncode == 0
    return ToolResult.ok(
        message=("Tests passed: " if passed else "Tests failed: ") + " | ".join(tail)[:600],
        data={"passed": passed, "returncode": proc.returncode, "ran": paths, "tail": tail},
    )


# ── coordination ────────────────────────────────────────────────────────────
@capability(
    "crew.handoff",
    "Hand a sub-task to another bot you are allowed to use. Optional `shape` says what the "
    "task needs (picks the model tier): needs_tool_calling, output_verified (JSON/code that "
    "must validate), cost_of_error low|high, context_length_tier short|long.",
    "to, task, shape?",
)
def _handoff(ctx, args):
    from crew.router import coerce_shape
    from crew.transport import fail

    target = str(args.get("to") or "").strip().lower()
    task = str(args.get("task") or "").strip()
    if target not in ctx.spec.handoffs_to:
        return fail("HandoffDenied", f"{ctx.spec.id} may not hand work to {target!r}.")
    if ctx.turn.depth + 1 > MAX_HANDOFF_DEPTH:
        return fail("HandoffDepthExceeded", f"Handoffs are limited to {MAX_HANDOFF_DEPTH} deep.")
    if not task or ctx.dispatch is None:
        return fail("BotError", "A handoff needs a task and a live transport.")
    # A task shape belongs to ONE task: the child never inherits its parent's, and only the
    # valid part of what this handoff names is passed on (unknown keys and values are dropped).
    child_context = {k: v for k, v in ctx.turn.context.items() if k != "task_shape"}
    shape = coerce_shape(args.get("shape"))
    if shape:
        child_context["task_shape"] = shape
    return ctx.dispatch(
        target,
        Turn(
            task=task,
            context=child_context,
            depth=ctx.turn.depth + 1,
            origin=ctx.spec.id,
            deadline=ctx.turn.deadline,  # the same wall-clock budget, not a fresh one
        ),
    )
