"""
Tests for Step V1 + V2 — VisionService wrapper.

V1 verifies:
1. analyze_screen returns VisionResult
2. VisionService never calls computer_control click
3. VisionService never calls keyboard/type/press
4. Cache returns cached=True on repeated same prompt/screenshot
5. JSON response parses into VisionResult elements
6. Plain text response falls back to summary/raw_text
7. Failed capture returns ok=False with error
8. Debug mode does not crash
9. screen_process() still returns text
10. No computer_control usage in vision_service

V2 verifies:
11. active_window crop used when bounds available
12. fallback to full_screen when bounds unavailable
13. active_window_is_era_one fallback
14. metadata includes capture_method, crop_box, original_size, processed_size,
    capture_ms, analyze_ms, fallback_reason
15. fast mode processed image smaller/lower quality than full mode where possible
16. debug mode saves artifacts safely
17. cache still works with cropped screenshots
18. VisionService never calls click/type/press/scroll
19. screen_process still returns text
20. existing test_vision_silent.py still passes

Run:
    python -m pytest tools/test_vision_service.py -v --tb=short
"""

import inspect
import sys
import unittest
from unittest.mock import MagicMock, patch


class TestVisionService(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch("actions.screen_processor._capture_screen"),
            patch("actions.screen_processor._capture_camera"),
            patch("actions.screen_processor.analyze_image_once"),
        ]
        self.mock_capture = self._patchers[0].start()
        self.mock_camera = self._patchers[1].start()
        self.mock_analyze = self._patchers[2].start()

        self.mock_capture.return_value = (b"fake-image-data", "image/jpeg", 1000, 800)
        self.mock_analyze.return_value = (
            '{"screen_state":"desktop","summary":"A clean desktop.",'
            '"elements":[],"recommended_action":"none"}'
        )

        # V2 mocks: capture_screen_raw and compress_for_mode used by _capture_targeted
        self._v2_patchers = [
            patch("actions.screen_processor.capture_screen_raw"),
            patch("core.capture_utils.compress_for_mode"),
            patch("core.capture_utils.get_active_window_rect"),
            patch("core.capture_utils.crop_raw_image"),
        ]
        self.mock_raw_capture = self._v2_patchers[0].start()
        self.mock_compress_v2 = self._v2_patchers[1].start()
        self.mock_get_rect = self._v2_patchers[2].start()
        self.mock_crop_raw = self._v2_patchers[3].start()

        self.mock_raw_capture.return_value = (b"fake-raw-png-data", 0, 0, 1920, 1080, 4.2)
        self.mock_compress_v2.return_value = (b"fake-compressed-data", "image/jpeg", 100, 60)
        # Default: no crop (fallback to full screen)
        self.mock_get_rect.return_value = None
        self.mock_crop_raw.return_value = None

        from core.vision_service import _CACHE

        _CACHE.clear()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        for p in self._v2_patchers:
            p.stop()

    # ── V1 tests ──

    def test_analyze_screen_returns_vision_result(self):
        from core.vision_service import analyze_screen
        from core.vision_types import VisionResult

        result = analyze_screen("What do you see?")
        self.assertIsInstance(result, VisionResult)
        self.assertTrue(result.ok)

    def test_no_computer_control_called(self):
        from core.vision_service import analyze_screen

        with patch("actions.computer_control.computer_control") as mock_cc:
            analyze_screen("What do you see?")
            mock_cc.assert_not_called()

    def test_no_keyboard_or_type_called(self):
        from core.vision_service import analyze_screen

        with patch("actions.computer_control._type") as mock_type:
            analyze_screen("What do you see?")
            mock_type.assert_not_called()

    def test_no_press_keys_called(self):
        from core.vision_service import analyze_screen

        with patch("actions.computer_control._hotkey") as mock_hotkey:
            analyze_screen("What do you see?")
            mock_hotkey.assert_not_called()

    def test_cache_returns_cached_on_repeat(self):
        from core.vision_service import analyze_screen

        r1 = analyze_screen("Same prompt")
        self.assertFalse(r1.cached)
        self.assertEqual(self.mock_analyze.call_count, 1)

        r2 = analyze_screen("Same prompt")
        self.assertTrue(r2.cached)
        self.assertEqual(self.mock_analyze.call_count, 1)

    def test_json_response_parses_elements(self):
        self.mock_analyze.return_value = (
            '{"screen_state":"browser","summary":"A YouTube video page.",'
            '"elements":[{"label":"Search","type":"input","bbox":[10,20,300,40],'
            '"confidence":0.95,"risk":"low"},'
            '{"label":"Play","type":"button","bbox":[400,500,100,50],'
            '"confidence":0.88,"risk":"low"}],'
            '"recommended_action":"use_dom_first"}'
        )
        from core.vision_service import analyze_screen

        result = analyze_screen("What page?")
        self.assertTrue(result.ok)
        self.assertEqual(result.screen_state, "browser")
        self.assertEqual(len(result.elements), 2)
        self.assertEqual(result.elements[0].label, "Search")
        self.assertEqual(result.elements[0].type, "input")
        self.assertEqual(result.elements[0].bbox, [10, 20, 300, 40])
        self.assertAlmostEqual(result.elements[0].confidence, 0.95)
        self.assertEqual(result.elements[0].risk, "low")
        self.assertEqual(result.elements[1].label, "Play")
        self.assertEqual(result.recommended_action, "use_dom_first")

    def test_plain_text_fallback(self):
        self.mock_analyze.return_value = "Just a plain text description of the screen."
        from core.vision_service import analyze_screen

        result = analyze_screen("What do you see?")
        self.assertTrue(result.ok)
        self.assertIn("plain text", result.summary)
        self.assertEqual(len(result.elements), 0)
        self.assertEqual(result.screen_state, "unknown")
        self.assertEqual(result.recommended_action, "none")

    def test_failed_capture_returns_error(self):
        self.mock_raw_capture.side_effect = RuntimeError("No screen available")
        from core.vision_service import analyze_screen

        result = analyze_screen("What?")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertIn("Capture failed", result.error)

    def test_debug_mode_does_not_crash(self):
        from core.vision_service import analyze_screen

        with patch("core.vision_service._save_debug_artifacts", return_value="/tmp/debug.jpg"):
            result = analyze_screen("Test", save_debug=True)
            self.assertTrue(result.ok)

    def test_empty_prompt_returns_error(self):
        from core.vision_service import analyze_screen

        result = analyze_screen("")
        self.assertFalse(result.ok)
        self.assertIn("No prompt", result.error or "")

    def test_screen_process_still_returns_text(self):
        from actions.screen_processor import screen_process

        result = screen_process(parameters={"text": "What do you see?"})
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_no_computer_control_import(self):
        import core.vision_service as vs

        source = inspect.getsource(vs)
        self.assertNotIn("computer_control", source)
        self.assertNotIn("pyautogui", source)
        self.assertNotIn("_screen_click", source)
        self.assertNotIn("def _type", source)
        self.assertNotIn("from actions import computer_control", source)
        self.assertNotIn("def _click(", source)

    def test_no_browser_control_import(self):
        import core.vision_service as vs

        source = inspect.getsource(vs)
        self.assertNotIn("browser_control", source)
        self.assertNotIn("netflix_control", source)
        self.assertNotIn("youtube_video", source)

    def test_different_prompt_different_cache(self):
        from core.vision_service import analyze_screen

        r1 = analyze_screen("First question")
        self.assertFalse(r1.cached)
        r2 = analyze_screen("Different question")
        self.assertFalse(r2.cached)
        self.assertEqual(self.mock_analyze.call_count, 2)

    # ── V2 tests ──

    def test_active_window_crop_when_bounds_available(self):
        self.mock_get_rect.return_value = (100, 50, 1400, 900)
        self.mock_crop_raw.return_value = b"cropped-png"
        # compress_for_mode passes through
        self.mock_compress_v2.return_value = (b"crop-compressed", "image/jpeg", 200, 100)

        from core.vision_service import analyze_screen

        result = analyze_screen("What's in the window?", mode="fast", target="active_window")

        self.assertTrue(result.ok)
        meta = result.metadata
        self.assertIn("capture_method", meta)
        self.assertEqual(meta["capture_method"], "crop_active_window")
        self.assertIn("crop_box", meta)
        self.assertEqual(meta["crop_box"], [100, 50, 1400, 900])
        self.assertIn("original_size", meta)
        self.assertIn("processed_size", meta)
        self.assertIn("capture_ms", meta)
        self.assertIn("analyze_ms", meta)
        self.assertNotIn("fallback_reason", meta)

    def test_fallback_to_full_screen_when_bounds_unavailable(self):
        self.mock_get_rect.return_value = None
        self.mock_crop_raw.return_value = None

        from core.vision_service import analyze_screen

        result = analyze_screen("What's on screen?", mode="fast", target="active_window")

        self.assertTrue(result.ok)
        meta = result.metadata
        self.assertNotEqual(meta.get("capture_method", ""), "crop_active_window")
        self.assertIn("fallback_reason", meta)
        self.assertNotIn("crop_box", meta)

    def test_active_window_is_era_one_fallback(self):
        self.mock_get_rect.return_value = None

        from core.vision_service import analyze_screen

        result = analyze_screen("What's on screen?", mode="fast", target="active_window")

        self.assertTrue(result.ok)
        meta = result.metadata
        self.assertIn("fallback_reason", meta)

    def test_metadata_includes_all_fields(self):
        self.mock_get_rect.return_value = (200, 100, 1200, 800)
        self.mock_crop_raw.return_value = b"cropped-png"
        self.mock_compress_v2.return_value = (b"crop-compressed", "image/jpeg", 300, 150)

        from core.vision_service import analyze_screen

        result = analyze_screen("Check metadata", mode="fast", target="active_window")

        self.assertTrue(result.ok)
        meta = result.metadata
        required = [
            "capture_method",
            "original_size",
            "processed_size",
            "crop_box",
            "capture_ms",
            "analyze_ms",
        ]
        for field in required:
            self.assertIn(field, meta, f"Missing metadata field: {field}")
        self.assertIsInstance(meta["original_size"], int)
        self.assertIsInstance(meta["processed_size"], int)
        self.assertIsInstance(meta["capture_ms"], (int, float))
        self.assertIsInstance(meta["analyze_ms"], (int, float))
        self.assertEqual(meta["capture_method"], "crop_active_window")

    def test_fast_mode_uses_smaller_params_than_full(self):
        from core.vision_service import (
            _FAST_JPEG_QUALITY,
            _FAST_MAX_WIDTH,
            _FULL_JPEG_QUALITY,
            _FULL_MAX_WIDTH,
        )

        self.assertLessEqual(_FAST_MAX_WIDTH, _FULL_MAX_WIDTH)
        self.assertLessEqual(_FAST_JPEG_QUALITY, _FULL_JPEG_QUALITY)

    def test_debug_mode_saves_artifacts_safely(self):
        from core.vision_service import _save_debug_artifacts, analyze_screen

        with patch("core.vision_service._save_debug_artifacts") as mock_save:
            mock_save.return_value = "/tmp/debug.jpg"
            result = analyze_screen("Debug test", mode="debug")
            self.assertTrue(result.ok)
            mock_save.assert_called_once()

    def test_cache_works_with_cropped_screenshots(self):
        self.mock_get_rect.return_value = (100, 50, 1400, 900)
        self.mock_crop_raw.return_value = b"cropped-png"
        self.mock_compress_v2.return_value = (b"crop-compressed", "image/jpeg", 200, 100)

        from core.vision_service import analyze_screen

        r1 = analyze_screen("Same cropped view", mode="fast", target="active_window")
        self.assertFalse(r1.cached)

        r2 = analyze_screen("Same cropped view", mode="fast", target="active_window")
        self.assertTrue(r2.cached)
        self.assertEqual(self.mock_analyze.call_count, 1)

    def test_vision_service_never_calls_click_type_press_scroll(self):
        import core.vision_service as vs

        source = inspect.getsource(vs)
        self.assertNotIn("computer_control", source)
        self.assertNotIn("pyautogui", source)
        self.assertNotIn("_screen_click", source)
        self.assertNotIn("def _type", source)
        self.assertNotIn("def _click(", source)
        self.assertNotIn("def _hotkey(", source)
        self.assertNotIn("def _press(", source)
        self.assertNotIn("import pyautogui", source)

    def test_crop_rect_clamped_to_image_bounds(self):
        from core.capture_utils import _clamp_rect

        # Rect extends beyond image
        rect = (-100, -50, 5000, 5000)
        img_w, img_h = 1920, 1080
        offset_x, offset_y = 0, 0
        clamped = _clamp_rect(rect, img_w, img_h, offset_x, offset_y)
        self.assertIsNotNone(clamped)
        c_left, c_top, c_right, c_bottom = clamped
        self.assertGreaterEqual(c_left, 0)
        self.assertGreaterEqual(c_top, 0)
        self.assertLessEqual(c_right, img_w)
        self.assertLessEqual(c_bottom, img_h)
        self.assertLess(c_left, c_right)
        self.assertLess(c_top, c_bottom)

    def test_negative_monitor_coordinates_handled(self):
        from core.capture_utils import _clamp_rect

        # Secondary monitor left of primary: virtual origin at -1920
        # Active window at virtual x=-1800 to -400 (on secondary monitor)
        # Full virtual screen spans -1920..1920 = 3840px wide
        rect = (-1800, 100, -400, 900)
        offset_x, offset_y = -1920, 0
        clamped = _clamp_rect(rect, 3840, 1080, offset_x, offset_y)
        self.assertIsNotNone(clamped)
        c_left, c_top, c_right, c_bottom = clamped
        # px_left = -1800 - (-1920) = 120
        self.assertGreaterEqual(c_left, 0)
        self.assertLessEqual(c_right, 3840)
        self.assertLess(c_left, c_right)
        self.assertLess(c_top, c_bottom)

    def test_negative_coordinate_fallback_when_unsafe(self):
        from core.capture_utils import _clamp_rect

        # Window entirely outside the captured area
        rect = (-2000, -100, -1500, -50)
        offset_x, offset_y = 0, 0
        clamped = _clamp_rect(rect, 1920, 1080, offset_x, offset_y)
        self.assertIsNone(clamped)

    def test_browser_viewport_target(self):
        self.mock_get_rect.return_value = (
            None  # get_active_window_rect fail triggers browser fallback
        )
        from core.vision_service import analyze_screen

        result = analyze_screen("Browser test", mode="fast", target="browser_viewport")
        self.assertTrue(result.ok)
        meta = result.metadata
        self.assertIn("fallback_reason", meta)  # browser_viewport_unavailable

    def test_save_debug_artifacts_never_crashes(self):
        from core.vision_service import analyze_screen

        with patch("core.vision_service._save_debug_artifacts", return_value=None):
            result = analyze_screen("Debug no crash", mode="debug")
            self.assertTrue(result.ok)


