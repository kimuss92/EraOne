from __future__ import annotations

__test__ = False

from core.tool_registry_validator import (
    _extract_bracket_section,
    _extract_function_body,
    extract_declared_tool_name_list,
    extract_tool_names_from_declarations,
    extract_tool_names_from_executor_dispatch,
    extract_tool_names_from_main_dispatch,
    extract_tool_risk_map,
    validate_tool_registry_from_sources,
)

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


# ── Extraction tests with synthetic data ──

MAIN_SRC = """
TOOL_DECLARATIONS = [
    {"name": "open_app", "parameters": {"type": "OBJECT", "properties": {}, "required": []}},
    {"name": "send_message", "parameters": {"type": "OBJECT", "properties": {
        "receiver": {"type": "STRING"},
        "message_text": {"type": "STRING"},
        "platform": {"type": "STRING"}
    }, "required": ["receiver", "message_text", "platform"]}},
    {"name": "web_search", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]}},
    {"name": "generated_code", "parameters": {"type": "OBJECT", "properties": {"description": {"type": "STRING"}}, "required": ["description"]}},
    {"name": "confirm_pending_confirmation", "parameters": {"type": "OBJECT", "properties": {"confirmation_id": {"type": "STRING"}}, "required": []}},
    {"name": "cancel_pending_confirmation", "parameters": {"type": "OBJECT", "properties": {"confirmation_id": {"type": "STRING"}}, "required": []}},
    {"name": "retrieve_memory_context", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]}},
    {"name": "shutdown_era", "parameters": {"type": "OBJECT", "properties": {}, "required": []}},
]

async def _execute_tool_raw(self, tool_name: str, args: dict, fc=None) -> str:
    if tool_name == "enter_standby":
        return "Standby"
    if tool_name == "save_memory":
        return "Saved"
    if tool_name == "retrieve_memory_context":
        return "Retrieved"
    if tool_name == "open_app":
        return "Opened"
    elif tool_name == "send_message":
        return "Sent"
    elif tool_name in ("shutdown_era", "close_era", "exit_era"):
        return "Shutdown"
    elif tool_name == "confirm_pending_confirmation":
        return "Confirmed"
    elif tool_name == "cancel_pending_confirmation":
        return "Cancelled"
    elif tool_name == "generated_code":
        return "Generated"
    else:
        return "Unknown tool"
"""

EXECUTOR_SRC = """
def _call_tool_result(tool: str, parameters: dict, speak, confirmed=False, auto_confirm=False):
    if tool == "open_app":
        raw = "Opened"
    elif tool == "send_message":
        raw = "Sent"
    elif tool == "web_search":
        raw = "Searched"
    elif tool == "confirm_pending_confirmation":
        raw = "Confirmed"
    elif tool == "cancel_pending_confirmation":
        raw = "Cancelled"
    elif tool == "retrieve_memory_context":
        raw = "Retrieved"
    elif tool == "generated_code":
        raw = "Generated"
    else:
        return "Unknown"
    return raw
"""

SAFETY_SRC = """
LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"
BLOCKED = "BLOCKED"

def classify_action(tool: str, parameters: dict | None = None) -> dict:
    params = parameters or {}
    action = (params.get("action") or "").lower().strip()

    if tool in {"confirm_pending_confirmation", "cancel_pending_confirmation"}:
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Confirmation management."}

    if tool == "retrieve_memory_context":
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Read-only memory retrieval."}

    if tool == "send_message":
        return {"risk_level": HIGH, "requires_confirmation": True, "reason": "Sending message."}

    if tool in {"shutdown_era", "close_era", "exit_era"}:
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Shutdown."}

    LOW_TOOLS = {"open_app", "web_search"}
    if tool in LOW_TOOLS:
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Standard tool."}

    if tool in ("media_coordinator", "netflix_control", "spotify_control"):
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Media tool."}

    return {"risk_level": LOW, "requires_confirmation": False, "reason": "Default LOW."}
"""

