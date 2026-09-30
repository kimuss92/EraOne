"""
Tests for Step V5 — Vision Async Result Retrieval via screen_process.

Tests:
1. old blocking screen_process still works
2. async start returns request ID
3. result unknown ID returns not found
4. result queued returns queued message
5. result running returns running message
6. result done returns formatted summary
7. result includes screen_state
8. result includes needs_confirmation
9. result limits elements to top 3
10. result with no elements omits Visible elements section
11. status returns required fields
12. status unknown ID returns not found
13. list returns request IDs
14. clear removes old completed requests
15. invalid action returns help message
16. aliases work: get_result, check_status, requests, cleanup
17. no computer_control import in screen_processor.py
18. no pyautogui import in screen_processor.py
19. no dangerous call patterns in screen_processor.py
20. action="screen" alias performs analysis
21. action="screen" with async starts async
22. unknown action with text falls back to analysis
23. unknown action without text returns help
24. action="result" without request_id defaults to latest
25. action="status" without request_id defaults to latest
26. action="result" with request_id="latest" returns latest
27. blocking fallback stores latest result
28. screen_process failure does not call Netflix cooldown
29. VisionResult imported in screen_processor

Run:
    python -m pytest tools/test_vision_screen_process_async.py -v --tb=short
"""

import inspect
import threading
import time
import unittest
from unittest.mock import patch

from core.vision_types import VisionRequestState, VisionResult


class ControlledMock:
    def __init__(self, result=None, block_event=None):
        self.result = result or VisionResult(ok=True, summary="test summary")
        self.block_event = block_event
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        if self.block_event is not None:
            self.block_event.wait(timeout=10)
        return self.result


