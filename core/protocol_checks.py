"""core/protocol_checks.py — the evidence-gathering checks Protocol One runs.

Every check returns Findings built from tool output (ruff JSON, pytest summary
lines, regex hits) — never from a model. A check that cannot run says so as a
finding of its own; it never reports "clean" because it was blind. That exact
failure — ruff absent from Era's interpreter, lint gates passing on UNAVAILABLE —
was live when this module was written.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECK_DIRS = ("actions", "agent", "app", "core", "utils", "windows", "memory")
_TEST_TIMEOUT_S = 1500
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Tests take ~11 minutes; run them below normal priority so Era stays responsive.
_LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) | _NO_WINDOW

# Correctness rules: each one is a real bug or dead code, never style.
CORRECTNESS_RULES = {
    "E9": "high",  # syntax / io errors
    "F63": "high",  # invalid comparisons
    "F7": "high",  # break/continue/return outside their scope
    "F82": "high",  # undefined names
    "F811": "medium",  # a definition silently shadowed by a later one
    "F841": "low",  # assigned, never used
}
# Mechanically safe: whitespace, import ORDER. F401 is deliberately absent —
# removing an "unused" import breaks every `from module import name` re-export
# and every `patch("module.name")` in the tests, which ruff cannot see.
SAFE_AUTOFIX_RULES = ("I001", "E401", "W291", "W292", "W293")

# High-signal security rules. S110/S112 (bare except-pass) and S603/S607
# (subprocess with a list) fire hundreds of times on intentional code — noise.
SECURITY_RULES = {
    "S102": "high",  # exec
    "S104": "medium",  # bind all interfaces
    "S306": "medium",  # tempfile.mktemp race
    "S602": "high",  # subprocess shell=True
    "S605": "medium",  # os.system / shell start
    "S608": "medium",  # SQL string building
}
# Reviewed and accepted, WITH the reason. Shown in the report, never hidden.
ACCEPTED_RISKS = {
    ("core/feature_engine.py", "S102"): "Intentional trusted Python plugin loading, not a sandbox. "
    "Only direct features/*.py paths load; deployment requires explicit verified approval, "
    "and restore checks approved content hashes. Plugins execute with Era's privileges.",
    ("main.py", "S104"): "LAN bridge refuses every request unless ERA_REMOTE_TOKEN is set "
    "(hmac.compare_digest) — _remote_bridge_authorized.",
    ("actions/netflix_control.py", "S608"): "False positive: an f-string spoken reply, not SQL.",
    ("actions/spotify_control.py", "S605"): "Static 'spotify:' string, or a URI built with "
    "urllib.parse.quote — no shell metacharacter can survive.",
    ("actions/computer_settings.py", "S605"): "Static string 'start ms-settings:bluetooth'.",
    ("utils/find_position.py", "S605"): "Static 'cls' in a developer calibration script.",
}


@dataclass
class Finding:
    signature: str
    severity: str
    category: str
    title: str
    evidence: str = ""
    location: str = ""
    fix: str = "review"  # autofix | model | review
    accepted: str = ""  # reason, when this is a reviewed-and-accepted risk
    # Identical findings in one file share a signature (so a drifting line number
    # does not churn the hive), but they are still N problems, not one. The count
    # and the other lines ride along so the report never under-counts.
    count: int = 1
    also_at: str = ""  # "145, 166": the lines of the other occurrences

    def to_dict(self) -> dict:
        return asdict(self)


def ruff_command() -> list[str] | None:
    """Era's interpreter first; then any ruff on PATH. None = truly unavailable."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode == 0:
            return [sys.executable, "-m", "ruff"]
    except (OSError, subprocess.SubprocessError):
        pass
    exe = shutil.which("ruff")
    return [exe] if exe else None


