"""
Tests for browser/media routing logic (core/self_healing_router.py).

Validates:

1. Media query extraction from Gemini params and user_text
2. Scroll intent detection and routing to computer_control
3. Minimal browser intent routing (open/focus, list tabs, close tab)
4. No regression: all routing paths avoid "Finding action failed"

No real browser calls. No real YouTube/Netflix/network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.self_healing_router import (
    _detect_scroll_intent,
    _extract_media_query,
    _infer_scroll_target,
    _route_browser_intent,
    _route_ordinal_command,
    route_tool_call,
)
from utils.ordinal_selection import parse_ordinal_command

# ═════════════════════════════════════════════════════════════════════════════
# _extract_media_query — pure function tests
# ═════════════════════════════════════════════════════════════════════════════


class TestExtractMediaQuery:
    def test_play_one_piece_on_netflix(self):
        """Extract 'One Piece' from user_text 'Play One Piece on Netflix'."""
        result = _extract_media_query(
            {"action": "play"}, "Play One Piece on Netflix", "netflix", "play"
        )
        assert result == "One Piece"

    def test_search_one_piece_on_netflix(self):
        """Extract 'One Piece' from user_text 'Search One Piece on Netflix'."""
        result = _extract_media_query(
            {"action": "search"}, "Search One Piece on Netflix", "netflix", "search"
        )
        assert result == "One Piece"

    def test_find_breaking_bad_on_netflix(self):
        """Extract 'Breaking Bad' from user_text 'Find Breaking Bad on Netflix'."""
        result = _extract_media_query(
            {"action": "search"}, "Find Breaking Bad on Netflix", "netflix", "search"
        )
        assert result == "Breaking Bad"

    def test_params_query_takes_priority(self):
        """Gemini-provided query in params takes priority over user_text."""
        result = _extract_media_query(
            {"action": "play", "query": "Better Call Saul"},
            "Play One Piece on Netflix",
            "netflix",
            "play",
        )
        assert result == "Better Call Saul"

    def test_params_title_fallback(self):
        """Extract from params 'title' key when 'query' is absent."""
        result = _extract_media_query(
            {"action": "play", "title": "Stranger Things"},
            "Play something",
            "netflix",
            "play",
        )
        assert result == "Stranger Things"

    def test_params_value_fallback(self):
        """Extract from params 'value' key (used by netflix_control search)."""
        result = _extract_media_query(
            {"action": "search", "value": "The Crown"},
            "Search on Netflix",
            "netflix",
            "search",
        )
        assert result == "The Crown"

    def test_play_netflix_no_query(self):
        """'Play Netflix' does not produce a query (no title in text)."""
        result = _extract_media_query({"action": "play"}, "Play Netflix", "netflix", "play")
        assert result is None

    def test_open_netflix_no_query(self):
        """'Open Netflix' does not produce a query."""
        result = _extract_media_query({"action": "open"}, "Open Netflix", "netflix", "open")
        assert result is None

    def test_play_despacito_on_youtube(self):
        """Extract 'Despacito' from user_text 'Play Despacito on YouTube'."""
        result = _extract_media_query(
            {"action": "play"}, "Play Despacito on YouTube", "youtube", "play"
        )
        assert result == "Despacito"

    def test_play_youtube_no_query(self):
        """'Play YouTube' does not produce a query."""
        result = _extract_media_query({"action": "play"}, "Play YouTube", "youtube", "play")
        assert result is None

    def test_search_standalone_without_platform(self):
        """'Search One Piece' without 'on <platform>' returns query for search action."""
        result = _extract_media_query({"action": "search"}, "Search One Piece", None, "search")
        assert result == "One Piece"

    def test_find_standalone_without_platform(self):
        """'Find Breaking Bad' without 'on <platform>' returns query for find action."""
        result = _extract_media_query({"action": "find"}, "Find Breaking Bad", None, "find")
        assert result == "Breaking Bad"

    def test_empty_params_no_text_returns_none(self):
        """Empty input returns None."""
        result = _extract_media_query({}, "", None, "")
        assert result is None

    def test_none_params_returns_none(self):
        """None params returns None."""
        result = _extract_media_query(None, "Play Netflix", "netflix", "play")
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# French Netflix query extraction (Step 14B.2.15, Fix E)
# ═════════════════════════════════════════════════════════════════════════════


class TestFrenchNetflixQueryExtraction:
    """Verify French phrases correctly extract the show/movie title for Netflix."""

    def test_serie_champis_sur_netflix(self):
        """'Série Champis sur Netflix' → query='Champis' (strips 'série')."""
        result = _extract_media_query(
            {"action": "play"}, "Série Champis sur Netflix", "netflix", "play"
        )
        assert result == "Champis"

    def test_serie_champis_sur_netflix_lowercase(self):
        """'Serie Champis sur Netflix' → query='Champis' (lowercase 'serie')."""
        result = _extract_media_query(
            {"action": "play"}, "Serie Champis sur Netflix", "netflix", "play"
        )
        assert result == "Champis"

    def test_film_berlin_sur_netflix(self):
        """'Film Berlin sur Netflix' → query='Berlin' (strips 'film')."""
        result = _extract_media_query(
            {"action": "play"}, "Film Berlin sur Netflix", "netflix", "play"
        )
        assert result == "Berlin"

    def test_regarde_one_piece_sur_netflix(self):
        """'Regarde One Piece sur Netflix' → query='One Piece' (French verb)."""
        result = _extract_media_query(
            {"action": "play"}, "Regarde One Piece sur Netflix", "netflix", "play"
        )
        assert result == "One Piece"

    def test_cherche_one_piece_sur_netflix(self):
        """'Cherche One Piece sur Netflix' → query='One Piece' (French verb)."""
        result = _extract_media_query(
            {"action": "play"}, "Cherche One Piece sur Netflix", "netflix", "play"
        )
        assert result == "One Piece"

    def test_regarde_berlin_sur_netflix_lowercase(self):
        """'regarde Berlin sur Netflix' → query='Berlin' (lowercase verb)."""
        result = _extract_media_query(
            {"action": "play"}, "regarde Berlin sur Netflix", "netflix", "play"
        )
        assert result == "Berlin"

    def test_one_piece_on_netflix_still_works(self):
        """'One Piece on Netflix' still routes correctly alongside French patterns."""
        result = _extract_media_query({"action": "play"}, "One Piece on Netflix", "netflix", "play")
        assert result == "One Piece"

    def test_play_one_piece_on_netflix_still_works(self):
        """'Play One Piece on Netflix' still routes correctly."""
        result = _extract_media_query(
            {"action": "play"}, "Play One Piece on Netflix", "netflix", "play"
        )
        assert result == "One Piece"

    def test_serie_not_used_as_query(self):
        """'série' alone should not become the query when it's a prefix."""
        result = _extract_media_query(
            {"action": "play"}, "Série Champis sur Netflix", "netflix", "play"
        )
        assert result != "Série"
        assert result != "série"

    def test_film_not_used_as_query(self):
        """'film' alone should not become the query when it's a prefix."""
        result = _extract_media_query(
            {"action": "play"}, "Film Berlin sur Netflix", "netflix", "play"
        )
        assert result != "Film"
        assert result != "film"


# ═════════════════════════════════════════════════════════════════════════════
# _detect_scroll_intent — pure function tests
# ═════════════════════════════════════════════════════════════════════════════


class TestDetectScrollIntent:
    def test_params_action_scroll_down(self):
        is_scroll, direction = _detect_scroll_intent("", {"action": "scroll", "direction": "down"})
        assert is_scroll
        assert direction == "down"

    def test_params_action_scroll_up(self):
        is_scroll, direction = _detect_scroll_intent("", {"action": "scroll", "direction": "up"})
        assert is_scroll
        assert direction == "up"

    def test_params_action_scroll_no_direction_defaults_down(self):
        is_scroll, direction = _detect_scroll_intent("", {"action": "scroll"})
        assert is_scroll
        assert direction == "down"

    def test_user_text_scroll_down(self):
        is_scroll, direction = _detect_scroll_intent("scroll down", {"action": "play"})
        assert is_scroll
        assert direction == "down"

    def test_user_text_scroll_up(self):
        is_scroll, direction = _detect_scroll_intent("scroll up", {"action": "play"})
        assert is_scroll
        assert direction == "up"

    def test_user_text_scroll_left(self):
        is_scroll, direction = _detect_scroll_intent("scroll left", {"action": "play"})
        assert is_scroll
        assert direction == "left"

    def test_user_text_scroll_right(self):
        is_scroll, direction = _detect_scroll_intent("scroll right", {"action": "play"})
        assert is_scroll
        assert direction == "right"

    def test_user_text_precise_matching(self):
        """'movies update' should not match scroll direction 'up'."""
        is_scroll, direction = _detect_scroll_intent("movies update today", {})
        assert not is_scroll
        assert direction == ""

    def test_no_scroll_intent(self):
        is_scroll, direction = _detect_scroll_intent("play netflix", {})
        assert not is_scroll
        assert direction == ""

    def test_scroll_down_exact_word_boundary(self):
        """'scroll down' matches, but 'scrolldown' does not."""
        is_scroll, direction = _detect_scroll_intent("scrolldown", {})
        assert not is_scroll
        assert direction == ""


