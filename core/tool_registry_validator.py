from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Source-level extraction helpers
# ---------------------------------------------------------------------------


def _extract_bracket_section(
    source: str, marker: str, open_b: str = "[", close_b: str = "]"
) -> str | None:
    start = source.find(marker)
    if start == -1:
        return None
    bracket_start = start + source[start:].find(open_b)
    depth = 0
    for i in range(bracket_start, len(source)):
        if source[i] == open_b:
            depth += 1
        elif source[i] == close_b:
            depth -= 1
            if depth == 0:
                return source[bracket_start + 1 : i]
    return None


def _extract_function_body(source: str, func_name: str) -> str | None:
    lines = source.splitlines()
    def_indent = -1
    def_idx = -1
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"def {func_name}(") or stripped.startswith(
            f"async def {func_name}("
        ):
            def_indent = len(line) - len(line.lstrip())
            def_idx = i
            break
    if def_idx == -1:
        return None

    # Find end of signature by balancing parentheses from the def line
    sig_end_idx = def_idx
    paren_count = 0
    for i in range(def_idx, len(lines)):
        paren_count += lines[i].count("(") - lines[i].count(")")
        if paren_count == 0:
            sig_end_idx = i
            break

    body_parts = []
    for i in range(sig_end_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.rstrip()
        if stripped == "":
            body_parts.append(stripped)
            continue
        leading = len(line) - len(line.lstrip())
        if leading > def_indent:
            body_parts.append(stripped)
        else:
            break
    return "\n".join(body_parts)


# ---------------------------------------------------------------------------
# Per-file extraction functions (take source strings)
# ---------------------------------------------------------------------------

RE_TOOL_NAME = re.compile(r'"name":\s*"([^"]+)"')


def extract_declared_tool_name_list(source: str) -> list[str]:
    section = _extract_bracket_section(source, "TOOL_DECLARATIONS = [")
    if section is None:
        return []
    return RE_TOOL_NAME.findall(section)


def extract_tool_names_from_declarations(source: str) -> set[str]:
    return set(extract_declared_tool_name_list(source))


def extract_tool_names_from_main_dispatch(source: str) -> set[str]:
    body = _extract_function_body(source, "_execute_tool_raw")
    if body is None:
        return set()
    names: set[str] = set()
    names.update(re.findall(r'(?:if|elif)\s+tool_name\s*==\s*"([^"]+)"', body))
    for match in re.findall(r"(?:if|elif)\s+tool_name\s+in\s+\(([^)]+)\)", body):
        for name in re.findall(r'"([^"]+)"', match):
            names.add(name)
    return names


def extract_tool_names_from_executor_dispatch(source: str) -> set[str]:
    body = _extract_function_body(source, "_call_tool_result")
    if body is None:
        return set()
    return set(re.findall(r'(?:if|elif)\s+tool\s*==\s*"([^"]+)"', body))


def _risk_after(lines: list[str], start: int, max_scan: int = 15) -> str | None:
    """Scan up to max_scan lines from start for the first 'risk_level' value."""
    for i in range(start, min(start + max_scan, len(lines))):
        m = re.search(r'"risk_level":\s*"?(\w+)"?', lines[i])
        if m:
            return m.group(1)
    return None


def extract_tool_risk_map(source: str) -> dict[str, str]:
    body = _extract_function_body(source, "classify_action")
    if body is None:
        return {}
    result: dict[str, str] = {}
    lines = body.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()

        # tool in {"a", "b"}:
        m = re.match(r"^if\s+tool\s+in\s+\{([^}]+)\}\s*:\s*(?:$|#)", stripped)
        if m:
            risk = _risk_after(lines, i + 1)
            if risk:
                for name in re.findall(r'"([^"]+)"', m.group(1)):
                    result[name] = risk

        # if tool == "name":  (simple tool name match only, not action-dependent)
        m2 = re.match(r'^if\s+tool\s*==\s*"([^"]+)"\s*:\s*(?:$|#)', stripped)
        if m2:
            risk = _risk_after(lines, i + 1)
            if risk:
                result[m2.group(1)] = risk

        # tool in ("media_coordinator", "netflix_control", "spotify_control"):
        m3 = re.search(
            r'tool\s+in\s+\(?"media_coordinator",\s*"netflix_control",\s*"spotify_control"',
            stripped,
        )
        if m3:
            for t in ("media_coordinator", "netflix_control", "spotify_control"):
                result[t] = "LOW"

    # LOW_TOOLS = {"a", "b", ...}
    low_match = re.search(r"LOW_TOOLS\s*=\s*\{([^}]+)\}", body)
    if low_match:
        for name in re.findall(r'"([^"]+)"', low_match.group(1)):
            result[name] = "LOW"

    return result


# ---------------------------------------------------------------------------
# File-reading wrappers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# These point at the files the 2026-07-23 refactor moved the two registries
# into. They used to both be main.py; after the split main.py only RE-EXPORTS
# TOOL_DECLARATIONS and inherits _execute_tool_raw from a mixin, so its SOURCE
# TEXT contains neither. This validator works on source text, so it silently
# started extracting 0 declarations and 0 handlers — every cross-check then
# compared two empty sets and passed vacuously, while the confirmation-tool and
# shutdown-family checks reported errors for tools that were in fact correctly
# wired. A registry safety check that cannot see the registry is worse than no
# check, so keep these pointed at wherever the definitions actually live.
_DECLARATIONS_PY = REPO_ROOT / "app" / "tool_declarations.py"
_MAIN_DISPATCH_PY = REPO_ROOT / "app" / "era_live" / "tool_dispatch.py"
_EXECUTOR_PY = REPO_ROOT / "agent" / "executor.py"
_SAFETY_PY = REPO_ROOT / "core" / "safety_guard.py"


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_live_registry_source() -> str:
    """Declarations + live dispatch, concatenated.

    The two used to sit in one file. The *_from_sources helpers still take a
    single "main" source so their synthetic-input tests keep working, so join
    the two real files here — the extractors key off distinct markers
    (`TOOL_DECLARATIONS = [` and `def _execute_tool_raw`), never position.
    """
    return _read_source(_DECLARATIONS_PY) + "\n" + _read_source(_MAIN_DISPATCH_PY)


def extract_tool_names_from_declarations_on_disk() -> set[str]:
    return extract_tool_names_from_declarations(_read_source(_DECLARATIONS_PY))


def extract_declared_tool_name_list_on_disk() -> list[str]:
    return extract_declared_tool_name_list(_read_source(_DECLARATIONS_PY))


def extract_tool_names_from_main_dispatch_on_disk() -> set[str]:
    return extract_tool_names_from_main_dispatch(_read_source(_MAIN_DISPATCH_PY))


def extract_tool_names_from_executor_dispatch_on_disk() -> set[str]:
    return extract_tool_names_from_executor_dispatch(_read_source(_EXECUTOR_PY))


def extract_tool_risk_map_on_disk() -> dict[str, str]:
    return extract_tool_risk_map(_read_source(_SAFETY_PY))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CONFIRM_TOOLS = {"confirm_pending_confirmation", "cancel_pending_confirmation"}
_SHUTDOWN_FAMILY = {"shutdown_era", "close_era", "exit_era"}

# Aliases are handled by runtime dispatch but intentionally not declared to Gemini.
_TOOL_ALIASES: dict[str, str] = {
    "close_era": "shutdown_era",
    "exit_era": "shutdown_era",
}

# Internal executor-only tools — never exposed to Gemini.
_INTERNAL_TOOLS: set[str] = {"generated_code"}

# Tools whose safety is determined entirely by action parameter (compound conditions
# the static extractor cannot resolve). These do not produce "no explicit safety" INFO.
_ACTION_CLASSIFIED_TOOLS: set[str] = {
    "browser_control",
    "computer_settings",
    "desktop_control",
    "window_control",
    "agent_browser",
    "gmail_control",
    "scheduled_task",
}


def validate_tool_registry_from_sources(
    main_src: str, executor_src: str, safety_src: str
) -> list[dict]:
    declared_names_list = extract_declared_tool_name_list(main_src)
    declared_names = set(declared_names_list)
    main_handlers = extract_tool_names_from_main_dispatch(main_src)
    executor_handlers = extract_tool_names_from_executor_dispatch(executor_src)
    safety_map = extract_tool_risk_map(safety_src)

    issues: list[dict] = []

    # ── Duplicate check ──
    if len(declared_names_list) != len(declared_names):
        dupes = {n: c for n, c in Counter(declared_names_list).items() if c > 1}
        details = "; ".join(f"'{n}' appears {c}x" for n, c in dupes.items())
        issues.append(
            {
                "severity": "ERROR",
                "category": "duplicate",
                "message": f"Duplicate tool names in TOOL_DECLARATIONS: {details}",
            }
        )

    # ── Confirmation tools ──
    for t in _CONFIRM_TOOLS:
        if t not in declared_names:
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "missing_confirmation",
                    "message": f"Confirmation tool '{t}' not declared in TOOL_DECLARATIONS",
                }
            )
        if t not in main_handlers:
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "missing_confirmation",
                    "message": f"Confirmation tool '{t}' not dispatchable in main.py",
                }
            )
        if t not in executor_handlers:
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "missing_confirmation",
                    "message": f"Confirmation tool '{t}' not dispatchable in agent/executor.py",
                }
            )
        risk = safety_map.get(t)
        if risk != "LOW":
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "wrong_risk",
                    "message": f"Confirmation tool '{t}' has risk '{risk}', expected 'LOW'",
                }
            )

    # ── Shutdown family ──
    for t in _SHUTDOWN_FAMILY:
        if t == "shutdown_era" and t not in declared_names:
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "missing_declaration",
                    "message": f"Tool '{t}' not declared in TOOL_DECLARATIONS",
                }
            )
        if safety_map.get(t) != "LOW":
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "wrong_risk",
                    "message": f"Shutdown tool '{t}' has risk '{safety_map.get(t)}', expected 'LOW'",
                }
            )

    # ── send_message HIGH ──
    if safety_map.get("send_message") != "HIGH":
        issues.append(
            {
                "severity": "ERROR",
                "category": "wrong_risk",
                "message": f"send_message has risk '{safety_map.get('send_message')}', expected 'HIGH'",
            }
        )

    # ── Declared tools with no handler at all ──
    for t in declared_names:
        if t not in main_handlers and t not in executor_handlers:
            issues.append(
                {
                    "severity": "ERROR",
                    "category": "no_handler",
                    "message": f"Tool '{t}' declared in TOOL_DECLARATIONS but has no handler in main.py or executor.py",
                }
            )

    # ── Main-only tools (in TOOL_DECLARATIONS + main dispatch but not executor) ──
    main_only_candidates = (
        (declared_names & main_handlers) - executor_handlers - _CONFIRM_TOOLS - _SHUTDOWN_FAMILY
    )
    if main_only_candidates:
        tools_str = ", ".join(sorted(main_only_candidates))
        issues.append(
            {
                "severity": "WARN",
                "category": "main_only",
                "message": f"Tools dispatchable in main.py but missing from executor.py: {tools_str}",
            }
        )

    # ── Executor-only tools (exclude internal-only tools) ──
    executor_only = executor_handlers - main_handlers - _CONFIRM_TOOLS - _INTERNAL_TOOLS
    if executor_only:
        tools_str = ", ".join(sorted(executor_only))
        issues.append(
            {
                "severity": "WARN",
                "category": "executor_only",
                "message": f"Tools dispatchable in executor.py but not in main.py: {tools_str}",
            }
        )

    # ── Alias-only tools intentionally NOT warned — handled by runtime dispatch ──
    # Tools in _TOOL_ALIASES keys (close_era, exit_era) appear in dispatch
    # but are purposely omitted from TOOL_DECLARATIONS. No WARN needed.

    # ── Missing explicit safety classification ──
    for t in sorted(declared_names):
        if t not in safety_map and t not in _SHUTDOWN_FAMILY and t not in _ACTION_CLASSIFIED_TOOLS:
            issues.append(
                {
                    "severity": "INFO",
                    "category": "no_safety",
                    "message": f"Tool '{t}' has no explicit safety classification (defaults to LOW)",
                }
            )

    return issues


def validate_tool_registry() -> list[dict]:
    return validate_tool_registry_from_sources(
        _read_live_registry_source(),
        _read_source(_EXECUTOR_PY),
        _read_source(_SAFETY_PY),
    )


def registry_summary() -> str:
    declared = extract_tool_names_from_declarations_on_disk()
    main_handlers = extract_tool_names_from_main_dispatch_on_disk()
    executor_handlers = extract_tool_names_from_executor_dispatch_on_disk()
    safety = extract_tool_risk_map_on_disk()
    return (
        f"{len(declared)} declared, {len(main_handlers)} dispatchable in main.py, "
        f"{len(executor_handlers)} in executor.py, {len(safety)} with explicit safety"
    )
