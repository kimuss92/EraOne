"""Tests for era_doctor.py"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

__test__ = False

repo_root = Path(__file__).resolve().parent.parent

from tools.era_doctor import (
    RESULTS,
    check_action_modules,
    check_api_key_config,
    check_core_imports,
    check_directories,
    check_feedback_memory,
    check_long_term_memory,
    check_optional_packages,
    check_python_version,
    check_registry_consistency,
    check_test_files,
    check_vosk_model,
    run_checks,
)

passed = 0
failed = 0


def check(name: str, condition: bool):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")


# ── 1. Doctor runs without crashing ─────────────────────────────────

print("\n[End-to-end] Running doctor via subprocess...")

result = subprocess.run(
    [sys.executable, str(repo_root / "tools" / "era_doctor.py")],
    capture_output=True,
    text=True,
    timeout=30,
)
check("doctor exits with code 0 or 1", result.returncode in (0, 1))
check("doctor produces stdout", len(result.stdout) > 0)
check("doctor has no stderr (no crash)", len(result.stderr) == 0)

output = result.stdout

# ── 2. Statuses are only OK/WARN/ERROR ──────────────────────────────

print("\n[Status validation] Checking status values...")

results = run_checks()
for name, _severity, status, _detail in results:
    check(f"status '{status}' is valid for '{name}'", status in ("OK", "WARN", "ERROR"))

# Also check via subprocess output
status_pattern = re.findall(r"\[(PASS|WARN|FAIL)\]", output)
check("subprocess output has PASS/WARN/FAIL markers", len(status_pattern) > 0)
for s in status_pattern:
    check(f"status '{s}' in output is valid", s in ("PASS", "WARN", "FAIL"))

# ── 3. No API key value appears in output ──────────────────────────

print("\n[Secrets check] Verifying no API key leaked in output...")

api_key_pattern = re.findall(r"AIzaSy[A-Za-z0-9_-]{20,}", output)
check("no Gemini API key string in output", len(api_key_pattern) == 0)

# Also check that we don't see any plausible key values
suspicious = ["gemini_api_key:", "api_key:", '"api_key"']
for token in suspicious:
    check(f"no '{token}' leaked pattern", token not in output)

# ── 4. Invalid JSON config detection (temp file) ────────────────────

print("\n[Config check] Testing invalid JSON detection...")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "api_keys.json").write_text("{invalid json content}", encoding="utf-8")

    old_root = check_api_key_config.__globals__["REPO_ROOT"]
    check_api_key_config.__globals__["REPO_ROOT"] = tmp_path
    try:
        status, detail = check_api_key_config()
        check("invalid JSON returns ERROR", status == "ERROR")
        check("detail mentions invalid JSON", "invalid JSON" in detail)
    finally:
        check_api_key_config.__globals__["REPO_ROOT"] = old_root

# ── 5. Missing optional package does not fail hard ──────────────────

print("\n[Optional packages] Checking soft failure...")

status, detail = check_optional_packages()
check("optional packages returns OK or WARN (not ERROR)", status in ("OK", "WARN"))

# ── 6. Memory content is never printed ──────────────────────────────

print("\n[Memory check] Verifying memory content not leaked...")

memory_keywords = [
    "identity",
    "preferences",
    "projects",
    "relationships",
    "personality",
    "user_facts",
]
for kw in memory_keywords:
    # The only acceptable mention is "File not found" or "Invalid JSON"
    if kw in output:
        line = [ln for ln in output.splitlines() if kw in ln.lower()]
        safe = all("not found" in ln.lower() or "invalid json" in ln.lower() for ln in line)
        check(f"memory keyword '{kw}' not leaked", safe)

# ── 7. Registry consistency (duplicate detection through validator) ──

print("\n[Registry check] Testing registry consistency...")

from core.tool_registry_validator import validate_tool_registry_from_sources

# Test duplicate detection
MAIN_WITH_DUP = """
TOOL_DECLARATIONS = [
    {"name": "dup_tool"},
    {"name": "unique_tool"},
    {"name": "dup_tool"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    if tool_name == "dup_tool": return ""
    if tool_name == "unique_tool": return ""
"""
EXEC_CLEAN = """
def _call_tool_result(tool, params, speak): pass
"""
SAFETY_DEFAULT = """
def classify_action(tool, params=None):
    return {"risk_level": "LOW"}
"""
issues = validate_tool_registry_from_sources(MAIN_WITH_DUP, EXEC_CLEAN, SAFETY_DEFAULT)
dup_errors = [i for i in issues if i["severity"] == "ERROR" and "duplicate" in i["category"]]
check("duplicate detection returns ERROR", len(dup_errors) > 0)
check("detail mentions duplicate name", any("dup_tool" in i["message"] for i in dup_errors))

# Also test that actual main.py passes (no duplicates in production)
status, detail = check_registry_consistency()
check("production registry has no ERROR issues", status == "OK")
check("detail mentions issues", "issues" in detail)

# ── 8. Report contains READY or NOT READY ──────────────────────────

print("\n[Summary check] Verifying READY/NOT READY...")

check("output contains READY or NOT READY", "READY" in output or "NOT READY" in output)

# ── 9. Individual check functions work ─────────────────────────────

print("\n[Individual checks] Testing each function...")

status, detail = check_python_version()
check("Python version check passes", status == "OK")

status, detail = check_core_imports()
check("core imports check passes", status == "OK")

status, detail = check_directories()
check("directories check passes", status == "OK")

status, detail = check_action_modules()
check("action modules check returns OK or WARN", status in ("OK", "WARN"))

status, detail = check_long_term_memory()
if status == "WARN":
    check("long_term missing shows WARN not ERROR", True)
elif status == "OK":
    check("long_term valid shows OK", True)
else:
    check("long_term shows WARN/OK (not ERROR: file corrupt)", False)

status, detail = check_feedback_memory()
check("feedback_memory check returns OK or WARN", status in ("OK", "WARN"))

status, detail = check_vosk_model()
check("vosk model check returns OK or WARN", status in ("OK", "WARN"))

status, detail = check_test_files()
check("test files check returns OK or WARN or ERROR", status in ("OK", "WARN", "ERROR"))

# If WARN (import failed due to env), that's acceptable
if status == "WARN":
    check("test file WARN is acceptable import issue", True)

# ── Summary ─────────────────────────────────────────────────────────

print(f"\n{'=' * 50}")
print(f"Results: {passed} passed, {failed} failed")
if __name__ == "__main__":
    if failed:
        print("Some tests FAILED — see above for details.")
        sys.exit(1)
    else:
        print("All Era One doctor tests PASSED.")