# ═════════════════════════════════════════════════════════════════════════════
# _route_browser_intent — pure function tests
# ═════════════════════════════════════════════════════════════════════════════


class TestRouteBrowserIntent:
    def test_open_youtube(self):
        result = _route_browser_intent("open YouTube", {"action": "open"})
        assert result is not None
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "youtube.com" in params.get("url", "")

    def test_open_netflix(self):
        result = _route_browser_intent("open Netflix", {"action": "open"})
        assert result is not None
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "netflix.com" in params.get("url", "")

    def test_focus_netflix(self):
        result = _route_browser_intent("focus Netflix", {"action": "focus"})
        assert result is not None
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "netflix.com" in params.get("url", "")

    def test_list_tabs(self):
        result = _route_browser_intent("list tabs", {"action": "list_tabs"})
        assert result is not None
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "list_tabs"

    def test_close_youtube_tab(self):
        result = _route_browser_intent("close YouTube tab", {"action": "close_tab"})
        assert result is not None
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "close_tab"
        assert "YouTube" in params.get("url", "")

    def test_close_youtube_in_brave_closes_tab_not_browser(self):
        """Regression: 'close youtube in brave' must close the YouTube tab,
        NOT fall through (which previously caused the whole Brave browser to be killed)."""
        result = _route_browser_intent("close youtube in brave", {"action": "close_tab"})
        assert result is not None, "Must route to close_tab, not fall through"
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "close_tab"
        url = params.get("url", "").lower()
        assert "youtube" in url, f"Fragment should contain youtube, got {url!r}"
        # Browser name must be stripped so _find_tab_index doesn't get confused
        assert "brave" not in url, f"Browser name must be stripped from fragment, got {url!r}"

    def test_close_netflix_on_brave_closes_tab(self):
        result = _route_browser_intent("close netflix on brave", {"action": "close_tab"})
        assert result is not None
        tool, params = result
        assert tool == "browser_control"
        assert params["action"] == "close_tab"
        assert "netflix" in params.get("url", "").lower()
        assert "brave" not in params.get("url", "").lower()

    def test_close_brave_alone_still_falls_through(self):
        """Sanity: 'close brave' with no site name still falls through to let
        the LLM decide (it may kill the whole browser). We don't auto-interpret
        a bare browser name as a tab close."""
        result = _route_browser_intent("close brave", {"action": "close_tab"})
        assert result is None, "Bare browser close must fall through to LLM"

    def test_unknown_command_returns_none(self):
        result = _route_browser_intent("play despacito", {"action": "play"})
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# route_tool_call — integration tests with mocks
# ═════════════════════════════════════════════════════════════════════════════


