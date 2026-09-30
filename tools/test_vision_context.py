"""Tests for core.vision_context — JIT vision capture and temporal cooldown."""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.vision_context import (
    _JIT_COOLDOWN_SECONDS,
    _LAST_JIT_EXECUTION_TIME,
    _jit_cooldown_ok,
    _jit_should_capture,
    clear_latest_vision_result,
    get_latest_vision_result,
    jit_vision_capture,
    store_latest_vision_result,
)
from core.vision_types import VisionResult


class TestJitShouldCapture(unittest.TestCase):
    """Verify the tool × action mutation matrix filter."""

    def test_computer_control_click(self):
        self.assertTrue(_jit_should_capture("computer_control", "click"))

    def test_browser_control_type(self):
        self.assertTrue(_jit_should_capture("browser_control", "type"))

    def test_browser_control_press(self):
        self.assertTrue(_jit_should_capture("browser_control", "press"))

    def test_browser_control_fill_form(self):
        self.assertTrue(_jit_should_capture("browser_control", "fill_form"))

    def test_browser_control_smart_click(self):
        self.assertTrue(_jit_should_capture("browser_control", "smart_click"))

    def test_browser_control_hotkey(self):
        self.assertTrue(_jit_should_capture("browser_control", "hotkey"))

    def test_computer_control_scroll_skipped(self):
        """scroll is NOT in the mutation matrix."""
        self.assertFalse(_jit_should_capture("computer_control", "scroll"))

    def test_non_mutation_tool_skipped(self):
        self.assertFalse(_jit_should_capture("web_search", "click"))

    def test_read_only_action_skipped(self):
        self.assertFalse(_jit_should_capture("browser_control", "get_text"))

    def test_media_control_skipped(self):
        self.assertFalse(_jit_should_capture("spotify_control", "play"))

    def test_empty_tool_skipped(self):
        self.assertFalse(_jit_should_capture("", "click"))

    def test_empty_action_skipped(self):
        self.assertFalse(_jit_should_capture("computer_control", ""))

    def test_unknown_tool_action_combo_skipped(self):
        self.assertFalse(_jit_should_capture("file_controller", "delete"))


class TestJitCooldown(unittest.TestCase):
    """Verify the 4-second temporal cooldown guard."""

    def setUp(self):
        import core.vision_context as vc

        self._original = vc._LAST_JIT_EXECUTION_TIME

    def tearDown(self):
        import core.vision_context as vc

        vc._LAST_JIT_EXECUTION_TIME = self._original

    def test_first_call_always_passes(self):
        import core.vision_context as vc

        vc._LAST_JIT_EXECUTION_TIME = 0.0
        self.assertTrue(_jit_cooldown_ok())

    def test_second_call_within_cooldown_rejected(self):
        import core.vision_context as vc

        vc._LAST_JIT_EXECUTION_TIME = time.monotonic()
        self.assertFalse(_jit_cooldown_ok())

    def test_call_after_cooldown_passes(self):
        import core.vision_context as vc

        vc._LAST_JIT_EXECUTION_TIME = time.monotonic() - _JIT_COOLDOWN_SECONDS - 0.1
        self.assertTrue(_jit_cooldown_ok())


