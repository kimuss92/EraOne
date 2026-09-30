"""Tests for proactive completion reporting on async vision queries.

Karim's instruction: Era must not wait to be asked "did it finish?" — the
moment an async task (vision query, agent_task, community-management post)
completes, she must state the outcome unprompted. agent_task and
community_manager already did this (they speak their own summary on
completion); this closed the one real gap: submit_vision_request() ran
entirely in the background with no way to notify the caller when done, so
an async vision check only ever surfaced its result if the user separately
polled with action="status"/"result".

Covers:
  - submit_vision_request(on_complete=...) fires on success and on failure
  - the callback receives (request_id, result) and never blocks the worker
  - a broken callback cannot crash the worker or block the next request
  - dedup (two submits of the same in-flight prompt) still fires both callbacks
  - dedup against an already-finished request fires the callback immediately
  - actions.screen_process(speak=...) wires a spoken announcement end-to-end

No network, no real Gemini/vision calls — core.vision_service.analyze_screen
is mocked throughout, matching tools/test_vision_worker.py's conventions.
"""

import threading
import time
import unittest
from unittest.mock import patch

from core.vision_types import VisionResult


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


class TestVisionProactiveCompletion(unittest.TestCase):
    def setUp(self):
        from core.vision_worker import _reset_worker_state_for_tests

        _reset_worker_state_for_tests()
        self._vs_patcher = patch("core.vision_service.analyze_screen")
        self.mock_vs = self._vs_patcher.start()
        self.mock_vs.return_value = VisionResult(ok=True, summary="Worker test result")

    def tearDown(self):
        self._vs_patcher.stop()
        from core.vision_worker import _reset_worker_state_for_tests

        _reset_worker_state_for_tests()

    def _wait_for_calls(self, calls, count=1, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(calls) >= count:
                return
            time.sleep(0.02)
        self.fail(f"Timed out waiting for {count} on_complete call(s), got {len(calls)}")

    # ── on_complete fires on success ──

    def test_on_complete_fires_on_success(self):
        from core.vision_worker import submit_vision_request

        calls = []
        rid = submit_vision_request(
            "Success test", on_complete=lambda r, res: calls.append((r, res))
        )
        self._wait_for_calls(calls)
        self.assertEqual(calls[0][0], rid)
        self.assertTrue(calls[0][1].ok)
        self.assertEqual(calls[0][1].summary, "Worker test result")

    # ── on_complete fires on failure ──

    def test_on_complete_fires_on_failure(self):
        self.mock_vs.side_effect = RuntimeError("Gemini API error")
        from core.vision_worker import submit_vision_request

        calls = []
        rid = submit_vision_request(
            "Failure test", on_complete=lambda r, res: calls.append((r, res))
        )
        self._wait_for_calls(calls)
        self.assertEqual(calls[0][0], rid)
        self.assertFalse(calls[0][1].ok)

    # ── a broken callback never crashes the worker or blocks the next request ──

    def test_broken_callback_does_not_block_worker(self):
        from core.vision_worker import submit_vision_request

        def bad_callback(request_id, result):
            raise RuntimeError("callback exploded")

        rid1 = submit_vision_request("Broken callback test", on_complete=bad_callback)

        from core.vision_worker import get_vision_result

        deadline = time.time() + 5
        result1 = None
        while time.time() < deadline:
            result1 = get_vision_result(rid1)
            if result1 is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(result1, "First request must still complete despite its bad callback")

        calls = []
        rid2 = submit_vision_request(
            "Second request after broken callback",
            on_complete=lambda r, res: calls.append((r, res)),
        )
        self._wait_for_calls(calls)
        self.assertEqual(calls[0][0], rid2)

    # ── dedup: two submits of the same in-flight prompt both get notified ──

    def test_dedup_in_flight_fires_both_callbacks(self):
        block_event = threading.Event()
        controlled = ControlledMock(
            result=VisionResult(ok=True, summary="Shared result"),
            block_event=block_event,
        )
        self.mock_vs.side_effect = controlled

        from core.vision_worker import submit_vision_request

        calls_a, calls_b = [], []
        rid_a = submit_vision_request(
            "Shared prompt", on_complete=lambda r, res: calls_a.append((r, res))
        )
        rid_b = submit_vision_request(
            "Shared prompt", on_complete=lambda r, res: calls_b.append((r, res))
        )
        self.assertEqual(rid_a, rid_b, "Dedup should hand back the same in-flight request")

        block_event.set()
        self._wait_for_calls(calls_a)
        self._wait_for_calls(calls_b)
        self.assertEqual(calls_a[0][1].summary, "Shared result")
        self.assertEqual(calls_b[0][1].summary, "Shared result")

    # ── dedup against an already-finished request fires immediately ──

    def test_dedup_recent_done_fires_immediately(self):
        from core.vision_worker import get_vision_result, submit_vision_request

        rid1 = submit_vision_request("Recently done prompt")
        deadline = time.time() + 5
        while time.time() < deadline:
            if get_vision_result(rid1) is not None:
                break
            time.sleep(0.02)

        calls = []
        rid2 = submit_vision_request(
            "Recently done prompt", on_complete=lambda r, res: calls.append((r, res))
        )
        self.assertEqual(rid1, rid2)
        # Should already have fired synchronously — no need to wait.
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].ok)

    # ── end-to-end: screen_process(speak=...) announces the outcome ──

    def test_screen_process_speaks_on_completion(self):
        from actions.screen_processor import screen_process

        spoken = []
        result = screen_process(
            parameters={"text": "What changed on screen?", "async": True},
            speak=lambda msg: spoken.append(msg),
        )
        self.assertTrue(result.startswith("Vision analysis started. Request ID: vr-"))
        self._wait_for_calls(spoken)
        self.assertIn("Worker test result", spoken[0])

    def test_screen_process_speaks_failure_message(self):
        self.mock_vs.side_effect = RuntimeError("capture broke")
        from actions.screen_processor import screen_process

        spoken = []
        screen_process(
            parameters={"text": "What changed on screen?", "async": True},
            speak=lambda msg: spoken.append(msg),
        )
        self._wait_for_calls(spoken)
        self.assertIn("didn't work", spoken[0])

    def test_screen_process_without_speak_stays_silent(self):
        """No speak= given (e.g. a headless/executor caller) — no callback at
        all, matching the pre-existing behavior exactly."""
        from actions.screen_processor import _speak_vision_completion, screen_process

        result = screen_process(parameters={"text": "Silent async test", "async": True})
        self.assertTrue(result.startswith("Vision analysis started. Request ID: vr-"))
        # Just confirm no exception path exists without speak — nothing to
        # assert on since there is, by design, no callback to observe.
        self.assertTrue(callable(_speak_vision_completion))


if __name__ == "__main__":
    unittest.main()
