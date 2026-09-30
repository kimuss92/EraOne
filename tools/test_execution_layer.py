__test__ = False

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


def run():
    global passed, failed
    print("[ToolResult] Testing ToolResult.from_any()...")

    from core.tool_result import NOT_VERIFIED, ToolResult

    r1 = ToolResult.from_any(True)
    check("from_any(True).success is True", r1.success is True)

    r2 = ToolResult.from_any(False)
    check("from_any(False).success is False", r2.success is False)

    r3 = ToolResult.from_any("Done")
    check("from_any('Done').success is True", r3.success is True)

    r4 = ToolResult.from_any("Error: failed")
    check("from_any('Error: failed').success is False", r4.success is False)

    r5 = ToolResult.from_any({"success": True, "message": "ok"})
    check("from_any(dict).success is True", r5.success is True and r5.message == "ok")

    r6 = ToolResult.from_any({"success": False, "error": "boom"})
    check("from_any(fail dict).success is False", r6.success is False and r6.error == "boom")

    r7 = ToolResult.from_any(None)
    check("from_any(None).success is True", r7.success is True)

    r8 = ToolResult.from_any(ToolResult.ok("wrapped"))
    check("from_any(ToolResult) passthrough", r8.message == "wrapped" and r8.success is True)

    # --- safety_guard ---
    print("\n[safety_guard] Testing classify_action()...")

    from core.safety_guard import classify_action

    s1 = classify_action(
        "file_controller", {"action": "delete", "path": "desktop", "name": "x.txt"}
    )
    check("file_controller delete requires confirmation", s1["requires_confirmation"] is True)

    s2 = classify_action("send_message", {"message": "hello"})
    check("send_message requires confirmation", s2["requires_confirmation"] is True)

    s3 = classify_action("open_app", {"app_name": "spotify"})
    check("open_app does not require confirmation", s3["requires_confirmation"] is False)

    s4 = classify_action("file_controller", {"action": "delete", "path": "C:\\Windows\\system.ini"})
    check("file_controller delete in protected path is BLOCKED", s4["risk_level"] == "BLOCKED")

    # --- verification ---
    print("\n[verification] Testing verify_tool_result()...")

    from core.verification import verify_tool_result

    v1 = verify_tool_result(ToolResult.ok("Done"))
    check(
        "verify_tool_result(ok) returns NOT_VERIFIED with no expected",
        v1.verification_status == "NOT_VERIFIED",
    )

    v2 = verify_tool_result(ToolResult.fail("failed"))
    check("verify_tool_result(fail) returns FAILED", v2.verification_status == "FAILED")

    # --- to_dict / __bool__ / short_message ---
    print("\n[ToolResult] Testing utility methods...")

    d = ToolResult.ok("hello").to_dict()
    check("to_dict has success key", d.get("success") is True)
    check("to_dict has message key", d.get("message") == "hello")

    check("__bool__ returns success", bool(ToolResult.ok()) is True)
    check("__bool__ for failure", bool(ToolResult.fail()) is False)

    short = ToolResult.ok("a" * 1000).short_message(50)
    check("short_message truncates", len(short) <= 53)

    ok_result = ToolResult.ok("Done")
    fail_result = ToolResult.fail("Error: something went wrong")

    check("ok().success is True", ok_result.success is True)
    check("fail().success is False", fail_result.success is False)
    check("fail().error is set", fail_result.error == "Error: something went wrong")

    print(f"\n{'=' * 40}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
    if failed == 0:
        print("Execution layer diagnostics passed.")
    else:
        print("Some diagnostics failed.")


if __name__ == "__main__":
    run()
