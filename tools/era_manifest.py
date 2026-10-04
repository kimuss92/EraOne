#!/usr/bin/env python3
"""Era's codebase index — the single source of truth for "where do I go?".

WHY THIS EXISTS
  Two different readers kept having to re-derive the same facts by scanning the
  whole repo:

    * Claude, on every fix. 1,289 Python files means a full scan before a
      three-line change, which is slow and burns the context that the fix
      itself needs.
    * Era, about herself. Her self-description lives as prose in
      core/prompt.txt while her real abilities live in app/tool_declarations.py
      and core/action_registry.py. Prose drifts; a tool removed in code stays
      claimed in the prompt. That drift IS the "false self-perception" problem.

  Both readers want the same artifact: an accurate map from a capability or a
  symbol to the file that implements it.

GENERATED, NEVER HAND-WRITTEN
  Every fact below is read out of the source tree with the `ast` module or
  imported from modules that are pure data. Nothing is typed in by hand, so
  nothing can be stale in the way a hand-maintained doc goes stale — and
  tools/test_era_manifest.py fails the suite when the committed copy no longer
  matches what a fresh run produces. That test is what makes this trustworthy
  enough for Era to ground an answer on.

OUTPUTS
  docs/MANIFEST.md     the navigation map (for Claude, and for a human)
  runtime/manifest.json machine-readable (for Era's self-model)

USAGE
  python tools/era_manifest.py            # write both artifacts
  python tools/era_manifest.py --check    # exit 1 if they are stale
  python tools/era_manifest.py --stdout   # print the JSON, write nothing
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# Directories that are not Era's source. Kept deliberately close to pytest.ini's
# norecursedirs: a directory that is not worth testing is not worth indexing,
# and a backup copy of a module would otherwise appear as a second definition
# of every symbol in it.
EXCLUDE_DIRS = {
    ".git",
    ".claude",
    ".claude-flow",
    ".pytest_cache",
    ".ruff_cache",
    ".swarm",
    "__pycache__",
    "Era One Obsidian vault",
    "graphify-out",
    "logs",
    "models",
    "node_modules",
    "obsidian_era",
    "runtime",
    "scratchpad",
    "source_code_folder",
    "ui_src",
    "venv",
    ".venv",
}
# Prefix match, for the dated backup trees (.phase1_backups, .phase2_backups...).
EXCLUDE_PREFIXES = (".phase",)

# Top-level packages that count as Era's own code, for the dependency graph. An
# import of anything else is a third-party or stdlib edge and is not recorded —
# the graph exists to answer "if I change this, what else moves?", and stdlib
# never moves.
FIRST_PARTY = {
    "actions",
    "agent",
    "app",
    "core",
    "features",
    "memory",
    "src",
    "tools",
    "utils",
    "windows",
}
# Single-module first-party entry points that live at the repo root.
FIRST_PARTY_ROOT_MODULES = {
    "era_dispatch",
    "era_offers",
    "era_scheduler",
    "era_supervisor",
    "era_telegram",
    "lane_dispatch",
    "lane_router",
    "main",
    "media_control",
    "media_host",
    "music_generate",
    "ui",
}


def _excluded(path: Path) -> bool:
    # Self-improvement tests temporarily create this fake source in the repo.
    # It is not a shipped capability and must not make the manifest drift while
    # verification is running (or if a killed test leaves the fixture behind).
    if path.relative_to(REPO).as_posix() == "tools/_verifier_selftest.py":
        return True
    for part in path.relative_to(REPO).parts:
        if part in EXCLUDE_DIRS or part.startswith(EXCLUDE_PREFIXES):
            return True
    return False


def iter_python_files() -> list[Path]:
    return sorted(p for p in REPO.rglob("*.py") if not _excluded(p))


def _dotted(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts) if parts else rel.parts[0]


def _is_first_party(dotted: str) -> bool:
    head = dotted.split(".", 1)[0]
    return head in FIRST_PARTY or head in FIRST_PARTY_ROOT_MODULES


def _first_line(doc: str | None) -> str:
    """The first sentence of a docstring, collapsed to one line.

    Docstrings in this repo open with a one-line summary and then a blank line,
    so the summary is everything up to the first blank line — not up to the
    first newline, which would truncate a wrapped summary mid-sentence.
    """
    if not doc:
        return ""
    summary: list[str] = []
    for raw in doc.strip().splitlines():
        line = raw.strip()
        if not line:
            break
        summary.append(line)
    return " ".join(summary)[:200]


def _resolve(candidate: str, known: frozenset[str]) -> str | None:
    """A dotted name -> the real module it names, or None.

    Resolved against the set of modules actually found on disk rather than
    assumed, because an import statement alone cannot say which part of a
    dotted name is a module and which is a symbol inside it. Falls back to the
    longest known prefix, so `core.utils.log` (a function) still records an
    edge to `core.utils`.
    """
    parts = candidate.split(".")
    for stop in range(len(parts), 0, -1):
        prefix = ".".join(parts[:stop])
        if prefix in known:
            return prefix
    return None


def _imports(tree: ast.Module, dotted: str, known: frozenset[str]) -> set[str]:
    """First-party modules this one imports, at any nesting depth.

    Function-local imports are counted too: this repo uses them heavily to
    break circular imports, so a graph that only looked at module level would
    miss most of the real coupling.

    `from core.llm import claude_accounts` is the case worth spelling out. The
    statement's own `module` is `core.llm`, and recording only that loses the
    edge to `core.llm.claude_accounts` entirely — which is most of this repo's
    imports, and made targeted test selection report that nothing covered a
    module its tests plainly import.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_first_party(alias.name):
                    resolved = _resolve(alias.name, known)
                    if resolved:
                        found.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative: resolve against this module's own package, dropping
                # one level per extra dot.
                package = dotted.split(".")[: -node.level] if "." in dotted else []
                base = ".".join([*package, node.module] if node.module else package)
            else:
                base = node.module or ""
            if not base or not _is_first_party(base):
                continue
            resolved = _resolve(base, known)
            if resolved:
                found.add(resolved)
            # Each imported name may itself be a submodule of `base`.
            for alias in node.names:
                if alias.name == "*":
                    continue
                child = _resolve(f"{base}.{alias.name}", known)
                if child:
                    found.add(child)
    return found