print("\n[Extraction tests]")
print("  --- extract_declared_tool_name_list ---")
names_list = extract_declared_tool_name_list(MAIN_SRC)
check("returns list", isinstance(names_list, list))
check("finds open_app", "open_app" in names_list)
check("finds confirm_pending_confirmation", "confirm_pending_confirmation" in names_list)
check("finds cancel_pending_confirmation", "cancel_pending_confirmation" in names_list)
check("finds shutdown_era", "shutdown_era" in names_list)
check("finds all 8 tools", len(names_list) == 8)

print("  --- extract_tool_names_from_declarations ---")
declared = extract_tool_names_from_declarations(MAIN_SRC)
check("returns set", isinstance(declared, set))
check("open_app in set", "open_app" in declared)

print("  --- extract_tool_names_from_main_dispatch ---")
main_handlers = extract_tool_names_from_main_dispatch(MAIN_SRC)
check("captures close_era from tuple", "close_era" in main_handlers)
check("captures exit_era from tuple", "exit_era" in main_handlers)
check("captures shutdown_era", "shutdown_era" in main_handlers)
check("captures open_app", "open_app" in main_handlers)
check("captures enter_standby", "enter_standby" in main_handlers)
check("captures save_memory", "save_memory" in main_handlers)
check("captures generated_code", "generated_code" in main_handlers)

print("  --- extract_tool_names_from_executor_dispatch ---")
exec_handlers = extract_tool_names_from_executor_dispatch(EXECUTOR_SRC)
check("captures open_app", "open_app" in exec_handlers)
check("captures generated_code", "generated_code" in exec_handlers)
check("captures confirm_pending_confirmation", "confirm_pending_confirmation" in exec_handlers)

print("  --- extract_tool_risk_map ---")
safety_map = extract_tool_risk_map(SAFETY_SRC)
check("confirm_pending_confirmation = LOW", safety_map.get("confirm_pending_confirmation") == "LOW")
check("cancel_pending_confirmation = LOW", safety_map.get("cancel_pending_confirmation") == "LOW")
check("send_message = HIGH", safety_map.get("send_message") == "HIGH")
check("shutdown_era = LOW", safety_map.get("shutdown_era") == "LOW")
check("close_era = LOW", safety_map.get("close_era") == "LOW")
check("exit_era = LOW", safety_map.get("exit_era") == "LOW")
check("open_app = LOW", safety_map.get("open_app") == "LOW")
check("media_coordinator = LOW", safety_map.get("media_coordinator") == "LOW")
check("netflix_control = LOW", safety_map.get("netflix_control") == "LOW")
check("spotify_control = LOW", safety_map.get("spotify_control") == "LOW")

print("  --- _extract_bracket_section ---")
check("nonexistent marker returns None", _extract_bracket_section("hello", "NOT_THERE") is None)
check("extracts between brackets", _extract_bracket_section("x = [a, b, c]", "x = [") == "a, b, c")

print("  --- _extract_function_body ---")
check("nonexistent function returns None", _extract_function_body("x = 1", "no_such_func") is None)
src_with_func = "def foo(x):\n    return x + 1"
body = _extract_function_body(src_with_func, "foo")
check("extracts function body", body is not None and "return x + 1" in body)
check("body is single line", body.strip() == "return x + 1")

print("  --- Empty source tests ---")
check("empty declarations list", extract_declared_tool_name_list("no declarations") == [])
check("empty declarations set", extract_tool_names_from_declarations("") == set())
check("empty main dispatch", extract_tool_names_from_main_dispatch("") == set())
check(
    "empty executor dispatch", extract_tool_names_from_executor_dispatch("def foo(): pass") == set()
)
check("empty risk map", extract_tool_risk_map("no classify_action") == {})

# ── Validation tests with synthetic sources ──

print("\n[Validation tests]")

