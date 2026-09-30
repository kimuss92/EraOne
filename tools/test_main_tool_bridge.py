"""Tests for main.py tool bridge helpers and safety integration."""

import sys
from pathlib import Path

__test__ = False

repo_root = Path(__file__).resolve().parents[1]

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


from core.confirmation import ConfirmationManager
from core.main_tool_bridge import (
    _build_suggested_confirmation_question,
    _safe_preview_value,
    build_function_response,
    fc_args_to_dict,
    tool_result_to_response_payload,
)
from core.pending_confirmation import PendingConfirmationStore
from core.safety_guard import classify_action
from core.tool_result import ToolResult


class FakeFC:
    name = "test_tool"
    id = "fc-001"
    args = {"action": "search", "query": "test"}


class FakeFCEmpty:
    name = "empty_tool"
    id = "fc-002"
    args = None


class FakeFCBad:
    pass


class FakeFunctionResponse:
    def __init__(self, id, name, response):
        self.id = id
        self.name = name
        self.response = response


# -- fc_args_to_dict ---------------------------------------------------

print("\n[fc_args_to_dict] Testing arg extraction...")

d = fc_args_to_dict(FakeFC())
check("normal args to dict", isinstance(d, dict) and d.get("action") == "search")

d = fc_args_to_dict(FakeFCEmpty())
check("args=None -> {}", d == {})

d = fc_args_to_dict(FakeFCBad())
check("missing args attr -> {}", d == {})

# -- tool_result_to_response_payload -----------------------------------

print("\n[tool_result_to_response_payload] Testing payload conversion...")

p = tool_result_to_response_payload(ToolResult.ok("Task completed.", tool_name="t", duration_ms=50))
check("success payload has result", p.get("result") == "Task completed.")
check("success payload has success=True", p.get("success") is True)
check("success payload has duration_ms", p.get("duration_ms") == 50)
check("success payload has risk_level", p.get("risk_level") == "LOW")
check("success payload has verification_status", p.get("verification_status") == "NOT_VERIFIED")

p = tool_result_to_response_payload(
    ToolResult.fail("Op failed.", error_type="ValueError", tool_name="t")
)
check("fail payload success=False", p.get("success") is False)
check("fail payload error_type", p.get("error_type") == "ValueError")
check("fail payload error", p.get("error") == "Op failed.")

r = ToolResult.fail("Blocked", error_type="BlockedAction", tool_name="t")
r.risk_level = "BLOCKED"
p = tool_result_to_response_payload(r)
check("blocked payload risk_level", p.get("risk_level") == "BLOCKED")

r = ToolResult.fail("Needs confirm", error_type="ConfirmationRequired", tool_name="t")
r.risk_level = "HIGH"
r.requires_confirmation = True
p = tool_result_to_response_payload(r)
check("confirm payload risk_level", p.get("risk_level") == "HIGH")
check("confirm payload requires_confirmation", p.get("requires_confirmation") is True)

r = ToolResult.fail("Unknown tool: fake", error_type="UnknownTool", tool_name="fake")
p = tool_result_to_response_payload(r)
check("unknown tool payload error_type", p.get("error_type") == "UnknownTool")
check("unknown tool payload contains name", "fake" in p.get("result", ""))

r = ToolResult.ok("Done", tool_name="t")
r.risk_level = "HIGH"
r.requires_confirmation = True
r.verified = True
r.verification_status = "VERIFIED"
p = tool_result_to_response_payload(r)
check("payload carries risk_level", p.get("risk_level") == "HIGH")
check("payload carries requires_confirmation", p.get("requires_confirmation") is True)
check("payload carries verified", p.get("verified") is True)
check("payload carries verification_status", p.get("verification_status") == "VERIFIED")

r = ToolResult.ok("Done", tool_name="t")
r.data = {"big": "x" * 2000}
p = tool_result_to_response_payload(r)
check("large data excluded from payload", "data" not in p)

# -- build_function_response ------------------------------------------

print("\n[build_function_response] Testing response builder...")

r = ToolResult.ok("All good", tool_name="test_tool", duration_ms=30)
fr = build_function_response(FakeFC(), r, FakeFunctionResponse)
check("normal fc id", fr.id == "fc-001")
check("normal fc name", fr.name == "test_tool")
check("normal response result", fr.response["result"] == "All good")
check("normal response success", fr.response["success"] is True)
check("no silent flag for normal tool", "silent" not in fr.response)


class FakeFCSave:
    name = "save_memory"
    id = "fc-003"
    args = {"category": "notes", "key": "test", "value": "ok"}


