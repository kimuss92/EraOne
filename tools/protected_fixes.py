"""Protected-fixes registry: what was fixed, the rule that must stay true, and the
tests that prove it. The registry itself is human-first Markdown in
``docs/PROTECTED_FIXES.md`` so every coding agent (Claude, Codex, ChatGPT, Lunar)
reads the same thing; this module is the machine side of it.

Why it exists (owner, 2026-11-12): several coding agents work on Era One, and a
fix made by one was repeatedly broken by another working on a different tool.
The registry turns "please don't break Spotify" into something that FAILS:

  * every guard test listed here runs on every ``tools/era_gate.py`` run,
    whatever file changed (a YouTube edit re-proves Spotify);
  * ``tools/test_protected_fixes.py`` fails the suite if a guard test is deleted
    or renamed, or an entry points at a file that no longer exists;
  * a Claude Code PreToolUse hook shows the rules when a protected file is
    about to be edited.

Usage:
    python tools/protected_fixes.py show actions/spotify_control.py   # rules for a file
    python tools/protected_fixes.py list                              # every entry
    python tools/protected_fixes.py check                             # validate registry
    python tools/protected_fixes.py run                               # run all guards
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "docs" / "PROTECTED_FIXES.md"

_HEADING = re.compile(r"^### (PF-\d{3}) · (.+?)\s*$")
_FIELD = re.compile(r"^- \*\*(\w+):\*\*\s*(.*)$")
_TICKED = re.compile(r"`([^`]+)`")


@dataclass
class Entry:
    id: str
    title: str
    files: list[str] = field(default_factory=list)
    rule: str = ""
    why: str = ""
    guards: list[str] = field(default_factory=list)
    since: str = ""

    def touches(self, path: str) -> bool:
        p = _norm(path)
        return any(p == f or p.endswith("/" + f) for f in self.files)

    def describe(self) -> str:
        guards = "\n".join(f"    - {g}" for g in self.guards)
        return (
            f"{self.id} - {self.title}\n"
            f"  RULE: {self.rule}\n"
            f"  WHY:  {self.why}\n"
            f"  GUARD TESTS (must stay green):\n{guards}"
        )


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/")
    root = str(REPO).replace("\\", "/")
    if p.lower().startswith(root.lower() + "/"):
        p = p[len(root) + 1 :]
    return p.lstrip("./")


def load(path: Path = REGISTRY) -> list[Entry]:
    """Parse the registry. Never raises on content; a broken entry simply has
    empty fields, which ``problems()`` then reports."""
    entries: list[Entry] = []
    current: Entry | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = Entry(id=heading.group(1), title=heading.group(2))
            entries.append(current)
            continue
        if line.startswith("## "):
            current = None
            continue
        match = _FIELD.match(line)
        if not (current and match):
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == "files":
            current.files = [_norm(v) for v in _TICKED.findall(value)]
        elif key == "guards":
            current.guards = _TICKED.findall(value)
        elif key in {"rule", "why", "since"}:
            setattr(current, key, value)
    return entries


def _resolve(test_file: Path, parts: list[str]) -> list[ast.AST] | None:
    """The chain of nodes for ``Class::method`` / ``function``, or None."""
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    scope: list[ast.stmt] = tree.body
    chain: list[ast.AST] = [tree]
    for i, name in enumerate(parts):
        found = None
        for node in scope:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
                node.name == name
            ):
                found = node
                break
        if found is None:
            return None
        chain.append(found)
        if i < len(parts) - 1:
            if not isinstance(found, ast.ClassDef):
                return None
            scope = found.body
    return chain


def _switched_off(chain: list[ast.AST]) -> bool:
    """A guard marked skip/xfail (on itself, its class, or the module) is not a guard.

    ``skipif`` on a real environmental condition (e.g. Node.js not installed) is
    allowed — ``\\bskip\\b`` deliberately does not match ``skipif`` — but a
    constant ``skipif(True)`` is an unconditional skip and is reported."""
    off = re.compile(r"\b(skip|xfail)\b|skipif\(\s*True\b")
    for node in chain:
        decorators = getattr(node, "decorator_list", [])
        if any(off.search(ast.unparse(d)) for d in decorators):
            return True
        if isinstance(node, ast.Module):
            for stmt in node.body:
                text = ast.unparse(stmt) if isinstance(stmt, (ast.Assign, ast.Expr)) else ""
                if (text.startswith("pytestmark") and off.search(text)) or re.search(
                    r"^pytest\.skip\(.*allow_module_level", text
                ):
                    return True
    return False


def guard_exists(node_id: str) -> bool:
    file_part, *names = node_id.split("::")
    test_file = REPO / file_part
    if not test_file.is_file():
        return False
    return not names or _resolve(test_file, names) is not None


def guard_switched_off(node_id: str) -> bool:
    file_part, *names = node_id.split("::")
    chain = _resolve(REPO / file_part, names)
    return bool(chain) and _switched_off(chain)


def problems(entries: list[Entry] | None = None) -> list[str]:
    """Everything wrong with the registry; empty means it is enforceable."""
    entries = load() if entries is None else entries
    out: list[str] = []
    seen: set[str] = set()
    if not entries:
        out.append("registry has no entries")
    for e in entries:
        if e.id in seen:
            out.append(f"{e.id}: duplicate id")
        seen.add(e.id)
        for name in ("files", "rule", "why", "guards", "since"):
            if not getattr(e, name):
                out.append(f"{e.id}: missing **{name.capitalize()}:**")
        for f in e.files:
            if not (REPO / f).exists():
                out.append(f"{e.id}: file does not exist: {f}")
        for g in e.guards:
            if not g.startswith("tools/test_"):
                out.append(f"{e.id}: guard is not a tools/test_* node id: {g}")
            elif not guard_exists(g):
                out.append(f"{e.id}: guard test not found (deleted or renamed?): {g}")
            elif guard_switched_off(g):
                out.append(f"{e.id}: guard test is marked skip/xfail: {g}")
    return out


def all_guards(entries: list[Entry] | None = None) -> list[str]:
    entries = load() if entries is None else entries
    return sorted({g for e in entries for g in e.guards})


def for_files(paths: list[str], entries: list[Entry] | None = None) -> list[Entry]:
    entries = load() if entries is None else entries
    return [e for e in entries if any(e.touches(p) for p in paths)]


def run_guards(extra: list[str] | None = None) -> int:
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    return subprocess.run(cmd + (extra or []) + all_guards(), cwd=str(REPO)).returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args else "list"
    if command == "check":
        found = problems()
        for p in found:
            print("  ! " + p)
        print("REGISTRY OK" if not found else f"REGISTRY BROKEN ({len(found)} problem(s))")
        return 1 if found else 0
    if command == "run":
        return run_guards(args)
    if command == "show":
        hits = for_files(args)
        if not hits:
            print("No protected fix covers these files.")
        for e in hits:
            print(e.describe() + "\n")
        return 0
    if command == "list":
        for e in load():
            print(f"{e.id} - {e.title}  ({len(e.guards)} guard(s))")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
