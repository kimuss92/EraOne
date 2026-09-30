"""
Tests for the centralized scroll engine (utils/scroll.py).

Validates presets, direction resolution, repeat clamping,
JS script generation, and pyautogui fallback strength.

No real browser movement. No real pyautogui movement — everything is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.scroll import (
    _PAGE_FALLBACK_PX,
    _PRESET_MAP,
    build_scroll_script,
    clamp_repeat,
    resolve_amount,
    resolve_behavior,
    resolve_direction,
    scroll,
)

# ═════════════════════════════════════════════════════════════════════════════
# resolve_amount
# ═════════════════════════════════════════════════════════════════════════════


class TestResolveAmount:
    def test_small_preset(self):
        assert resolve_amount("small") == 300

    def test_sm_alias(self):
        assert resolve_amount("sm") == 300

    def test_normal_preset(self):
        assert resolve_amount("normal") == 700

    def test_md_alias(self):
        assert resolve_amount("md") == 700

    def test_med_alias(self):
        assert resolve_amount("med") == 700

    def test_large_preset(self):
        assert resolve_amount("large") == 1200

    def test_lg_alias(self):
        assert resolve_amount("lg") == 1200

    def test_page_with_viewport(self):
        assert resolve_amount("page", viewport_height=1000) == 900

    def test_page_fallback_no_viewport(self):
        assert resolve_amount("page") == _PAGE_FALLBACK_PX

    def test_page_zero_viewport(self):
        assert resolve_amount("page", viewport_height=0) == _PAGE_FALLBACK_PX

    def test_numeric_string(self):
        assert resolve_amount("500") == 500

    def test_integer_amount(self):
        assert resolve_amount(500) == 500

    def test_negative_integer_clamped_to_zero(self):
        assert resolve_amount(-100) == 0

    def test_unknown_amount_defaults_to_normal(self):
        assert resolve_amount("foobar") == _PRESET_MAP["normal"]

    def test_case_insensitivity(self):
        assert resolve_amount("LARGE") == 1200
        assert resolve_amount("Normal") == 700
        assert resolve_amount("SM") == 300

    def test_page_small_viewport(self):
        assert resolve_amount("page", viewport_height=100) == 90


# ═════════════════════════════════════════════════════════════════════════════
# resolve_direction
# ═════════════════════════════════════════════════════════════════════════════


class TestResolveDirection:
    def test_down(self):
        assert resolve_direction("down") == (0, 1)

    def test_up(self):
        assert resolve_direction("up") == (0, -1)

    def test_left(self):
        assert resolve_direction("left") == (-1, 0)

    def test_right(self):
        assert resolve_direction("right") == (1, 0)

    def test_case_insensitive(self):
        assert resolve_direction("DOWN") == (0, 1)
        assert resolve_direction("Up") == (0, -1)

    def test_unknown_falls_back_to_down(self):
        assert resolve_direction("diagonal") == (0, 1)


# ═════════════════════════════════════════════════════════════════════════════
# clamp_repeat
# ═════════════════════════════════════════════════════════════════════════════


class TestClampRepeat:
    def test_default(self):
        assert clamp_repeat(1) == 1

    def test_max_ten(self):
        assert clamp_repeat(10) == 10

    def test_clamps_above_max(self):
        assert clamp_repeat(15) == 10

    def test_clamps_below_min(self):
        assert clamp_repeat(0) == 1

    def test_negative(self):
        assert clamp_repeat(-5) == 1

    def test_mid_range(self):
        assert clamp_repeat(5) == 5


# ═════════════════════════════════════════════════════════════════════════════
# resolve_behavior
# ═════════════════════════════════════════════════════════════════════════════


class TestResolveBehavior:
    def test_smooth(self):
        assert resolve_behavior("smooth") == "smooth"

    def test_auto(self):
        assert resolve_behavior("auto") == "auto"

    def test_instant_maps_to_auto(self):
        assert resolve_behavior("instant") == "auto"

    def test_case_insensitive(self):
        assert resolve_behavior("SMOOTH") == "smooth"

    def test_unknown_falls_back_to_smooth(self):
        assert resolve_behavior("bouncy") == "smooth"


# ═════════════════════════════════════════════════════════════════════════════
# build_scroll_script
# ═════════════════════════════════════════════════════════════════════════════


class TestBuildScrollScript:
    def test_contains_scrollBy(self):
        script = build_scroll_script(0, 1, 700, "smooth")
        assert "window.scrollBy" in script

    def test_correct_top_amount_down(self):
        script = build_scroll_script(0, 1, 700, "smooth")
        assert "top: 700" in script

    def test_correct_top_amount_up(self):
        script = build_scroll_script(0, -1, 300, "smooth")
        assert "top: -300" in script

    def test_correct_left_amount_right(self):
        script = build_scroll_script(1, 0, 500, "smooth")
        assert "left: 500" in script

    def test_correct_left_amount_left(self):
        script = build_scroll_script(-1, 0, 500, "smooth")
        assert "left: -500" in script

    def test_behavior_in_script(self):
        script = build_scroll_script(0, 1, 700, "auto")
        assert "behavior: 'auto'" in script

    def test_instant_mapped_to_auto(self):
        script = build_scroll_script(0, 1, 700, "instant")
        assert "behavior: 'auto'" in script

    def test_returns_json_stringify(self):
        script = build_scroll_script(0, 1, 700, "smooth")
        assert "JSON.stringify" in script


# ═════════════════════════════════════════════════════════════════════════════
# scroll — async engine (mocked page)
# ═════════════════════════════════════════════════════════════════════════════


class TestScrollEngine:
    @pytest.mark.asyncio
    async def test_basic_scroll_down_normal(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        result = await scroll(page, direction="down", amount="normal")
        assert result["direction"] == "down"
        assert result["amount_px"] == 700
        assert result["repeat"] == 1
        assert result["total_px"] == 700
        assert result["method"] == "js"
        assert result["source"] == "browser_control"
        page.evaluate.assert_called()

    @pytest.mark.asyncio
    async def test_scroll_up_reverses_sign(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        result = await scroll(page, direction="up", amount=500)
        assert result["direction"] == "up"
        assert result["amount_px"] == 500

    @pytest.mark.asyncio
    async def test_repeat_multiplies_total(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        result = await scroll(page, direction="down", amount=300, repeat=3)
        assert result["repeat"] == 3
        assert result["total_px"] == 900
        assert result["amount_px"] == 300

    @pytest.mark.asyncio
    async def test_repeat_clamped_to_max_ten(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        result = await scroll(page, direction="down", amount=100, repeat=20)
        assert result["repeat"] == 10
        assert result["total_px"] == 1000

    @pytest.mark.asyncio
    async def test_behavior_mapping(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        result = await scroll(page, direction="down", amount=500, behavior="instant")
        assert result["behavior"] == "auto"

    @pytest.mark.asyncio
    async def test_custom_source(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        result = await scroll(page, direction="down", amount=500, source="test_suite")
        assert result["source"] == "test_suite"

    @pytest.mark.asyncio
    async def test_page_evaluate_called_with_js(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        await scroll(page, direction="down", amount=700)
        call_args = page.evaluate.call_args
        assert call_args is not None
        script = call_args[0][0]
        assert "window.scrollBy" in script
        assert "top: 700" in script

    @pytest.mark.asyncio
    async def test_page_evaluate_for_left(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="900")
        await scroll(page, direction="left", amount=400)
        call_args = page.evaluate.call_args
        assert call_args is not None
        script = call_args[0][0]
        assert "left: -400" in script


# ═════════════════════════════════════════════════════════════════════════════
# pyautogui fallback strength
# ═════════════════════════════════════════════════════════════════════════════


class TestPyautoguiFallback:
    """Validate that _scroll converts presets to strong click counts."""

    def test_small_at_least_7_clicks(self):
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll") as mock_scroll:
                _scroll(direction="down", amount="small")
                args, _ = mock_scroll.call_args
                assert abs(args[0]) >= 7

    def test_normal_at_least_17_clicks(self):
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll") as mock_scroll:
                _scroll(direction="down", amount="normal")
                args, _ = mock_scroll.call_args
                assert abs(args[0]) >= 17

    def test_large_at_least_30_clicks(self):
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll") as mock_scroll:
                _scroll(direction="down", amount="large")
                args, _ = mock_scroll.call_args
                assert abs(args[0]) >= 30

    def test_up_returns_negative_clicks(self):
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll") as mock_scroll:
                _scroll(direction="up", amount="small")
                args, _ = mock_scroll.call_args
                assert args[0] < 0

    def test_scroll_down_default_return_contains_px(self):
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll"):
                result = _scroll(direction="down")
                assert "700px" in result
                assert "Scrolled" in result

    def test_scroll_up_default_return_contains_px(self):
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll"):
                result = _scroll(direction="up")
                assert "700px" in result

    def test_no_real_pyautogui_movement(self):
        """Ensure tests never call real pyautogui.scroll."""
        from actions.computer_control import _scroll

        with patch("actions.computer_control._require_pyautogui"):
            with patch("pyautogui.scroll") as mock_scroll:
                _scroll(direction="down", amount=500)
                mock_scroll.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# Smart scroll JS strategy (Part D) — tests for the JS generator
# ═════════════════════════════════════════════════════════════════════════════


class TestSmartScrollJS:
    """Part D: validate the smart scroll JS strategy contains all fallback steps."""

    def test_smart_scroll_js_has_largest_scrollable_container(self):
        # The smart JS is embedded in computer_control; verify it has the key strategies
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert "largestScrollable" in src, "Smart JS must try largest scrollable container"
        assert "activeElement" in src, "Smart JS must try active element"
        assert "scrollingElement" in src, "Smart JS must try document.scrollingElement"
        assert "window.scrollBy" in src, "Smart JS must fall back to window.scrollBy"

    def test_smart_scroll_reports_changed(self):
        # Verify the JS script has before/after change detection
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert "result.changed" in src, "Smart JS must report changed status"

    def test_smart_scroll_reports_target(self):
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert "result.target" in src, "Smart JS must report which element scrolled"

    def test_smart_scroll_fallback_pyautogui(self):
        """If JS unchanged, pyautogui fallback must be reachable."""
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert "pyautogui fallback" in src or "pyautogui scroll" in src

    def test_smart_scroll_js_window_method(self):
        """Smart JS maps 'window' target to 'js-window' method."""
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert 'method_type = "js-window" if target == "window" else "js-container"' in src

    def test_smart_scroll_js_container_method(self):
        """Smart JS maps 'largestScrollable' target to 'js-container' method."""
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert '"js-container"' in src

    def test_smart_scroll_pyautogui_method_in_return(self):
        """Pyautogui fallback return includes method=pyautogui."""
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert "(method=pyautogui)" in src


class TestScrollAmountNormalization:
    """Part D: generic amount normalization logic (matches self_healing_router Issue 6)."""

    def test_generic_amount_10_normalizes_to_700(self):
        from unittest.mock import patch

        from core.self_healing_router import route_tool_call

        with patch("core.self_healing_router.load_state", return_value={}):
            with patch("core.self_healing_router.get_active_title", return_value=""):
                tool, params = route_tool_call(
                    "unknown_tool", {"action": "scroll", "amount": 10}, "scroll down"
                )
                assert tool == "computer_control"
                assert "amount" not in params or params.get("amount") != 10

    def test_explicit_10_px_preserved(self):
        from unittest.mock import patch

        from core.self_healing_router import route_tool_call

        with patch("core.self_healing_router.load_state", return_value={}):
            with patch("core.self_healing_router.get_active_title", return_value=""):
                tool, params = route_tool_call(
                    "computer_control",
                    {"action": "scroll", "amount": 10, "direction": "down"},
                    "scroll down 10 px",
                )
                assert tool == "computer_control"
                assert params.get("amount") == 10


# ═════════════════════════════════════════════════════════════════════════════
# Scroll target platform integration (14B.2)
# ═════════════════════════════════════════════════════════════════════════════


class TestScrollTargetPlatform:
    """Validate that computer_control scroll handler accepts a 'target' param
    and attempts platform_tab acquisition when target is youtube/netflix."""

    def test_scroll_handler_reads_target_param(self):
        """The scroll handler in computer_control must read params['target']."""
        import inspect

        from actions.computer_control import computer_control

        src = inspect.getsource(computer_control)
        assert '.get("target", "")' in src or 'params.get("target"' in src

    def test_scroll_with_youtube_target_falls_back_pyautogui(self):
        """When platform_tab acquisition fails (no browser session), pyautogui fallback runs."""
        from actions.computer_control import computer_control

        with patch(
            "actions.computer_control._scroll", return_value="Scrolled down (method=pyautogui)"
        ):
            result = computer_control(
                {"action": "scroll", "direction": "down", "target": "youtube"}
            )
            assert "pyautogui" in result or "Scrolled" in result

    def test_scroll_with_netflix_target_falls_back_pyautogui(self):
        """Netflix target also falls back gracefully."""
        from actions.computer_control import computer_control

        with patch(
            "actions.computer_control._scroll", return_value="Scrolled down (method=pyautogui)"
        ):
            result = computer_control(
                {"action": "scroll", "direction": "down", "target": "netflix"}
            )
            assert "pyautogui" in result or "Scrolled" in result

    def test_scroll_without_target_still_works(self):
        """Scroll without target param still falls back to pyautogui."""
        from actions.computer_control import computer_control

        with patch(
            "actions.computer_control._scroll", return_value="Scrolled down (method=pyautogui)"
        ):
            result = computer_control({"action": "scroll", "direction": "down"})
            assert "pyautogui" in result or "Scrolled" in result


# ═════════════════════════════════════════════════════════════════════════════
# edge cases
# ═════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_preset_values_map_correctly(self):
        assert _PRESET_MAP["small"] == 300
        assert _PRESET_MAP["normal"] == 700
        assert _PRESET_MAP["large"] == 1200

    def test_page_fallback_is_900(self):
        assert _PAGE_FALLBACK_PX == 900

    def test_all_aliases_resolve_correctly(self):
        for alias in ("sm", "small"):
            assert resolve_amount(alias) == 300
        for alias in ("md", "med", "normal"):
            assert resolve_amount(alias) == 700
        for alias in ("lg", "large"):
            assert resolve_amount(alias) == 1200

    def test_down_direction_positive_y(self):
        x, y = resolve_direction("down")
        assert y > 0

    def test_up_direction_negative_y(self):
        x, y = resolve_direction("up")
        assert y < 0

    def test_left_negative_x(self):
        x, y = resolve_direction("left")
        assert x < 0

    def test_right_positive_x(self):
        x, y = resolve_direction("right")
        assert x > 0