print("  --- Clean registry ---")
MAIN_CLEAN = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
    {"name": "send_message"},
    {"name": "web_search"},
    {"name": "shutdown_era"},
    {"name": "confirm_pending_confirmation"},
    {"name": "cancel_pending_confirmation"},
    {"name": "retrieve_memory_context"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    if tool_name == "open_app": return ""
    if tool_name == "send_message": return ""
    if tool_name == "web_search": return ""
    if tool_name in ("shutdown_era", "close_era", "exit_era"): return ""
    if tool_name == "confirm_pending_confirmation": return ""
    if tool_name == "cancel_pending_confirmation": return ""
    if tool_name == "retrieve_memory_context": return ""
"""

EXEC_CLEAN = """
def _call_tool_result(tool, params, speak):
    if tool == "open_app": return ""
    if tool == "send_message": return ""
    if tool == "web_search": return ""
    if tool == "confirm_pending_confirmation": return ""
    if tool == "cancel_pending_confirmation": return ""
    if tool == "retrieve_memory_context": return ""
"""

SAFETY_CLEAN = """
def classify_action(tool, params=None):
    if tool in {"confirm_pending_confirmation", "cancel_pending_confirmation"}:
        return {"risk_level": "LOW"}
    if tool == "retrieve_memory_context":
        return {"risk_level": "LOW"}
    if tool == "send_message":
        return {"risk_level": "HIGH"}
    if tool in {"shutdown_era", "close_era", "exit_era"}:
        return {"risk_level": "LOW"}
    LOW_TOOLS = {"open_app", "web_search"}
    if tool in LOW_TOOLS:
        return {"risk_level": "LOW"}
    return {"risk_level": "LOW"}
"""

issues = validate_tool_registry_from_sources(MAIN_CLEAN, EXEC_CLEAN, SAFETY_CLEAN)
check("clean registry: no ERROR issues", all(i["severity"] != "ERROR" for i in issues))

print("  --- Missing confirmation tools ---")
MAIN_NO_CONFIRM = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
    {"name": "send_message"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    if tool_name == "open_app": return ""
"""
EXEC_NO_CONFIRM = """
def _call_tool_result(tool, params, speak):
    if tool == "open_app": return ""
"""
SAFETY_NO_CONFIRM = """
def classify_action(tool, params=None):
    LOW_TOOLS = {"open_app"}
    if tool in LOW_TOOLS:
        return {"risk_level": "LOW"}
    return {"risk_level": "LOW"}
"""
issues = validate_tool_registry_from_sources(MAIN_NO_CONFIRM, EXEC_NO_CONFIRM, SAFETY_NO_CONFIRM)
has_confirm_error = any(
    i["severity"] == "ERROR"
    and (
        "confirm_pending_confirmation" in i["message"]
        or "cancel_pending_confirmation" in i["message"]
    )
    for i in issues
)
check("missing confirm tools produces ERROR", has_confirm_error)

print("  --- send_message wrong risk ---")
SAFETY_WRONG_SEND = """
def classify_action(tool, params=None):
    if tool == "send_message":
        return {"risk_level": "LOW"}
    return {"risk_level": "LOW"}
"""
issues = validate_tool_registry_from_sources(MAIN_CLEAN, EXEC_CLEAN, SAFETY_WRONG_SEND)
has_send_error = any(
    i["severity"] == "ERROR" and "send_message" in i["message"] and "HIGH" in i["message"]
    for i in issues
)
check("send_message wrong risk produces ERROR", has_send_error)

print("  --- Shutdown family wrong risk ---")
SAFETY_WRONG_SHUTDOWN = """
def classify_action(tool, params=None):
    if tool in {"shutdown_era", "close_era", "exit_era"}:
        return {"risk_level": "HIGH"}
    return {"risk_level": "LOW"}
"""
issues = validate_tool_registry_from_sources(MAIN_CLEAN, EXEC_CLEAN, SAFETY_WRONG_SHUTDOWN)
has_shutdown_error = any(
    i["severity"] == "ERROR"
    and (
        "shutdown_era" in i["message"] or "close_era" in i["message"] or "exit_era" in i["message"]
    )
    for i in issues
)
check("shutdown family wrong risk produces ERROR", has_shutdown_error)

print("  --- Duplicate declarations ---")
MAIN_DUP = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
    {"name": "send_message"},
    {"name": "open_app"},
]
"""
issues = validate_tool_registry_from_sources(MAIN_DUP, EXEC_CLEAN, SAFETY_CLEAN)
has_dup_error = any(i["severity"] == "ERROR" and "duplicate" in i["category"] for i in issues)
check("duplicate declarations produces ERROR", has_dup_error)

print("  --- Declared tool with no handler ---")
MAIN_NO_HANDLER = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
    {"name": "phantom_tool"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    pass
"""
EXEC_NO_HANDLER = """
def _call_tool_result(tool, params, speak):
    pass
"""
issues = validate_tool_registry_from_sources(MAIN_NO_HANDLER, EXEC_NO_HANDLER, SAFETY_CLEAN)
has_no_handler_error = any(
    i["severity"] == "ERROR" and "phantom_tool" in i["message"] and "no handler" in i["message"]
    for i in issues
)
check("phantom tool produces ERROR", has_no_handler_error)

