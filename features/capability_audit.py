"""Structured, truthful capability assessment for planning.

This reloadable feature builds on :mod:`core.capability_index`, the existing
source of truth for tools actually offered to Era after runtime gates are
applied. It never upgrades a related tool into proof that a requested outcome
is supported. The result is deliberately structured so callers can inspect a
set of goals before promising work.

Registration and invocation are provided by Era's feature loader: this module's
``FEATURE`` metadata is discovered automatically and its ``run(arguments)``
entry point is called through ``run_feature`` after the normal confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping

from core import capability_index

FEATURE = {
    "id": "capability_audit",
    "description": (
        "Assess one or more requested goals against Era's currently offered tools, "
        "returning truthful related, unsupported, or unavailable findings."
    ),
}

_MAX_GOALS = 20
_MAX_GOAL_CHARS = 500
_MAX_INVENTORY = 100


def _error(message: str) -> dict:
    return {"ok": False, "feature": FEATURE["id"], "error": message}


def _goals(arguments: Mapping) -> tuple[list[str], int] | str:
    """Validate, normalize, and case-insensitively deduplicate requested goals."""
    raw = arguments.get("goals")
    if raw is None and "goal" in arguments:
        raw = [arguments.get("goal")]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return "Provide 'goal' as text or 'goals' as a list of text values."

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return "Every goal must be text."
        goal = " ".join(item.split())
        if not goal:
            continue
        if len(goal) > _MAX_GOAL_CHARS:
            return f"Each goal must be at most {_MAX_GOAL_CHARS} characters."
        key = goal.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(goal)

    if not normalized:
        return "Provide at least one non-empty goal."
    omitted = max(0, len(normalized) - _MAX_GOALS)
    return normalized[:_MAX_GOALS], omitted


def _verdict(evidence: str) -> str:
    """Map the capability index's intentionally explicit prose to stable data."""
    if evidence.startswith("RELATED TOOLS FOUND"):
        return "related_not_confirmed"
    if evidence.startswith(("NOT CONFIRMED", "NOT SUPPORTED")):
        return "unsupported"
    if evidence.startswith("I could not read my own capability list"):
        return "unavailable"
    return "unavailable"


def _inventory(arguments: Mapping) -> dict:
    raw_limit = arguments.get("limit", 0)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        return _error("'limit' must be an integer from 0 to 100; 0 lists all tools.")
    if raw_limit < 0 or raw_limit > _MAX_INVENTORY:
        return _error("'limit' must be an integer from 0 to 100; 0 lists all tools.")
    return {
        "ok": True,
        "feature": FEATURE["id"],
        "mode": "inventory",
        "inventory": capability_index.summary(limit=raw_limit),
    }


def run(arguments) -> dict:
    """Run a capability inventory or a structured multi-goal assessment.

    Accepted arguments::

        {"mode": "assess", "goal": "play music"}
        {"mode": "assess", "goals": ["play music", "book a flight"]}
        {"mode": "inventory", "limit": 25}

    ``related_not_confirmed`` means only that a real tool is in the same area.
    It is never represented as support, preserving capability_index's refusal
    to infer a capability from token overlap alone.
    """
    if not isinstance(arguments, Mapping):
        return _error("Arguments must be an object.")

    mode = arguments.get("mode", "assess")
    if mode == "inventory":
        return _inventory(arguments)
    if mode != "assess":
        return _error("Unsupported mode. Use 'assess' or 'inventory'.")

    parsed = _goals(arguments)
    if isinstance(parsed, str):
        return _error(parsed)
    goals, omitted = parsed

    assessments = []
    counts = {"related_not_confirmed": 0, "unsupported": 0, "unavailable": 0}
    for goal in goals:
        try:
            evidence = capability_index.check(goal)
        except Exception as exc:  # Defensive: an audit must report failure, not guess.
            evidence = (
                "I could not read my own capability list "
                f"({type(exc).__name__}). Do not claim that this goal is supported."
            )
        verdict = _verdict(evidence)
        counts[verdict] += 1
        assessments.append({"goal": goal, "verdict": verdict, "evidence": evidence})

    return {
        "ok": True,
        "feature": FEATURE["id"],
        "mode": "assess",
        "assessments": assessments,
        "counts": counts,
        "omitted": omitted,
        "guidance": (
            "A related tool is not confirmation. Promise a goal only when its declared "
            "summary plainly covers the requested outcome."
        ),
    }
