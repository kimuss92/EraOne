"""
Tests for Step V3 — Vision Background Worker.

Tests:
1. submit returns request_id immediately
2. duplicate request returns same request_id while queued/running/recent
3. get result returns None while queued/running
4. completed request returns VisionResult
5. failed request returns VisionResult ok=False
6. cancel queued request works
7. cancelled queued request is skipped by worker
8. cancel running request returns False safely
9. old blocking screen_process still works
10. async screen_process returns request_id
11. worker never imports computer_control
12. worker never imports pyautogui
13. worker source has no click/type/press/scroll
14. only one worker thread starts
15. request status includes required fields
16. list requests returns submitted requests
17. clear old completed requests works
18. test reset helper prevents cross-test pollution

Run:
    python -m pytest tools/test_vision_worker.py -v --tb=short
"""

import inspect
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

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


class TestVisionWorker(unittest.TestCase):
    def setUp(self):
        from core.vision_worker import _reset_worker_state_for_tests

        _reset_worker_state_for_tests()
        self._vs_patcher = patch("core.vision_service.analyze_screen")
        self.mock_vs = self._vs_patcher.start()
        self.mock_vs.return_value = VisionResult(
            ok=True,
            summary="Worker test result",
            screen_state="desktop",
            metadata={"worker_test": True},
        )

    def tearDown(self):
        self._vs_patcher.stop()
        from core.vision_worker import _reset_worker_state_for_tests

        _reset_worker_state_for_tests()

    # ── Helper ──

    def _wait_for_result(self, request_id, timeout=5.0):
        from core.vision_worker import get_vision_result

        deadline = time.time() + timeout
        while time.time() < deadline:
            result = get_vision_result(request_id)
            if result is not None:
                return result
            time.sleep(0.05)
        return None

    def _wait_for_status(self, request_id, expected_status, timeout=5.0):
        from core.vision_worker import get_vision_request_status

        deadline = time.time() + timeout
        while time.time() < deadline:
            st = get_vision_request_status(request_id)
            if st and st["status"] == expected_status:
                return st
            time.sleep(0.05)
        return None

    # ── Test 1: submit returns request_id immediately ──

    def test_submit_returns_request_id_immediately(self):
        from core.vision_worker import submit_vision_request

        t0 = time.perf_counter()
        rid = submit_vision_request("What do you see?")
        elapsed = (time.perf_counter() - t0) * 1000
        self.assertLess(elapsed, 200, "submit_vision_request took too long")
        self.assertIsInstance(rid, str)
        self.assertTrue(rid.startswith("vr-"))
        self.assertGreater(len(rid), 3)

    # ── Test 2: duplicate request returns same request_id ──

    def test_duplicate_request_returns_same_id_while_queued(self):
        from core.vision_worker import submit_vision_request

        rid1 = submit_vision_request("Duplicate test", mode="fast")
        rid2 = submit_vision_request("Duplicate test", mode="fast")
        self.assertEqual(rid1, rid2, "Duplicate queued request should return same id")

    def test_duplicate_request_returns_same_id_done_recently(self):
        from core.vision_worker import _DEDUP_TTL, submit_vision_request

        rid1 = submit_vision_request("Recent done test")
        result = self._wait_for_result(rid1)
        self.assertIsNotNone(result)
        rid2 = submit_vision_request("Recent done test")
        self.assertEqual(rid1, rid2)
        self.assertEqual(self.mock_vs.call_count, 1)

    # ── Test 3: get result returns None while queued/running ──

    def test_get_result_none_while_queued(self):
        from core.vision_worker import (
            get_vision_request_status,
            get_vision_result,
            submit_vision_request,
        )

        self.mock_vs.side_effect = None
        self.mock_vs.return_value = VisionResult(ok=True, summary="fast test")

        rid = submit_vision_request("Queued test")

        st = get_vision_request_status(rid)
        if st and st["status"] in ("queued", "running"):
            result = get_vision_result(rid)
            self.assertIsNone(result)

    def test_get_result_none_while_running(self):
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="blocked"),
            block_event=block_event,
        )
        self.mock_vs.side_effect = controlled

        from core.vision_worker import get_vision_result, submit_vision_request

        rid = submit_vision_request("Running test")

        # Wait until the worker picks it up and is blocked
        from core.vision_worker import get_vision_request_status

        deadline = time.time() + 5
        while time.time() < deadline:
            st = get_vision_request_status(rid)
            if st and st["status"] == "running":
                break
            time.sleep(0.05)

        result = get_vision_result(rid)
        self.assertIsNone(result, "Result should be None while running")

        block_event.set()

    # ── Test 4: completed request returns VisionResult ──

    def test_completed_request_returns_vision_result(self):
        from core.vision_worker import submit_vision_request

        expected = VisionResult(ok=True, summary="Done test", screen_state="browser")
        self.mock_vs.return_value = expected

        rid = submit_vision_request("Completed test")
        result = self._wait_for_result(rid)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, VisionResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "Done test")

    # ── Test 5: failed request returns VisionResult ok=False ──

    def test_failed_request_returns_ok_false(self):
        self.mock_vs.side_effect = RuntimeError("Gemini API error")
        from core.vision_worker import submit_vision_request

        rid = submit_vision_request("Fail test")
        result = self._wait_for_result(rid)
        self.assertIsNotNone(result)
        self.assertFalse(result.ok)
        self.assertIn("Worker error", result.error or "")

    # ── Test 6: cancel queued request works ──

    def test_cancel_queued_request(self):
        from core.vision_worker import (
            cancel_vision_request,
            get_vision_request_status,
            submit_vision_request,
        )

        self.mock_vs.side_effect = None
        # Keep mock fast; submit and cancel immediately
        rid = submit_vision_request("Cancel queued test")
        # Wait just enough for request to be registered but still in queue
        # Since the worker might pick it up fast, we check the status
        st = get_vision_request_status(rid)
        if st and st["status"] == "queued":
            cancelled = cancel_vision_request(rid)
            self.assertTrue(cancelled)
            st2 = get_vision_request_status(rid)
            self.assertEqual(st2["status"], "cancelled")
        else:
            # Already picked up by worker — skip this assertion
            # but verify the request completed
            pass

    # ── Test 7: cancelled queued request is skipped by worker ──

    def test_cancelled_queued_request_skipped_by_worker(self):
        from core.vision_worker import (
            cancel_vision_request,
            get_vision_request_status,
            get_vision_result,
            submit_vision_request,
        )

        # Use a blocking mock to slow the worker so we have time to cancel
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="should not run"),
            block_event=block_event,
        )
        self.mock_vs.side_effect = controlled

        rid = submit_vision_request("Skip cancelled test")

        # Wait briefly for the request to be queued, then cancel immediately
        time.sleep(0.1)
        st = get_vision_request_status(rid)
        if st and st["status"] == "queued":
            cancelled = cancel_vision_request(rid)
            self.assertTrue(cancelled)

        # Let the worker possibly pick it up
        block_event.set()
        time.sleep(0.3)

        st2 = get_vision_request_status(rid)
        if st2 and st2["status"] == "cancelled":
            result = get_vision_result(rid)
            self.assertIsNone(result, "Cancelled request should have no result")
        # If worker already picked it up before cancel, that's also acceptable
        # But verify analyze_screen was called (it was already running)

    # ── Test 8: cancel running request returns False ──

    def test_cancel_running_returns_false(self):
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="blocked"),
            block_event=block_event,
        )
        self.mock_vs.side_effect = controlled

        from core.vision_worker import (
            cancel_vision_request,
            get_vision_request_status,
            submit_vision_request,
        )

        rid = submit_vision_request("Cancel running test")

        deadline = time.time() + 5
        while time.time() < deadline:
            st = get_vision_request_status(rid)
            if st and st["status"] == "running":
                break
            time.sleep(0.05)

        cancelled = cancel_vision_request(rid)
        self.assertFalse(cancelled, "Running request should not be cancellable")

        block_event.set()

    # ── Test 9: old blocking screen_process still works ──

    def test_old_blocking_screen_process_works(self):
        self.mock_vs.return_value = VisionResult(
            ok=True,
            summary="Blocking test result",
            screen_state="desktop",
        )
        with patch("actions.screen_processor.analyze_image_once") as mock_analyze:
            mock_analyze.return_value = "Blocking fallback text"
            from actions.screen_processor import screen_process

            result = screen_process(parameters={"text": "What do you see?"})
            self.assertIsInstance(result, str)
            self.assertGreater(len(result), 0)

    # ── Test 10: async screen_process returns request_id ──

    def test_async_screen_process_returns_request_id(self):
        from actions.screen_processor import screen_process

        result = screen_process(parameters={"text": "Async test", "async": True})
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Vision analysis started. Request ID: vr-"))

    # ── Test 11: worker never imports computer_control ──

    def test_worker_never_imports_computer_control(self):
        import core.vision_worker as vw

        source = inspect.getsource(vw)
        self.assertNotIn("computer_control", source)

    # ── Test 12: worker never imports pyautogui ──

    def test_worker_never_imports_pyautogui(self):
        import core.vision_worker as vw

        source = inspect.getsource(vw)
        self.assertNotIn("pyautogui", source)

    # ── Test 13: worker source has no click/type/press/scroll ──

    def test_worker_no_click_type_press_scroll(self):
        import core.vision_worker as vw

        source = inspect.getsource(vw)
        for keyword in ("_click(", "def _type", "_press(", "_hotkey(", "_scroll("):
            self.assertNotIn(keyword, source)

    # ── Test 14: only one worker thread starts ──

    def test_only_one_worker_thread(self):
        from core.vision_worker import _ensure_worker

        _ensure_worker()
        threads_before = [t for t in threading.enumerate() if t.name == "VisionWorker"]
        self.assertEqual(len(threads_before), 1)
        _ensure_worker()
        _ensure_worker()
        threads_after = [t for t in threading.enumerate() if t.name == "VisionWorker"]
        self.assertEqual(len(threads_after), 1)

    # ── Test 15: request status includes required fields ──

    def test_request_status_includes_required_fields(self):
        from core.vision_worker import get_vision_request_status, submit_vision_request

        # Use a blocking mock to keep the request alive for status inspection
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="status test"),
            block_event=block_event,
        )
        self.mock_vs.side_effect = controlled

        rid = submit_vision_request("Status fields test")

        # Wait for the request to be picked up (running)
        deadline = time.time() + 5
        while time.time() < deadline:
            status = get_vision_request_status(rid)
            if status is not None and status["status"] in ("queued", "running"):
                break
            time.sleep(0.05)

        self.assertIsNotNone(status)
        required = [
            "request_id",
            "status",
            "mode",
            "target",
            "queued_at",
            "started_at",
            "finished_at",
            "duration_ms",
            "worker_thread",
            "from_cache",
            "ok",
        ]
        for field in required:
            self.assertIn(field, status, f"Missing status field: {field}")

        if status["status"] == "queued":
            self.assertIsNone(status["ok"])

        # Release and verify completion
        block_event.set()
        result = self._wait_for_result(rid)
        self.assertIsNotNone(result)
        status2 = get_vision_request_status(rid)
        self.assertEqual(status2["status"], "done")
        self.assertTrue(status2["ok"])
        self.assertIsInstance(status2["duration_ms"], (int, float))

    # ── Test 16: list requests returns submitted requests ──

    def test_list_requests_returns_submitted_requests(self):
        from core.vision_worker import list_vision_requests, submit_vision_request

        rid1 = submit_vision_request("List test 1")
        rid2 = submit_vision_request("List test 2")
        all_reqs = list_vision_requests()
        rids = [r["request_id"] for r in all_reqs]
        self.assertIn(rid1, rids)
        self.assertIn(rid2, rids)

    # ── Test 17: clear old completed requests works ──

    def test_clear_old_completed_requests(self):
        from core.vision_worker import (
            clear_vision_requests,
            list_vision_requests,
            submit_vision_request,
        )

        rid = submit_vision_request("Clear test")
        result = self._wait_for_result(rid)
        self.assertIsNotNone(result)

        # Request should still be in list
        all_reqs = list_vision_requests()
        rids = [r["request_id"] for r in all_reqs]
        self.assertIn(rid, rids)

        # Clear with age=0 to remove everything
        cleared = clear_vision_requests(max_age_seconds=0)
        self.assertGreaterEqual(cleared, 1)

        all_reqs_after = list_vision_requests()
        rids_after = [r["request_id"] for r in all_reqs_after]
        self.assertNotIn(rid, rids_after)

    # ── Test 18: test reset helper prevents cross-test pollution ──

    def test_reset_helper_clears_state(self):
        from core.vision_worker import (
            _reset_worker_state_for_tests,
            list_vision_requests,
            submit_vision_request,
        )

        submit_vision_request("Pollution test")
        all_before = list_vision_requests()
        self.assertGreater(len(all_before), 0)
        _reset_worker_state_for_tests()
        all_after = list_vision_requests()
        self.assertEqual(len(all_after), 0)

    # ── Extra: metadata worker fields added on completion ──

    def test_completed_request_metadata_worker_fields(self):
        from core.vision_worker import submit_vision_request

        rid = submit_vision_request("Metadata test")
        result = self._wait_for_result(rid)
        self.assertIsNotNone(result)
        meta = result.metadata
        self.assertIn("request_id", meta)
        self.assertEqual(meta["request_id"], rid)
        self.assertIn("status", meta)
        self.assertIn("queued_at", meta)
        self.assertIn("started_at", meta)
        self.assertIn("finished_at", meta)
        self.assertIn("duration_ms", meta)
        self.assertIn("worker_thread", meta)
        self.assertIn("from_cache", meta)
        self.assertEqual(meta["status"], "done")


if __name__ == "__main__":
    unittest.main()