def parse_module(path: Path, known: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """AST facts about one module, or None when it cannot be parsed.

    A syntax error is reported rather than raised: a single broken file must not
    stop the index that would help you find and fix it.
    """
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": str(path.relative_to(REPO)).replace("\\", "/"), "error": str(exc)}
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {
            "path": str(path.relative_to(REPO)).replace("\\", "/"),
            "error": f"SyntaxError line {exc.lineno}: {exc.msg}",
        }

    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append({"name": node.name, "line": node.lineno})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                functions.append({"name": node.name, "line": node.lineno})

    rel = str(path.relative_to(REPO)).replace("\\", "/")
    dotted = _dotted(path)
    return {
        "path": rel,
        "module": dotted,
        "lines": source.count("\n") + 1,
        "doc": _first_line(ast.get_docstring(tree)),
        "classes": classes,
        "functions": functions,
        "imports": sorted(_imports(tree, dotted, known)),
        "is_test": path.name.startswith("test_"),
    }


def build_reverse_deps(modules: list[dict[str, Any]]) -> dict[str, list[str]]:
    """module -> the modules that import it.

    This is the direction you actually need before a change: forward imports are
    visible by reading the file, dependents are not.
    """
    reverse: dict[str, set[str]] = {}
    for mod in modules:
        if mod.get("error"):
            continue
        for target in mod["imports"]:
            reverse.setdefault(target, set()).add(mod["module"])
    return {k: sorted(v) for k, v in sorted(reverse.items())}


def load_capabilities() -> dict[str, Any]:
    """Era's REAL abilities, read from the two modules that define them.

    Both are pure data (app/tool_declarations.py is a literal list;
    core/action_registry.py's handlers are imported lazily), so importing them
    here runs no Era machinery. Import failure degrades to an empty section with
    the reason recorded — an index that cannot load the tool list must say so,
    not silently claim Era has no abilities.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    out: dict[str, Any] = {"tools": [], "actions": [], "errors": []}

    try:
        from app.tool_declarations import TOOL_DECLARATIONS
    except Exception as exc:  # noqa: BLE001 - any import failure is reportable, not fatal
        out["errors"].append(f"app.tool_declarations: {exc}")
        TOOL_DECLARATIONS = []

    dispatch_sites = _dispatch_sites()
    for decl in TOOL_DECLARATIONS:
        name = decl.get("name", "")
        params = (decl.get("parameters") or {}).get("properties") or {}
        required = (decl.get("parameters") or {}).get("required") or []
        handler = f"actions/{name}.py"
        if not (REPO / handler).exists():
            handler = dispatch_sites.get(name, "")
        out["tools"].append(
            {
                "name": name,
                "summary": _first_line(decl.get("description", "")),
                "params": sorted(params),
                "required": list(required),
                "handler": handler,
            }
        )

    try:
        from core.action_registry import ACTION_SPECS

        for action, spec in sorted(ACTION_SPECS.items()):
            out["actions"].append(
                {
                    "action": action,
                    "tool": spec.tool,
                    "target_action": spec.action,
                    "aliases": list(spec.aliases),
                    "risk_level": spec.risk_level,
                    "requires_confirmation": spec.requires_confirmation,
                }
            )
    except Exception as exc:  # noqa: BLE001 - see above
        out["errors"].append(f"core.action_registry: {exc}")

    return out


def _dispatch_sites() -> dict[str, str]:
    """tool name -> 'file:line' where the live dispatcher mentions it.

    A tool without an actions/<name>.py module is handled inline in the
    dispatcher. Finding it means locating the string literal, which is what an
    AST walk over string constants gives exactly — a grep would also match
    prose in comments and docstrings.
    """
    sites: dict[str, str] = {}
    for rel in ("app/era_live/tool_dispatch.py", "era_dispatch.py"):
        path = REPO / rel
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sites.setdefault(node.value, f"{rel}:{node.lineno}")
    return sites


def build(_argv: list[str] | None = None) -> dict[str, Any]:
    files = iter_python_files()
    # Two passes. The first learns which dotted names are real modules on disk;
    # the second needs that set to tell `from core.llm import claude_accounts`
    # (a submodule, and an edge) from `from core.llm import registry` used as a
    # symbol. Without it every `from package import submodule` edge is lost,
    # which is most of this repo's coupling.
    known = frozenset(_dotted(p) for p in files)
    modules = [m for m in (parse_module(p, known) for p in files) if m]
    source_modules = [m for m in modules if not m.get("is_test") and not m.get("error")]
    test_modules = [m for m in modules if m.get("is_test")]
    broken = [m for m in modules if m.get("error")]

    return {
        "schema": 1,
        "counts": {
            "python_files": len(files),
            "source_modules": len(source_modules),
            "test_modules": len(test_modules),
            "unparseable": len(broken),
            "source_lines": sum(m["lines"] for m in source_modules),
        },
        "capabilities": load_capabilities(),
        "modules": sorted(modules, key=lambda m: m["path"]),
        "reverse_deps": build_reverse_deps(modules),
        "unparseable": broken,
    }


# ── rendering ────────────────────────────────────────────────────────────────

# The subsystems a fix actually gets routed to. Each entry is
# (heading, path prefixes, one-line "go here when"). Hand-curated on purpose:
# the directory layout alone does not say which directory answers a question,
# and that routing judgement is the whole value of the map.
SUBSYSTEMS: list[tuple[str, tuple[str, ...], str]] = [
    ("Voice loop / live session", ("app/era_live/",), "hearing, speaking, reconnects, barge-in"),
    (
        "Tool dispatch & declarations",
        ("app/era_live/tool_dispatch.py", "app/tool_declarations.py"),
        "a tool is not called, called wrongly, or needs adding",
    ),
    ("HUD (browser UI)", ("app/hud/", "ui.py", "ui/"), "anything the user SEES"),
    (
        "Safety & confirmation",
        (
            "core/safety_guard.py",
            "core/confirmation",
            "core/input_safety_gate.py",
            "core/pending_confirmation.py",
            "core/panic_stop.py",
            "core/voice_gate.py",
        ),
        "gates, approvals, owner-only checks, abort",
    ),
    (
        "Model routing & Claude",
        ("core/llm/",),
        "which model answers, cost, quota failover, Claude CLI",
    ),
    (
        "Memory",
        ("memory/", "core/turn_memory.py", "core/intent_memory.py", "core/memory_intent.py"),
        "recall, save_memory, long-term facts",
    ),
    ("Actions (what Era can DO)", ("actions/",), "desktop, browser, media, files, apps"),
    ("Vision", ("core/vision_",), "screen understanding"),
    (
        "Audio devices & TTS",
        ("core/audio_", "core/tts_", "core/voice_output.py", "core/local_tts.py"),
        "which speaker, which voice, volume routing",
    ),
    (
        "Community management (CM)",
        ("era_dispatch.py", "core/cm_", "core/vault_", "core/daily_note.py"),
        "brands, vault, ads, publishing",
    ),
    (
        "Self-healing & diagnosis",
        (
            "core/self_heal",
            "core/self_healing_router.py",
            "actions/self_",
            "core/handled_exceptions.py",
        ),
        "Era fixing or reporting on herself",
    ),
    (
        "Scheduling",
        ("core/task_scheduler.py", "era_scheduler.py", "actions/scheduled_task.py"),
        "timed and recurring jobs",
    ),
    (
        "Health & tests",
        ("tools/era_doctor.py", "tools/run_all_checks.py", "conftest.py"),
        "is the system OK, and does the suite pass",
    ),
]


def render_markdown(data: dict[str, Any]) -> str:
    c = data["counts"]
    caps = data["capabilities"]
    lines: list[str] = []
    add = lines.append

    add("# Era One — Codebase Manifest")
    add("")
    add("> **GENERATED FILE — do not edit by hand.**")
    add("> `python tools/era_manifest.py` rewrites it; `--check` fails when it is stale.")
    add("> Every fact here is read out of the source tree, so it cannot drift from the code.")
    add("")
    add("This is the map. Find the subsystem, jump to the file, skip the repo-wide scan.")
    add("")
    add(
        f"**{c['source_modules']} source modules** · {c['source_lines']:,} lines · "
        f"{c['test_modules']} test modules · {len(caps['tools'])} declared tools · "
        f"{len(caps['actions'])} registered actions"
    )
    if c["unparseable"]:
        add("")
        add(f"**{c['unparseable']} file(s) do not parse** — see the end of this document.")
    for err in caps.get("errors", []):
        add("")
        add(f"**Capability list incomplete:** {err}")
    add("")

    add("## 1. Where do I go?")
    add("")
    add("| Subsystem | Go here when | Entry points |")
    add("|---|---|---|")
    known = {m["path"] for m in data["modules"]}
    for heading, prefixes, when in SUBSYSTEMS:
        hits = sorted(p for p in known if p.startswith(prefixes))
        if not hits:
            continue
        shown = ", ".join(f"`{h}`" for h in hits[:4])
        if len(hits) > 4:
            shown += f" _(+{len(hits) - 4} more)_"
        add(f"| **{heading}** | {when} | {shown} |")
    add("")

    add("## 2. Capability manifest — what Era can actually do")
    add("")
    add("The tools the live model is offered, and the file that implements each.")
    add("This table is Era's self-model: if a tool is not here, she cannot do it.")
    add("")
    add("| Tool | Does | Required args | Implemented in |")
    add("|---|---|---|---|")
    for tool in caps["tools"]:
        req = ", ".join(f"`{r}`" for r in tool["required"]) or "—"
        handler = f"`{tool['handler']}`" if tool["handler"] else "_unresolved_"
        summary = tool["summary"].replace("|", "\\|")[:110]
        add(f"| `{tool['name']}` | {summary} | {req} | {handler} |")
    add("")

    unresolved = [t["name"] for t in caps["tools"] if not t["handler"]]
    if unresolved:
        add(
            f"_{len(unresolved)} tool(s) have no resolved implementation file: "
            + ", ".join(f"`{n}`" for n in unresolved)
            + ". Each is declared to the model but the index cannot point at code — "
            "either handled inline somewhere this scan does not reach, or dead._"
        )
        add("")

    add("## 3. Highest-traffic modules")
    add("")
    add("Most-imported first. A change here has the widest blast radius.")
    add("")
    add("| Module | Imported by | Purpose |")
    add("|---|---|---|")
    by_module = {m["module"]: m for m in data["modules"] if not m.get("error")}
    ranked = sorted(data["reverse_deps"].items(), key=lambda kv: -len(kv[1]))[:25]
    for module, dependents in ranked:
        doc = (by_module.get(module, {}).get("doc") or "").replace("|", "\\|")[:90]
        add(f"| `{module}` | {len(dependents)} | {doc} |")
    add("")

    add("## 4. Module index")
    add("")
    add("Every source module, its size, and what it is for.")
    add("")
    for heading, prefixes, _when in SUBSYSTEMS:
        group = [
            m
            for m in data["modules"]
            if not m.get("is_test") and not m.get("error") and m["path"].startswith(prefixes)
        ]
        if not group:
            continue
        add(f"### {heading}")
        add("")
        add("| File | Lines | Purpose |")
        add("|---|---:|---|")
        for m in group:
            doc = (m["doc"] or "").replace("|", "\\|")[:110]
            add(f"| `{m['path']}` | {m['lines']} | {doc} |")
        add("")

    if data["unparseable"]:
        add("## 5. Files that do not parse")
        add("")
        add("These are real breakage — a module that will not parse cannot be imported.")
        add("")
        for m in data["unparseable"]:
            add(f"- `{m['path']}` — {m['error']}")
        add("")

    return "\n".join(lines) + "\n"


# ── entry point ──────────────────────────────────────────────────────────────

MD_PATH = REPO / "docs" / "MANIFEST.md"
JSON_PATH = REPO / "runtime" / "manifest.json"


def _serialize(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the artifacts are stale")
    parser.add_argument("--stdout", action="store_true", help="print JSON, write nothing")
    args = parser.parse_args(argv)

    data = build()
    payload = _serialize(data)
    markdown = render_markdown(data)

    if args.stdout:
        print(payload)
        return 0

    if args.check:
        stale = []
        for path, want in ((JSON_PATH, payload), (MD_PATH, markdown)):
            have = path.read_text(encoding="utf-8") if path.exists() else None
            if have != want:
                stale.append(path.relative_to(REPO))
        if stale:
            print("STALE: " + ", ".join(str(p) for p in stale))
            print("Run: python tools/era_manifest.py")
            return 1
        print("Manifest is current.")
        return 0

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(payload, encoding="utf-8")
    MD_PATH.write_text(markdown, encoding="utf-8")
    c = data["counts"]
    print(
        f"Wrote {MD_PATH.relative_to(REPO)} and {JSON_PATH.relative_to(REPO)} — "
        f"{c['source_modules']} modules, {len(data['capabilities']['tools'])} tools, "
        f"{c['unparseable']} unparseable."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