r = ToolResult.ok("ok", tool_name="save_memory")
fr = build_function_response(FakeFCSave(), r, FakeFunctionResponse)
check("save_memory fc id", fr.id == "fc-003")
check("save_memory response result", fr.response["result"] == "ok")
check("save_memory silent flag", fr.response.get("silent") is True)

# -- classify_action integration ---------------------------------------

print("\n[classify_action] Integration tests...")

s = classify_action("open_app", {"app_name": "Chrome"})
check("open_app is LOW", s["risk_level"] in ("SAFE", "LOW"))

s = classify_action("web_search", {"query": "test"})
check("web_search is LOW", s["risk_level"] in ("SAFE", "LOW"))

s = classify_action("weather_report", {"city": "Paris"})
check("weather_report is LOW", s["risk_level"] in ("SAFE", "LOW"))

s = classify_action("send_message", {"receiver": "John", "message_text": "hi"})
check("send_message is HIGH", s["risk_level"] == "HIGH")
check("send_message requires confirmation", s["requires_confirmation"] is True)

s = classify_action("shutdown_era")
check("shutdown_era is LOW", s["risk_level"] in ("SAFE", "LOW"))
check("shutdown_era does NOT require confirmation", s["requires_confirmation"] is False)

s = classify_action("close_era")
check("close_era is LOW", s["risk_level"] in ("SAFE", "LOW"))
check("close_era does NOT require confirmation", s["requires_confirmation"] is False)

s = classify_action("exit_era")
check("exit_era is LOW", s["risk_level"] in ("SAFE", "LOW"))
check("exit_era does NOT require confirmation", s["requires_confirmation"] is False)

s = classify_action("enter_standby")
check("enter_standby is LOW", s["risk_level"] in ("SAFE", "LOW"))

s = classify_action("save_memory", {"category": "notes", "key": "x", "value": "y"})
check("save_memory is LOW", s["risk_level"] in ("SAFE", "LOW"))

s = classify_action("nonexistent_tool_xyz")
check("unknown tool defaults to LOW", s["risk_level"] in ("SAFE", "LOW"))

s = classify_action("browser_control", {"action": "search", "query": "test"})
check("browser search is LOW", s["risk_level"] in ("SAFE", "LOW"))

# -- ToolResult integration via payload --------------------------------

print("\n[ToolResult -> payload] Simulated _execute_tool_result outputs...")

r = ToolResult.fail("Unknown tool: fake_tool", error_type="UnknownTool", tool_name="fake_tool")
p = tool_result_to_response_payload(r)
check("unknown tool payload error_type", p.get("error_type") == "UnknownTool")
check("unknown tool payload success=False", p.get("success") is False)

r = ToolResult.fail(
    "Blocked unsafe action.",
    error="Protected path",
    error_type="BlockedAction",
    tool_name="file_controller",
)
r.risk_level = "BLOCKED"
p = tool_result_to_response_payload(r)
check("blocked payload risk_level", p.get("risk_level") == "BLOCKED")
check("blocked payload success=False", p.get("success") is False)

r = ToolResult.fail(
    "Confirmation required",
    error="Sending a message",
    error_type="ConfirmationRequired",
    tool_name="send_message",
)
r.risk_level = "HIGH"
r.requires_confirmation = True
p = tool_result_to_response_payload(r)
check("confirm required error_type", p.get("error_type") == "ConfirmationRequired")
check("confirm required risk_level", p.get("risk_level") == "HIGH")

r = ToolResult.ok("Hello", tool_name="t", action_name="greet", duration_ms=100)
p = tool_result_to_response_payload(r)
expected = {
    "result",
    "success",
    "verification_status",
    "verified",
    "error",
    "error_type",
    "risk_level",
    "requires_confirmation",
    "duration_ms",
}
check("all expected payload keys present", expected.issubset(p.keys()))

# -- Dangerous actions still require confirmation (Step 4.1) ----------

print("\n[Dangerous actions] Confirmation still required...")

s = classify_action("computer_settings", {"action": "shutdown"})
check("computer shutdown requires confirmation", s["requires_confirmation"] is True)

s = classify_action("computer_settings", {"action": "restart"})
check("computer restart requires confirmation", s["requires_confirmation"] is True)

s = classify_action("file_controller", {"action": "delete", "path": "C:/Users/test/Desktop/a.txt"})
check("file delete requires confirmation", s["requires_confirmation"] is True)

s = classify_action("send_message", {"message": "hello"})
check("send message requires confirmation", s["requires_confirmation"] is True)

