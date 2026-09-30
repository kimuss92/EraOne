"""
Tests for Netflix Browser Hard-Lock + Vision Suppression (Step 14B.2.10).

Validates:
1. browser_control Netflix search URL reroutes to netflix_control search
2. browser_control Netflix title/watch URLs do not execute browser_control
3. browser_control legacy netflix_search_and_play cannot execute
4. no live path can produce 'Could not resolve a visible thumbnail link locator'
5. open_app Netflix routes to netflix_control browse/home
6. screen_process 'What is on the Netflix search page?' suppressed in Netflix context
7. screen_process 'What titles are visible on Netflix?' suppressed in Netflix context
8. browser_control Netflix failure does not trigger screen_process
9. 503/429/RESOURCE_EXHAUSTED sets cooldown
10. cooldown suppresses Netflix-related screen_process
11. normal non-Netflix screen_process still works
12. 'search One Piece on Netflix' routes only to netflix_control search

No real browser calls. No real screen_process calls.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.self_healing_router import (
    _is_netflix_screen_process_prompt,
    record_tool_result,
    route_tool_call,
)


class TestBrowserControlNetflixReroute:
    """Tests 1-3: browser_control Netflix URLs reroute to netflix_control."""

    def test_browser_control_netflix_search_url_reroutes(self):
        """browser_control go_to netflix.com/search?q=Berlin -> netflix_control search."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/search?q=Berlin"},
            "Play Berlin on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "berlin"

    def test_browser_control_netflix_title_url_reroutes(self):
        """browser_control go_to netflix.com/title/XXXXX -> netflix_control play."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/title/81476183"},
            "Play a title on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "play"

    def test_browser_control_netflix_watch_url_reroutes(self):
        """browser_control go_to netflix.com/watch/XXXXX -> netflix_control status."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/watch/81476183"},
            "Watch Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_browser_control_legacy_netflix_search_and_play_cannot_execute(self):
        """browser_control dispatcher no longer has netflix_search_and_play case."""
        source = Path(__file__).resolve().parent.parent / "actions" / "browser_control.py"
        text = source.read_text("utf-8")
        # The dispatcher inside browser_control() must not reference netflix_search_and_play
        after_bc_func = text.split("def browser_control(")[1]
        assert "case 'netflix_search_and_play'" not in after_bc_func
        assert "'netflix_search_and_play'" not in after_bc_func

    def test_no_live_path_produces_thumbnail_locator_error(self):
        """No reachable code path can produce 'visible thumbnail link locator'."""
        source = Path(__file__).resolve().parent.parent / "actions" / "browser_control.py"
        text = source.read_text("utf-8")
        # The string exists in the unreachable netflix_search_and_play method,
        # but must not appear in the live go_to() or browser_control() code paths.
        go_to_func = text.split("def go_to(")[1].split("async def search(")[0]
        assert "visible thumbnail link locator" not in go_to_func
        assert "Could not resolve a visible thumbnail link locator" not in go_to_func

    def test_browser_control_netflix_home_reroutes_to_browse(self):
        """browser_control go_to plain netflix.com -> netflix_control browse."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com"},
            "Open Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "browse"

    def test_browser_control_click_type_on_netflix_reroutes(self):
        """browser_control click/type on Netflix -> netflix_control search."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "description": "Netflix search bar"},
            "click on Netflix search",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert "netflix" in params.get("query", "").lower()