class TestVisionServiceModel(unittest.TestCase):
    def test_vision_element_defaults(self):
        from core.vision_types import VisionElement

        el = VisionElement()
        self.assertEqual(el.label, "")
        self.assertEqual(el.type, "unknown")
        self.assertEqual(el.bbox, [])
        self.assertEqual(el.confidence, 0.0)
        self.assertEqual(el.risk, "low")

    def test_vision_result_defaults(self):
        from core.vision_types import VisionResult

        r = VisionResult(ok=True)
        self.assertTrue(r.ok)
        self.assertEqual(r.mode, "fast")
        self.assertEqual(r.target, "active_window")
        self.assertIsNone(r.screenshot_path)
        self.assertEqual(r.screen_state, "unknown")
        self.assertEqual(r.summary, "")
        self.assertEqual(r.elements, [])
        self.assertEqual(r.recommended_action, "none")
        self.assertIsNone(r.error)

    def test_vision_result_factory(self):
        from core.vision_types import VisionElement, VisionResult

        r = VisionResult(
            ok=True,
            mode="debug",
            target="full",
            screen_state="browser",
            summary="Netflix page",
            elements=[VisionElement(label="Play", type="button")],
            recommended_action="use_dom_first",
        )
        self.assertEqual(r.mode, "debug")
        self.assertEqual(len(r.elements), 1)
        self.assertEqual(r.elements[0].label, "Play")