class TestJitVisionCapture(unittest.TestCase):
    """Test the async jit_vision_capture function."""

    def setUp(self):
        clear_latest_vision_result()
        import core.vision_context as vc

        self._original_time = vc._LAST_JIT_EXECUTION_TIME
        vc._LAST_JIT_EXECUTION_TIME = 0.0

    def tearDown(self):
        import core.vision_context as vc

        vc._LAST_JIT_EXECUTION_TIME = self._original_time
        clear_latest_vision_result()

    def test_non_mutation_tool_skips_capture(self):
        """Non-mutation tools should not trigger any capture."""
        asyncio.run(jit_vision_capture("web_search", "query"))
        result = get_latest_vision_result()
        self.assertIsNone(result)

    def test_non_mutation_action_skips_capture(self):
        """Read-only actions on mutation tools should skip."""
        asyncio.run(jit_vision_capture("browser_control", "get_text"))
        result = get_latest_vision_result()
        self.assertIsNone(result)

    def test_cooldown_prevents_rapid_captures(self):
        """Second call within cooldown window should be skipped."""
        import core.vision_context as vc

        vc._LAST_JIT_EXECUTION_TIME = time.monotonic()

        mock_result = VisionResult(ok=True, screen_state="desktop")
        with patch("core.vision_service.analyze_screen", return_value=mock_result) as mock_analyze:
            asyncio.run(jit_vision_capture("computer_control", "click"))
            mock_analyze.assert_not_called()

    def test_successful_capture_stores_result(self):
        """Successful capture should store result in vision_context."""
        mock_result = VisionResult(ok=True, screen_state="desktop", summary="Clean desktop")

        async def _run():
            with patch("core.vision_service.analyze_screen", return_value=mock_result):
                await jit_vision_capture("computer_control", "click")

        asyncio.run(_run())
        stored = get_latest_vision_result(max_age_seconds=5.0)
        self.assertIsNotNone(stored)
        req_id, result = stored
        self.assertEqual(req_id, "jit-capture")
        self.assertTrue(result.ok)
        self.assertEqual(result.screen_state, "desktop")

    def test_capture_failure_swallows_exception(self):
        """If analyze_screen raises, jit_vision_capture should not propagate."""

        async def _run():
            with patch("core.vision_service.analyze_screen", side_effect=RuntimeError("API down")):
                await jit_vision_capture("browser_control", "click")

        asyncio.run(_run())
        result = get_latest_vision_result()
        self.assertIsNone(result)

    def test_timeout_swallows_exception(self):
        """If capture exceeds timeout, jit_vision_capture should not propagate."""
        import time as _time

        def _slow_capture(*args, **kwargs):
            _time.sleep(10)
            return VisionResult(ok=True)

        async def _run():
            with patch("core.vision_service.analyze_screen", side_effect=_slow_capture):
                await jit_vision_capture("computer_control", "type", timeout=0.1)

        asyncio.run(_run())
        result = get_latest_vision_result()
        self.assertIsNone(result)

    def test_import_error_swallows_exception(self):
        """If vision_service cannot be imported, should silently skip."""
        import core.vision_context as vc

        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def _mock_import(name, *args, **kwargs):
            if name == "core.vision_service":
                raise ImportError("no vision_service")
            return original_import(name, *args, **kwargs)

        async def _run():
            with patch("builtins.__import__", side_effect=_mock_import):
                await jit_vision_capture("computer_control", "click")

        asyncio.run(_run())
        result = get_latest_vision_result()
        self.assertIsNone(result)

    def test_cooldown_respected_across_calls(self):
        """Two rapid calls should only capture once."""
        call_count = 0

        def _counting_capture(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return VisionResult(ok=True, screen_state="desktop")

        async def _run():
            with patch("core.vision_service.analyze_screen", side_effect=_counting_capture):
                await jit_vision_capture("computer_control", "click")
                await jit_vision_capture("computer_control", "click")
                await jit_vision_capture("computer_control", "click")

        asyncio.run(_run())
        self.assertEqual(call_count, 1)

    def test_cooldown_resets_after_window(self):
        """After cooldown window expires, capture should fire again."""
        import core.vision_context as vc

        call_count = 0

        def _counting_capture(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return VisionResult(ok=True, screen_state="desktop")

        async def _run():
            with patch("core.vision_service.analyze_screen", side_effect=_counting_capture):
                await jit_vision_capture("computer_control", "click")
                vc._LAST_JIT_EXECUTION_TIME = time.monotonic() - _JIT_COOLDOWN_SECONDS - 0.1
                await jit_vision_capture("computer_control", "click")

        asyncio.run(_run())
        self.assertEqual(call_count, 2)


class TestStoreAndGet(unittest.TestCase):
    """Basic store/get/clear lifecycle."""

    def setUp(self):
        clear_latest_vision_result()

    def tearDown(self):
        clear_latest_vision_result()

    def test_store_and_get(self):
        vr = VisionResult(ok=True, screen_state="desktop")
        store_latest_vision_result("req-1", vr)
        stored = get_latest_vision_result()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], "req-1")
        self.assertTrue(stored[1].ok)

    def test_get_expired_returns_none(self):
        vr = VisionResult(ok=True)
        store_latest_vision_result("req-2", vr)
        result = get_latest_vision_result(max_age_seconds=0.0)
        self.assertIsNone(result)

    def test_clear(self):
        vr = VisionResult(ok=True)
        store_latest_vision_result("req-3", vr)
        clear_latest_vision_result()
        result = get_latest_vision_result()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
