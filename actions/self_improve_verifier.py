"""actions/self_improve_verifier.py — independent second pass after a self_improve apply.

_apply's own gate (py_compile + ruff + ONE name-matched test file) decides
whether a rewrite lands. This module re-checks the landed file for what that
gate cannot see, scoped strictly to the one modified file:

  1. syntax        py_compile                       (FAIL → rollback)
  2. import        import the module in a subprocess (FAIL → rollback)
                   — py_compile never catches a broken import or a crash at
                   module load; that is the gap that bricks Era at startup.
  3. lint          ruff check, no --fix             (FAIL → rollback)
  4. tests         every test file that IMPORTS the module, not just
                   test_<stem>.py                   (FAIL → rollback, unless
                   the same failures reproduce on the backup: PRE-EXISTING)
  5. format        ruff format --check              (warning only)
  6. diff safety   removed gates / truncation       (NEEDS HUMAN REVIEW, no rollback)
  7. header        module docstring/header deleted  (FAIL → rollback)
                   — the generation prompt asks the model to preserve
                   comments; step 7 is that asked-for promise enforced.

Invariants:
  - Never executes main.py / era_supervisor.py (import is skipped for them).
  - Before any rollback the rejected version is kept under
    runtime/self_improve/rejected/<stamp>/<rel> — an LLM rewrite cannot be
    regenerated identically.
  - A failure of the CHECKER (pytest-asyncio missing, ruff missing) is an
    ENVIRONMENT result and never triggers a rollback.
  - Test runs set _GUARD_ENV so a test that drives _apply cannot recurse into
    another verification.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import py_compile
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.utils import log
from core.win_subprocess import NO_WINDOW

_GUARD_ENV = "ERA_SELF_IMPROVE_VERIFYING"
_IMPORT_TIMEOUT_S = 30
_TEST_TIMEOUT_S = 180
_MAX_TEST_FILES = 8
_NEVER_IMPORT = frozenset({"main.py", "era_supervisor.py"})
_SHRINK_LIMIT = 0.30
_TEST_DIRS = (".", "tools", "tests")
# A removed line mentioning any of these is a weakened safety net until a human says otherwise.
_SAFETY_MARKERS = re.compile(
    r"requires_confirmation|SAFETY_CRITICAL|_PROTECTED_GLOBS|_backup\(|confirm|safety_guard"
)
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_TIMEOUT_ID = "<timeout>"


@dataclass
class Verdict:
    status: str  # pass | rolled_back | review | environment | skipped | rollback_failed
    lines: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def report(self) -> str:
        return "\n".join(self.lines)


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess | None:
    env = dict(os.environ, **{_GUARD_ENV: "1"})
    try:
        return subprocess.run(
            [sys.executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            env=env,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 125, "", str(e))


def _first_line(text: str, limit: int = 150) -> str:
    for line in (text or "").strip().splitlines()[::-1]:
        if line.strip():
            return line.strip()[:limit]
    return ""


def dotted_name(rel: str) -> str:
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")


def select_tests(rel: str, repo_root: Path) -> list[Path]:
    """test_<stem>.py plus every test file that imports the module, name match first."""
    dotted = dotted_name(rel)
    parent, _, stem = dotted.rpartition(".")
    patterns = [rf"^\s*(?:from|import)\s+{re.escape(dotted)}\b"]
    if parent:
        patterns.append(rf"^\s*from\s+{re.escape(parent)}\s+import\s+[^\n]*\b{re.escape(stem)}\b")
    importer = re.compile("|".join(patterns), re.MULTILINE)

    named: list[Path] = []
    importing: list[Path] = []
    for d in _TEST_DIRS:
        root = repo_root / d
        if not root.is_dir():
            continue
        for f in sorted(root.glob("test_*.py")):
            if f.name == f"test_{stem}.py":
                named.append(f)
                continue
            try:
                if importer.search(f.read_text(encoding="utf-8", errors="replace")):
                    importing.append(f)
            except OSError:
                continue
    return (named + importing)[:_MAX_TEST_FILES]


def _pytest(files: list[Path], repo_root: Path) -> tuple[bool, set[str], str]:
    """(passed, failed test ids, one-line detail)."""
    proc = _run(
        ["-m", "pytest", *[str(f) for f in files], "-q", "-rfE", "-p", "no:cacheprovider"],
        repo_root,
        _TEST_TIMEOUT_S,
    )
    if proc.returncode == 124:
        return False, {_TIMEOUT_ID}, f"timed out after {_TEST_TIMEOUT_S}s"
    out = proc.stdout or ""
    if proc.returncode in (0, 5):  # 5 = nothing collected
        return True, set(), _first_line(out)
    failed = set(_FAILED_RE.findall(out)) or {f"<exit {proc.returncode}>"}
    return False, failed, _first_line(out) or _first_line(proc.stderr)


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return True


def _module_header(source: str) -> tuple[str | None, int]:
    """(module docstring, number of leading comment lines) for one file.

    The docstring comes from `ast`, not a regex, so quoting style, raw/unicode
    prefixes and a docstring that follows comments all read correctly. The
    comment count is the `#` block at the very top — `# actions/open_app.py`
    and the license/provenance headers that live there. A file that fails to
    parse returns (None, 0); the syntax check has already failed by then and
    this must not raise on top of it.
    """
    try:
        doc = ast.get_docstring(ast.parse(source))
    except (SyntaxError, ValueError):
        return None, 0
    comments = 0
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            comments += 1
        elif stripped:
            break  # first real code or the docstring — the header is over
    return doc, comments


def header_loss(before: str, after: str) -> str | None:
    """ "You deleted something nobody asked you to touch", or None.

    WHY THIS IS A ROLLBACK AND NOT A WARNING (board item 27)
    On 2026-09-29 a self-edit that was asked to add a function to
    `actions/open_app.py` also **deleted the file's 9-line module header** —
    "High-performance Windows Application Launcher for Era One" and the four
    lines describing how the launcher works. Nothing caught it: the file
    compiled, imported, linted and passed its tests, the removed lines matched
    no `_SAFETY_MARKERS`, and 9 lines out of 434 is a 2% shrink against
    `_SHRINK_LIMIT`'s 30%. Verdict: `pass`. The loss was found by diffing the
    backup by hand, days later.

    The generation prompt already says "Preserve all existing behavior that the
    change does not touch: comments, public function signatures, imports used
    elsewhere." This is that instruction, enforced instead of requested — an
    unverified instruction to a model is a preference, not a guarantee.

    It rolls back rather than flagging, because a model that silently discarded
    the header has already broken the non-destructive contract, and nothing
    about the rest of its rewrite is more trustworthy than that. Rejecting is
    cheap and reversible here in a way the deletion is not: `_keep_rejected`
    preserves the rejected version under `runtime/self_improve/rejected/`, so a
    wanted change is never actually lost — it just stops landing unattended.

    Deliberately narrow. It fires only on OUTRIGHT REMOVAL of the module
    header, never on rewording, extending or reformatting it, and it says
    nothing about docstrings on functions and classes — a wider "no deletions"
    rule would refuse the ordinary edits this tool exists to make.
    """
    if not _parses(after):
        # Step 1 has already failed and is rolling back for the right reason.
        # Adding "the docstring was deleted" on top of a SyntaxError would be a
        # second, misleading cause for one failure.
        return None
    doc_before, comments_before = _module_header(before)
    doc_after, comments_after = _module_header(after)
    if doc_before and not doc_after:
        return f"the module docstring was deleted ({len(doc_before.splitlines())} lines)"
    if comments_before and not comments_after:
        return f"the {comments_before}-line header comment block was deleted"
    return None


def diff_flags(before: str, after: str) -> list[str]:
    flags = []
    removed = set(before.splitlines()) - set(after.splitlines())
    hits = sorted({m.group(0) for line in removed for m in [_SAFETY_MARKERS.search(line)] if m})
    if hits:
        flags.append("removed lines touching " + ", ".join(hits))
    b, a = len(before.splitlines()), len(after.splitlines())
    if b and (b - a) / b > _SHRINK_LIMIT:
        flags.append(f"file shrank {b}→{a} lines (possible truncation)")
    return flags


def verify(
    target: Path,
    backup: Path,
    rel: str,
    repo_root: Path,
    rollback: Callable[[], str],
    player=None,
) -> Verdict:
    """Verify the landed change; roll back on failure. Never raises."""
    try:
        return _verify(target, backup, rel, repo_root, rollback, player)
    except Exception as e:  # the checker breaking must never cost Era her change silently
        log("SelfImproveVerify", f"verifier crashed on {rel}: {e}", player)
        return Verdict("environment", [f"VERIFIER ERROR — {e}"], f"(the second check crashed: {e})")


def _verify(target, backup, rel, repo_root, rollback, player) -> Verdict:
    head = f"SELF-IMPROVE VERIFICATION — {rel}"
    if os.environ.get(_GUARD_ENV):
        return Verdict("skipped", [f"{head} — SKIPPED (nested run)"])
    if not target.is_file() or not backup.is_file():
        return Verdict("skipped", [f"{head} — ABORTED (target or backup missing)"])
    before = backup.read_text(encoding="utf-8", errors="replace")
    after = target.read_text(encoding="utf-8", errors="replace")
    if before == after:
        return Verdict("skipped", [f"{head} — ABORTED (no difference from backup)"])

    lines: list[str] = []
    failures: list[str] = []
    notes: list[str] = []

    # 1. syntax
    try:
        py_compile.compile(str(target), doraise=True)
        lines.append("Syntax:   PASS")
    except py_compile.PyCompileError as e:
        lines.append(f"Syntax:   FAIL  {_first_line(str(e.msg))}")
        failures.append("syntax")

    # 2. import
    if failures:
        lines.append("Import:   SKIPPED (syntax failed)")
    elif rel in _NEVER_IMPORT:
        lines.append("Import:   SKIPPED (entry script — never executed)")
    else:
        code = f"import importlib; importlib.import_module({dotted_name(rel)!r})"
        proc = _run(["-c", code], repo_root, _IMPORT_TIMEOUT_S)
        if proc.returncode == 0:
            lines.append("Import:   PASS")
        else:
            lines.append(f"Import:   FAIL  {_first_line(proc.stderr)}")
            failures.append("import")

    # 3. lint (no --fix — the verifier never edits)
    proc = _run(
        ["-m", "ruff", "check", "--no-fix", "--output-format", "concise", str(target)],
        repo_root,
        _IMPORT_TIMEOUT_S,
    )
    ruff_missing = "No module named" in (proc.stderr or "") and "ruff" in (proc.stderr or "")
    if ruff_missing or proc.returncode in (124, 125):
        lines.append("Lint:     N/A  ruff unavailable")
        notes.append("lint not run")
    elif proc.returncode == 0:
        lines.append("Lint:     PASS")
    else:
        codes = sorted(set(re.findall(r"\b([A-Z]{1,3}\d{3,4})\b", proc.stdout or "")))
        lines.append(f"Lint:     FAIL  {', '.join(codes) or _first_line(proc.stdout)}")
        failures.append("lint")

    # 4. targeted tests
    tests = select_tests(rel, repo_root)
    failed_ids: set[str] = set()
    if not tests:
        lines.append("Tests:    N/A  no test file imports this module")
        notes.append("no test coverage")
    elif importlib.util.find_spec("pytest_asyncio") is None:
        lines.append("Tests:    N/A  pytest-asyncio missing — results would be phantom")
        notes.append("ENVIRONMENT: install pytest-asyncio")
    else:
        ok, failed_ids, detail = _pytest(tests, repo_root)
        names = ", ".join(t.name for t in tests)
        lines.append(f"Tests:    {'PASS' if ok else 'FAIL'}  {detail} — {names}")
        if not ok:
            failures.append("tests")

    # 5. format (warning)
    proc = _run(["-m", "ruff", "format", "--check", str(target)], repo_root, _IMPORT_TIMEOUT_S)
    lines.append("Format:   " + ("OK" if proc.returncode == 0 else "DRIFT/N/A"))

    # 6. diff safety (review, not rollback)
    flags = diff_flags(before, after)
    lines.append("Safety:   " + ("OK" if not flags else "FLAG  " + "; ".join(flags)))

    # 7. header preservation (rollback — see header_loss)
    lost = header_loss(before, after)
    if lost:
        lines.append(f"Header:   FAIL  {lost}")
        failures.append("header")
    else:
        lines.append("Header:   OK")

    # A change is only to blame for test failures that don't also happen without it.
    if failures == ["tests"]:
        return _attribute_tests(
            head,
            lines,
            notes,
            flags,
            target,
            backup,
            rel,
            repo_root,
            tests,
            failed_ids,
            rollback,
            player,
        )
    if failures:
        return _roll_back(
            head, lines, notes, failures, target, backup, rel, repo_root, rollback, player
        )
    return _finish(head, lines, notes, flags, player)


def _finish(head, lines, notes, flags, player) -> Verdict:
    if flags:
        status, label = "review", "NEEDS HUMAN REVIEW"
        summary = f"An independent check passed, but please review it: {'; '.join(flags)}."
    else:
        status, label, summary = "pass", "PASS", "An independent second check also passed."
    lines.insert(0, f"{head} — {label}")
    lines += ["Action:   kept"] + ([f"Notes:    {'; '.join(notes)}"] if notes else [])
    log("SelfImproveVerify", "\n".join(lines), player)
    return Verdict(status, lines, summary)


def _keep_rejected(target: Path, backup: Path, rel: str) -> Path:
    """Beside the backups (<backup_root>/../rejected/<stamp>/<rel>), so a sandboxed
    BACKUP_ROOT keeps rejected copies sandboxed too."""
    depth = len(Path(rel).parts)
    stamp = backup.parents[depth - 1].name
    dest = backup.parents[depth].parent / "rejected" / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(target), str(dest))
    return dest


def _restore_ok(target: Path, backup: Path, result: str) -> bool:
    return "Rolled" in (result or "") and target.read_bytes() == backup.read_bytes()


def _roll_back(head, lines, notes, failures, target, backup, rel, repo_root, rollback, player):
    rejected = _keep_rejected(target, backup, rel)
    result = rollback()
    if not _restore_ok(target, backup, result):
        result = rollback()  # one retry, never a manual copy
    if not _restore_ok(target, backup, result):
        lines.insert(0, f"{head} — ROLLBACK FAILED — MANUAL ACTION REQUIRED")
        lines.append(f"Action:   rollback failed ({_first_line(result)}); backup {backup}")
        log("SelfImproveVerify", "\n".join(lines), player)
        return Verdict(
            "rollback_failed",
            lines,
            f"The second check failed ({', '.join(failures)}) and my rollback did NOT "
            f"restore {rel} — it needs manual repair from {backup}.",
        )
    lines.insert(0, f"{head} — FAIL→ROLLED BACK")
    lines.append(f"Action:   rolled back (rejected copy: {rejected})")
    if notes:
        lines.append(f"Notes:    {'; '.join(notes)}")
    log("SelfImproveVerify", "\n".join(lines), player)
    detail = next((ln.split("FAIL", 1)[1].strip() for ln in lines if "FAIL  " in ln), "")
    return Verdict(
        "rolled_back",
        lines,
        f"a second, deeper check failed ({', '.join(failures)}: {detail[:200]}), so I rolled "
        f"{rel} back — my code is untouched. The rejected version is saved in "
        f"{rejected.parent.parent}.",
    )


def _attribute_tests(
    head, lines, notes, flags, target, backup, rel, repo_root, tests, failed_ids, rollback, player
):
    rejected = _keep_rejected(target, backup, rel)
    result = rollback()
    if not _restore_ok(target, backup, result):
        return _roll_back(
            head, lines, notes, ["tests"], target, backup, rel, repo_root, rollback, player
        )
    _ok, baseline_ids, _ = _pytest(tests, repo_root)
    if failed_ids and failed_ids <= baseline_ids:
        shutil.copy2(str(rejected), str(target))  # the change was blameless — put it back
        notes.append(
            f"PRE-EXISTING test failures, also fail without the change: {', '.join(sorted(failed_ids))[:200]}"
        )
        return _finish(head, lines, notes, flags, player)
    new = sorted(failed_ids - baseline_ids)
    lines.insert(0, f"{head} — FAIL→ROLLED BACK")
    lines.append(f"Action:   rolled back (rejected copy: {rejected})")
    lines.append(f"Notes:    failures introduced by the change: {', '.join(new)[:200]}")
    log("SelfImproveVerify", "\n".join(lines), player)
    return Verdict(
        "rolled_back",
        lines,
        f"a second, deeper check found tests the change broke ({', '.join(new)[:200]}), so I "
        f"rolled {rel} back — my code is untouched. The rejected version is saved in "
        f"{rejected.parent.parent}.",
    )