class TestRouteToolCall:
    """Integration tests for route_tool_call with mocked state.

    Mocks:
        - load_state returns empty dict (no last browser)
        - get_active_title returns empty string
    """

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield

    # ── Media query preservation ──

    def test_play_one_piece_routes_to_netflix_with_query(self):
        """'Play One Piece on Netflix' routes to netflix_control play with query preserved."""
        tool, params = route_tool_call(
            "netflix_control", {"action": "play"}, "Play One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params["action"] == "play"
        assert params["query"] == "One Piece"
        assert params.get("_auto_play") is True

    def test_netflix_play_preserves_existing_query(self):
        """netflix_control play with existing query in params preserves it."""
        tool, params = route_tool_call(
            "netflix_control",
            {"action": "play", "query": "Better Call Saul"},
            "Play Better Call Saul on Netflix",
        )
        assert tool == "netflix_control"
        assert params["action"] == "play"
        assert params["query"] == "Better Call Saul"
        assert params.get("_auto_play") is True

    def test_netflix_play_no_query(self):
        """'Play Netflix' remains plain play with no invented query."""
        tool, params = route_tool_call("netflix_control", {"action": "play"}, "Play Netflix")
        assert tool == "media_coordinator"
        assert params["target"] == "netflix"
        assert params["action"] == "play"
        assert "query" not in params

    def test_search_one_piece_on_netflix(self):
        """'Search One Piece on Netflix' routes with search action and query."""
        tool, params = route_tool_call(
            "netflix_control", {"action": "search"}, "Search One Piece on Netflix"
        )
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_play_despacito_on_youtube(self):
        """'Play Despacito on YouTube' stays on youtube_video with query (bypasses media_coordinator)."""
        tool, params = route_tool_call(
            "youtube_video", {"action": "play"}, "Play Despacito on YouTube"
        )
        assert tool == "youtube_video"
        assert params["action"] == "play"
        assert params["query"] == "Despacito"

    def test_search_despacito_on_youtube(self):
        """'Search Despacito on YouTube' routes with search action and query."""
        tool, params = route_tool_call(
            "youtube_video", {"action": "search"}, "Search Despacito on YouTube"
        )
        assert params.get("action") == "search"
        assert params.get("query") == "Despacito"

    def test_play_youtube_no_query(self):
        """'Play YouTube' remains plain play with no invented query."""
        tool, params = route_tool_call("youtube_video", {"action": "play"}, "Play YouTube")
        assert tool == "media_coordinator"
        assert params["target"] == "youtube"
        assert params["action"] == "play"
        assert "query" not in params

    # ── Scroll routing ──

    def test_scroll_down_routes_to_computer_control(self):
        """'scroll down' routes to computer_control with direction down."""
        tool, params = route_tool_call("netflix_control", {"action": "play"}, "scroll down")
        assert tool == "computer_control"
        assert params["action"] == "scroll"
        assert params["direction"] == "down"

    def test_scroll_up_routes_to_computer_control(self):
        """'scroll up' routes to computer_control with direction up."""
        tool, params = route_tool_call("youtube_video", {"action": "play"}, "scroll up")
        assert tool == "computer_control"
        assert params["action"] == "scroll"
        assert params["direction"] == "up"

    def test_scroll_params_only(self):
        """Params-only scroll with direction works without user_text."""
        tool, params = route_tool_call(
            "unknown_tool", {"action": "scroll", "direction": "left"}, ""
        )
        assert tool == "computer_control"
        assert params["action"] == "scroll"
        assert params["direction"] == "left"

    def test_scroll_from_user_text_when_params_empty(self):
        """Scroll detection uses user_text even when params have no scroll data."""
        tool, params = route_tool_call("unknown_tool", {"action": "play"}, "scroll right please")
        # user_text contains "scroll right" → should route to computer_control
        assert tool == "computer_control"
        assert params["action"] == "scroll"
        assert params["direction"] == "right"

    # ── Browser intent routing ──

    def test_open_youtube_routes_to_go_to(self):
        """'open YouTube' routes to browser_control go_to."""
        tool, params = route_tool_call("unknown_tool", {"action": "open"}, "open YouTube")
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "youtube.com" in params.get("url", "")

    def test_open_netflix_routes_to_go_to(self):
        """'open Netflix' routes to browser_control go_to."""
        tool, params = route_tool_call("unknown_tool", {"action": "open"}, "open Netflix")
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "netflix.com" in params.get("url", "")

    def test_focus_netflix_routes_to_go_to(self):
        """'focus Netflix' routes to browser_control go_to."""
        tool, params = route_tool_call("unknown_tool", {"action": "focus"}, "focus Netflix")
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "netflix.com" in params.get("url", "")

    def test_list_tabs_routes_to_browser_control(self):
        """'list tabs' routes to browser_control list_tabs."""
        tool, params = route_tool_call("unknown_tool", {"action": "list_tabs"}, "list tabs")
        assert tool == "browser_control"
        assert params["action"] == "list_tabs"

    def test_close_tab_routes_to_browser_control(self):
        """'close YouTube tab' routes to browser_control close_tab."""
        tool, params = route_tool_call("unknown_tool", {"action": "close_tab"}, "close YouTube tab")
        assert tool == "browser_control"
        assert params["action"] == "close_tab"

    # ── Issue 1: user_text handling ──

    def test_netflix_play_without_user_text_does_not_invent_query(self):
        """When user_text is empty, no query is invented."""
        tool, params = route_tool_call("netflix_control", {"action": "play"}, "")
        assert tool == "media_coordinator"
        assert params["target"] == "netflix"
        assert params["action"] == "play"
        assert "query" not in params

    def test_netflix_play_user_text_logged_when_present(self):
        """When user_text is provided, query extraction routes to netflix_control play."""
        tool, params = route_tool_call(
            "netflix_control", {"action": "play"}, "Play One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("query") == "One Piece"
        assert params.get("action") == "play"

    # ── Issue 2: Explicit target lock ──

    def test_media_coordinator_explicit_youtube_target_preserved(self):
        """media_coordinator with target=youtube stays youtube, not overwritten."""
        tool, params = route_tool_call(
            "media_coordinator", {"target": "youtube", "action": "play"}, ""
        )
        assert tool == "media_coordinator"
        assert params["target"] == "youtube"
        assert params["action"] == "play"

    def test_media_coordinator_explicit_netflix_target_preserved(self):
        """media_coordinator with target=netflix stays netflix, not overwritten."""
        tool, params = route_tool_call(
            "media_coordinator", {"target": "netflix", "action": "play"}, ""
        )
        assert tool == "media_coordinator"
        assert params["target"] == "netflix"

    def test_media_coordinator_explicit_spotify_target_preserved(self):
        """media_coordinator with target=spotify stays spotify."""
        tool, params = route_tool_call(
            "media_coordinator", {"target": "spotify", "action": "play"}, ""
        )
        assert tool == "media_coordinator"
        assert params["target"] == "spotify"

    def test_media_coordinator_missing_target_falls_back_to_spotify(self):
        """media_coordinator without explicit target falls back to text detection."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "play music")
        assert tool == "media_coordinator"
        assert params["target"] == "spotify"

    # ── Issue 6: Scroll amount normalization ──

    def test_scroll_amount_10_normalized_to_700(self):
        """Generic scroll with amount=10 drops amount (defaults to normal 700px)."""
        tool, params = route_tool_call(
            "unknown_tool", {"action": "scroll", "amount": 10}, "scroll down"
        )
        assert tool == "computer_control"
        assert params["action"] == "scroll"
        assert params["direction"] == "down"
        assert "amount" not in params

    def test_scroll_amount_500_preserved_when_large(self):
        """Generic scroll with amount=500 (>=100) preserves the amount."""
        tool, params = route_tool_call(
            "unknown_tool", {"action": "scroll", "amount": 500}, "scroll down"
        )
        assert tool == "computer_control"
        assert params["action"] == "scroll"
        assert params.get("amount") == 500

    def test_scroll_amount_10_with_px_user_text_preserved(self):
        """Explicit '10 px' in user_text preserves small amount."""
        tool, params = route_tool_call(
            "computer_control",
            {"action": "scroll", "amount": 10, "direction": "down"},
            "scroll down 10 px",
        )
        assert tool == "computer_control"
        assert params.get("amount") == 10

    # ── Issue 14B.0.2 Part E: open_app → netflix re-routing ──

    def test_open_app_netflix_with_media_query_routes_to_netflix_control(self):
        """open_app netflix with 'play One Piece on Netflix' re-routes to netflix_control."""
        tool, params = route_tool_call(
            "open_app", {"app_name": "netflix"}, "Play One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_open_app_netflix_with_search_intent_no_query_routes_to_netflix_control(self):
        """open_app netflix with 'play Netflix' but no specific title re-routes to play."""
        tool, params = route_tool_call("open_app", {"app_name": "netflix"}, "Play Netflix")
        assert tool == "netflix_control"
        assert params.get("action") == "play"
        assert "query" not in params

    def test_open_app_netflix_without_media_intent_routes_to_netflix_control(self):
        """open_app netflix without play/search intent → netflix_control browse (hard-lock)."""
        tool, params = route_tool_call("open_app", {"app_name": "netflix"}, "Open Netflix app")
        assert tool == "netflix_control"
        assert params.get("action") == "browse"

    def test_open_app_youtube_with_media_query_routes_to_youtube_video(self):
        """open_app youtube with 'play despacito on YouTube' re-routes to youtube_video."""
        tool, params = route_tool_call(
            "open_app", {"app_name": "youtube"}, "Play despacito on YouTube"
        )
        assert tool == "youtube_video"
        assert params.get("action") == "search"

    def test_netflix_play_title_never_routes_to_open_app(self):
        """'Play One Piece on Netflix' routes to netflix_control play (not open_app)."""
        tool, params = route_tool_call(
            "netflix_control", {"action": "play"}, "Play One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("query") == "One Piece"
        assert params.get("action") == "play"
        assert tool != "open_app"

    # ── Fix 2: browser_control go_to re-routing ──

    def test_browser_control_go_to_netflix_search_routes_to_netflix_control(self):
        """browser_control go_to with netflix.com/search?q=Berlin routes to netflix_control."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/search?q=Berlin"},
            "Play Berlin on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "berlin"

    def test_browser_control_go_to_youtube_search_routes_to_youtube_video(self):
        """browser_control go_to with youtube.com/results routes to youtube_video."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.youtube.com/results?search_query=despacito"},
            "Play Despacito on YouTube",
        )
        assert tool == "youtube_video"
        assert params.get("action") == "play"
        assert "despacito" in params.get("query", "").lower()

    def test_browser_control_go_to_netflix_home_not_rerouted(self):
        """browser_control go_to with plain netflix.com (not search) routes to netflix_control browse."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com"},
            "Open Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "browse"

    def test_search_berlin_on_netflix_does_not_become_browser_control_go_to(self):
        """'Search Berlin on Netflix' does not route to browser_control go_to."""
        tool, params = route_tool_call(
            "netflix_control", {"action": "search"}, "Search Berlin on Netflix"
        )
        assert tool != "browser_control"
        assert params.get("query") == "Berlin"

    # ── No regression: "Finding action failed" ──

    def test_no_routing_path_produces_finding_action_failed(self):
        """All tested routing paths must not contain 'Finding action failed'."""
        test_cases = [
            ("netflix_control", {"action": "play"}, "Play One Piece on Netflix"),
            ("netflix_control", {"action": "play"}, "Play Netflix"),
            ("netflix_control", {"action": "search"}, "Search One Piece on Netflix"),
            ("youtube_video", {"action": "play"}, "Play Despacito on YouTube"),
            ("youtube_video", {"action": "play"}, "Play YouTube"),
            ("netflix_control", {"action": "play"}, "scroll down"),
            ("unknown_tool", {"action": "scroll", "direction": "down"}, ""),
            ("unknown_tool", {"action": "open"}, "open YouTube"),
            ("unknown_tool", {"action": "list_tabs"}, "list tabs"),
        ]
        for tool, params, user_text in test_cases:
            result_tool, result_params = route_tool_call(tool, params, user_text)
            result_str = str(result_tool) + " " + str(result_params)
            assert "finding action failed" not in result_str.lower(), (
                f"Route ({tool}, {params}, {user_text!r}) produced: {result_str}"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Fix 1: YouTube selected video verification
# ═════════════════════════════════════════════════════════════════════════════


class TestYoutubeVideoVerification:
    """Tests for _verify_youtube_navigation and _get_page_url (actions/youtube_video.py)."""

    def _mock_platform_tab_none(self):
        """Patch get_or_open_platform_tab to return None page (fallback to get_browser_session)."""
        return patch(
            "actions.platform_tab.get_or_open_platform_tab", return_value=MagicMock(page=None)
        )

    def test_verify_watch_url_returns_true(self):
        from actions.youtube_video import _verify_youtube_navigation

        with (
            self._mock_platform_tab_none(),
            patch("actions.youtube_video.get_browser_session") as mock_get,
        ):
            mock_sess = MagicMock()
            mock_sess.run.return_value = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            mock_get.return_value = mock_sess
            ok, url = _verify_youtube_navigation(
                "brave", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )
            assert ok is True
            assert "dQw4w9WgXcQ" in url

    def test_verify_chatgpt_url_returns_false(self):
        from actions.youtube_video import _verify_youtube_navigation

        with (
            self._mock_platform_tab_none(),
            patch("actions.youtube_video.get_browser_session") as mock_get,
        ):
            mock_sess = MagicMock()
            mock_sess.run.return_value = "https://chatgpt.com"
            mock_get.return_value = mock_sess
            ok, url = _verify_youtube_navigation("brave", "https://chatgpt.com")
            assert ok is False
            assert "chatgpt.com" in url

    def test_verify_youtu_be_returns_true(self):
        from actions.youtube_video import _verify_youtube_navigation

        with (
            self._mock_platform_tab_none(),
            patch("actions.youtube_video.get_browser_session") as mock_get,
        ):
            mock_sess = MagicMock()
            mock_sess.run.return_value = "https://youtu.be/dQw4w9WgXcQ"
            mock_get.return_value = mock_sess
            ok, url = _verify_youtube_navigation("brave", "https://youtu.be/dQw4w9WgXcQ")
            assert ok is True

    def test_verify_exception_returns_false_empty_url(self):
        from actions.youtube_video import _verify_youtube_navigation

        with (
            self._mock_platform_tab_none(),
            patch("actions.youtube_video.get_browser_session") as mock_get,
        ):
            mock_get.side_effect = Exception("Browser not available")
            ok, url = _verify_youtube_navigation("brave", "https://www.youtube.com/watch?v=test")
            assert ok is False
            assert url == ""

    def test_get_page_url_returns_url(self):
        from actions.youtube_video import _get_page_url

        with patch("actions.youtube_video.get_browser_session") as mock_get:
            mock_sess = MagicMock()
            mock_sess.run.return_value = "https://www.youtube.com/watch?v=test"
            mock_get.return_value = mock_sess
            url = _get_page_url("brave")
            assert "test" in url

    def test_get_page_url_exception_returns_empty(self):
        from actions.youtube_video import _get_page_url

        with patch("actions.youtube_video.get_browser_session") as mock_get:
            mock_get.side_effect = Exception("No session")
            url = _get_page_url("brave")
            assert url == ""


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2 — open_app hard-block for YT/NF
# ═════════════════════════════════════════════════════════════════════════════


class TestOpenAppHardBlock:
    """open_app netflix/youtube must NEVER reach open_app — hard-block to media tool or go_to."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield

    def test_open_app_netflix_with_query_routes_to_netflix_control(self):
        """open_app netflix with 'play One Piece on Netflix' → netflix_control search."""
        tool, params = route_tool_call(
            "open_app", {"app_name": "netflix"}, "Play One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_open_app_netflix_play_no_query_routes_to_play(self):
        """open_app netflix 'play Netflix' → netflix_control play."""
        tool, params = route_tool_call("open_app", {"app_name": "netflix"}, "Play Netflix")
        assert tool == "netflix_control"
        assert params.get("action") == "play"

    def test_open_app_netflix_no_media_intent_routes_to_netflix_control(self):
        """open_app netflix without play/search intent → netflix_control browse."""
        tool, params = route_tool_call("open_app", {"app_name": "netflix"}, "Open Netflix app")
        assert tool == "netflix_control"
        assert params.get("action") == "browse"

    def test_open_app_youtube_with_query_routes_to_youtube_video(self):
        """open_app youtube with 'play despacito' → youtube_video."""
        tool, params = route_tool_call(
            "open_app", {"app_name": "youtube"}, "Play despacito on YouTube"
        )
        assert tool == "youtube_video"
        assert params.get("action") == "search"
        assert "despacito" in params.get("query", "").lower()

    def test_open_app_youtube_no_media_intent_routes_to_browser_control(self):
        """open_app youtube without media intent → browser_control go_to."""
        tool, params = route_tool_call("open_app", {"app_name": "youtube"}, "Open YouTube app")
        assert tool == "browser_control"
        assert params.get("action") == "go_to"
        assert "youtube.com" in params.get("url", "")

    def test_open_app_netflix_never_stays_open_app(self):
        """open_app netflix must never return open_app as tool (hard-block)."""
        tools_seen = set()
        for user_text in ["Open Netflix app", "Play Netflix", "Play One Piece on Netflix"]:
            t, _ = route_tool_call("open_app", {"app_name": "netflix"}, user_text)
            tools_seen.add(t)
        assert "open_app" not in tools_seen


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2 — browser_control new_tab → go_to conversion for YT/NF
# ═════════════════════════════════════════════════════════════════════════════


class TestBrowserControlNewTabConversion:
    """new_tab for YouTube/Netflix must be converted to go_to (reuse existing tab)."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield

    def test_new_tab_youtube_converts_to_go_to(self):
        """browser_control new_tab youtube.com → go_to (reuse tab)."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "new_tab", "url": "https://www.youtube.com/watch?v=abc"},
            "play this video",
        )
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "youtube.com" in params["url"]
        assert params.get("force_new") is False

    def test_new_tab_netflix_converts_to_go_to(self):
        """browser_control new_tab netflix.com → go_to (reuse tab)."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "new_tab", "url": "https://www.netflix.com/title/123"},
            "watch this",
        )
        assert tool == "browser_control"
        assert params["action"] == "go_to"
        assert "netflix.com" in params["url"]

    def test_new_tab_non_platform_stays_new_tab(self):
        """browser_control new_tab for non-platform URL stays unchanged."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "new_tab", "url": "https://example.com"},
            "open example",
        )
        assert tool == "browser_control"
        assert params["action"] == "new_tab"


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2 — _route_ordinal_command
# ═════════════════════════════════════════════════════════════════════════════


class TestRouteOrdinalCommand:
    """Direct tests for the _route_ordinal_command helper.

    Note: _route_ordinal_command falls through to load_recent_grids() when no
    explicit platform hint is provided, so we seed a youtube snapshot for
    tests that need it.
    """

    @pytest.fixture(autouse=True)
    def _seed_youtube_grid(self):
        from core.browser_grid_state import GridSnapshot, clear_all_for_tests, save_grid

        clear_all_for_tests()
        snap = GridSnapshot(platform="youtube", query="test", timestamp=__import__("time").time())
        save_grid(snap)
        yield
        clear_all_for_tests()

    def test_second_video_returns_params(self):
        result = _route_ordinal_command("play the second video")
        assert result is not None
        tool, params = result
        assert tool == "youtube_video"
        assert params.get("ordinal") == 2
        assert params.get("action") == "grid_play"

    def test_first_netflix_title(self):
        result = _route_ordinal_command("play the first Netflix title")
        assert result is not None
        tool, params = result
        assert tool == "netflix_control"
        assert params.get("ordinal") == 1

    def test_third_youtube_video(self):
        result = _route_ordinal_command("play the third YouTube video")
        assert result is not None
        tool, params = result
        assert tool == "youtube_video"
        assert params.get("ordinal") == 3

    def test_non_ordinal_returns_none(self):
        assert _route_ordinal_command("play despacito") is None

    def test_empty_returns_none(self):
        assert _route_ordinal_command("") is None


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2 — ordinal route through route_tool_call
# ═════════════════════════════════════════════════════════════════════════════


class TestOrdinalRoutingInRouteToolCall:
    """Ordinal commands must be intercepted by route_tool_call before browser intent."""

    @pytest.fixture(autouse=True)
    def _mock_deps_and_grid(self):
        from core.browser_grid_state import GridSnapshot, clear_all_for_tests, save_grid

        clear_all_for_tests()
        snap = GridSnapshot(platform="youtube", query="test", timestamp=__import__("time").time())
        save_grid(snap)
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_second_video_from_unknown_tool(self):
        tool, params = route_tool_call("unknown_tool", {}, "play the second video")
        assert tool == "youtube_video"
        assert params.get("ordinal") == 2
        assert params.get("action") == "grid_play"

    def test_first_netflix_title_from_browser_control(self):
        tool, params = route_tool_call(
            "browser_control", {"action": "go_to"}, "play the first Netflix title"
        )
        assert tool == "netflix_control"
        assert params.get("ordinal") == 1
        assert params.get("action") == "grid_play"


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2 — _infer_scroll_target
# ═════════════════════════════════════════════════════════════════════════════


class TestInferScrollTarget:
    """Direct tests for the _infer_scroll_target helper."""

    def test_no_context_returns_none(self):
        from core.browser_grid_state import clear_scroll_context_for_tests

        clear_scroll_context_for_tests()
        result = _infer_scroll_target("scroll down", {})
        assert result is None

    def test_user_text_youtube_hint(self):
        result = _infer_scroll_target("scroll down on YouTube", {})
        assert result == "youtube"

    def test_user_text_netflix_hint(self):
        result = _infer_scroll_target("scroll up on Netflix", {})
        assert result == "netflix"

    def test_params_with_youtube_url(self):
        result = _infer_scroll_target("scroll", {"url": "https://www.youtube.com/watch?v=abc"})
        assert result == "youtube"

    def test_params_with_netflix_url(self):
        result = _infer_scroll_target("scroll", {"url": "https://www.netflix.com/title/123"})
        assert result == "netflix"

    def test_params_with_youtube_target(self):
        result = _infer_scroll_target("scroll", {"target": "youtube"})
        assert result == "youtube"

    def test_no_match_returns_none(self):
        result = _infer_scroll_target("scroll", {"url": "https://example.com"})
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2 — scroll target injection in route_tool_call
# ═════════════════════════════════════════════════════════════════════════════


class TestScrollTargetInjection:
    """route_tool_call must inject 'target' into scroll params when context exists."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield

    def test_scroll_down_with_youtube_user_text(self):
        """'scroll down on YouTube' injects target=youtube."""
        tool, params = route_tool_call("unknown_tool", {}, "scroll down on YouTube")
        assert tool == "computer_control"
        assert params.get("target") == "youtube"

    def test_scroll_up_with_netflix_user_text(self):
        """'scroll up on Netflix' injects target=netflix."""
        tool, params = route_tool_call(
            "netflix_control", {"action": "play"}, "scroll up on Netflix"
        )
        assert tool == "computer_control"
        assert params.get("target") == "netflix"

    def test_scroll_without_platform_does_not_inject_target(self):
        """Generic 'scroll down' without platform hint does not inject target."""
        tool, params = route_tool_call("unknown_tool", {}, "scroll down")
        assert tool == "computer_control"
        assert params.get("target") is None or params["target"] not in ("youtube", "netflix")


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2.3 — next-video routing
# ═════════════════════════════════════════════════════════════════════════════


