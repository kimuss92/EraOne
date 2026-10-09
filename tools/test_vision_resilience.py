"""Vision failure-loop guards: capture verification, provider benching, time budget.

No network, no real screen or camera: every provider, grab and camera read is mocked.

    python -m pytest tools/test_vision_resilience.py -q
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np

from core import vision_resilience as vr
from core.vision_types import VisionResult


def _gemini_response(text):
    response = MagicMock()
    type(response).text = PropertyMock(return_value=text)
    return response


class TestFrameVerification(unittest.TestCase):
    def test_uniform_black_frame_is_blank(self):
        self.assertTrue(vr.frame_is_blank(bytes(1920 * 1080 * 3)))

    def test_empty_and_none_are_blank(self):
        self.assertTrue(vr.frame_is_blank(b""))
        self.assertTrue(vr.frame_is_blank(None))

    def test_real_content_is_not_blank(self):
        rng = np.random.default_rng(0)
        self.assertFalse(vr.frame_is_blank(rng.integers(0, 255, 300_000, dtype=np.uint8)))

    def test_dark_camera_frame_is_unusable(self):
        self.assertFalse(vr.camera_frame_usable(np.zeros((480, 640, 3), dtype=np.uint8)))
        self.assertFalse(vr.camera_frame_usable(None))
        self.assertTrue(vr.camera_frame_usable(np.full((480, 640, 3), 120, dtype=np.uint8)))


class TestErrorClassification(unittest.TestCase):
    def test_quota_is_never_transient(self):
        err = RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
        self.assertTrue(vr.is_quota_error(err))
        self.assertFalse(vr.is_transient_error(err))

    def test_overload_is_transient(self):
        self.assertTrue(vr.is_transient_error(RuntimeError("503 UNAVAILABLE model overloaded")))
        self.assertFalse(vr.is_transient_error(RuntimeError("400 INVALID_ARGUMENT")))

    def test_daily_quota_cooldown_ignores_short_retry_hint(self):
        err = RuntimeError("GenerateRequestsPerDayPerProjectPerModel-FreeTier. retry in 29.1s")
        self.assertEqual(vr.quota_cooldown_seconds(err), 900.0)

    def test_retry_hint_is_clamped(self):
        self.assertEqual(vr.quota_cooldown_seconds(RuntimeError("retry in 5s")), 30.0)
        self.assertEqual(vr.quota_cooldown_seconds(RuntimeError("retry in 120s")), 120.0)


class TestProviderBench(unittest.TestCase):
    def test_second_consecutive_failure_benches(self):
        vr.record_provider_failure("ollama")
        self.assertEqual(vr.provider_cooldown_remaining("ollama"), 0.0)
        vr.record_provider_failure("ollama")
        self.assertGreater(vr.provider_cooldown_remaining("ollama"), 0.0)

    def test_success_resets_the_count(self):
        vr.record_provider_failure("ollama")
        vr.record_provider_success("ollama")
        vr.record_provider_failure("ollama")
        self.assertEqual(vr.provider_cooldown_remaining("ollama"), 0.0)


class TestAnalyzeImageOnceGuards(unittest.TestCase):
    def setUp(self):
        self._order = patch(
            "core.vision_config.resolve_vision_provider_order",
            return_value=["ollama", "gemini"],
        )
        self._order.start()

    def tearDown(self):
        self._order.stop()

    def test_benched_provider_is_skipped_without_probing(self):
        from actions.screen_processor import analyze_image_once

        vr.bench_provider("ollama", 60)
        with (
            patch("core.vision_ollama.check_ollama_availability") as probe,
            patch("core.vision_ollama.analyze_image_with_ollama") as ollama,
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as get_client,
        ):
            get_client.return_value.models.generate_content.return_value = _gemini_response("ok")
            self.assertEqual(analyze_image_once(b"img", "image/jpeg", "Q?"), "ok")
            probe.assert_not_called()
            ollama.assert_not_called()

    def test_gemini_quota_benches_it_for_the_next_look(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=False),
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as get_client,
        ):
            generate = get_client.return_value.models.generate_content
            generate.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED retry in 40s")
            self.assertIn("could not analyze", analyze_image_once(b"img", "image/jpeg", "Q?"))
            self.assertGreater(vr.provider_cooldown_remaining("gemini"), 0.0)
            analyze_image_once(b"img", "image/jpeg", "Q?")
            self.assertEqual(generate.call_count, 1, "a benched provider must not be re-called")

    def test_gemini_transient_error_is_retried_once(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=False),
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as get_client,
            patch("actions.screen_processor.time.sleep"),
        ):
            generate = get_client.return_value.models.generate_content
            generate.side_effect = [RuntimeError("503 UNAVAILABLE"), _gemini_response("second")]
            self.assertEqual(analyze_image_once(b"img", "image/jpeg", "Q?"), "second")
            self.assertEqual(generate.call_count, 2)

    def test_gemini_request_disables_thinking_and_caps_output(self):
        from actions import screen_processor
        from actions.screen_processor import _VISION_MAX_OUTPUT_TOKENS, analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=False),
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as get_client,
        ):
            generate = get_client.return_value.models.generate_content
            generate.return_value = _gemini_response("ok")
            analyze_image_once(b"img", "image/jpeg", "Q?")
            config = generate.call_args.kwargs["config"]
            self.assertEqual(config.max_output_tokens, _VISION_MAX_OUTPUT_TOKENS)
            if screen_processor._VISION_TEXT_MODEL.startswith("gemini-3"):
                self.assertEqual(config.thinking_config.thinking_level.value.lower(), "low")
            else:
                self.assertEqual(config.thinking_config.thinking_budget, 0)
            self.assertIsNotNone(config.http_options.timeout)

    def test_structured_callers_get_no_brief_prefix(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=True),
            patch("core.vision_ollama.analyze_image_with_ollama", return_value="{}") as ollama,
        ):
            analyze_image_once(b"img", "image/jpeg", "Return ONLY valid JSON", brief=False)
            self.assertEqual(ollama.call_args.kwargs["prompt"], "Return ONLY valid JSON")

    def test_spent_budget_starts_no_provider(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("actions.screen_processor._VISION_TOTAL_BUDGET_SECONDS", 1.0),
            patch("core.vision_ollama.check_ollama_availability") as probe,
        ):
            self.assertIn("could not analyze", analyze_image_once(b"img", "image/jpeg", "Q?"))
            probe.assert_not_called()


def _fake_shot(rgb: bytes, size=(4, 4)):
    return SimpleNamespace(rgb=rgb, size=size)


class TestGrabVerified(unittest.TestCase):
    def setUp(self):
        self._sleep = patch("actions.screen_processor.time.sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()

    def test_persistently_blank_grab_raises(self):
        from actions.screen_processor import _CAPTURE_ATTEMPTS, _grab_verified

        sct = MagicMock()
        sct.grab.return_value = _fake_shot(bytes(48))
        with self.assertRaises(vr.CaptureVerificationError):
            _grab_verified(sct, {})
        self.assertEqual(sct.grab.call_count, _CAPTURE_ATTEMPTS)

    def test_blank_then_real_frame_recovers(self):
        from actions.screen_processor import _grab_verified

        real = bytes(range(48))
        sct = MagicMock()
        sct.grab.side_effect = [_fake_shot(bytes(48)), _fake_shot(real)]
        png = _grab_verified(sct, {})
        self.assertTrue(png.startswith(b"\x89PNG"))


class TestScreenProcessLoopBreaking(unittest.TestCase):
    def setUp(self):
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()
        self._capture = patch(
            "actions.screen_processor._capture_screen",
            return_value=(b"jpeg-bytes", "image/jpeg", 100, 50),
        )
        self._capture.start()

    def tearDown(self):
        self._capture.stop()
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()

    def test_failed_service_analysis_is_not_run_twice(self):
        from actions.screen_processor import screen_process

        failed = VisionResult(ok=True, error="Analysis timed out or failed")
        failed.metadata["analysis_failed"] = True
        with (
            patch("core.vision_service.analyze_screen", return_value=failed),
            patch("actions.screen_processor.analyze_image_once") as fallback,
        ):
            result = screen_process({"text": "Where do I click?"})
        fallback.assert_not_called()
        self.assertIn("could not analyze", result)
        self.assertIn("do not repeat", result)

    def test_failure_sentence_is_not_cached_or_published(self):
        from actions import screen_processor as sp

        with (
            patch("core.vision_service.analyze_screen", side_effect=RuntimeError("boom")),
            patch.object(sp, "analyze_image_once", return_value=sp._TIMEOUT_MESSAGE),
            patch("core.vision_context.store_latest_vision_result") as store,
            patch.object(sp, "_set_cached") as set_cached,
        ):
            result = sp.screen_process({"text": "What is this?"})
        self.assertIn("do not repeat", result)
        store.assert_not_called()
        set_cached.assert_not_called()

    def test_verified_capture_failure_explains_why(self):
        from actions.screen_processor import screen_process

        self._capture.stop()
        with patch(
            "actions.screen_processor._capture_screen",
            side_effect=vr.CaptureVerificationError("screen capture came back blank"),
        ):
            result = screen_process({"text": "What is this?"})
        self._capture.start()
        self.assertIn("could not analyze the screen", result)
        self.assertIn("came back blank", result)


class TestCameraFallback(unittest.TestCase):
    def test_moved_camera_index_is_found(self):
        from actions import screen_processor as sp

        if not sp._CV2:
            self.skipTest("OpenCV not installed")
        good = np.full((48, 64, 3), 120, dtype=np.uint8)
        reads = {0: (None, "could not be opened"), 1: (good, None)}
        with (
            patch.object(sp, "_get_camera_index", return_value=0),
            patch.object(
                sp, "_read_camera_frame", side_effect=lambda i, b: reads.get(i, (None, "x"))
            ),
        ):
            data, mime, _, _ = sp._capture_camera()
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(data)

    def test_no_usable_camera_raises_verification_error(self):
        from actions import screen_processor as sp

        if not sp._CV2:
            self.skipTest("OpenCV not installed")
        with (
            patch.object(sp, "_get_camera_index", return_value=0),
            patch.object(sp, "_read_camera_frame", return_value=(None, "only dark frames")),
        ):
            with self.assertRaises(vr.CaptureVerificationError):
                sp._capture_camera()


if __name__ == "__main__":
    unittest.main()
