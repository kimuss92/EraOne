"""core/lint_feedback.py — turn ruff's output into something an agent can act on.

Era's self-improve pipeline verifies every rewrite with `ruff check`. When that
failed it rolled the file back and the next attempt started from the SAME
context-free prompt — so the same I001 ("imports un-sorted") and F401 ("imported
but unused") came straight back. Retrying a language model against an unchanged
prompt is not self-healing, it is a coin flip.

This module makes the repair deterministic, in that order:

  1. `run_ruff()` parses ruff's JSON diagnostics into `Finding`s — rule code,
     line, message, and whether ruff can fix it *itself*, safely.
  2. `autofix()` lets ruff fix everything in that class. I001 and F401 are
     always in it: sorting imports and deleting a named unused import are
     mechanical edits with exactly one correct answer, and asking a model to
     guess at them is how a rewrite loses a line it needed.
  3. `repair_brief()` / `repair_prompt()` describe only what is LEFT, with a
     rule-specific remediation note per finding — so the repair prompt reads
     "delete the unused import `os` on line 12", never "fix the lint errors".

Boundaries: nothing here chooses a path — the caller passes a target its own
safety layer already vetted, and ruff is only ever pointed at that one file.
Every subprocess failure degrades to "no findings" plus an error string; a
missing or broken ruff must cost the caller lint coverage, never the job.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core.win_subprocess import NO_WINDOW

_RUFF_TIMEOUT_S = 60

# Returned as the error when ruff itself cannot be run. Callers treat this as
# "skip the lint gate", distinct from "ruff ran and something went wrong".
UNAVAILABLE = "unavailable"

_MAX_LISTED_FINDINGS = 25


@dataclass(frozen=True)
class Finding:
    """One ruff diagnostic, flattened to what a repair prompt actually needs."""

    code: str  # "F401"
    line: int
    column: int
    message: str  # "`os` imported but unused"
    rule: str  # "unused-import"
    fixable: bool  # ruff can apply a SAFE fix with no judgement call
    fix_message: str  # "Remove unused import: `os`"
    url: str = ""

    def describe(self) -> str:
        loc = f"line {self.line}" if self.line else "file"
        return f"{self.code} ({self.rule}) at {loc}: {self.message}"


# ── Rule-specific remediation ────────────────────────────────────────────────
# Keyed by exact code first, then by rule-family prefix. Each entry says what to
# DO, in terms of the thing ruff named in its message — never "fix the lint".
#
# On I001: this project has no separate isort. Ruff's `I` rules ARE the isort
# implementation (ruff.toml selects "I"), and `ruff check --fix` applies the
# same ordering isort would. The text below is that ordering, spelled out for
# the case where a model has to re-emit the block by hand.

_ISORT_ORDER = (
    "Ruff's `I` rules are isort — do not hand-sort against a different "
    "convention. Order the import block exactly: (1) `from __future__` "
    "imports, (2) standard library, (3) third-party packages, (4) this "
    "project's own packages (core, actions, agent, app, utils, windows, "
    "tools) — one blank line between groups, none inside a group. Within a "
    "group sort case-insensitively by module name, with plain `import x` "
    "lines before `from x import y` lines. Do not add, remove or re-scope any "
    "import while fixing the ordering."
)

_REMEDIATION: dict[str, str] = {
    "I001": _ISORT_ORDER,
    "F401": (
        "Delete the whole import statement for the name ruff quoted. Do not "
        "invent a use for it and do not leave a bare `import` behind — if the "
        "line imports several names, drop only the unused ones. If the name is "
        "deliberately re-exported, keep it and append `# noqa: F401` to that "
        "line instead."
    ),
    "F811": (
        "The name is defined twice. Delete the earlier definition or import — "
        "the later one is what runs — unless they differ, in which case rename."
    ),
    "F821": (
        "The name ruff quoted is not defined anywhere in this file. Restore the "
        "import or definition that supplied it. Never silence this with a noqa: "
        "at runtime it is a NameError."
    ),
    "F841": (
        "Remove the assignment ruff named. If its right-hand side has side "
        "effects, keep the call and drop the `name =` part; if it is genuinely "
        "unused, delete the line."
    ),
    "E402": (
        "Move that import up with the others at the top of the file. If it must "
        "run late (an env var has to be set first), leave it where it is and add "
        "`# noqa: E402` with a comment saying why."
    ),
    "E711": "Compare to None with `is None` / `is not None`, never `==`.",
    "E712": "Compare to booleans with `if flag:` / `if not flag:`, never `== True`.",
    "E722": (
        "Replace the bare `except:` with `except Exception:` so it stops "
        "swallowing KeyboardInterrupt and SystemExit."
    ),
    "E731": "Bind the lambda with a `def` instead of assigning it to a name.",
    "B006": (
        "A mutable default ([] or {}) is shared across calls. Default to None "
        "and build the real value inside the function body."
    ),
    "B904": (
        "Inside `except`, re-raise with `raise ... from e` (or `from None`) so "
        "the original traceback is not lost."
    ),
}

_FAMILY_REMEDIATION: dict[str, str] = {
    "I": _ISORT_ORDER,
    "F": (
        "A pyflakes correctness rule: act on exactly the name ruff quoted — "
        "remove it, define it, or import it. Do not restructure around it."
    ),
    "E": "A pycodestyle rule. Fix the construct ruff named; do not reflow unrelated lines.",
    "W": "A whitespace rule — `ruff format` settles it; do not hand-edit other lines.",
    "UP": (
        "A pyupgrade rule: this project targets Python 3.12, so use the modern "
        "form ruff suggests (e.g. `X | None` over `Optional[X]`, f-strings over "
        "`.format`)."
    ),
    "B": (
        "A flake8-bugbear rule flagging a real latent bug. Apply the specific "
        "correction in the message rather than suppressing it."
    ),
}


def remediation_for(code: str) -> str:
    """Precise instruction for one rule code, falling back to its family."""
    if code in _REMEDIATION:
        return _REMEDIATION[code]
    prefix = "".join(ch for ch in code if ch.isalpha())
    return _FAMILY_REMEDIATION.get(prefix, "Apply the correction described in ruff's message.")


# ── Running ruff ─────────────────────────────────────────────────────────────


def _run(args: list[str], repo_root: Path) -> subprocess.CompletedProcess | None:
    """Run `python -m ruff ...` from the repo root so ruff.toml applies."""
    try:
        return subprocess.run(
            [sys.executable, "-m", "ruff", *args],
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_S,
            cwd=str(repo_root),
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def parse_diagnostics(raw: str) -> list[Finding]:
    """Parse `ruff check --output-format json` output. Never raises."""
    try:
        items = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") or {}
        fix = item.get("fix") or {}
        # "safe" only. An unsafe fix can change behavior (F401 inside a package
        # __init__ drops a deliberate re-export), so it stays a model decision.
        fixable = isinstance(fix, dict) and fix.get("applicability") == "safe"
        findings.append(
            Finding(
                code=str(item.get("code") or "?"),
                line=int(loc.get("row") or 0),
                column=int(loc.get("column") or 0),
                message=str(item.get("message") or "").strip(),
                rule=str(item.get("name") or "").strip(),
                fixable=fixable,
                fix_message=str(fix.get("message") or "").strip() if fixable else "",
                url=str(item.get("url") or ""),
            )
        )
    return findings


def run_ruff(target: Path, repo_root: Path) -> tuple[list[Finding], str]:
    """Lint one file. Returns (findings, error).

    error is "" on success, UNAVAILABLE when ruff could not be run at all, and
    otherwise a short message. Findings are empty whenever error is set.
    """
    proc = _run(
        ["check", "--no-fix", "--output-format", "json", str(target)],
        repo_root,
    )
    if proc is None:
        return [], UNAVAILABLE
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if "No module named" in stderr and "ruff" in stderr:
        return [], UNAVAILABLE
    if proc.returncode == 0:
        return [], ""
    if not stdout.startswith("["):
        # ruff exits 2 for its own errors (bad config, unreadable file).
        return [], (stderr or stdout or "ruff failed")[:300]
    return parse_diagnostics(stdout), ""


def autofix(target: Path, repo_root: Path) -> list[str]:
    """Let ruff repair what ruff can repair. Returns human-readable notes.

    Safe fixes only (`ruff check --fix` without --unsafe-fixes), then
    `ruff format` — the same gate every hand-written edit in this repo passes
    through. Returns [] when nothing changed, so the caller can tell a real
    repair from a no-op and stop.
    """
    try:
        before_text = target.read_text(encoding="utf-8")
    except OSError:
        return []

    before, err = run_ruff(target, repo_root)
    if err:
        return []
    fixable = sorted({f.code for f in before if f.fixable})

    if fixable and _run(["check", "--fix", str(target)], repo_root) is None:
        return []
    _run(["format", str(target)], repo_root)

    try:
        after_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    if after_text == before_text:
        return []

    after_codes = {f.code for f in run_ruff(target, repo_root)[0]}
    cleared = [c for c in fixable if c not in after_codes]
    notes = []
    if cleared:
        notes.append("ruff auto-fixed " + ", ".join(cleared))
    if not cleared or after_text != before_text:
        notes.append("ruff format applied")
    return notes


# ── Feedback for the repair prompt ───────────────────────────────────────────


def summarize(findings: list[Finding], limit: int = 6) -> str:
    """One compact line for a user-facing failure message."""
    if not findings:
        return ""
    shown = [f.describe() for f in findings[:limit]]
    extra = len(findings) - len(shown)
    if extra > 0:
        shown.append(f"(+{extra} more)")
    return "; ".join(shown)


def repair_brief(findings: list[Finding]) -> str:
    """The block injected into a repair prompt: what failed, and how to fix it.

    Findings first (so the model sees the exact lines), then one remediation
    note per distinct rule code — deduplicated, because twelve F401s need the
    instruction once, not twelve times.
    """
    if not findings:
        return ""
    lines = ["Ruff reported these violations, with exact locations:"]
    for f in findings[:_MAX_LISTED_FINDINGS]:
        suffix = f" — ruff's own fix: {f.fix_message}" if f.fix_message else ""
        lines.append(f"  - {f.describe()}{suffix}")
    if len(findings) > _MAX_LISTED_FINDINGS:
        lines.append(f"  - (+{len(findings) - _MAX_LISTED_FINDINGS} more of the same kinds)")

    seen: list[str] = []
    for f in findings:
        if f.code not in seen:
            seen.append(f.code)
    lines.append("\nHow to resolve each rule:")
    for code in seen:
        lines.append(f"  {code}: {remediation_for(code)}")
    return "\n".join(lines)


REPAIR_SYSTEM_INSTRUCTION = (
    "You are a careful senior Python engineer fixing linter and test failures "
    "in a production codebase. You address the exact diagnostics given and "
    "change nothing else. You output only code."
)


def repair_prompt(
    rel_path: str,
    current_code: str,
    instruction: str,
    failures: list[str],
    findings: list[Finding],
) -> str:
    """Full prompt for a guided repair pass — NOT a retry of the original ask.

    It carries the file as it stands *after* the deterministic autofix, every
    remaining diagnostic with its line number, and the remediation rule for each
    code. Re-sending the original instruction unchanged is what produced the
    same I001/F401 on every attempt; this replaces it.

    `failures` may include non-lint entries (a failing test, a timeout); those
    are appended verbatim so one pass can settle both kinds.
    """
    brief = repair_brief(findings)
    other = [f for f in failures if not f.startswith("ruff:")]
    other_block = ("\nOther verification failures:\n  - " + "\n  - ".join(other)) if other else ""
    return (
        f"File: {rel_path} (part of Era One, a Python voice assistant).\n\n"
        f"You already applied this change: {instruction}\n"
        "The result does not pass verification. Fix ONLY the problems listed "
        "below — keep the requested change and every unrelated line exactly as "
        f"they are now.\n\n{brief}{other_block}\n\n"
        f"Current code:\n{current_code}\n\n"
        "Return the COMPLETE corrected file. Output ONLY raw Python code — no "
        "explanation, no markdown fences. Do not remove safety checks, "
        "confirmation gates or backups, and do not silence a rule with noqa "
        "unless its instruction above explicitly allows it."
    )