# -- ConfirmationManager behavior (Step 4.1) --------------------------

# -- confirm_pending_confirmation / cancel_pending_confirmation safety --

print("\n[Confirmation tools] Testing safety classification...")

s = classify_action("confirm_pending_confirmation")
check("confirm_pending_confirmation is LOW", s["risk_level"] in ("SAFE", "LOW"))
check("confirm_pending_confirmation no confirmation", s["requires_confirmation"] is False)

s = classify_action("cancel_pending_confirmation")
check("cancel_pending_confirmation is LOW", s["risk_level"] in ("SAFE", "LOW"))
check("cancel_pending_confirmation no confirmation", s["requires_confirmation"] is False)

# -- TOOL_DECLARATIONS contains confirm_pending_confirmation / cancel_pending_confirmation --

print("\n[TOOL_DECLARATIONS] Verifying new tool declarations exist...")

try:
    import ast
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    main_py = (repo_root / "app" / "tool_declarations.py").read_text(encoding="utf-8")
    # Find TOOL_DECLARATIONS list via simple string search
    has_confirm = (
        '"confirm_pending_confirmation"' in main_py or "'confirm_pending_confirmation'" in main_py
    )
    has_cancel = (
        '"cancel_pending_confirmation"' in main_py or "'cancel_pending_confirmation'" in main_py
    )
    check("TOOL_DECLARATIONS contains confirm_pending_confirmation", has_confirm)
    check("TOOL_DECLARATIONS contains cancel_pending_confirmation", has_cancel)
except Exception as e:
    check(f"TOOL_DECLARATIONS scan failed: {e}", False)
    has_confirm = has_cancel = False

if has_confirm and has_cancel:
    check("both confirmation tools declared in TOOL_DECLARATIONS", True)

print("\n[ConfirmationManager] Testing request_confirmation...")

cm = ConfirmationManager()
check(
    "shutdown_era LOW returns True",
    cm.request_confirmation(tool="shutdown_era", action_name="", message="", risk_level="LOW")
    is True,
)
check(
    "send_message HIGH unconfirmed returns False",
    cm.request_confirmation(
        tool="send_message", action_name="", message="", risk_level="HIGH", confirmed=False
    )
    is False,
)
check(
    "send_message HIGH confirmed returns True",
    cm.request_confirmation(
        tool="send_message", action_name="", message="", risk_level="HIGH", confirmed=True
    )
    is True,
)
check(
    "LOW risk returns True",
    cm.request_confirmation(tool="open_app", action_name="", message="", risk_level="LOW") is True,
)

# -- record_tool_result arg shape (Step 3.1 hotfix) -------------------

print("\n[record_tool_result] Verifying second arg is always a dict...")

params = fc_args_to_dict(FakeFC())
check("second arg is dict for normal fc", isinstance(params, dict))
check("second arg contains expected key", params.get("action") == "search")

params = fc_args_to_dict(FakeFCEmpty())
check("second arg is dict for args=None", isinstance(params, dict))

params = fc_args_to_dict(FakeFCBad())
check("second arg is dict for missing args", isinstance(params, dict))

# -- cancel_pending_confirmation behavior -----------------------------

print("\n[cancel_pending_confirmation] Testing honest cancellation...")

store = PendingConfirmationStore()
check("cancel with no pending -> no pending msg", not store.is_pending())

store.store("send_message", {"to": "John"}, "HIGH", "test")
check("cancel with active pending clears", store.is_pending())
store.clear()
check("after clear -> no pending", not store.is_pending())

# -- shutdown_era duplicate guard ----------------------------------

print("\n[shutdown_era] Testing duplicate guard...")

# Simulate: first call returns normal, second returns already-in-progress
_requested = False


def simulate_shutdown():
    global _requested
    if _requested:
        return "Era One shutdown is already in progress."
    _requested = True
    return "Shutting down..."


check("first shutdown returns normal", simulate_shutdown() == "Shutting down...")
check(
    "second shutdown returns already in progress",
    simulate_shutdown() == "Era One shutdown is already in progress.",
)

s = classify_action("shutdown_era")
check("shutdown_era remains LOW", s["risk_level"] in ("SAFE", "LOW"))
check("shutdown_era no confirmation", s["requires_confirmation"] is False)

s = classify_action("computer_settings", {"action": "shutdown"})
check("computer shutdown still requires confirmation", s["requires_confirmation"] is True)

s = classify_action("computer_settings", {"action": "restart"})
check("computer restart still requires confirmation", s["requires_confirmation"] is True)