class TestVisionScreenProcessAsync(unittest.TestCase):
    def setUp(self):
        from actions.netflix_control import _reset_netflix_vision_cooldown
        from core.vision_worker import _reset_worker_state_for_tests

        _reset_worker_state_for_tests()
        _reset_netflix_vision_cooldown()
        import actions.screen_processor as sp_mod

        sp_mod._CACHE.clear()
        self._intent_patcher = patch(
            "core.intent_memory.load_state",
            return_value={
                "last_app": None,
                "last_media_app": None,
                "last_browser": "brave",
                "last_action": None,
                "last_tool": None,
                "last_window": None,
                "previous_window": None,
                "last_result": None,
                "last_error": None,
                "updated_at": 0.0,
                "history": [],
            },
        )
        self._intent_patcher.start()
        self._vs_patcher = patch("core.vision_service.analyze_screen")
        self.mock_vs = self._vs_patcher.start()
        self.mock_vs.return_value = VisionResult(
            ok=True,
            summary="Async test result",
            screen_state="desktop",
        )

    def tearDown(self):
        self._vs_patcher.stop()
        self._intent_patcher.stop()
        from actions.netflix_control import _reset_netflix_vision_cooldown
        from core.vision_worker import _reset_worker_state_for_tests

        _reset_worker_state_for_tests()
        _reset_netflix_vision_cooldown()

    # ── Helpers ──

    def _wait_for_result(self, request_id, timeout=5.0):
        from core.vision_worker import get_vision_result

        deadline = time.time() + timeout
        while time.time() < deadline:
            result = get_vision_result(request_id)
            if result is not None:
                return result
            time.sleep(0.02)
        self.fail(f"Timed out waiting for result on {request_id} after {timeout}s")

    def _wait_for_status(self, request_id, expected_status, timeout=5.0):
        from core.vision_worker import get_vision_request_status

        deadline = time.time() + timeout
        while time.time() < deadline:
            st = get_vision_request_status(request_id)
            if st and st["status"] == expected_status:
                return st
            time.sleep(0.02)
        # Diagnostic: fail clearly instead of silently proceeding
        final = get_vision_request_status(request_id)
        final_status = final["status"] if final else "not_found"
        self.fail(
            f"Timed out waiting for status={expected_status!r} on {request_id} "
            f"(got {final_status!r} after {timeout}s)"
        )

    def _submit_and_wait(self, prompt="Async test"):
        from core.vision_worker import submit_vision_request

        rid = submit_vision_request(prompt)
        result = self._wait_for_result(rid)
        self.assertIsNotNone(result)
        return rid, result

    # ── Test 1: old blocking still works ──

    def test_old_blocking_still_works(self):
        with patch("actions.screen_processor.analyze_image_once") as mock_analyze:
            mock_analyze.return_value = "Blocking fallback text"
            from actions.screen_processor import screen_process

            result = screen_process(parameters={"text": "What do you see?"})
            self.assertIsInstance(result, str)
            self.assertGreater(len(result), 0)

    # ── Test 2: async start returns request ID ──

    def test_async_start_returns_request_id(self):
        from actions.screen_processor import screen_process

        result = screen_process(parameters={"text": "Async test", "async": True})
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Vision analysis started. Request ID: vr-"))

    # ── Test 3: result unknown ID returns not found ──

    def test_result_unknown_id_returns_not_found(self):
        from actions.screen_processor import screen_process

        result = screen_process({"action": "result", "request_id": "vr-nonexistent"})
        self.assertIn("not found", result)

    # ── Test 4: result queued returns queued message ──

    def test_result_queued_returns_queued_message(self):
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="Queued result"),
            block_event=block_event,
        )
        from core.vision_worker import submit_vision_request

        self.mock_vs.side_effect = controlled
        rid = submit_vision_request("Queued test")

        try:
            # Worker is blocked on the mock, so status must still be "queued"
            from actions.screen_processor import screen_process

            result = screen_process({"action": "result", "request_id": rid})
            self.assertIn("queued", result.lower())
        finally:
            block_event.set()

    # ── Test 5: result running returns running message ──

    def test_result_running_returns_running_message(self):
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="Running result", screen_state="desktop"),
            block_event=block_event,
        )
        from core.vision_worker import submit_vision_request

        self.mock_vs.side_effect = controlled
        rid = submit_vision_request("Running test")

        try:
            # Wait deterministically for the worker to enter running state.
            # _wait_for_status fails the test immediately if it doesn't reach
            # "running" within the timeout, instead of silently proceeding.
            self._wait_for_status(rid, "running", timeout=5.0)

            from actions.screen_processor import screen_process

            result = screen_process({"action": "result", "request_id": rid})
            self.assertIn("running", result.lower())
        finally:
            # Always release the mock so the worker thread cannot hang
            block_event.set()

    # ── Test 6: result done returns formatted summary ──

    def test_result_done_returns_formatted_summary(self):
        from actions.screen_processor import screen_process

        rid, _ = self._submit_and_wait("Done test")
        result = screen_process({"action": "result", "request_id": rid})
        self.assertIn("Vision result", result)
        self.assertIn(rid, result)
        self.assertIn("Async test result", result)

    # ── Test 7: result includes screen_state ──

    def test_result_includes_screen_state(self):
        from actions.screen_processor import screen_process

        rid, _ = self._submit_and_wait("Screen state test")
        result = screen_process({"action": "result", "request_id": rid})
        self.assertIn("Screen: desktop", result)

    # ── Test 8: result includes needs_confirmation ──

    def test_result_includes_needs_confirmation(self):
        from actions.screen_processor import screen_process

        self.mock_vs.return_value = VisionResult(
            ok=True,
            summary="Confirmation test",
            screen_state="form",
            needs_confirmation=True,
            elements=[],
        )
        rid, _ = self._submit_and_wait("Confirmation test")
        result = screen_process({"action": "result", "request_id": rid})
        self.assertIn("Needs confirmation: true", result)

    # ── Test 9: result limits elements to top 3 ──

    def test_result_limits_elements_to_3(self):
        from actions.screen_processor import screen_process
        from core.vision_types import VisionElement

        elements = [
            VisionElement(
                label=f"Element {i}",
                type="button",
                risk="low",
                action_hint="none",
                confidence=0.9 - i * 0.1,
            )
            for i in range(5)
        ]
        self.mock_vs.return_value = VisionResult(
            ok=True,
            summary="Elements test",
            screen_state="browser",
            elements=elements,
        )
        rid, _ = self._submit_and_wait("Elements test")
        result = screen_process({"action": "result", "request_id": rid})
        # First 3 elements (indices 0, 1, 2) should be shown
        self.assertIn("Element 0", result)
        self.assertIn("Element 1", result)
        self.assertIn("Element 2", result)
        # Element 3 (index 3) is the 4th — should NOT be shown
        self.assertNotIn("Element 3", result)
        self.assertNotIn("Element 4", result)

    # ── Test 10: result with no elements omits Visible elements section ──

    def test_result_no_elements_when_empty(self):
        from actions.screen_processor import screen_process

        self.mock_vs.return_value = VisionResult(
            ok=True,
            summary="No elements",
            screen_state="desktop",
            elements=[],
        )
        rid, _ = self._submit_and_wait("No elements test")
        result = screen_process({"action": "result", "request_id": rid})
        self.assertNotIn("Visible elements", result)

    # ── Test 11: status returns required fields ──

    def test_status_returns_required_fields(self):
        from actions.screen_processor import screen_process

        rid, _ = self._submit_and_wait("Status test")
        result = screen_process({"action": "status", "request_id": rid})
        self.assertIn("Status:", result)
        self.assertIn("Duration:", result)
        self.assertIn("Queued", result)

    # ── Test 12: status unknown ID returns not found ──

    def test_status_unknown_id_returns_not_found(self):
        from actions.screen_processor import screen_process

        result = screen_process({"action": "status", "request_id": "vr-nonexistent"})
        self.assertIn("not found", result)

    # ── Test 13: list returns request IDs ──

    def test_list_returns_request_ids(self):
        from actions.screen_processor import screen_process
        from core.vision_worker import submit_vision_request

        rid1 = submit_vision_request("List test 1")
        rid2 = submit_vision_request("List test 2")

        self._wait_for_result(rid1)
        self._wait_for_result(rid2)

        result = screen_process({"action": "list"})
        self.assertIn(rid1, result)
        self.assertIn(rid2, result)
        self.assertIn("Vision requests", result)

    # ── Test 14: clear removes old completed requests ──

    def test_clear_removes_old_completed_requests(self):
        from actions.screen_processor import screen_process
        from core.vision_worker import submit_vision_request

        rid = submit_vision_request("Clear test")
        self._wait_for_result(rid)

        result = screen_process({"action": "clear"})
        self.assertIn("Cleared", result)
        self.assertIn("vision request", result)

    # ── Test 15: invalid action returns help message ──

    def test_invalid_action_returns_help_message(self):
        from actions.screen_processor import screen_process

        result = screen_process({"action": "invalid_action_xyz"})
        self.assertIn("Unknown vision action", result)
        self.assertIn("Available actions", result)

    # ── Test 16: aliases work ──

    def test_alias_get_result_works(self):
        from actions.screen_processor import screen_process

        rid, _ = self._submit_and_wait("Alias test")
        result = screen_process({"action": "get_result", "request_id": rid})
        self.assertIn("Vision result", result)

    def test_alias_check_status_works(self):
        from actions.screen_processor import screen_process

        rid, _ = self._submit_and_wait("Alias status test")
        result = screen_process({"action": "check_status", "request_id": rid})
        self.assertIn("Status:", result)

    def test_alias_requests_works(self):
        from actions.screen_processor import screen_process
        from core.vision_worker import submit_vision_request

        rid = submit_vision_request("Alias list test")
        self._wait_for_result(rid)
        result = screen_process({"action": "requests"})
        self.assertIn(rid, result)

    def test_alias_cleanup_works(self):
        from actions.screen_processor import screen_process
        from core.vision_worker import submit_vision_request

        rid = submit_vision_request("Alias clear test")
        self._wait_for_result(rid)
        result = screen_process({"action": "cleanup"})
        self.assertIn("Cleared", result)

    # ── Test 17: no computer_control import in screen_processor.py ──

    def test_no_computer_control_import(self):
        import actions.screen_processor as sp

        source = inspect.getsource(sp)
        self.assertNotIn("computer_control", source)

    # ── Test 18: no pyautogui import in screen_processor.py ──

    def test_no_pyautogui_import(self):
        import actions.screen_processor as sp

        source = inspect.getsource(sp)
        self.assertNotIn("pyautogui", source)

    # ── Test 19: no dangerous call patterns ──

    def test_no_dangerous_call_patterns(self):
        import actions.screen_processor as sp

        source = inspect.getsource(sp)
        for pattern in ("_click(", "_type(", "_press(", "_scroll(", "pyautogui."):
            self.assertNotIn(pattern, source, f"Found dangerous pattern: {pattern}")

    # ── Test 20: action="screen" performs analysis, not unknown action ──

    def test_action_screen_alias_performs_analysis(self):
        with patch("actions.screen_processor.analyze_image_once") as mock_analyze:
            mock_analyze.return_value = "Screen alias analysis result"
            from actions.screen_processor import screen_process

            result = screen_process({"text": "What do you see?", "action": "screen"})
            self.assertIsInstance(result, str)
            self.assertGreater(len(result), 0)
            self.assertNotIn("Unknown vision action", result)

    # ── Test 21: action="screen" with async starts async ──

    def test_action_screen_with_async_starts_async(self):
        from actions.screen_processor import screen_process

        result = screen_process({"text": "Analyze screen", "async": True, "action": "screen"})
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Vision analysis started. Request ID: vr-"))

    # ── Test 22: unknown action with text falls back to analysis ──

    def test_unknown_action_with_text_falls_back(self):
        with patch("actions.screen_processor.analyze_image_once") as mock_analyze:
            mock_analyze.return_value = "Fallback analysis result"
            from actions.screen_processor import screen_process

            result = screen_process({"text": "Analyze this", "action": "bogus_action"})
            self.assertIsInstance(result, str)
            self.assertNotIn("Unknown vision action", result)

    # ── Test 23: unknown action without text returns help ──

    def test_unknown_action_without_text_returns_help(self):
        from actions.screen_processor import screen_process

        result = screen_process({"action": "bogus_action"})
        self.assertIn("Unknown vision action", result)
        self.assertIn("Available actions", result)

    # ── Test 24: action="result" without request_id defaults to latest ──

    def test_action_result_defaults_to_latest(self):
        from core.vision_context import clear_latest_vision_result, store_latest_vision_result
        from core.vision_types import VisionElement, VisionResult

        clear_latest_vision_result()

        vr = VisionResult(
            ok=True,
            screen_state="desktop",
            summary="Latest result",
            recommended_action="none",
            needs_confirmation=False,
            elements=[VisionElement(label="Icon", type="icon", risk="low", confidence=0.9)],
        )
        store_latest_vision_result("vr-test-latest", vr)

        from actions.screen_processor import screen_process

        result = screen_process({"action": "result"})
        self.assertIn("Vision result", result)
        self.assertIn("Latest result", result)
        clear_latest_vision_result()

    # ── Test 25: action="status" without request_id defaults to latest ──

    def test_action_status_defaults_to_latest(self):
        from core.vision_context import clear_latest_vision_result, store_latest_vision_result
        from core.vision_types import VisionResult
        from core.vision_worker import submit_vision_request

        clear_latest_vision_result()

        rid = submit_vision_request("Status latest test")
        from core.vision_worker import get_vision_request_status, get_vision_result

        deadline = time.time() + 5
        while time.time() < deadline:
            r = get_vision_result(rid)
            if r is not None:
                break
            time.sleep(0.05)

        r = get_vision_result(rid)
        if r is not None:
            store_latest_vision_result(rid, r)

        from actions.screen_processor import screen_process

        result = screen_process({"action": "status"})
        self.assertIn("Status:", result)
        self.assertIn("Request", result)
        clear_latest_vision_result()

    # ── Test 26: action="result" with request_id="latest" returns latest ──

    def test_action_result_request_id_latest(self):
        from core.vision_context import clear_latest_vision_result, store_latest_vision_result
        from core.vision_types import VisionElement, VisionResult

        clear_latest_vision_result()

        vr = VisionResult(
            ok=True,
            screen_state="browser",
            summary="Explicit latest request",
            recommended_action="none",
            needs_confirmation=False,
            elements=[VisionElement(label="Btn", type="button", risk="low", confidence=0.85)],
        )
        store_latest_vision_result("vr-explicit", vr)

        from actions.screen_processor import screen_process

        result = screen_process({"action": "result", "request_id": "latest"})
        self.assertIn("Vision result", result)
        self.assertIn("Explicit latest request", result)
        clear_latest_vision_result()

    # ── Test 27: blocking fallback stores latest result ──

    def test_blocking_fallback_stores_latest(self):
        from core.vision_context import clear_latest_vision_result

        clear_latest_vision_result()

        with (
            patch("core.vision_service.analyze_screen", return_value=VisionResult(ok=False)),
            patch("actions.screen_processor.analyze_image_once") as mock_analyze,
        ):
            mock_analyze.return_value = "Fallback summary text"
            from actions.screen_processor import screen_process

            result = screen_process({"text": "What do you see?"})
            self.assertEqual(result, "Fallback summary text")

        from core.vision_context import get_latest_vision_result

        ctx = get_latest_vision_result()
        self.assertIsNotNone(ctx, "Fallback path must store latest vision result")
        ctx_id, ctx_vr = ctx
        self.assertEqual(ctx_id, "latest")
        self.assertEqual(ctx_vr.summary, "Fallback summary text")
        self.assertTrue(ctx_vr.ok)
        clear_latest_vision_result()

    # ── Test 28: screen_process failure does not call Netflix cooldown ──

    def test_screen_process_failure_no_netflix_cooldown(self):
        from actions.netflix_control import is_netflix_vision_blocked
        from core.vision_context import clear_latest_vision_result

        clear_latest_vision_result()

        # Verify cooldown is not active before test
        self.assertFalse(is_netflix_vision_blocked(), "Cooldown should not be active before test")

        with (
            patch("core.vision_service.analyze_screen", return_value=VisionResult(ok=False)),
            patch(
                "actions.screen_processor._capture_screen", side_effect=RuntimeError("capture fail")
            ),
        ):
            from actions.screen_processor import screen_process

            result = screen_process({"text": "What do you see?"})
            self.assertIn("could not analyze", result.lower())

        # Verify cooldown was NOT set after generic failure
        self.assertFalse(
            is_netflix_vision_blocked(), "Generic Vision failure must not set Netflix cooldown"
        )
        clear_latest_vision_result()

    # ── Test 29: VisionResult imported in screen_processor ──

    def test_vision_result_imported(self):
        import actions.screen_processor as sp

        self.assertTrue(hasattr(sp, "VisionResult"))