class TestOpenAppNetflix:
    """Test 5: open_app Netflix routes correctly."""

    def test_open_app_netflix_routes_to_netflix_control_browse(self):
        """open_app Netflix (plain, no media query) -> netflix_control browse."""
        tool, params = route_tool_call(
            "open_app",
            {"app_name": "netflix"},
            "Open Netflix app",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "browse"

    def test_open_app_netflix_with_search_query_routes_to_search(self):
        """open_app Netflix with 'Search One Piece' -> netflix_control search."""
        tool, params = route_tool_call(
            "open_app",
            {"app_name": "netflix"},
            "Search One Piece on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_open_app_netflix_with_play_intent_routes_to_search(self):
        """open_app Netflix with 'Play One Piece' -> netflix_control search."""
        tool, params = route_tool_call(
            "open_app",
            {"app_name": "netflix"},
            "Play One Piece on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"

    def test_open_app_netflix_with_media_play_no_query_routes_to_netflix_control(self):
        """open_app Netflix with 'Play Netflix' -> netflix_control."""
        tool, params = route_tool_call(
            "open_app",
            {"app_name": "netflix"},
            "Play Netflix",
        )
        assert tool != "browser_control"
        assert tool == "netflix_control"


class TestScreenProcessSuppression:
    """Tests 6-7, 10-11: screen_process prompt suppression."""

    def test_screen_process_what_is_on_netflix_suppressed(self):
        """'What is on the Netflix search page?' detected as Netflix prompt."""
        assert _is_netflix_screen_process_prompt("What is on the Netflix search page?")

    def test_screen_process_what_titles_visible_suppressed(self):
        """'What titles are visible on Netflix?' detected as Netflix prompt."""
        assert _is_netflix_screen_process_prompt("What titles are visible on Netflix?")

    def test_screen_process_analyze_netflix_grid_suppressed(self):
        """'Analyze the Netflix grid layout' detected as Netflix prompt."""
        assert _is_netflix_screen_process_prompt("Analyze the Netflix grid layout")

    def test_screen_process_netflix_titles_suppressed(self):
        """'What Netflix titles are on the screen' detected as Netflix prompt."""
        assert _is_netflix_screen_process_prompt("What Netflix titles are on the screen")

    def test_screen_process_non_netflix_not_suppressed(self):
        """Non-Netflix screen_process prompts are NOT detected."""
        assert not _is_netflix_screen_process_prompt("What is on the YouTube search page?")
        assert not _is_netflix_screen_process_prompt("Describe this image")
        assert not _is_netflix_screen_process_prompt("What tabs are open?")
        assert not _is_netflix_screen_process_prompt("")

    def test_vision_cooldown_suppresses_netflix_screen_process(self):
        """When cooldown is active, _handle_status returns controlled message."""
        from actions import netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = time.monotonic() + 60.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        nc._LAST_NETFLIX_FAILURE_TOOL = ""
        try:
            result = nc._handle_status({"state": "search_results", "signals": {}})
            assert "vision is temporarily unavailable" in result.lower()
            assert "search again" in result.lower()
        finally:
            nc._VISION_COOLDOWN_UNTIL = 0.0

    def test_failure_context_suppresses_netflix_status(self):
        """When failure context is active, _handle_status returns controlled message."""
        from actions import netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = time.monotonic()
        nc._LAST_NETFLIX_FAILURE_TOOL = "netflix_control.search"
        try:
            result = nc._handle_status({"state": "search_results", "signals": {}})
            assert "netflix automation failure" in result.lower()
            assert "search again" in result.lower()
        finally:
            nc._LAST_NETFLIX_FAILURE_AT = 0.0
            nc._LAST_NETFLIX_FAILURE_TOOL = ""


class TestFailureContextAndCooldown:
    """Tests 8-9: failure context and cooldown detection."""

    def test_browser_control_netflix_failure_sets_context(self):
        """record_tool_result with Netflix failure -> failure context active."""
        from actions import netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        record_tool_result(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/search?q=berlin"},
            result="Could not resolve a visible thumbnail link locator for 'berlin'",
        )
        assert nc.is_netflix_failure_context_active()
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        nc._LAST_NETFLIX_FAILURE_TOOL = ""

    def test_netflix_control_failure_sets_context(self):
        """record_tool_result with netflix_control failure -> failure context active."""
        from actions import netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        record_tool_result(
            "netflix_control",
            {"action": "search"},
            result="Netflix search had an internal error before selecting a safe title.",
        )
        assert nc.is_netflix_failure_context_active()
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        nc._LAST_NETFLIX_FAILURE_TOOL = ""

    def test_vision_quota_sets_cooldown(self):
        """record_tool_result with 503/429 -> vision cooldown active when Netflix context."""
        from actions import netflix_control as nc
        from core import browser_grid_state as bgs

        nc._VISION_COOLDOWN_UNTIL = 0.0
        bgs.set_scroll_context("netflix")
        try:
            record_tool_result(
                "screen_process",
                {"action": "analyze"},
                result="503 Service Unavailable: high demand, retry delay",
            )
            assert nc.is_netflix_vision_blocked()
        finally:
            nc._VISION_COOLDOWN_UNTIL = 0.0
            bgs.clear_scroll_context_for_tests()

    def test_resource_exhausted_sets_cooldown(self):
        """record_tool_result with RESOURCE_EXHAUSTED -> vision cooldown active when Netflix context."""
        from actions import netflix_control as nc
        from core import browser_grid_state as bgs

        nc._VISION_COOLDOWN_UNTIL = 0.0
        bgs.set_scroll_context("netflix")
        try:
            record_tool_result(
                "screen_process",
                {"action": "analyze"},
                result="RESOURCE_EXHAUSTED: quota exceeded for Vision API",
            )
            assert nc.is_netflix_vision_blocked()
        finally:
            nc._VISION_COOLDOWN_UNTIL = 0.0
            bgs.clear_scroll_context_for_tests()

    def test_generic_screen_process_503_no_cooldown_without_netflix_context(self):
        """Generic screen_process 503 must NOT set Netflix cooldown when no Netflix context."""
        from actions import netflix_control as nc
        from core import browser_grid_state as bgs
        from core.intent_memory import save_state

        nc._VISION_COOLDOWN_UNTIL = 0.0
        bgs.clear_scroll_context_for_tests()
        save_state({})
        try:
            record_tool_result(
                "screen_process",
                {"action": "analyze"},
                result="503 Service Unavailable: high demand, retry delay",
            )
            assert not nc.is_netflix_vision_blocked()
        finally:
            nc._VISION_COOLDOWN_UNTIL = 0.0

    def test_failure_context_ttl_expires(self):
        """Failure context expires after TTL window."""
        from actions import netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = time.monotonic() - 121.0
        nc._LAST_NETFLIX_FAILURE_TOOL = "old_failure"
        assert not nc.is_netflix_failure_context_active()
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        nc._LAST_NETFLIX_FAILURE_TOOL = ""

    def test_normal_non_netflix_result_does_not_set_failure(self):
        """record_tool_result with normal result -> no failure context."""
        from actions import netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        record_tool_result(
            "browser_control",
            {"action": "go_to", "url": "https://www.example.com"},
            result="Navigated to example.com successfully",
        )
        assert not nc.is_netflix_failure_context_active()
        assert nc._LAST_NETFLIX_FAILURE_AT == 0.0

    def test_youtube_failure_does_not_set_netflix_context(self):
        """record_tool_result with YouTube failure -> no Netflix failure context."""
        from actions import netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        record_tool_result(
            "youtube_video",
            {"action": "play"},
            result="Could not resolve video ID",
        )
        assert not nc.is_netflix_failure_context_active()


class TestSearchOnePieceOnNetflix:
    """Test 12: 'search One Piece on Netflix' routes purely."""

    def test_search_one_piece_routes_to_netflix_control_search(self):
        """'search One Piece on Netflix' -> netflix_control search with query."""
        tool, params = route_tool_call(
            "netflix_control",
            {"action": "search"},
            "Search One Piece on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_search_one_piece_does_not_route_to_browser_control(self):
        """'search One Piece on Netflix' never routes to browser_control."""
        tool, params = route_tool_call(
            "unknown_tool",
            {"action": "search"},
            "Search One Piece on Netflix",
        )
        assert tool != "browser_control"

    def test_search_one_piece_does_not_trigger_screen_process(self):
        """'search One Piece on Netflix' does not produce screen_process route."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/search?q=one+piece"},
            "Search One Piece on Netflix",
        )
        assert tool == "netflix_control"
        assert tool != "screen_process"


class TestSourceCodeGuardrails:
    """Verify source-level guardrails are in place."""

    def test_browser_control_safety_belt_present(self):
        """browser_control() has Netflix safety belt returning controlled message."""
        source = Path(__file__).resolve().parent.parent / "actions" / "browser_control.py"
        text = source.read_text("utf-8")
        assert "Netflix browsing/search/card actions are handled by netflix_control only" in text

    def test_browser_control_go_to_netflix_blocked(self):
        """browser_control go_to no longer calls netflix_search_and_play."""
        source = Path(__file__).resolve().parent.parent / "actions" / "browser_control.py"
        text = source.read_text("utf-8")
        # The legacy comment mentions netflix_search_and_play but it must not be called.
        go_to_body = text.split("async def go_to(")[1].split("async def search(")[0]
        assert "return await self.netflix_search_and_play" not in go_to_body

    def test_router_has_netflix_failure_context(self):
        """Router's record_tool_result contains _set_netflix_failure call."""
        source = Path(__file__).resolve().parent.parent / "core" / "self_healing_router.py"
        text = source.read_text("utf-8")
        assert "_set_netflix_failure" in text.split("def record_tool_result")[1].split("def ")[0]

    def test_router_has_vision_cooldown_call(self):
        """Router's record_tool_result contains _set_netflix_vision_cooldown call."""
        source = Path(__file__).resolve().parent.parent / "core" / "self_healing_router.py"
        text = source.read_text("utf-8")
        assert (
            "_set_netflix_vision_cooldown"
            in text.split("def record_tool_result")[1].split("def ")[0]
        )