# -- _safe_preview_value tests ----------------------------------------

print("\n[_safe_preview_value] Testing safe value formatting...")

check("safe preview empty", _safe_preview_value("name", "") == "")
check("safe preview normal", _safe_preview_value("name", "John") == "John")
check("safe preview long truncated", len(_safe_preview_value("message", "x" * 200)) == 120)
check("safe preview long ends with ...", _safe_preview_value("message", "x" * 200).endswith("..."))
check("safe preview password masked", _safe_preview_value("password", "my_secret_123") == "***")
check("safe preview token masked", _safe_preview_value("api_key", "sk-1234") == "***")
check("safe preview secret masked", _safe_preview_value("secret", "hidden") == "***")

# -- _build_suggested_confirmation_question tests ---------------------

print("\n[_build_suggested_confirmation_question] Testing confirmation preview...")

# No question for non-ConfirmationRequired
r = ToolResult.ok("Done.")
check("success returns empty", _build_suggested_confirmation_question(r) == "")

r = ToolResult.fail("Blocked", error_type="BlockedAction")
check("blocked returns empty", _build_suggested_confirmation_question(r) == "")

# Default question for unknown tool
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="some_tool",
    action_name="some_tool",
)
check("default question", "do you want to" in _build_suggested_confirmation_question(r).lower())

# send_message preview
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="send_message",
    action_name="send_message",
    data={"platform": "WhatsApp", "receiver": "Marwa", "message_text": "Hello"},
)
q = _build_suggested_confirmation_question(r)
check("send_message preview contains platform", "WhatsApp" in q)
check("send_message preview contains receiver", "Marwa" in q)
check("send_message preview contains message", "Hello" in q)

# send_message with long message truncated
long_msg = "A" * 200
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="send_message",
    action_name="send_message",
    data={"platform": "WhatsApp", "receiver": "Marwa", "message_text": long_msg},
)
q = _build_suggested_confirmation_question(r)
check("long message truncated", "..." in q)

# send_message with sensitive fields masked
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="send_message",
    action_name="send_message",
    data={
        "platform": "WhatsApp",
        "receiver": "Marwa",
        "message_text": "Hello",
        "password": "secret123",
    },
)
q = _build_suggested_confirmation_question(r)
check("sensitive field not leaked", "***" in q or "secret123" not in q)

# file_controller preview
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="file_controller",
    action_name="delete",
    data={"action": "delete", "name": "test.txt"},
)
q = _build_suggested_confirmation_question(r)
check("file_controller preview contains action", "delete" in q)
check("file_controller preview contains name", "test.txt" in q)

# computer_settings preview
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="computer_settings",
    action_name="shutdown",
    data={"action": "shutdown"},
)
q = _build_suggested_confirmation_question(r)
check("computer_settings preview contains action", "shutdown" in q)

# forget_memory preview
r = ToolResult.fail(
    "Needs confirm",
    error_type="ConfirmationRequired",
    tool_name="forget_memory",
    action_name="forget_memory",
    data={"category": "preferences", "key": "favorite_color"},
)
q = _build_suggested_confirmation_question(r)
check("forget_memory preview contains category", "preferences" in q)
check("forget_memory preview contains key", "favorite_color" in q)
check(
    "forget_memory preview does NOT leak value",
    "favorite_color" not in q.split("favorite_color")[1] if "favorite_color" in q else True,
)

# -- retrieve_memory_context tool declaration --------------------------

print("\n[retrieve_memory_context] Verifying tool declaration and safety...")

try:
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    main_py = (repo_root / "app" / "tool_declarations.py").read_text(encoding="utf-8")
    has_retrieve = '"retrieve_memory_context"' in main_py or "'retrieve_memory_context'" in main_py
    check("TOOL_DECLARATIONS contains retrieve_memory_context", has_retrieve)
except Exception as e:
    check(f"TOOL_DECLARATIONS scan failed: {e}", False)
    has_retrieve = False

safety = classify_action("retrieve_memory_context")
check("retrieve_memory_context classified as LOW", safety["risk_level"] == "LOW")
check("retrieve_memory_context no confirmation required", safety["requires_confirmation"] is False)
check("retrieve_memory_context reason mentions read-only", "read-only" in safety["reason"].lower())

# Test compact_memory_preview returns relevant memory
from memory.memory_manager import compact_memory_preview

