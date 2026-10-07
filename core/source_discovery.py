"""Bounded local project map for autonomous development; no LLM or network.

Source and skill excerpts are planning context, never authority to bypass gates.
The index is invalidated by file size/mtime, so new modules become discoverable.
"""

from __future__ import annotations

import ast
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOTS = (
    "actions",
    "app",
    "core",
    "features",
    "agent",
    "memory",
    "utils",
    "windows",
    "src",
    "ui_src/src",
    "tools",
)
_IGNORE = {"node_modules", "__pycache__", "runtime", "logs", "dist", "backups", "venv"}
_lock = threading.RLock()
_cache = {}


def _tokens(text: str) -> set[str]:
    from actions.self_improve_targets import description_tokens

    # Split React camelCase names as well as Python snake_case.
    return description_tokens(re.sub(r"([a-z])([A-Z])", r"\1 \2", text))


def _paths(root: Path, suffixes: set[str]):
    if not root.exists():
        return
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and d not in _IGNORE and not (Path(folder) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(folder) / name
            if (
                path.suffix in suffixes
                and not path.is_symlink()
                and path.resolve().is_relative_to(ROOT.resolve())
            ):
                yield path


def index() -> list[dict]:
    rows = []
    with _lock:
        for source_root in ROOTS:
            for path in _paths(ROOT / source_root, {".py", ".tsx", ".ts", ".css"}):
                rel = path.relative_to(ROOT).as_posix()
                if path.stat().st_size > 200000:
                    continue
                stamp = (path.stat().st_mtime_ns, path.stat().st_size)
                cached = _cache.get(rel)
                if cached and cached[0] == stamp:
                    rows.append(cached[1])
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                doc, symbols, imports = "", [], []
                if path.suffix == ".py":
                    try:
                        tree = ast.parse(text)
                        doc = (ast.get_docstring(tree) or "")[:700]
                        symbols = [
                            n.name
                            for n in tree.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        ]
                        imports = [
                            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                        ]
                        imports += [
                            a.name
                            for n in ast.walk(tree)
                            if isinstance(n, ast.Import)
                            for a in n.names
                        ]
                    except SyntaxError:
                        doc = "SOURCE SYNTAX ERROR: inspect before editing"
                else:
                    doc = text[:700]
                    symbols = re.findall(r"(?:function|class|const)\s+(\w+)", text)[:30]
                    imports = re.findall(r"from\s+['\"]([^'\"]+)", text)
                row = {
                    "path": rel,
                    "description": doc,
                    "symbols": symbols[:40],
                    "imports": imports[:50],
                    "test": path.name.startswith("test_"),
                }
                _cache[rel] = (stamp, row)
                rows.append(row)
    return rows


def discover(request: str, limit: int = 12) -> dict:
    from core.development import _path

    tokens = _tokens(request)
    tokens |= (
        {"hud", "ui", "activity"}
        if tokens & {"interface", "animation", "liquid", "panel", "button"}
        else set()
    )
    tokens |= {"memory", "recall"} if tokens & {"remember", "persistent", "forget"} else set()
    rows = index()
    ranked = []
    for row in rows:
        name_score = len(tokens & _tokens(row["path"])) * 4
        score = (
            name_score
            + min(4, len(tokens & _tokens(row["description"])))
            + min(3, len(tokens & _tokens(" ".join(row["symbols"]))))
        )
        if not score:
            continue
        try:
            _path(row["path"])
            editable = True
        except ValueError:
            editable = False
        ranked.append({**row, "score": score, "editable": editable})
    ranked.sort(key=lambda row: (-row["score"], row["test"], row["path"]))
    selected = ranked[: max(1, min(limit, 20))]
    modules = {
        row["path"][:-3].replace("/", "."): row for row in rows if row["path"].endswith(".py")
    }
    dependencies = []
    for row in selected[:6]:
        for name in row["imports"]:
            if name in modules and modules[name]["path"] not in {
                r["path"] for r in selected + dependencies
            }:
                dependencies.append(modules[name])
    skills = []
    skill_paths = (
        path
        for folder in ("skills", ".agents/skills", ".claude/skills")
        for path in _paths(ROOT / folder, {".md"})
    )
    for path in skill_paths:
        if path.name != "SKILL.md" or path.stat().st_size > 100000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if tokens & _tokens(path.as_posix() + " " + text[:1000]):
            skills.append({"path": path.relative_to(ROOT).as_posix(), "instructions": text[:5000]})
            if len(skills) == 3:
                break
    return {
        "candidates": selected,
        "dependencies": dependencies[:6],
        "skills": skills,
        "wiring": {
            "reloadable": "features/<name>.py: FEATURE={id,description}; run(arguments). Approved features register automatically and execute through run_feature with confirmation.",
            "tests": "tools/test_<name>.py must test behavior. Fixed verification gates cannot be changed.",
            "native_tool": "Declarations, dispatcher and safety framework are protected; propose a reviewed framework patch instead of bypassing them.",
            "ui": "ui_src/src React components; wire imports/rendering in parent components; typecheck and rebuild before activation.",
        },
    }