def _ruff_json(select: list[str], paths: list[str]) -> tuple[list[dict], str]:
    cmd = ruff_command()
    if cmd is None:
        return [], "ruff is not installed for Era's interpreter nor on PATH"
    try:
        proc = subprocess.run(
            [
                *cmd,
                "check",
                "--no-fix",
                "--output-format",
                "json",
                "--select",
                ",".join(select),
                *paths,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"ruff failed to run: {e}"
    out = (proc.stdout or "").strip()
    if not out.startswith("["):
        return [], (proc.stderr or out or "ruff produced no JSON")[:300]
    try:
        return json.loads(out), ""
    except ValueError as e:
        return [], f"unreadable ruff output: {e}"


def _rel(filename: str) -> str:
    try:
        return Path(filename).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return filename


def _targets() -> list[str]:
    return ["*.py", *[d for d in _CHECK_DIRS if (REPO_ROOT / d).is_dir()]]


def _rule_severity(code: str, table: dict[str, str]) -> str | None:
    for prefix, sev in table.items():
        if code.startswith(prefix):
            return sev
    return None


def _toolchain_finding(err: str) -> Finding:
    return Finding(
        "toolchain:ruff", "high", "toolchain", "Lint checks could not run", err, sys.executable
    )


def check_lint() -> list[Finding]:
    rules = list(CORRECTNESS_RULES) + list(SAFE_AUTOFIX_RULES) + ["F401"]
    paths = [str(p) for p in REPO_ROOT.glob("*.py")] + _targets()[1:]
    items, err = _ruff_json(rules, paths)
    if err:
        return [_toolchain_finding(err)]
    out: list[Finding] = []
    by_signature: dict[str, Finding] = {}
    for d in sorted(
        items,
        key=lambda x: (x.get("filename", ""), (x.get("location") or {}).get("row", 0)),
    ):
        code = d.get("code") or ""
        rel = _rel(d.get("filename", ""))
        line = (d.get("location") or {}).get("row", 0)
        msg = d.get("message", "")
        signature = f"lint:{rel}:{code}:{msg}"
        seen = by_signature.get(signature)
        if seen is not None:
            # Same rule, same message, same file: one finding with a true count.
            seen.count += 1
            seen.also_at = f"{seen.also_at}, {line}" if seen.also_at else str(line)
            continue
        if code in SAFE_AUTOFIX_RULES:
            sev, fix = "low", "autofix"
        elif code == "F401":
            sev, fix = "low", "review"
        else:
            sev = _rule_severity(code, CORRECTNESS_RULES) or "low"
            fix = "model" if sev in ("high", "medium") else "review"
        finding = Finding(signature, sev, "lint", f"{code} {msg}", msg, f"{rel}:{line}", fix)
        by_signature[signature] = finding
        out.append(finding)
    return out


def check_security() -> list[Finding]:
    from core.protocol_spies import scan_secrets

    out: list[Finding] = []
    paths = [str(p) for p in REPO_ROOT.glob("*.py")] + _targets()[1:]
    items, err = _ruff_json(list(SECURITY_RULES), paths)
    if err:
        out.append(_toolchain_finding(err))
    for d in items:
        code = d.get("code") or ""
        rel = _rel(d.get("filename", ""))
        line = (d.get("location") or {}).get("row", 0)
        out.append(
            Finding(
                f"security:{rel}:{code}:{line}",
                _rule_severity(code, SECURITY_RULES) or "medium",
                "security",
                f"{code} {d.get('message', '')}"[:200],
                location=f"{rel}:{line}",
                # A tempfile race has one mechanical fix; the rest are design calls.
                fix="model" if code == "S306" else "review",
                accepted=ACCEPTED_RISKS.get((rel, code), ""),
            )
        )
    scan_roots = [REPO_ROOT / d for d in _CHECK_DIRS] + [REPO_ROOT]
    for root in scan_roots:
        files = root.glob("*.py") if root == REPO_ROOT else root.rglob("*.py")
        for p in files:
            if "__pycache__" in p.parts:
                continue
            try:
                found = scan_secrets(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if found:
                rel = _rel(str(p))
                out.append(
                    Finding(
                        f"secret:{rel}",
                        "critical",
                        "security",
                        f"Credential in source: {', '.join(found)}",
                        "Value withheld. Move it to config/api_keys.json and rotate the key.",
                        rel,
                    )
                )
    return out


_FAILED = re.compile(r"^(FAILED|ERROR) (\S+?)(?:::(\S+))?(?: - (.*))?$")

# What the last check_tests() run actually measured. The report states these
# numbers instead of trusting a one-line summary: totals, and the scope that
# was run (the project's own testpaths, `tools` AND the repo root — running only
# `tools/` silently skipped 91 root-level tests).
_LAST_TEST_STATS: dict = {}


def last_test_stats() -> dict:
    """Totals from the most recent check_tests() run (empty if it did not run)."""
    return dict(_LAST_TEST_STATS)


def _first_line(text: str, limit: int = 300) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line:
            return line[:limit]
    return ""


def _failures_from_junit(xml_path: Path) -> tuple[list[Finding], dict] | None:
    """Failures + totals from pytest's junit XML, WITH the real error message.

    The console summary truncates every reason to the terminal width ("Asser...",
    "Playbac...") so a report built from it could say a test failed but not why.
    The XML carries the whole message. None = unreadable (caller falls back to
    the console parse, which is the old, lossy behaviour).
    """
    import xml.etree.ElementTree as ET  # nosec - our own pytest output file

    try:
        root = ET.parse(xml_path).getroot()  # noqa: S314 - our own pytest output
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    stats = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in stats:
            try:
                stats[key] += int(suite.get(key) or 0)
            except ValueError:
                pass
    stats["passed"] = max(
        0, stats["tests"] - stats["failures"] - stats["errors"] - stats["skipped"]
    )
    out: list[Finding] = []
    for case in root.iter("testcase"):
        bad = case.find("failure")
        kind = "Failed"
        if bad is None:
            bad = case.find("error")
            kind = "Error"
        if bad is None:
            continue
        path = (case.get("file") or "").replace("\\", "/")
        if not path:
            module = (case.get("classname") or "").split(".")
            path = (
                "/".join(module[:2]) + ".py" if len(module) >= 2 else (case.get("classname") or "")
            )
        stem = path[:-3].replace("/", ".") if path.endswith(".py") else path
        owner = case.get("classname") or ""
        owner = owner[len(stem) + 1 :] if owner.startswith(stem + ".") else ""
        name = f"{owner}::{case.get('name')}" if owner else (case.get("name") or "")
        message = _first_line(bad.get("message") or "") or _first_line(bad.text or "")
        detail = ""
        for raw in (bad.text or "").splitlines():  # pytest marks the real error with "E "
            if raw.startswith("E ") and raw[2:].strip():
                detail = raw[2:].strip()
        evidence = message if not detail or detail in message else f"{message} | {detail}"
        out.append(
            Finding(
                f"tests:{path}::{name}",
                "high",
                "tests",
                f"{kind}: {path}::{name}",
                evidence[:400],
                path,
            )
        )
    return out, stats


def check_tests() -> tuple[list[Finding], str]:
    """Returns (failures, pytest's final summary line).

    Runs the project's own testpaths (`tools` and the repo root), not a hand-picked
    subset, and reads results from junit XML so each failure carries its real cause.
    """
    import tempfile

    _LAST_TEST_STATS.clear()
    xml_path = Path(tempfile.gettempdir()) / f"protocol_one_pytest_{int(time.time())}.xml"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={xml_path}",
                "-o",
                "junit_family=xunit1",
            ],
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT_S,
            cwd=str(REPO_ROOT),
            encoding="utf-8",
            errors="replace",
            creationflags=_LOW_PRIORITY,
        )
    except subprocess.TimeoutExpired:
        return [
            Finding(
                "tests:timeout",
                "high",
                "tests",
                f"Test suite did not finish within {_TEST_TIMEOUT_S}s",
            )
        ], "timed out"
    except (OSError, subprocess.SubprocessError) as e:
        return [Finding("tests:unrunnable", "high", "tests", f"Test suite could not run: {e}")], ""
    out: list[Finding] = []
    parsed = _failures_from_junit(xml_path)
    try:
        xml_path.unlink(missing_ok=True)
    except OSError:
        pass
    if parsed is not None:
        out, stats = parsed
        _LAST_TEST_STATS.update(stats)
    else:  # no XML (pytest died early): the console lines, truncated but better than nothing
        for line in (proc.stdout or "").splitlines():
            m = _FAILED.match(line.strip())
            if m:
                kind, path, test, msg = m.groups()
                out.append(
                    Finding(
                        f"tests:{path}::{test}",
                        "high",
                        "tests",
                        f"{kind.title()}: {path}::{test or ''}",
                        (msg or "")[:300],
                        path,
                    )
                )
    summary = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    if proc.returncode not in (0, 1) and not out:
        # pytest exit 2-4: collection/usage errors. The suite did not really run.
        out.append(
            Finding(
                "tests:collection", "high", "tests", "Test suite failed to run", summary[0][:300]
            )
        )
    return out, summary[0][:200]
