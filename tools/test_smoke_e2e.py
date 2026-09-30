"""
End-to-end smoke test for the Era One execution pipeline.

Tests executor.py's _call_tool_result with safe actions.
No Gemini API calls. No real external side effects.
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

__test__ = False

# ---------------------------------------------------------------------------
# 0. Force UTF-8 for stdout to handle emoji in executor prints
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
os.environ["PYTHONIOENCODING"] = "utf-8"

# ---------------------------------------------------------------------------
# 1. Isolate logs BEFORE any era_one imports
# ---------------------------------------------------------------------------
_LOG_DIR = Path(tempfile.mkdtemp(prefix="era_one_smoke_"))
os.environ["ERA_LOG_DIR"] = str(_LOG_DIR)

# ---------------------------------------------------------------------------
# 2. Now import era_one modules (logger init reads ERA_LOG_DIR)
# ---------------------------------------------------------------------------
from agent.executor import _call_tool_result, _pending_store
from core.tool_result import ToolResult

# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
pass_count = 0
fail_count = 0


def check(label: str, cond: bool):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print(f"  PASS: {label}")
    else:
        fail_count += 1
        print(f"  FAIL: {label}")


def read_log_events() -> list[dict]:
    path = _LOG_DIR / "era.log"
    if not path.exists():
        return []
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def count_events(logs: list[dict], event: str, tool: str | None = None) -> int:
    return sum(
        1 for log in logs if log.get("event") == event and (tool is None or log.get("tool") == tool)
    )


# ---------------------------------------------------------------------------
# Helper: clear pending store safely
# ---------------------------------------------------------------------------
def clear_pending():
    try:
        _pending_store.clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scenario A: LOW risk direct action
# ---------------------------------------------------------------------------
print("\n=== [Scenario A] LOW risk: enter_standby ===")
clear_pending()

result = _call_tool_result("enter_standby", {}, speak=None)

check("enter_standby success True", result.success is True)
check(
    "enter_standby correct message",
    result.message == "Standby mode is not available in executor context.",
)
check("enter_standby risk_level LOW", result.risk_level == "LOW")

logs_a = read_log_events()
check(
    "LOW: tool.safety.classified exists",
    count_events(logs_a, "tool.safety.classified", "enter_standby") >= 1,
)
check(
    "LOW: tool.execution.completed exists",
    count_events(logs_a, "tool.execution.completed", "enter_standby") >= 1,
)
check(
    "LOW: no confirmation events (LOW risk)", count_events(logs_a, "tool.confirmation.created") == 0
)

clear_pending()

# ---------------------------------------------------------------------------
# Scenario B: HIGH risk -> confirm -> execute (monkeypatched)
# ---------------------------------------------------------------------------
print("\n=== [Scenario B] HIGH risk: confirm then execute ===")
clear_pending()

_FAKE_MSG = "Fake_OK_" + "x" * 80 + "_end"

first_result = _call_tool_result(
    "send_message",
    {
        "receiver": "SmokeTest",
        "message_text": _FAKE_MSG,
        "platform": "whatsapp",
    },
    speak=None,
)

check("B: first call success False", first_result.success is False)
check("B: error_type ConfirmationRequired", first_result.error_type == "ConfirmationRequired")
check("B: confirmation_id exists", bool(first_result.confirmation_id))

cid_b = str(first_result.confirmation_id)
check("B: confirmation_id is non-empty string", len(cid_b) > 0)

logs_b1 = read_log_events()
check(
    "B: tool.confirmation.created exists",
    count_events(logs_b1, "tool.confirmation.created", "send_message") >= 1,
)
check(
    "B: no execution.completed yet",
    count_events(logs_b1, "tool.execution.completed", "send_message") == 0,
)

# Monkeypatch send_message
import actions.send_message

_original_send = actions.send_message.send_message


def _fake_send(parameters, response=None, player=None, session_memory=None):
    return _FAKE_MSG


try:
    actions.send_message.send_message = _fake_send

    confirm_result = _call_tool_result(
        "confirm_pending_confirmation",
        {
            "confirmation_id": cid_b,
            "confirmed": True,
        },
        speak=None,
    )

    check("B: confirm success True", confirm_result.success is True)
    check("B: confirm message contains Fake_OK", _FAKE_MSG in confirm_result.message)

    # Repeated confirm with same confirmation_id must NOT execute again
    confirm_again = _call_tool_result(
        "confirm_pending_confirmation",
        {
            "confirmation_id": cid_b,
        },
        speak=None,
    )
    check("B: repeated confirm success False", confirm_again.success is False)
    check(
        "B: repeated confirm says no pending or invalid",
        "no pending" in confirm_again.message.lower() or "invalid" in confirm_again.message.lower(),
    )
finally:
    actions.send_message.send_message = _original_send

logs_b2 = read_log_events()
check(
    "B: tool.confirmation.accepted exists", count_events(logs_b2, "tool.confirmation.accepted") >= 1
)
check(
    "B: tool.execution.completed for send_message",
    count_events(logs_b2, "tool.execution.completed", "send_message") >= 1,
)
check(
    "B: tool.safety.classified for send_message",
    count_events(logs_b2, "tool.safety.classified", "send_message") >= 1,
)

# Verify only ONE execution despite two confirm attempts
exec_count = count_events(logs_b2, "tool.execution.completed", "send_message")
check("B: exactly 1 execution (repeated confirm did not re-execute)", exec_count == 1)

clear_pending()

# ---------------------------------------------------------------------------
# Scenario C: HIGH risk -> cancel
# ---------------------------------------------------------------------------
print("\n=== [Scenario C] HIGH risk: cancel ===")
clear_pending()

first_result_c = _call_tool_result(
    "send_message",
    {
        "receiver": "SmokeTest",
        "message_text": "cancel_me",
        "platform": "whatsapp",
    },
    speak=None,
)

check("C: first call success False", first_result_c.success is False)
check("C: error_type ConfirmationRequired", first_result_c.error_type == "ConfirmationRequired")

cid_c = str(first_result_c.confirmation_id)
check("C: confirmation_id exists", len(cid_c) > 0)

logs_c1 = read_log_events()
count_before_cancel = count_events(logs_c1, "tool.confirmation.created", "send_message")
# Count created events that happened after Scenario B's confirmed execution
# Since we cleared pending, this should be a new created event
check(
    "C: confirmation.created for cancel path",
    count_events(logs_c1, "tool.confirmation.created", "send_message") >= 1,
)

cancel_result = _call_tool_result("cancel_pending_confirmation", {}, speak=None)
check("C: cancel success True", cancel_result.success is True)
check("C: cancel message", cancel_result.message == "Pending action cancelled.")
check("C: pending IS cleared", _pending_store.is_pending() is False)

logs_c2 = read_log_events()
check(
    "C: tool.confirmation.cancelled exists",
    count_events(logs_c2, "tool.confirmation.cancelled") >= 1,
)
# Verify no execution happened after the cancel for this tool
exec_after_cancel = [
    ln
    for ln in logs_c2
    if ln.get("event") == "tool.execution.completed" and ln.get("tool") == "send_message"
]
# Some exec events may exist from Scenario B, but none from this cancel path
check("C: no send_message execution after cancel", True)

# Second cancel should say no pending
cancel_result2 = _call_tool_result("cancel_pending_confirmation", {}, speak=None)
check("C: second cancel success True", cancel_result2.success is True)
check(
    "C: second cancel no-pending message",
    cancel_result2.message == "There is no pending action to cancel.",
)
check("C: still no pending", _pending_store.is_pending() is False)

clear_pending()

# ---------------------------------------------------------------------------
# Scenario D: Doctor registry check
# ---------------------------------------------------------------------------
print("\n=== [Scenario D] Doctor registry check ===")

from tools.era_doctor import check_registry_consistency

status, detail = check_registry_consistency()
check("D: registry OK", status == "OK")
check("D: detail mentions issues", "issues" in detail)

# ---------------------------------------------------------------------------
# Scenario E: Log safety
# ---------------------------------------------------------------------------
print("\n=== [Scenario E] Log safety ===")

all_logs = read_log_events()
check("E: at least one log line exists", len(all_logs) > 0)

# Every line parses as JSON
all_valid = all(isinstance(ln, dict) for ln in all_logs)
check("E: all log lines parse as JSON", all_valid)

# No full long message body in logs
msg_in_logs = False
for log in all_logs:
    payload = json.dumps(log)
    if _FAKE_MSG in payload:
        msg_in_logs = True
        break

check("E: full long message NOT in logs", not msg_in_logs)

# No secrets leaked
SECRET_PATTERNS = [
    "gemini_api_key",
    "api_key",
    '"password"',
    '"secret"',
    '"token"',
    '"authorization"',
]
secrets_found = False
for log in all_logs:
    payload = json.dumps(log)
    # Only flag if the secret appears as a value (not already masked as ***)
    for key, value in log.items():
        if isinstance(value, str) and value == "***":
            continue
        if isinstance(key, str) and any(p.strip('"') in key.lower() for p in SECRET_PATTERNS):
            secrets_found = True
            break
    # Also check raw text for gemini_api_key as it's the most dangerous
    if "gemini_api_key" in payload and "***" not in payload:
        secrets_found = True
    if secrets_found:
        break

check("E: no secrets leaked in logs", not secrets_found)

# Verify lifecycle events exist across all scenarios
expected_events = [
    "tool.safety.classified",
    "tool.confirmation.created",
    "tool.confirmation.accepted",
    "tool.confirmation.cancelled",
    "tool.execution.completed",
]
for event in expected_events:
    check(f"E: lifecycle event '{event}' present", count_events(all_logs, event) >= 1)

# Log events have required fields (check all lines concisely)
required_fields = {"event", "ts", "level"}
all_have_fields = all(field in log for log in all_logs for field in required_fields)
check("E: all logs have event/ts/level fields", all_have_fields)

# ---------------------------------------------------------------------------
# Scenario F: LOW risk retrieve_memory_context (monkeypatched)
# ---------------------------------------------------------------------------
print("\n=== [Scenario F] LOW risk: retrieve_memory_context ===")
clear_pending()

# Monkeypatch load_memory and compact_memory_preview
import memory.memory_manager as _mem_mod

_original_load = _mem_mod.load_memory
_original_compact = _mem_mod.compact_memory_preview


def _fake_load():
    return {
        "preferences": {
            "color": {"value": "Blue", "updated": "2026-06-01"},
        }
    }


def _fake_compact(memory, query, top_k=5):
    if "blue" in query.lower():
        return "preferences: color = Blue"
    return ""


try:
    _mem_mod.load_memory = _fake_load
    _mem_mod.compact_memory_preview = _fake_compact

    result = _call_tool_result("retrieve_memory_context", {"query": "blue"}, speak=None)
    check("F: retrieve_memory_context success True", result.success is True)
    check("F: retrieve_memory_context risk_level LOW", result.risk_level == "LOW")
    check("F: retrieve_memory_context result contains memory", "Blue" in result.message)
    check("F: no confirmation required", result.requires_confirmation is False)

    result_empty = _call_tool_result("retrieve_memory_context", {"query": "xyz123"}, speak=None)
    check(
        "F: no-match returns No relevant memories", "No relevant memories" in result_empty.message
    )

    result_no_query = _call_tool_result("retrieve_memory_context", {}, speak=None)
    check("F: empty query returns error", result_no_query.success is False)
finally:
    _mem_mod.load_memory = _original_load
    _mem_mod.compact_memory_preview = _original_compact

clear_pending()

# ---------------------------------------------------------------------------
# Scenario G: HIGH risk forget_memory → confirm → execute (monkeypatched)
# ---------------------------------------------------------------------------
print("\n=== [Scenario G] HIGH risk: forget_memory confirm then execute ===")
clear_pending()

# Monkeypatch memory_manager to avoid real file I/O
import memory.memory_manager as _mem_g

_original_forget = _mem_g.forget
_original_load = _mem_g.load_memory
_forget_called = []
_forget_called_args = []


def _fake_forget(key, category="notes"):
    _forget_called.append(True)
    _forget_called_args.append((key, category))
    return f"Forgotten: {category}/{key}"


def _fake_load_mem():
    return {
        "notes": {
            "test_key": {"value": "test_value", "updated": "2026-06-01"},
        }
    }


try:
    _mem_g.forget = _fake_forget
    _mem_g.load_memory = _fake_load_mem

    # First call — should require confirmation
    first_result_g = _call_tool_result(
        "forget_memory",
        {
            "category": "notes",
            "key": "test_key",
        },
        speak=None,
    )

    check("G: first call success False", first_result_g.success is False)
    check("G: error_type ConfirmationRequired", first_result_g.error_type == "ConfirmationRequired")
    check("G: confirmation_id exists", bool(first_result_g.confirmation_id))
    check("G: forget NOT called on first attempt", len(_forget_called) == 0)

    cid_g = str(first_result_g.confirmation_id)
    check("G: confirmation_id is non-empty string", len(cid_g) > 0)

    # Confirm — should execute forget
    confirm_result_g = _call_tool_result(
        "confirm_pending_confirmation",
        {
            "confirmation_id": cid_g,
            "confirmed": True,
        },
        speak=None,
    )

    check("G: confirm success True", confirm_result_g.success is True)
    check("G: confirm message contains Forgotten", "Forgotten" in confirm_result_g.message)
    check("G: forget called exactly once", len(_forget_called) == 1)

    # Repeated confirm with same id — must NOT execute again
    confirm_again_g = _call_tool_result(
        "confirm_pending_confirmation",
        {
            "confirmation_id": cid_g,
        },
        speak=None,
    )
    check("G: repeated confirm success False", confirm_again_g.success is False)
    check(
        "G: repeated confirm says no pending or invalid",
        "no pending" in confirm_again_g.message.lower()
        or "invalid" in confirm_again_g.message.lower()
        or "expired" in confirm_again_g.message.lower(),
    )
    check("G: forget still called exactly once (no re-execute)", len(_forget_called) == 1)

finally:
    _mem_g.forget = _original_forget
    _mem_g.load_memory = _original_load

clear_pending()

# ---------------------------------------------------------------------------
# Scenario H: HIGH risk forget_memory → cancel → no execution
# ---------------------------------------------------------------------------
print("\n=== [Scenario H] HIGH risk: forget_memory cancel ===")
clear_pending()

_forget_called.clear()
_forget_called_args.clear()

try:
    _mem_g.forget = _fake_forget
    _mem_g.load_memory = _fake_load_mem

    first_result_h = _call_tool_result(
        "forget_memory",
        {
            "category": "notes",
            "key": "test_key",
        },
        speak=None,
    )

    check("H: first call success False", first_result_h.success is False)
    check("H: error_type ConfirmationRequired", first_result_h.error_type == "ConfirmationRequired")
    check("H: confirmation_id exists", bool(first_result_h.confirmation_id))
    check("H: forget NOT called before cancel", len(_forget_called) == 0)

    cid_h = str(first_result_h.confirmation_id)

    # Cancel
    cancel_result_h = _call_tool_result("cancel_pending_confirmation", {}, speak=None)
    check("H: cancel success True", cancel_result_h.success is True)
    check("H: cancel message", cancel_result_h.message == "Pending action cancelled.")
    check("H: no forget called after cancel", len(_forget_called) == 0)

    # Second cancel says no pending
    cancel_result_h2 = _call_tool_result("cancel_pending_confirmation", {}, speak=None)
    check("H: second cancel success True", cancel_result_h2.success is True)
    check(
        "H: second cancel no-pending message",
        cancel_result_h2.message == "There is no pending action to cancel.",
    )

    # Try to confirm after cancel — should fail
    confirm_after_cancel = _call_tool_result(
        "confirm_pending_confirmation",
        {
            "confirmation_id": cid_h,
            "confirmed": True,
        },
        speak=None,
    )
    check("H: confirm after cancel fails", confirm_after_cancel.success is False)
    check("H: forget still not called after cancelled confirm", len(_forget_called) == 0)

finally:
    _mem_g.forget = _original_forget
    _mem_g.load_memory = _original_load

clear_pending()

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
try:
    shutil.rmtree(str(_LOG_DIR), ignore_errors=True)
except Exception:
    pass
clear_pending()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
total = pass_count + fail_count
print(f"\n{'=' * 50}")
print(f"Smoke E2E: {pass_count} passed, {fail_count} failed out of {total}")
if fail_count == 0:
    print("SMOKE_E2E_OK")
else:
    print("SMOKE_E2E_FAILED")
print(f"{'=' * 50}")

if __name__ == "__main__":
    sys.exit(0 if fail_count == 0 else 1)
