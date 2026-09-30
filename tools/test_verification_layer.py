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

    # ── NEW ToolResult fields ──
    print("[ToolResult] New field defaults...")
    from core.tool_result import ToolResult

    r = ToolResult.ok("test")
    check("verified defaults to False", r.verified is False)
    check("verifier_name defaults to None", r.verifier_name is None)
    check("verification_message defaults to ''", r.verification_message == "")

    r2 = ToolResult.ok("test")
    r2.verified = True
    r2.verifier_name = "my_verifier"
    r2.verification_message = "all good"
    d = r2.to_dict()
    check("to_dict includes verified", d.get("verified") is True)
    check("to_dict includes verifier_name", d.get("verifier_name") == "my_verifier")
    check("to_dict includes verification_message", d.get("verification_message") == "all good")

    # ── append_verification_phrase ──
    r3 = ToolResult.ok("File created")
    r3.verification_status = "NOT_VERIFIED"
    phrase = r3.append_verification_phrase()
    check(
        "append_verification_phrase appends not verified",
        phrase == "File created (not independently verified)",
    )

    r4 = ToolResult.ok("File created")
    r4.verification_status = "VERIFIED"
    phrase2 = r4.append_verification_phrase()
    check("append_verification_phrase unchanged when verified", phrase2 == "File created")

    r5 = ToolResult.fail("error")
    r5.verification_status = "FAILED"
    phrase3 = r5.append_verification_phrase()
    check("append_verification_phrase unchanged when failed", phrase3 == "error")

    # ── verify_action_result router ──
    print("\n[action_verifiers] verify_action_result routing...")
    from core.action_verifiers import verify_action_result

    # open_app verifier
    r6 = ToolResult.ok("Spotify opened successfully")
    r6 = verify_action_result("open_app", r6)
    check("open_app verifier sets verified=True", r6.verified is True)
    check("open_app sets verifier_name", r6.verifier_name == "open_app")
    check("open_app sets verification_status=VERIFIED", r6.verification_status == "VERIFIED")

    # open_app failure message
    r7 = ToolResult.ok("could not locate")
    r7 = verify_action_result("open_app", r7, {"action": "open"})
    check("open_app does not fail on 'could not locate' (open_app is lenient)", r7.verified is True)

    # browser_control failure
    r8 = ToolResult.ok("Unknown action: go_away")
    r8 = verify_action_result("browser_control", r8, {"action": "go_away"})
    check("browser_control fails on 'Unknown action'", r8.verified is False)
    check("browser_control sets status FAILED", r8.verification_status == "FAILED")
    check("browser_control sets success=False", r8.success is False)

    # file_controller failure
    r9 = ToolResult.ok("Could not create file: access denied")
    r9 = verify_action_result("file_controller", r9, {"action": "write"})
    check("file_controller fails on 'Could not'", r9.verified is False)

    # send_message failure
    r10 = ToolResult.ok("PyAutoGUI is not installed")
    r10 = verify_action_result("send_message", r10, {"action": "whatsapp"})
    check("send_message fails on 'not installed'", r10.verified is False)

    # computer_control failure
    r11 = ToolResult.ok("Unknown action: dance")
    r11 = verify_action_result("computer_control", r11, {"action": "dance"})
    check("computer_control fails on 'Unknown action'", r11.verified is False)

    # computer_settings failure
    r12 = ToolResult.ok("Bluetooth Command Line Tools missing")
    r12 = verify_action_result("computer_settings", r12, {"action": "bluetooth"})
    check("computer_settings fails on 'missing'", r12.verified is False)

    # web_search failure
    r13 = ToolResult.ok("No results found for: xyz")
    r13 = verify_action_result("web_search", r13)
    check("web_search fails on 'No results'", r13.verified is False)

    # weather failure
    r14 = ToolResult.ok("Sir, the city is missing for the weather report")
    r14 = verify_action_result("weather_report", r14)
    check("weather fails on 'missing'", r14.verified is False)

    # media verifier (lenient)
    r15 = ToolResult.ok("Media played")
    r15 = verify_action_result("media", r15)
    check("media verifier sets verified=True", r15.verified is True)

    # unknown tool
    r16 = ToolResult.ok("something")
    r16 = verify_action_result("nonexistent_tool", r16)
    check("unknown tool returns verified=False", r16.verified is False)
    check("unknown tool verifier_name is None", r16.verifier_name is None)
    check(
        "unknown tool has message about no verifier",
        "No verifier registered" in r16.verification_message,
    )

    # fail result passed to verifier (should short-circuit)
    r17 = ToolResult.fail("already failed")
    r17 = verify_action_result("open_app", r17)
    check(
        "verify_action_result short-circuits on failure",
        r17.success is False and r17.verification_status == "NOT_VERIFIED",
    )

    # ── verify_tool_result with tool kwarg ──
    print("\n[verification] verify_tool_result with tool kwarg...")
    from core.verification import verify_tool_result

    r18 = ToolResult.ok("Successful file write")
    r18 = verify_tool_result(r18, tool="file_controller", parameters={"action": "write"})
    check(
        "verify_tool_result with tool routes to verifier",
        r18.verified is True and r18.verifier_name == "file_controller",
    )

    r19 = ToolResult.ok("No results found for query")
    r19 = verify_tool_result(r19, tool="web_search")
    check(
        "verify_tool_result with tool routes to web_search verifier",
        r19.verified is False and r19.verification_status == "FAILED",
    )

    # verify_tool_result without tool (old behavior)
    r20 = verify_tool_result(ToolResult.ok("Done"))
    check(
        "verify_tool_result without tool returns NOT_VERIFIED",
        r20.verification_status == "NOT_VERIFIED",
    )

    # verify_tool_result with callable expected (old behavior)
    r21 = verify_tool_result(ToolResult.ok("Done"), expected=lambda r: "Done" in r.message)
    check("verify_tool_result with callable expected", r21.verification_status == "VERIFIED")

    # ── summary ──
    print(f"\n{'=' * 40}")
    print(f"Verification layer: {passed} passed, {failed} failed out of {passed + failed}")
    if failed == 0:
        print("All verification layer checks passed.")
    else:
        print("Some checks failed.")


if __name__ == "__main__":
    run()