print("  --- Main-only tools produce WARN ---")
MAIN_MORE = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
    {"name": "main_only_tool_x"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    if tool_name == "open_app": return ""
    if tool_name == "main_only_tool_x": return ""
"""
EXEC_LESS = """
def _call_tool_result(tool, params, speak):
    pass
"""
issues = validate_tool_registry_from_sources(MAIN_MORE, EXEC_LESS, SAFETY_CLEAN)
has_main_only = any(i["severity"] == "WARN" and "main_only" in i["category"] for i in issues)
check("main-only tools produce WARN", has_main_only)

print("  --- Executor-only tools produce WARN ---")
MAIN_FEW = """
TOOL_DECLARATIONS = [
    {"name": "exec_only_tool_y"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    pass
"""
EXEC_MORE = """
def _call_tool_result(tool, params, speak):
    if tool == "exec_only_tool_y": return ""
"""
issues = validate_tool_registry_from_sources(MAIN_FEW, EXEC_MORE, SAFETY_CLEAN)
has_exec_only = any(i["severity"] == "WARN" and "executor_only" in i["category"] for i in issues)
check("executor-only tools produce WARN", has_exec_only)

print("  --- Alias-only tools do NOT produce WARN (intentional registry design) ---")
MAIN_ALIAS = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    if tool_name == "open_app": return ""
    if tool_name in ("close_era", "exit_era"): return ""
"""
issues = validate_tool_registry_from_sources(MAIN_ALIAS, EXEC_LESS, SAFETY_CLEAN)
has_alias_only = any(i["severity"] == "WARN" and "alias_only" in i["category"] for i in issues)
check("alias-only tools do NOT produce WARN", not has_alias_only)

print("  --- Alias mapping: close_era -> shutdown_era ---")
issues = validate_tool_registry_from_sources(MAIN_ALIAS, EXEC_LESS, SAFETY_CLEAN)
close_in_declared = "close_era" in extract_declared_tool_name_list(MAIN_ALIAS)
check("close_era not required in TOOL_DECLARATIONS", not close_in_declared)
from core.tool_registry_validator import _INTERNAL_TOOLS, _TOOL_ALIASES

check("close_era mapped to shutdown_era", _TOOL_ALIASES.get("close_era") == "shutdown_era")
check("exit_era mapped to shutdown_era", _TOOL_ALIASES.get("exit_era") == "shutdown_era")

print("  --- Internal tool: generated_code ---")
MAIN_INTERNAL = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    pass
"""
EXEC_INTERNAL = """
def _call_tool_result(tool, params, speak):
    if tool == "generated_code": return ""
    if tool == "open_app": return ""
"""
issues = validate_tool_registry_from_sources(MAIN_INTERNAL, EXEC_INTERNAL, SAFETY_CLEAN)
exec_warns_with_gc = [
    i
    for i in issues
    if i["severity"] == "WARN"
    and "executor_only" in i["category"]
    and "generated_code" in i["message"]
]
check("generated_code not mentioned in executor-only WARN", len(exec_warns_with_gc) == 0)
# open_app is legitimately executor-only here (no handler in MAIN_INTERNAL) — that's fine
check("generated_code is listed in _INTERNAL_TOOLS", "generated_code" in _INTERNAL_TOOLS)

print("  --- Missing safety produces INFO ---")
SAFETY_SPARSE = """
def classify_action(tool, params=None):
    return {"risk_level": "LOW"}
"""
main_with_tools = """
TOOL_DECLARATIONS = [
    {"name": "open_app"},
    {"name": "web_search"},
]
async def _execute_tool_raw(self, tool_name, args, fc=None):
    if tool_name == "open_app": return ""
    if tool_name == "web_search": return ""
"""
issues = validate_tool_registry_from_sources(main_with_tools, EXEC_LESS, SAFETY_SPARSE)
has_no_safety = any(
    i["severity"] == "INFO" and "no_safety" in i["category"] and "open_app" in i["message"]
    for i in issues
)
check("missing safety produces INFO", has_no_safety)

# ── On-disk extraction ──

print("\n[On-disk extraction tests]")
from core.tool_registry_validator import (
    extract_declared_tool_name_list_on_disk,
    extract_tool_names_from_declarations_on_disk,
    extract_tool_names_from_executor_dispatch_on_disk,
    extract_tool_names_from_main_dispatch_on_disk,
    extract_tool_risk_map_on_disk,
)

names_list_disk = extract_declared_tool_name_list_on_disk()
check("on-disk declarations non-empty", len(names_list_disk) > 0)
check("on-disk has open_app", "open_app" in names_list_disk)
check("on-disk has send_message", "send_message" in names_list_disk)
check("on-disk has shutdown_era", "shutdown_era" in names_list_disk)

main_handlers_disk = extract_tool_names_from_main_dispatch_on_disk()
check("on-disk main dispatch non-empty", len(main_handlers_disk) > 0)
check("on-disk main has open_app", "open_app" in main_handlers_disk)
check("on-disk main has enter_standby", "enter_standby" in main_handlers_disk)

exec_handlers_disk = extract_tool_names_from_executor_dispatch_on_disk()
check("on-disk executor dispatch non-empty", len(exec_handlers_disk) > 0)
check("on-disk executor has open_app", "open_app" in exec_handlers_disk)

safety_map_disk = extract_tool_risk_map_on_disk()
check("on-disk safety non-empty", len(safety_map_disk) > 0)
check("on-disk send_message HIGH", safety_map_disk.get("send_message") == "HIGH")
check("on-disk shutdown_era LOW", safety_map_disk.get("shutdown_era") == "LOW")
check("on-disk confirm_pending LOW", safety_map_disk.get("confirm_pending_confirmation") == "LOW")

# ── Requirement-specific alias checks ──
check("close_era NOT in TOOL_DECLARATIONS", "close_era" not in names_list_disk)
check("exit_era NOT in TOOL_DECLARATIONS", "exit_era" not in names_list_disk)
check("shutdown_era IS in TOOL_DECLARATIONS", "shutdown_era" in names_list_disk)
check("shutdown_era LOW safety", safety_map_disk.get("shutdown_era") == "LOW")
check("generated_code NOT in TOOL_DECLARATIONS", "generated_code" not in names_list_disk)
check("generated_code in _INTERNAL_TOOLS", "generated_code" in _INTERNAL_TOOLS)

# ── Full on-disk validation ──
print("  --- Full on-disk validation ---")
from core.tool_registry_validator import validate_tool_registry

all_issues = validate_tool_registry()
errors = [i for i in all_issues if i["severity"] == "ERROR"]
check("no ERROR issues in real registry", len(errors) == 0)

executor_only_warns = [
    i for i in all_issues if i["severity"] == "WARN" and "executor_only" in i["category"]
]
check("generated_code does not produce executor-only WARN", len(executor_only_warns) == 0)

# Grouped report
warns = [i for i in all_issues if i["severity"] == "WARN"]
infos = [i for i in all_issues if i["severity"] == "INFO"]
print(
    f"\n  Registry health: {len(all_issues)} issues ({len(errors)} errors, {len(warns)} warnings, {len(infos)} info)"
)
if warns:
    print("  WARNING categories:")
    for w in warns:
        print(f"    [{w['category']}] {w['message']}")
if infos:
    print("  INFO categories:")
    for info in infos:
        print(f"    [{info['category']}] {info['message']}")

# ── Summary ──
print(f"\n{'=' * 40}")
print(f"Results: {pass_count} passed, {fail_count} failed out of {pass_count + fail_count}")
if fail_count == 0:
    print("All tests passed.")
else:
    print("Some tests FAILED.")
print(f"{'=' * 40}")