class TestNextVideoRouting:
    """route_tool_call must route 'next video' to youtube_video next_video."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
            patch("core.self_healing_router.get_scroll_context", return_value="youtube"),
        ):
            yield

    def test_next_video_routes_to_youtube(self):
        """'next video' → youtube_video next_video."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "next video")
        assert tool == "youtube_video"
        assert params.get("action") == "next_video"

    def test_next_on_youtube_routes_to_youtube(self):
        """'next on YouTube' → youtube_video next_video."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "next on YouTube")
        assert tool == "youtube_video"
        assert params.get("action") == "next_video"

    def test_next_episode_on_netflix_does_not_route_to_youtube(self):
        """'next episode on Netflix' must not route to YouTube."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "next episode on Netflix"
        )
        assert tool != "youtube_video"


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2.3 — question form query extraction
# ═════════════════════════════════════════════════════════════════════════════


class TestQuestionFormQueryExtraction:
    """_extract_media_query must handle question forms."""

    def test_is_one_piece_on_netflix(self):
        """'Is One Piece on Netflix?' extracts query='One Piece'."""
        result = _extract_media_query({}, "Is One Piece on Netflix?", "netflix", "play")
        assert result == "One Piece"

    def test_esta_one_piece_en_netflix(self):
        """'Está One Piece en Netflix?' extracts query='One Piece'."""
        result = _extract_media_query({}, "Está One Piece en Netflix?", "netflix", "play")
        assert result == "One Piece"

    def test_esta_english_one_piece_on_netflix(self):
        """'Esta One Piece en Netflix?' (no accent) extracts query='One Piece'."""
        result = _extract_media_query({}, "Esta One Piece en Netflix?", "netflix", "play")
        assert result == "One Piece"

    def test_is_something_on_youtube(self):
        """'Is tutorial on YouTube?' extracts query='tutorial'."""
        result = _extract_media_query({}, "Is tutorial on YouTube?", "youtube", "play")
        assert result == "tutorial"

    def test_question_form_no_match_for_plain_play(self):
        """'Play Netflix' does not match question form."""
        result = _extract_media_query({}, "Play Netflix", "netflix", "play")
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2.3 — Netflix grid continuation routing
# ═════════════════════════════════════════════════════════════════════════════


