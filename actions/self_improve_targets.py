"""actions/self_improve_targets.py — feature → file resolution for self_improve.

"Improve your vision" must find core/vision_*.py without the user (or the
model) knowing the repo layout. Matching is on module NAME tokens only —
content search would match half the codebase for words like "search".

Pure ranking: never decides whether a file may be written. That is
_resolve_target / resolve_request in actions/self_improve.py.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# "app" was the missing one: 43 modules, including the entire HUD, invisible to
# this resolver until 2026-09-29. Kept as an explicit allowlist rather than
# "everything the manifest knows" so tools/ (the test suite), memory/ and
# config/ stay out — see _manifest_candidates.
_SOURCE_DIRS = ("actions", "app", "core", "agent", "src", "utils", "windows")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]+")
_STOPWORDS = frozenset(
    """the a an my your her his its our to of in on for and or is are be it that this
    with when can should must more less era one self own code file files module
    fix fixes fixing improve improves improving improved make making better best
    add adds adding added feature features new please sir you yourself bug error
    errors issue issues problem problems broken failing fails update updating
    change changes handling handle logic support way""".split()
)


def _manifest_candidates() -> list[Path]:
    """Era's modules as the generated index records them, or [] if unavailable.

    WHY THE MANIFEST AND NOT A DIRECTORY WALK (board item 26)
    The walk below globs `*.py` one level deep in a hand-kept list of
    directories. `app/` was not in that list and the glob does not recurse, so
    all 43 modules under `app/` — the entire HUD, the live-session layer, the
    tool dispatcher — were invisible to this resolver. Asked to "modify the
    HUD" on 2026-09-29, it could not see a single HUD file and answered with
    `actions/open_app.py`.

    `runtime/manifest.json` is generated from the code by `tools/era_manifest.py`
    and the gate regenerates it, so a new package becomes reachable by voice
    without anyone remembering to edit a tuple here. The walk is kept as a
    fallback: a missing or unreadable manifest must degrade this to the old
    behaviour, never to "no candidates at all".
    """
    try:
        import json

        data = json.loads((REPO_ROOT / "runtime" / "manifest.json").read_text(encoding="utf-8"))
        entries = data.get("modules")
        if not isinstance(entries, list):
            return []
    except Exception:
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("is_test"):
            continue  # is_test is the manifest's own call — better than guessing by path
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel.endswith(".py") or rel.endswith("__init__.py"):
            continue
        parts = Path(rel).parts
        # A leading-underscore PACKAGE is private implementation — today that is
        # actions/_netflix ("Internal implementation package for
        # actions/netflix_control.py", 4 files). Listing those as candidates for
        # "fix the netflix search" pushes the module the user means off the
        # answer. They stay editable by explicit file_path; this is only about
        # what a DESCRIPTION resolves to.
        if any(part.startswith("_") for part in parts[:-1]):
            continue
        top = parts[0] if len(parts) > 1 else ""
        # memory/, config/ and scripts/ are state, secrets and one-shots, and
        # none was ever a candidate. Widening WHAT Era can be pointed at is a
        # separate decision from fixing what she can SEE; _resolve_target
        # refuses most of them anyway.
        if top and top not in _SOURCE_DIRS:
            continue
        out.append(REPO_ROOT / rel)
    return out


def _candidate_files() -> list[Path]:
    if from_manifest := _manifest_candidates():
        return from_manifest
    files = [p for p in REPO_ROOT.glob("*.py")]
    for d in _SOURCE_DIRS:
        root = REPO_ROOT / d
        if root.is_dir():
            files += [p for p in root.glob("*.py") if p.name != "__init__.py"]
    return files


def _name_tokens(path: Path) -> set[str]:
    """The words a user could plausibly use for this module: its filename, plus
    any SUB-package it sits in.

    `app/hud/api.py` is "hud" and "api" — without the directory, the word "HUD"
    matches nothing in this repo, which is exactly what happened on 2026-09-29.
    The TOP-level directory is deliberately excluded: "app", "core" and
    "actions" would otherwise each hand a free point to a hundred files and
    drown the specific word the user actually said.
    """
    try:
        parts = path.relative_to(REPO_ROOT).parts
    except ValueError:
        parts = path.parts
    tokens = set(path.stem.lower().split("_"))
    for part in parts[1:-1]:  # sub-packages only
        tokens |= set(part.lower().split("_"))
    return tokens


def find_target_files(text: str, limit: int = 8) -> list[tuple[int, str]]:
    """Rank Era's own modules against a natural-language feature description.

    Score = number of distinct description tokens that appear in the module
    name ('vision' hits every core/vision_*.py). Returns [(score, rel_path)],
    best first, only scored > 0.
    """
    tokens = {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS}
    tokens |= {t.rstrip("s") for t in tokens if len(t) > 3}
    if not tokens:
        return []
    ranked: list[tuple[int, str]] = []
    for f in _candidate_files():
        score = len(tokens & _name_tokens(f))
        if score:
            ranked.append((score, f.relative_to(REPO_ROOT).as_posix()))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return ranked[:limit]