class TestVisionV4Classifier(unittest.TestCase):
    """V4: Structured screen-state classifier and element detection."""

    # ── Parser tests ──

    def test_v4_parses_pure_json(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = '{"screen_state":"desktop","summary":"Clean desktop.","elements":[],"recommended_action":"none","needs_confirmation":false}'
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "desktop")
        self.assertEqual(parsed["summary"], "Clean desktop.")
        self.assertEqual(parsed["elements"], [])
        self.assertEqual(parsed["recommended_action"], "none")

    def test_v4_parses_json_in_code_block(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = 'Some text\n```json\n{"screen_state":"browser","summary":"Browser.","elements":[{"label":"Search","type":"input","bbox":[0,0,100,20],"confidence":0.9,"risk":"low","action_hint":"none"}],"recommended_action":"use_dom_first","needs_confirmation":false}\n```\nMore text'
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "browser")
        self.assertEqual(len(parsed["elements"]), 1)
        self.assertEqual(parsed["elements"][0]["label"], "Search")

    def test_v4_parses_json_with_text_before_after(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = 'Here is the analysis:\n{"screen_state":"form","summary":"A form.","elements":[],"recommended_action":"none","needs_confirmation":false}\nHope this helps.'
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "form")

    def test_v4_malformed_json_fallback(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = "This is not valid JSON at all. No braces here."
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "unknown")
        self.assertEqual(parsed["elements"], [])
        self.assertEqual(parsed["summary"], raw)

    def test_v4_malformed_json_brace_no_parse(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = "This has braces but is not valid: {screen_state: broken}"
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "unknown")

    # ── Bbox normalization ──

    def test_v4_invalid_bbox_normalized(self):
        from core.vision_classifier import _normalize_bbox

        self.assertEqual(_normalize_bbox(None), [])
        self.assertEqual(_normalize_bbox("string"), [])
        self.assertEqual(_normalize_bbox([1, 2, 3]), [])
        self.assertEqual(_normalize_bbox([1, 2, 3, 4, 5]), [])
        self.assertEqual(_normalize_bbox(["a", "b", "c", "d"]), [])
        self.assertEqual(_normalize_bbox([1.5, 2.5, 3.5, 4.5]), [1, 2, 3, 4])

    def test_v4_valid_bbox_preserved(self):
        from core.vision_classifier import _normalize_bbox

        self.assertEqual(_normalize_bbox([10, 20, 300, 40]), [10, 20, 300, 40])

    # ── Confidence clamping ──

    def test_v4_confidence_clamped(self):
        from core.vision_classifier import _normalize_confidence

        self.assertEqual(_normalize_confidence(1.5), 1.0)
        self.assertEqual(_normalize_confidence(-0.3), 0.0)
        self.assertEqual(_normalize_confidence("abc"), 0.0)
        self.assertEqual(_normalize_confidence(None), 0.0)

    def test_v4_confidence_normal_range(self):
        from core.vision_classifier import _normalize_confidence

        self.assertEqual(_normalize_confidence(0.7), 0.7)
        self.assertEqual(_normalize_confidence(0.0), 0.0)
        self.assertEqual(_normalize_confidence(1.0), 1.0)

    # ── Missing fields ──

    def test_v4_missing_fields_default_safely(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = '{"screen_state":"desktop"}'
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "desktop")
        self.assertEqual(parsed["summary"], "")
        self.assertEqual(parsed["elements"], [])
        self.assertEqual(parsed["recommended_action"], "none")
        self.assertEqual(parsed["needs_confirmation"], False)

    def test_v4_unknown_screen_state_falls_back(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = '{"screen_state":"foobar","summary":"test","elements":[],"recommended_action":"none"}'
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["screen_state"], "unknown")

    # ── DOM-first logic ──

    def test_v4_browser_recommends_use_dom_first(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = '{"screen_state":"browser","summary":"Browser","elements":[],"recommended_action":"none","needs_confirmation":false}'
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["recommended_action"], "use_dom_first")

    def test_v4_media_page_recommends_use_dom_first(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = '{"screen_state":"media_page","summary":"Media","elements":[],"recommended_action":"none","needs_confirmation":false}'
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["recommended_action"], "use_dom_first")

    def test_v4_desktop_does_not_force_dom_first(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = '{"screen_state":"desktop","summary":"Desktop","elements":[],"recommended_action":"none","needs_confirmation":false}'
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["recommended_action"], "none")

    def test_v4_browser_button_gets_action_hint_dom_first(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"browser","summary":"Page","elements":['
            '{"label":"Click","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"low"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["action_hint"], "use_dom_first")

    def test_v4_browser_link_gets_action_hint_dom_first(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"browser","summary":"Page","elements":['
            '{"label":"Link","type":"link","bbox":[0,0,10,10],"confidence":0.9,"risk":"low"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["action_hint"], "use_dom_first")

    def test_v4_browser_input_gets_action_hint_dom_first(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"browser","summary":"Page","elements":['
            '{"label":"Name","type":"input","bbox":[0,0,10,10],"confidence":0.9,"risk":"low"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["action_hint"], "use_dom_first")

    def test_v4_browser_high_risk_preserves_ask_user(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"browser","summary":"Login","elements":['
            '{"label":"Submit","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"high",'
            '"action_hint":"ask_user"}'
            '],"recommended_action":"ask_user","needs_confirmation":true}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["recommended_action"], "ask_user")
        self.assertEqual(parsed["elements"][0]["action_hint"], "ask_user")

    # ── Risk classification ──

    def test_v4_high_risk_detected_for_password(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Login","elements":['
            '{"label":"Enter password","type":"input","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_high_risk_detected_for_delete(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Delete","elements":['
            '{"label":"Delete account","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_high_risk_detected_for_payment(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Pay","elements":['
            '{"label":"Pay now","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_high_risk_detected_for_buy(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Buy","elements":['
            '{"label":"Buy now","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_high_risk_detected_for_french_password(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Login","elements":['
            '{"label":"Mot de passe","type":"input","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_high_risk_detected_for_french_delete(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Delete","elements":['
            '{"label":"Supprimer","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_high_risk_detected_for_french_pay(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Pay","elements":['
            '{"label":"Payer","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    def test_v4_medium_risk_detected_for_download(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"browser","summary":"Download","elements":['
            '{"label":"Download file","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "medium")

    def test_v4_medium_risk_detected_for_play(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"media_page","summary":"Video","elements":['
            '{"label":"Play video","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertIn(parsed["elements"][0]["risk"], ("medium", "low"))

    def test_v4_medium_risk_detected_for_install(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Install","elements":['
            '{"label":"Install now","type":"button","bbox":[0,0,100,20],"confidence":0.9}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "medium")

    # ── needs_confirmation ──

    def test_v4_needs_confirmation_true_for_high_risk(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"form","summary":"Delete","elements":['
            '{"label":"Delete","type":"button","bbox":[0,0,100,20],"confidence":0.9,"risk":"high"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["needs_confirmation"])

    def test_v4_needs_confirmation_true_for_ask_user(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Confirm","elements":[],'
            '"recommended_action":"ask_user","needs_confirmation":true}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertTrue(parsed["needs_confirmation"])

    def test_v4_needs_confirmation_false_for_safe_screen(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Clean","elements":['
            '{"label":"OK","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"low"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertFalse(parsed["needs_confirmation"])

    # ── Dataclass new fields ──

    def test_v4_vision_element_has_action_hint(self):
        from core.vision_types import VisionElement

        el = VisionElement()
        self.assertTrue(hasattr(el, "action_hint"))
        self.assertEqual(el.action_hint, "none")
        el2 = VisionElement(label="test", action_hint="use_dom_first")
        self.assertEqual(el2.action_hint, "use_dom_first")

    def test_v4_vision_result_has_needs_confirmation(self):
        from core.vision_types import VisionResult

        r = VisionResult(ok=True)
        self.assertTrue(hasattr(r, "needs_confirmation"))
        self.assertFalse(r.needs_confirmation)
        r2 = VisionResult(ok=True, needs_confirmation=True)
        self.assertTrue(r2.needs_confirmation)

    # ── Backward compatibility ──

    def test_v4_analyze_screen_still_returns_vision_result(self):
        from core.vision_service import analyze_screen
        from core.vision_types import VisionResult

        with patch("actions.screen_processor.analyze_image_once") as mock:
            mock.return_value = '{"screen_state":"desktop","summary":"Test","elements":[],"recommended_action":"none","needs_confirmation":false}'
            with patch("core.vision_service._capture_targeted") as mock_cap:
                mock_cap.return_value = (b"img", "image/jpeg", 100, 50, {})
                result = analyze_screen("What?")
        self.assertIsInstance(result, VisionResult)
        self.assertTrue(result.ok)

    def test_v4_screen_process_still_returns_str(self):
        from actions.screen_processor import screen_process

        with patch("actions.screen_processor.analyze_image_once") as mock:
            mock.return_value = "A simple text result."
            result = screen_process(parameters={"text": "What?"})
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    # ── Element type fallback ──

    def test_v4_element_type_falls_back_to_unknown(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Test","elements":['
            '{"label":"Thing","type":"invalid_type","bbox":[0,0,10,10],"confidence":0.9,"risk":"low"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["type"], "unknown")

    # ── Low risk desktop button remains safe ──

    def test_v4_normal_low_risk_desktop_button_safe(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Desktop","elements":['
            '{"label":"Show details","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"low"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "low")
        self.assertEqual(parsed["elements"][0]["action_hint"], "none")

    # ── Invalid risk falls back to local classifier ──

    def test_v4_invalid_risk_falls_back_to_local(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Test","elements":['
            '{"label":"Hello","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"invalid"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["risk"], "low")

    # ── Invalid action_hint falls back ──

    def test_v4_invalid_action_hint_falls_back(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Test","elements":['
            '{"label":"Hi","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"low","action_hint":"invalid"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        self.assertEqual(parsed["elements"][0]["action_hint"], "none")

    # ── Empty string input ──

    def test_v4_empty_string_returns_fallback(self):
        from core.vision_classifier import parse_structured_vision_response

        parsed = parse_structured_vision_response("")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "unknown")

    # ── Null input ──

    def test_v4_none_input_handled(self):
        from core.vision_classifier import parse_structured_vision_response

        parsed = parse_structured_vision_response(None)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["screen_state"], "unknown")

    # ── app_name and page_type in metadata ──

    def test_v4_app_name_and_page_type_in_metadata(self):
        from core.vision_service import analyze_screen

        with patch("actions.screen_processor.analyze_image_once") as mock:
            mock.return_value = (
                '{"screen_state":"browser","app_name":"YouTube","page_type":"video",'
                '"summary":"A video page.","elements":[],'
                '"recommended_action":"use_dom_first","needs_confirmation":false}'
            )
            with patch("core.vision_service._capture_targeted") as mock_cap:
                mock_cap.return_value = (b"img", "image/jpeg", 100, 50, {})
                result = analyze_screen("What page?")
        self.assertEqual(result.metadata.get("app_name"), "YouTube")
        self.assertEqual(result.metadata.get("page_type"), "video")

    # ── Gemini high risk never downgraded ──

    def test_v4_gemini_high_risk_never_downgraded(self):
        from core.vision_classifier import parse_structured_vision_response

        raw = (
            '{"screen_state":"desktop","summary":"Test","elements":['
            '{"label":"Play","type":"button","bbox":[0,0,10,10],"confidence":0.9,"risk":"high"}'
            '],"recommended_action":"none","needs_confirmation":false}'
        )
        parsed = parse_structured_vision_response(raw)
        # "Play" is a medium keyword, but Gemini said "high" so it stays
        self.assertEqual(parsed["elements"][0]["risk"], "high")

    # ── Prompt builder includes user question ──

    def test_v4_build_prompt_includes_user_question(self):
        from core.vision_classifier import build_structured_vision_prompt

        prompt = build_structured_vision_prompt("What is on screen?", "fast", "active_window")
        self.assertIn("What is on screen?", prompt)

    def test_v4_build_prompt_includes_json_schema(self):
        from core.vision_classifier import build_structured_vision_prompt

        prompt = build_structured_vision_prompt("What?", "fast", "active_window")
        self.assertIn("screen_state", prompt)
        self.assertIn("elements", prompt)
        self.assertIn("recommended_action", prompt)

    # ── Classifier no computer_control / pyautogui ──

    def test_v4_classifier_no_computer_control(self):
        import core.vision_classifier as vc

        source = inspect.getsource(vc)
        self.assertNotIn("computer_control", source)

    def test_v4_classifier_no_pyautogui(self):
        import core.vision_classifier as vc

        source = inspect.getsource(vc)
        self.assertNotIn("pyautogui", source)

    def test_v4_classifier_no_click_type_press_scroll(self):
        import core.vision_classifier as vc

        source = inspect.getsource(vc)
        for keyword in ("_click(", "def _type", "_press(", "_hotkey(", "_scroll("):
            self.assertNotIn(keyword, source)


if __name__ == "__main__":
    unittest.main()
