"""actions/self_improve_locator.py — "where does this live, and where would it go?"

The read-only half of self-improvement. `self_improve action=locate` answers, in
plain words Era can speak or act on:

  * which AREA of her code a request belongs to (HUD, vision, memory, ...),
  * which existing FILE to edit, and whether the answer is decisive,
  * for a brand-new ability, the files a new one must touch to actually work.

WHY IT EXISTS. `find` ranks filenames only, so "improve the HUD interface"
(one shared word with `app/hud/api.py`) came back as a list and Era asked the
user for a filename they could not know. This adds two more signals the repo
already has: the area (`runtime/manifest.json` -> "subsystems", the same
hand-curated table docs/MANIFEST.md prints) and each module's own one-line
description. Narrowing to the area first is what stops a HUD request being
answered with a file from `actions/` that shares the word "open".

CONTRACT. Never writes, never decides whether a file may be edited — that stays
`_resolve_target` / `resolve_request` in actions/self_improve.py, which this
asks (lazily; the import is circular) so its answer can never contradict the
real gate. A "decisive" pick is advice: the caller still goes through apply,
backup, verification and the safety-critical confirmation.
"""

from __future__ import annotations

import json
import re

from actions.self_improve_targets import (
    _SOURCE_DIRS,
    REPO_ROOT,
    _name_tokens,
    description_tokens,
)

# A pick is decisive only with a lead over the runner-up AND two filename words
# (see decisive). The bare feature name scores 1 on every HUD file, so it can
# never be decisive however the descriptions fall.
_DECISIVE_MIN_NAME_SCORE = 2
_DECISIVE_MIN_MARGIN = 1
_DOC_POINTS_CAP = 2  # a long description must not outvote the filename

# "add a new tool", "create an ability", "give her a skill" — the noun must sit
# within a few words of the verb. "action" is NOT a noun here: "add error
# handling to the click action" is an edit, not a new ability.
_NEW_THING_RE = re.compile(
    r"\b(new|add|create|build|make|give)\b(\W+\w+){0,4}?\W+"
    r"(tools?|abilit(?:y|ies)|capabilit(?:y|ies)|commands?|skills?)\b",
    re.IGNORECASE,
)

# What a NEW tool must touch before it exists as far as Era is concerned. Read
# off how `weather_report` and `capability_check` are actually wired, not
# guessed. Order is the order to do it in.
NEW_TOOL_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("actions/<name>.py", "the implementation (new file; module docstring first)"),
    ("app/tool_declarations.py", "the declaration the live model is offered"),
    ("app/era_live/tool_dispatch.py", "import it and add its `elif tool_name == ...` branch"),
    ("agent/executor.py", "so multi-step agent tasks can call it"),
    ("agent/planner.py", "so the planner knows the tool exists"),
    ("core/safety_guard.py", "its risk class — an unlisted tool gets the strict default"),
    (
        "tools/golden/tool_declarations_anthropic.json",
        "regenerate; ANY declaration edit fails test_toolspec",
    ),
    (
        "core/prompt.txt",
        "Protocol Zero re-syncs section 16; add a rule only if behaviour needs one",
    ),
    ("tools/test_<name>.py", "a test, seen to fail before it is trusted"),
)