class TestOrdinalContinuationRoute:
    """Ordinal continuation (I mean the second title) must route to grid_play."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        from core.browser_grid_state import GridSnapshot, clear_all_for_tests, save_grid

        clear_all_for_tests()
        from core.browser_grid_state import GridCandidate

        candidates = [
            GridCandidate(
                visible_order=1,
                title="One Piece Episode 1",
                url="https://netflix.com/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
            GridCandidate(
                visible_order=2,
                title="One Piece Episode 2",
                url="https://netflix.com/2",
                kind="title",
                safe=True,
                source="netflix",
            ),
            GridCandidate(
                visible_order=3,
                title="One Piece Episode 3",
                url="https://netflix.com/3",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        snap = GridSnapshot(
            platform="netflix",
            query="one piece",
            timestamp=__import__("time").time(),
            candidates=candidates,
        )
        save_grid(snap)
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_i_mean_the_second_title_routes_to_netflix_grid_play(self):
        """'I mean the second title' after Netflix grid → netflix_control grid_play ordinal=2."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "I mean the second title"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 2

    def test_the_second_one_routes_to_netflix_grid_play(self):
        """'the second one' after Netflix grid → netflix_control grid_play ordinal=2."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "the second one")
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 2

    def test_choose_number_2_routes_to_netflix_grid_play(self):
        """'choose number 2' after Netflix grid → netflix_control grid_play ordinal=2."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "choose number 2")
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 2


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2.4 — I-mean-on-platform routing
# ═════════════════════════════════════════════════════════════════════════════


class TestIMeanOnPlatform:
    """'I mean on <platform>' reuses last_user_query and routes correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from core.browser_grid_state import clear_all_for_tests, set_last_user_query

        clear_all_for_tests()
        set_last_user_query("Shape of My Heart", "youtube")
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_i_mean_on_youtube_reuses_last_query(self):
        """'I mean on YouTube' → youtube_video play with query."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "I mean on YouTube")
        assert tool == "youtube_video"
        assert params.get("action") == "play"
        assert params.get("query") == "Shape of My Heart"

    def test_i_mean_on_netflix_reuses_last_query(self):
        """'I mean on Netflix' → netflix_control search with query."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "I mean on Netflix")
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Shape of My Heart"

    def test_i_mean_on_spotify_bypasses_router(self):
        """'I mean on Spotify' reaches router but Spotify hard-bypass at line 365 applies."""
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "I mean on Spotify")
        assert tool == "media_coordinator"  # Spotify is hard-bypassed

    def test_i_mean_on_platform_no_previous_query_falls_through(self):
        """No last_user_query → i_mean_on_platform returns None, falls through normal routing.

        With no query to reuse, 'I mean on YouTube' routes to youtube_video play (no query),
        which then falls through to media_coordinator since there is no media_query.
        """
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
            patch("core.self_healing_router.get_scroll_context", return_value=None),
        ):
            tool, params = route_tool_call(
                "media_coordinator", {"action": "play"}, "I mean on YouTube"
            )
            # Without a stored query, _route_i_mean_on_platform returns youtube_video play (no query).
            # Then media_coordinator intercepts it (no media_query) → media_coordinator with target=youtube
            assert tool == "media_coordinator"


class TestIMeanOnPlatformDoesNotStealOrdinal:
    """'I mean the second title' must still route as ordinal, not I-mean-on-platform."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from core.browser_grid_state import GridSnapshot, clear_all_for_tests, save_grid

        clear_all_for_tests()
        from core.browser_grid_state import GridCandidate

        candidates = [
            GridCandidate(
                visible_order=1,
                title="Song One",
                url="https://netflix.com/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
            GridCandidate(
                visible_order=2,
                title="Song Two",
                url="https://netflix.com/2",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        snap = GridSnapshot(
            platform="netflix",
            query="test",
            timestamp=__import__("time").time(),
            candidates=candidates,
        )
        save_grid(snap)
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_i_mean_the_second_title_still_routes_ordinal(self):
        """'I mean the second title' → netflix_control grid_play, not I-mean-on-platform."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "I mean the second title"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 2

    def test_i_mean_the_first_one_still_routes_ordinal(self):
        """'I mean the first one' → netflix_control grid_play (ordinal)."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "I mean the first one"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 1


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2.4 — recent-platform preference over Spotify default
# ═════════════════════════════════════════════════════════════════════════════


class TestRecentPlatformPreference:
    """When no target set, recent scroll context should win over Spotify default."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_recent_youtube_context_defaults_to_youtube(self):
        """Generic 'play' with recent YouTube context → youtube target."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("youtube")
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            tool, params = route_tool_call("media_coordinator", {"action": "play"}, "play")
            assert params.get("target") == "youtube"

    def test_recent_netflix_context_defaults_to_netflix(self):
        """Generic 'play' with recent Netflix context → netflix target."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            tool, params = route_tool_call("media_coordinator", {"action": "play"}, "play")
            assert params.get("target") == "netflix"

    def test_no_recent_context_falls_to_spotify(self):
        """No scroll context → Spotify default (preserved)."""
        from core.browser_grid_state import clear_scroll_context_for_tests

        clear_scroll_context_for_tests()
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            tool, params = route_tool_call("media_coordinator", {"action": "play"}, "play")
            assert params.get("target") == "spotify"

    def test_explicit_platform_still_wins(self):
        """Explicit 'play on YouTube' → sets target=youtube, overrides scroll context."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            tool, params = route_tool_call(
                "media_coordinator", {"action": "play"}, "play on YouTube"
            )
            # 'play on YouTube' without a specific query → media_coordinator with target=youtube
            # (No search query extracted, so it becomes 'play on target=youtube')
            assert tool == "media_coordinator"
            assert params.get("target") == "youtube"
            assert params.get("action") == "play"


# ═══════════════════════════════════════════════════════════════════════════════
# Netflix Vision Guard — screen_process suppression tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNetflixVisionGuard:
    """Verify router-level Netflix Vision guard suppresses repeated screen_process calls."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        try:
            from core.browser_grid_state import clear_all_for_tests

            clear_all_for_tests()
        except Exception:
            pass
        try:
            from core.intent_memory import save_state

            save_state({})
        except Exception:
            pass
        yield

    def test_screen_process_without_netflix_keywords_passes_through(self):
        """screen_process for non-Netflix topics passes through unaffected."""
        tool, params = route_tool_call(
            "screen_process",
            {"text": "What is on my desktop?"},
            "look at my screen",
        )
        assert tool == "screen_process"

    def test_screen_process_without_cooldown_passes_through(self):
        """screen_process with Netflix keywords but no active cooldown passes through."""
        tool, params = route_tool_call(
            "screen_process",
            {"text": "Analyze the Netflix screen for play button"},
            "click play on Netflix",
        )
        assert tool == "screen_process"

    def test_screen_process_with_netflix_grid_suppressed_during_cooldown(self):
        """screen_process requesting Netflix grid is suppressed when cooldown active."""
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "Analyze current Netflix screen and show grid"},
            "what titles are visible",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_screen_process_with_visible_titles_suppressed_during_cooldown(self):
        """screen_process asking about visible Netflix titles is suppressed."""
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "what titles are visible on Netflix"},
            "show me Netflix titles",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_screen_process_play_title_again_suppressed_during_cooldown(self):
        """'play title again' with Netflix intent is suppressed during cooldown."""
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "play title again on Netflix"},
            "play title again",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_screen_process_grid_layout_suppressed_during_cooldown(self):
        """'grid layout' Netflix request is suppressed during cooldown."""
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "analyze grid layout"},
            "show grid layout on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"


class TestGenericScreenProcessSuppressedOnNetflix:
    """Verify generic screen_process is suppressed when Netflix active + failure/cooldown."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        from core.browser_grid_state import clear_all_for_tests as clear_grid_state

        clear_grid_state()
        from core.intent_memory import save_state

        save_state({})
        yield

    def test_generic_prompt_suppressed_when_scroll_context_netflix(self):
        """Generic 'analyze my screen' suppressed when scroll context is netflix + failure."""
        import actions.netflix_control as nc
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "Analyze my screen, tell me what you see"},
            "look at my screen",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_generic_prompt_suppressed_when_intent_memory_netflix(self):
        """Generic prompt suppressed when intent_memory.last_media_app is netflix."""
        import actions.netflix_control as nc
        from core.intent_memory import save_state

        save_state({"last_media_app": "netflix"})
        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "What is on my screen?"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_generic_prompt_passes_through_when_no_failure_no_cooldown(self):
        """Generic prompt passes through when no failure or cooldown."""
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "What is on my screen?"},
            "",
        )
        assert tool == "screen_process"

    def test_generic_prompt_passes_through_when_not_netflix_active(self):
        """Generic prompt passes through when Netflix not active even with cooldown."""
        import actions.netflix_control as nc
        from core.intent_memory import save_state

        save_state({"last_media_app": "spotify"})
        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        tool, params = route_tool_call(
            "screen_process",
            {"text": "What is on my screen?"},
            "",
        )
        assert tool == "screen_process"