_test_mem = {
    "preferences": {
        "color": {"value": "Blue", "updated": "2026-06-01"},
        "food": {"value": "Italian pasta", "updated": "2026-06-02"},
    }
}
result = compact_memory_preview(_test_mem, "blue", top_k=5)
check(
    "retrieve_memory: relevant result contains 'blue' or 'Blue'",
    "blue" in result.lower() or "Blue" in result,
)
check("retrieve_memory: result non-empty for matching query", len(result) > 0)

empty_result = compact_memory_preview({"notes": {"x": {"value": "nothing"}}}, "xyz123", top_k=5)
check(
    "retrieve_memory: no-match still returns entries (ranked, not filtered)", len(empty_result) > 0
)
# The output is BARE "category/key: value" lines — no header, deliberately.
# agent/executor.py returns this straight back as the agent lane's
# retrieve_memory_context result, so anything decorative here would be read by
# the model as if it were remembered content.
#
# This check used to assert `"[COMPACT MEMORY" in empty_result`. That header
# has never existed in any version of compact_memory_preview (the July 2026
# backup is byte-identical on this point), so the check had been failing since
# it was written — invisibly, because check() only printed. Corrected
# 2026-09-20 to assert the contract the function actually has.
check(
    "retrieve_memory: preview lines are bare 'category/key: value'",
    all(line.startswith("notes/x: ") for line in empty_result.splitlines() if line.strip()),
)
check("retrieve_memory: preview carries no decorative header", "[" not in empty_result)

# -- canonicalize_tool_call / YouTube volume routing ------------------

from core.action_registry import ACTION_SPECS, canonicalize_tool_call, get_spec

print("\n[YouTube volume routing] Testing canonicalize_tool_call...")

tool, params = canonicalize_tool_call("youtube_video", {"action": "volume_up"})
check("youtube_video volume_up stays youtube_video", tool == "youtube_video")
check("youtube_video volume_up preserves action", params.get("action") == "volume_up")
check("youtube_video volume_up defaults amount to 10", params.get("amount") == 10)

tool, params = canonicalize_tool_call("youtube_video", {"action": "volume_up", "amount": 20})
check(
    "youtube_video volume_up with custom amount",
    tool == "youtube_video" and params.get("amount") == 20,
)

tool, params = canonicalize_tool_call("youtube_video", {"action": "volume_down"})
check("youtube_video volume_down stays youtube_video", tool == "youtube_video")
check("youtube_video volume_down preserves action", params.get("action") == "volume_down")
check("youtube_video volume_down defaults amount to 10", params.get("amount") == 10)

tool, params = canonicalize_tool_call("computer_settings", {"action": "volume_up"})
check("generic volume_up goes computer_settings", tool == "computer_settings")

tool, params = canonicalize_tool_call("computer_settings", {"action": "volume_down"})
check("generic volume_down goes computer_settings", tool == "computer_settings")

# Ensure youtube_volume_up / youtube_volume_down specs exist
check("youtube_volume_up spec exists", get_spec("youtube_volume_up") is not None)
check(
    "youtube_volume_up spec maps to youtube_video",
    get_spec("youtube_volume_up").tool == "youtube_video",
)
check("youtube_volume_down spec exists", get_spec("youtube_volume_down") is not None)
check(
    "youtube_volume_down spec maps to youtube_video",
    get_spec("youtube_volume_down").tool == "youtube_video",
)

# Ensure generic volume_up spec still maps to computer_settings
check(
    "generic volume_up spec maps to computer_settings",
    get_spec("volume_up").tool == "computer_settings",
)
check(
    "generic volume_down spec maps to computer_settings",
    get_spec("volume_down").tool == "computer_settings",
)

# youtube_video + volume_up must not be rewritten via spec
spec_via_spec = get_spec("volume_up")
tool_spec, params_spec = (
    spec_via_spec.tool,
    spec_via_spec.action if spec_via_spec else ("none", None),
)
check("volume_up spec tool is computer_settings (unchanged)", tool_spec == "computer_settings")

# Test aliases still work
tool, params = canonicalize_tool_call("computer_settings", {"action": "louder"})
check(
    "alias 'louder' routes to computer_settings volume_up",
    tool == "computer_settings" and params.get("action") == "volume_up",
)

tool, params = canonicalize_tool_call("computer_settings", {"action": "quieter"})
check(
    "alias 'quieter' routes to computer_settings volume_down",
    tool == "computer_settings" and params.get("action") == "volume_down",
)

# -- Summary ----------------------------------------------------------

print(f"\n{'=' * 40}")
print(f"Main tool bridge tests: {passed} passed, {failed} failed out of {passed + failed}")
print(f"{'=' * 40}")