def _manifest() -> dict:
    try:
        return json.loads((REPO_ROOT / "runtime" / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _subsystem_keywords(sub: dict) -> set[str]:
    """Words that mean this area: its heading, its 'go here when', and the
    sub-package words of its path prefixes. Top-level dirs are NOT words —
    "core", "app" and "actions" would claim every request."""
    words = description_tokens(f"{sub.get('name', '')} {sub.get('when', '')}")
    for prefix in sub.get("prefixes", []):
        parts = [p for p in re.split(r"[/_.]", prefix.lower()) if p]
        words |= {p for p in parts if p not in {"app", "core", "actions", "py", "tools", "ui"}}
        if prefix.startswith("ui"):
            words.add("ui")
    return words


def pick_area(description: str, subsystems: list[dict]) -> dict | None:
    """The one area the request names, or None when it names none or several."""
    tokens = description_tokens(description)
    scored = sorted(
        ((len(tokens & _subsystem_keywords(s)), s) for s in subsystems),
        key=lambda x: -x[0],
    )
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None  # a tie is not an answer — fall back to the whole tree
    return scored[0][1]


def _candidates(modules: list[dict], area: dict | None) -> list[dict]:
    out = []
    for m in modules:
        rel = m.get("path", "")
        parts = rel.split("/")
        if m.get("is_test") or not rel.endswith(".py") or rel.endswith("__init__.py"):
            continue
        if any(p.startswith("_") for p in parts[:-1]):
            continue  # private implementation packages are not named by a request
        if len(parts) > 1 and parts[0] not in _SOURCE_DIRS:
            continue
        if area and not any(rel.startswith(p) for p in area["prefixes"]):
            continue
        out.append(m)
    return out


def rank_in_area(description: str, modules: list[dict], area: dict | None) -> list[dict]:
    """Candidates scored on filename words plus words in their own description."""
    tokens = description_tokens(description)
    doc_tokens = set(tokens)
    # Known HUD concepts map to its concrete endpoints; broad button requests
    # still abstain because they do not name one of these responsibilities.
    if "hud" in tokens:
        for term, symbol in {
            "interface": "api",
            "preferences": "settings",
            "health": "status",
            "stream": "events",
        }.items():
            if term in tokens:
                tokens.add(symbol)
    ranked = []
    for m in _candidates(modules, area):
        rel = m["path"]
        name_score = len(tokens & _name_tokens(REPO_ROOT / rel))
        doc_score = min(len(doc_tokens & description_tokens(m.get("doc") or "")), _DOC_POINTS_CAP)
        if name_score or doc_score:
            ranked.append(
                {
                    "path": rel,
                    "doc": (m.get("doc") or "").strip(),
                    "name_score": name_score,
                    "doc_score": doc_score,
                    "total": name_score + doc_score,
                }
            )
    ranked.sort(key=lambda r: (-r["total"], r["path"]))
    return ranked


def decisive(ranked: list[dict]) -> dict | None:
    """A confident pick needs TWO of the request's words in the filename — the
    same floor as resolve_request — and a lead over the runner-up. Descriptions
    only break ties: first draft let a doc line saying "Open ... the HUD" win the
    live 2026-09-29 button request for app/hud/browser.py, the very mistake
    that floor exists to prevent."""
    if not ranked or ranked[0]["name_score"] < _DECISIVE_MIN_NAME_SCORE:
        return None
    runner_up = ranked[1]["total"] if len(ranked) > 1 else 0
    if ranked[0]["total"] - runner_up < _DECISIVE_MIN_MARGIN:
        return None
    return ranked[0]


def _editable_note(rel: str) -> str:
    """Whether self_improve could write this file — asked of the real gate."""
    try:
        from actions.self_improve import SAFETY_CRITICAL, _resolve_target

        path, err = _resolve_target(rel)
        if path is None:
            return f"CANNOT edit: {err}"
        return "editable, needs the user's confirmation" if rel in SAFETY_CRITICAL else "editable"
    except Exception:
        return "editability unknown"


def _new_ability_answer() -> str:
    lines = ["That is a NEW ability, so no existing file is the answer. It needs these, in order:"]
    lines += [f"- {path} — {why}" for path, why in NEW_TOOL_CHECKLIST]
    lines.append(
        "Use autonomous_development propose to create features/<name>.py with FEATURE={id,description} "
        "and run(arguments), plus tools/test_<name>.py. The approved feature is automatically "
        "registered for run_feature; no protected dispatcher or declaration changes are needed. "
        "Full native tool wiring requires a separately reviewed framework change. "
        "Generation and tests run in staging and require final approval before activation."
    )
    return "\n".join(lines)


def _agreed_with_resolver(description: str, modules: list[dict]) -> dict | None:
    """If the real resolver would already say `ok`, locate must say the same —
    two answers to one question is how Era ends up promising what apply refuses."""
    try:
        from actions.self_improve import _rel, resolve_request

        target, status, _msg = resolve_request("", description)
        if status != "ok" or target is None:
            return None
        rel = _rel(target)
        doc = next((m.get("doc") or "" for m in modules if m.get("path") == rel), "")
        return {"path": rel, "doc": doc.strip()}
    except Exception:
        return None


def locate(description: str, limit: int = 5) -> str:
    """Plain-text answer for `self_improve action=locate`. Never raises."""
    description = (description or "").strip()
    if not description:
        return "Tell me what to fix or add and I'll say which of my files it belongs in."
    try:
        data = _manifest()
        modules, subsystems = data.get("modules") or [], data.get("subsystems") or []
        if not modules:
            return (
                "My code index (runtime/manifest.json) is missing, so I can't place that — "
                "run `python tools/era_manifest.py`, or name the file."
            )
        if _NEW_THING_RE.search(description):
            # No existing file is THE answer for something that does not exist
            # yet, and a loose word match ("checks" -> the safety area) sends it
            # somewhere confident and wrong. Give the recipe, not a guess.
            return _new_ability_answer()
        area = pick_area(description, subsystems)
        ranked = rank_in_area(description, modules, area) or rank_in_area(
            description, modules, None
        )
        lines = []
        if area:
            lines.append(f"Area: {area['name']} — {area['when']}.")
        pick = decisive(ranked) or _agreed_with_resolver(description, modules)
        if pick:
            lines.append(
                f"Edit {pick['path']} ({_editable_note(pick['path'])}) — {pick['doc']} "
                f"Next: call self_improve action=apply with file_path='{pick['path']}', "
                "and tell the user which file you chose."
            )
        elif ranked:
            lines.append("No single file is clearly right. Closest, with what each one is for:")
            for r in ranked[:limit]:
                note = _editable_note(r["path"])
                flag = f" [{note}]" if note.startswith("CANNOT") else ""
                lines.append(f"- {r['path']} — {r['doc'] or 'no description'}{flag}")
            lines.append(
                "Pick the one whose description matches the change and apply to it by "
                "file_path, naming your choice to the user. Ask the user only if two "
                "of these are genuinely equal."
            )
        else:
            lines.append("Nothing in my code matches those words.")
        return "\n".join(lines)
    except Exception as e:  # a locator that raises would hide the answer it exists to give
        return (
            f"I couldn't work out where that belongs ({e}). Name the file and I'll go from there."
        )