class TestScreenProcessNarrowGuard:
    """Verify the narrowed early screen_process guard preserves management/async/bare calls."""

    def test_action_list_stays_screen_process(self):
        """screen_process action=list must stay screen_process (management action)."""
        tool, params = route_tool_call("screen_process", {"action": "list"}, "")
        assert tool == "screen_process"

    def test_action_result_stays_screen_process(self):
        """screen_process action=result must stay screen_process (management action)."""
        tool, params = route_tool_call("screen_process", {"action": "result"}, "")
        assert tool == "screen_process"

    def test_action_get_result_stays_screen_process(self):
        """screen_process action=get_result must stay screen_process."""
        tool, params = route_tool_call("screen_process", {"action": "get_result"}, "")
        assert tool == "screen_process"

    def test_action_status_stays_screen_process(self):
        """screen_process action=status must stay screen_process."""
        tool, params = route_tool_call("screen_process", {"action": "status"}, "")
        assert tool == "screen_process"

    def test_async_stays_screen_process(self):
        """screen_process async=True must stay screen_process."""
        tool, params = route_tool_call("screen_process", {"action": "analyze", "async": True}, "")
        assert tool == "screen_process"

    def test_bare_action_no_text_stays_screen_process(self):
        """screen_process with empty action and no text must stay screen_process."""
        tool, params = route_tool_call("screen_process", {}, "")
        assert tool == "screen_process"

    def test_empty_action_with_text_flows_to_guard(self):
        """screen_process with empty action but text present flows to Netflix Vision Guard."""
        import actions.netflix_control as nc
        from core.browser_grid_state import set_scroll_context

        nc._VISION_COOLDOWN_UNTIL = __import__("time").monotonic() + 100.0
        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "screen_process",
            {"action": "", "text": "What is on my screen?"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"


class TestVagueGridPlayExpandedPhrases:
    """Verify new vague grid phrases route to netflix_control grid_play."""

    def test_select_the_first_title_routes(self):
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "I'll select the first title on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 1

    def test_choose_the_first_title_routes(self):
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "choose the first title on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 1

    def test_berlin_top_left_routes(self):
        tool, params = route_tool_call(
            "netflix_control",
            {"action": "play"},
            "Berlin top left",
        )
        if tool == "netflix_control":
            assert params.get("action") in ("grid_play", "play")

    def test_try_again_top_left_routes(self):
        tool, params = route_tool_call(
            "netflix_control",
            {"action": "play"},
            "try again top left",
        )
        if tool == "netflix_control":
            assert params.get("action") in ("grid_play", "play")

    def test_pick_the_first_title_routes(self):
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "pick the first title on Netflix",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("ordinal") == 1


# ═════════════════════════════════════════════════════════════════════
# 18. Media coordinator Netflix no fullscreen (14B.2.14)
# ═════════════════════════════════════════════════════════════════════


class TestMediaCoordinatorNoFullscreenAfterSearch:
    """Verify media_coordinator does NOT call fullscreen after Netflix search."""

    def test_search_with_query_no_fullscreen(self):
        from actions import media_coordinator as mc

        calls = []

        def fake_netflix(params, player=None):
            calls.append(params.get("action"))
            return "done"

        with patch("actions.media_coordinator.netflix_control", side_effect=fake_netflix):
            with patch("actions.media_coordinator._pause_spotify_safely"):
                mc.media_coordinator({"target": "netflix", "action": "play", "query": "One Piece"})
        assert "search" in calls
        assert "fullscreen" not in calls

    def test_no_query_with_saved_grid_delegates_to_netflix_control(self):
        from actions import media_coordinator as mc

        calls = []

        def fake_netflix(params, player=None):
            calls.append(params.get("action"))
            return "done"

        with patch("actions.media_coordinator.netflix_control", side_effect=fake_netflix):
            with patch("actions.media_coordinator._pause_spotify_safely"):
                with patch("actions.media_coordinator._focus_existing_tab", return_value=True):
                    mc.media_coordinator({"target": "netflix", "action": "play"})
        assert "play" in calls
        assert "grid_play" not in calls
        assert "fullscreen" not in calls

    def test_no_query_no_grid_uses_play(self):
        from actions import media_coordinator as mc

        calls = []

        def fake_netflix(params, player=None):
            calls.append(params.get("action"))
            return "done"

        with patch("actions.media_coordinator.netflix_control", side_effect=fake_netflix):
            with patch("actions.media_coordinator._pause_spotify_safely"):
                with patch("core.browser_grid_state.get_recent_grid", return_value=None):
                    with patch("actions.media_coordinator._focus_existing_tab", return_value=True):
                        mc.media_coordinator({"target": "netflix", "action": "play"})
        assert "play" in calls
        assert "fullscreen" not in calls


# ═══════════════════════════════════════════════════════════════════════════════
# Change 8: French Netflix query extraction E2E through route_tool_call
# ═══════════════════════════════════════════════════════════════════════════════


class TestFrenchNetflixE2E:
    """Full route through route_tool_call for French Netflix patterns."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield

    def test_serie_champis_sur_netflix_e2e(self):
        """'Série Champis sur Netflix' → netflix_control search query=Champis _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Série Champis sur Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Champis"
        assert params.get("_auto_play") is True

    def test_film_berlin_sur_netflix_e2e(self):
        """'Film Berlin sur Netflix' → netflix_control search query=Berlin _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Film Berlin sur Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Berlin"
        assert params.get("_auto_play") is True

    def test_regarde_one_piece_sur_netflix_e2e(self):
        """'Regarde One Piece sur Netflix' → netflix_control search query=One Piece _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Regarde One Piece sur Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"
        assert params.get("_auto_play") is True

    def test_cherche_one_piece_sur_netflix_e2e(self):
        """'Cherche One Piece sur Netflix' → netflix_control search query=One Piece _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Cherche One Piece sur Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"
        assert params.get("_auto_play") is True

    def test_lance_one_piece_sur_netflix_e2e(self):
        """'Lance One Piece sur Netflix' → netflix_control search query=One Piece _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Lance One Piece sur Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"
        assert params.get("_auto_play") is True

    def test_one_piece_on_netflix_e2e(self):
        """'One Piece on Netflix' → netflix_control search query=One Piece _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"
        assert params.get("_auto_play") is True


# ═══════════════════════════════════════════════════════════════════════════════
# Change 6: Netflix Enter-fallback guard tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNetflixEnterFallbackGuard:
    """Verify router blocks bare Enter press during Netflix failure/cooldown."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield

    def test_netflix_enter_fallback_suppressed(self):
        """Failure active + bare Enter → blocked."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_netflix_enter_fallback_explicit_english(self):
        """ "press Enter" in text → allowed through (canonicalized to computer_control)."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Enter"},
            "press Enter",
        )
        assert tool == "computer_control"
        assert params.get("action") == "press"

    def test_netflix_enter_fallback_explicit_french(self):
        """ "appuie sur Entrée" in text → allowed through (canonicalized to computer_control)."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Enter"},
            "appuie sur Entrée",
        )
        assert tool == "computer_control"
        assert params.get("action") == "press"

    def test_netflix_enter_fallback_no_failure(self):
        """No failure → Enter allowed (canonicalized to computer_control)."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Enter"},
            "",
        )
        assert tool == "computer_control"
        assert params.get("action") == "press"

    def test_netflix_enter_fallback_computer_control(self):
        """computer_control press Enter also blocked during failure."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "computer_control",
            {"action": "press", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_netflix_enter_fallback_computer_control_allowed(self):
        """computer_control press Enter with explicit text allowed."""
        import actions.netflix_control as nc
        from core.browser_grid_state import clear_scroll_context_for_tests
        from core.intent_memory import save_state

        clear_scroll_context_for_tests()
        save_state({})
        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "computer_control",
            {"action": "press", "key": "Enter"},
            "press enter to confirm",
        )
        assert tool == "computer_control"

    def test_netflix_enter_action_key_blocked(self):
        """action=key Enter blocked during failure (regression: 2026-06-23)."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "key", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_netflix_enter_action_key_allowed_with_explicit_text(self):
        """action=key Enter allowed when user says 'press Enter'."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "key", "key": "Enter"},
            "press Enter",
        )
        assert tool == "browser_control"
        assert params.get("action") == "key"

    def test_netflix_enter_action_key_allowed_with_french_text(self):
        """action=key Enter allowed when user says 'appuie sur Entrée'."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "key", "key": "Enter"},
            "appuie sur Entrée",
        )
        assert tool == "browser_control"
        assert params.get("action") == "key"

    def test_netflix_enter_action_keyboard_blocked(self):
        """action=keyboard Enter blocked during failure."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "keyboard", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_netflix_enter_action_type_key_blocked(self):
        """action=type_key Enter blocked during failure."""
        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = __import__("time").monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "type_key", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"


# ═══════════════════════════════════════════════════════════════════════════════
# No old fallback patterns
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoOldFallbackPatterns:
    """Verify no old/disallowed fallback patterns exist."""

    def test_no_old_netflix_search_and_play(self):
        """Router must not reference netflix_search_and_play (method kept in browser_control by design)."""
        import inspect

        from core import self_healing_router as router

        src = inspect.getsource(router)
        assert "netflix_search_and_play" not in src, (
            "Router must not reference netflix_search_and_play"
        )

    def test_no_screen_process_fallback_for_netflix(self):
        """No screen_process/computer_control click as Netflix fallback in router."""
        import inspect

        from core import self_healing_router as router

        src = inspect.getsource(router)
        lines_with_issue = []
        for i, line in enumerate(src.split("\n")):
            if "screen_process" in line.lower() and "fallback" in line.lower():
                lines_with_issue.append(f"line {i + 1}: {line}")
        assert len(lines_with_issue) == 0, "screen_process fallback patterns found:\n" + "\n".join(
            lines_with_issue
        )

    def test_matching_candidate_autoplays(self):
        """Matched query routes to autoplay."""
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "One Piece on Netflix"
        )
        assert tool == "netflix_control"
        assert params.get("query") == "One Piece"
        assert params.get("_auto_play") is True

    def test_unrelated_candidates_rejected(self):
        """Unrelated titles should be rejected by strict token filter."""
        from actions.netflix_control import _filter_candidates_safely_for_search

        candidates = [
            {"title": "Outlast", "url": "https://netflix.com/title/1"},
            {"title": "The Walking Dead", "url": "https://netflix.com/title/2"},
        ]
        page_url = "https://www.netflix.com/search?q=One+Piece"
        filtered = _filter_candidates_safely_for_search(candidates, "One Piece", page_url)
        assert len(filtered) == 0, f"Expected 0 candidates, got {len(filtered)}: {filtered}"


# ═══════════════════════════════════════════════════════════════════════════════
# V8B Live Netflix Browser-Control Click Guard (PART A.7)
# ═══════════════════════════════════════════════════════════════════════════════


class TestV8BNetflixBrowserControlClickGuard:
    """PART A.7: browser_control click/type/press/hotkey/smart_click is
    suppressed when Netflix runtime context is active, even without
    explicit Netflix mention in user_text or params."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()
        from core.intent_memory import save_state

        save_state({})
        yield

    # ── click / smart_click → netflix_control search ──────────────────────

    def test_click_suppressed_when_scroll_context_netflix(self):
        """scroll_context=netflix + browser_control click -> netflix_control search."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "text": "One Piece"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_click_suppressed_when_intent_memory_netflix(self):
        """intent_memory last_media_app=netflix + browser_control click -> netflix_control search."""
        from core.intent_memory import save_state

        save_state({"last_media_app": "netflix"})
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "text": "Berlin"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Berlin"

    def test_smart_click_suppressed_when_scroll_context_netflix(self):
        """scroll_context=netflix + browser_control smart_click -> netflix_control search."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "smart_click", "text": "Play button"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Play button"

    def test_smart_click_vague_text_grid_play(self):
        """scroll_context=netflix + smart_click vague text -> netflix_control grid_play."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "smart_click", "text": "first title"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    # ── type → netflix_control search ─────────────────────────────────────

    def test_type_suppressed_when_scroll_context_netflix(self):
        """scroll_context=netflix + browser_control type -> netflix_control search."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "type", "text": "Berlin"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Berlin"

    def test_type_suppressed_when_intent_memory_netflix(self):
        """intent_memory last_media_app=netflix + browser_control type -> netflix_control search."""
        from core.intent_memory import save_state

        save_state({"last_media_app": "netflix"})
        tool, params = route_tool_call(
            "browser_control",
            {"action": "type", "text": "Lupin"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "Lupin"

    # ── press (non-Enter) → netflix_control status ────────────────────────

    def test_press_tab_suppressed_when_scroll_context_netflix(self):
        """scroll_context=netflix + browser_control press Tab -> netflix_control status."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Tab"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_press_arrow_suppressed_when_scroll_context_netflix(self):
        """scroll_context=netflix + browser_control press ArrowRight -> netflix_control status."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "ArrowRight"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    # ── hotkey → netflix_control status ───────────────────────────────────

    def test_hotkey_suppressed_when_scroll_context_netflix(self):
        """scroll_context=netflix + browser_control hotkey -> netflix_control status."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "hotkey", "key": "Ctrl+F"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_hotkey_suppressed_when_intent_memory_netflix(self):
        """intent_memory last_media_app=netflix + browser_control hotkey -> netflix_control status."""
        from core.intent_memory import save_state

        save_state({"last_media_app": "netflix"})
        tool, params = route_tool_call(
            "browser_control",
            {"action": "hotkey", "key": "Ctrl+L"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    # ── press Enter preserved (PART A.6 handles it) ──────────────────────

    def test_press_enter_preserved_when_scroll_context_netflix(self):
        """scroll_context=netflix + press Enter -> falls through (not caught by V8B)."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Enter"},
            "",
        )
        # V8B skips Enter; router canonicalizes to computer_control press
        assert tool != "netflix_control" or params.get("action") != "search"

    # ── non-Netflix contexts: pass through unchanged ──────────────────────

    def test_click_passes_through_when_scroll_context_youtube(self):
        """scroll_context=youtube + browser_control click -> browser_control (unchanged)."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("youtube")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "text": "Subscribe"},
            "",
        )
        assert tool == "browser_control"

    def test_click_passes_through_when_no_context(self):
        """No scroll context + browser_control click -> browser_control (unchanged)."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "text": "Button"},
            "",
        )
        assert tool == "browser_control"

    def test_click_passes_through_when_intent_memory_spotify(self):
        """intent_memory last_media_app=spotify + browser_control click -> browser_control."""
        from core.intent_memory import save_state

        save_state({"last_media_app": "spotify"})
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "text": "Button"},
            "",
        )
        assert tool == "browser_control"

    # ── existing guards still work ────────────────────────────────────────

    def test_explicit_netflix_still_caught_by_part_a(self):
        """Explicit Netflix in user_text still caught by PART A."""
        tool, params = route_tool_call(
            "browser_control",
            {"action": "click", "text": "search"},
            "click on Netflix search bar",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"

    def test_netflix_url_still_caught_by_part_a5(self):
        """browser_control go_to netflix.com URL still caught by URL hard-lock."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "browser_control",
            {"action": "go_to", "url": "https://www.netflix.com/title/123"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "play"

    def test_enter_fallback_guard_still_works(self):
        """PART A.6 Enter fallback guard still blocks bare Enter during failure."""
        import time as _time

        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = _time.monotonic() - 1.0
        tool, params = route_tool_call(
            "browser_control",
            {"action": "press", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"
        nc._LAST_NETFLIX_FAILURE_AT = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# V8C: Query normalization, computer_control guard, vague follow-ups
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeSearchQuery:
    """Tests for _normalize_search_query — French/English filler stripping."""

    def test_cest_le_one_piece(self):
        from core.self_healing_router import _normalize_search_query

        assert _normalize_search_query("c'est le one piece") == "one piece"

    def test_cest_ou_one_piece_en_netflix(self):
        from core.self_healing_router import _normalize_search_query

        assert _normalize_search_query("c'est où One Piece en Netflix ?") == "One Piece"

    def test_regarde_le_one_piece_sur_netflix(self):
        from core.self_healing_router import _normalize_search_query

        assert _normalize_search_query("regarde le one piece sur Netflix") == "one piece"

    def test_play_the_one_piece_on_netflix(self):
        from core.self_healing_router import _normalize_search_query

        assert _normalize_search_query("play the one piece on Netflix") == "one piece"

    def test_unchanged_query_preserved(self):
        from core.self_healing_router import _normalize_search_query

        assert _normalize_search_query("One Piece") == "One Piece"


class TestV8CComputerControlGuard:
    """V8C: computer_control type/click/press/hotkey suppressed when Netflix context active."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()
        from core.intent_memory import save_state

        save_state({})
        yield

    def test_computer_control_type_netflix_context_reroutes(self):
        """scroll_context=netflix + computer_control type -> netflix_control search."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "computer_control",
            {"action": "type", "text": "One Piece"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_computer_control_click_netflix_context_reroutes(self):
        """scroll_context=netflix + computer_control click -> netflix_control search."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "computer_control",
            {"action": "click", "text": "One Piece"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "One Piece"

    def test_computer_control_press_netflix_context_reroutes_to_status(self):
        """scroll_context=netflix + computer_control press -> netflix_control status."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "computer_control",
            {"action": "press", "key": "Tab"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"

    def test_computer_control_passes_through_when_not_netflix(self):
        """computer_control type passes through when scroll context is not Netflix."""
        from core.browser_grid_state import clear_scroll_context_for_tests

        clear_scroll_context_for_tests()
        # With no Netflix context, computer_control should not be intercepted
        # (It will hit the V7 VisionPolicy guard in the normal flow)
        tool, params = route_tool_call(
            "computer_control",
            {"action": "type", "text": "Hello"},
            "",
        )
        # Must not become netflix_control
        assert tool == "computer_control"

    def test_computer_control_type_dirty_query_normalized(self):
        """computer_control type with dirty query is normalized before routing."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "computer_control",
            {"action": "type", "text": "c'est le one piece"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "search"
        assert params.get("query") == "one piece"

    def test_computer_control_press_enter_with_netflix_failure(self):
        """computer_control press Enter during Netflix failure -> status."""
        import time

        import actions.netflix_control as nc

        nc._LAST_NETFLIX_FAILURE_AT = time.monotonic() - 1.0
        from core.browser_grid_state import clear_scroll_context_for_tests

        clear_scroll_context_for_tests()
        tool, params = route_tool_call(
            "computer_control",
            {"action": "press", "key": "Enter"},
            "",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "status"
        nc._LAST_NETFLIX_FAILURE_AT = 0.0


class TestV8CVagueFollowUpRouting:
    """V8C: vague follow-ups on Netflix context must not loop through media_coordinator play."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()
        from core.intent_memory import save_state

        save_state({})
        yield

    def test_play_it_with_netflix_context_reroutes(self):
        """'play it' with netflix scroll context -> netflix_control grid_play."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "play it",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    def test_yes_with_netflix_context_reroutes(self):
        """'yes' with netflix scroll context -> netflix_control grid_play."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "yes",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    def test_that_one_with_netflix_context_reroutes(self):
        """'that one' with netflix scroll context -> netflix_control grid_play."""
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "that one",
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    def test_play_it_without_netflix_context_not_rerouted(self):
        """'play it' without Netflix context -> normal routing (not rerouted)."""
        from core.browser_grid_state import clear_scroll_context_for_tests

        clear_scroll_context_for_tests()
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "play it",
        )
        # Should fall through to normal routing (no Netflix context)
        assert tool != "netflix_control" or params.get("action") != "grid_play"


class TestNetflixDomDiagnostic:
    """V8C: scanner diagnostic must reject notification links and report counts."""

    def test_diagnostic_rejects_chrome_anchors(self):
        """js diagnostics count correctly (no real browser needed)."""
        from actions.browser_intelligence import _NETFLIX_DIAGNOSTIC_JS

        assert "isChrome" in _NETFLIX_DIAGNOSTIC_JS
        assert "visibleAnchors" in _NETFLIX_DIAGNOSTIC_JS
        assert "visibleTitleAnchors" in _NETFLIX_DIAGNOSTIC_JS
        assert "isChrome" in _NETFLIX_DIAGNOSTIC_JS
        # Verify notification rejection exists in diagnostic
        assert "notifications" in _NETFLIX_DIAGNOSTIC_JS
        # Verify no click actions in diagnostic
        assert ".click(" not in _NETFLIX_DIAGNOSTIC_JS
        assert "screen_process" not in _NETFLIX_DIAGNOSTIC_JS


# ═════════════════════════════════════════════════════════════════════
# V9C.2 — Router Query Cleaning + Availability Intent Tests
# ═════════════════════════════════════════════════════════════════════


class TestV9C2CleanNetflixMediaQuery:
    """_clean_netflix_media_query strips filler and extracts clean title."""

    def test_clean_play_filler(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("do you have playing war machine") == "War Machine"

    def test_clean_can_you(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("can you play the one piece") == "The One Piece"

    def test_clean_please_prefix(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("please find stranger things") == "Stranger Things"

    def test_clean_platform_suffix(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("play Berlin on Netflix") == "Berlin"

    def test_clean_question_form(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("is there breaking bad on netflix?") == "Breaking Bad"

    def test_clean_no_filler_returns_title(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("Breaking Bad") == "Breaking Bad"

    def test_empty_returns_none(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("") is None
        assert _clean_netflix_media_query("   ") is None

    def test_ordinal_command_returns_none(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("title number two") is None
        assert _clean_netflix_media_query("the third result") is None

    def test_clean_search_for_prefix(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert _clean_netflix_media_query("search for one piece") == "One Piece"

    def test_clean_look_for_prefix(self):
        from core.self_healing_router import _clean_netflix_media_query

        assert (
            _clean_netflix_media_query("look for the mandalorian on netflix") == "The Mandalorian"
        )


class TestV9C2AvailabilityIntent:
    """_is_netflix_availability_or_list_intent detects search/list queries."""

    def test_do_you_have(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("do you have war machine") is True

    def test_search_for(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("search for one piece") is True

    def test_find(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("find breaking bad") is True

    def test_look_for(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("look for the witcher") is True

    def test_show_me(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("show me what you have") is True

    def test_play_not_availability(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("play war machine") is False

    def test_watch_not_availability(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("watch breaking bad") is False

    def test_empty_text(self):
        from core.self_healing_router import _is_netflix_availability_or_list_intent

        assert _is_netflix_availability_or_list_intent("") is False


class TestV9C2RouterRoutesCleanedQuery:
    """Router should clean dirty queries before sending to netflix_control."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()
        from core.intent_memory import save_state

        save_state({})
        yield

    def test_dirty_query_cleaned_in_search(self):
        """'do you have playing war machine' should clean to 'War Machine'."""
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play", "query": "do you have playing war machine"},
            "do you have playing war machine on Netflix",
        )
        if tool == "netflix_control":
            q = params.get("query", "")
            assert "do you have" not in q.lower()
            assert "playing" not in q.lower() or "playing" in q.lower()

    def test_availability_sets_no_autoplay(self):
        """'do you have X' should set _auto_play=False."""
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play", "query": "do you have war machine"},
            "do you have war machine on Netflix",
        )
        if tool == "netflix_control":
            assert params.get("_auto_play") is not True

    def test_play_preserves_autoplay(self):
        """'play war machine' should set _auto_play=True."""
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play", "query": "war machine"},
            "play war machine on Netflix",
        )
        if tool == "netflix_control":
            assert params.get("_auto_play") is True
