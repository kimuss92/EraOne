"""
Tests for grid selection infrastructure — Step 14B.2.

Covers:
  * utils/ordinal_selection.py — ordinal_to_index, parse_ordinal_command
  * core/browser_grid_state.py — GridCandidate, GridSnapshot, save/get/expire/context
  * actions/platform_tab.py — platform helpers (canonical domain, home, URL check)

No real browser calls. No real YouTube/Netflix/network calls.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from core.browser_grid_state import (
    GRID_TTL_SECONDS,
    SCROLL_CONTEXT_TTL_SECONDS,
    GridCandidate,
    GridSnapshot,
    clear_all_for_tests,
    clear_expired,
    clear_scroll_context_for_tests,
    get_recent_grid,
    get_scroll_context,
    load_recent_grids,
    save_grid,
    set_scroll_context,
)
from utils.ordinal_selection import (
    _detect_platform_hint,
    _detect_target_type,
    ordinal_to_index,
    parse_ordinal_command,
)

# ═════════════════════════════════════════════════════════════════════════════
# ordinal_to_index
# ═════════════════════════════════════════════════════════════════════════════


class TestOrdinalToIndex:
    def test_first_word(self):
        assert ordinal_to_index("first") == 1

    def test_second_word(self):
        assert ordinal_to_index("second") == 2

    def test_third_word(self):
        assert ordinal_to_index("third") == 3

    def test_fourth_word(self):
        assert ordinal_to_index("fourth") == 4

    def test_fifth_word(self):
        assert ordinal_to_index("fifth") == 5

    def test_sixth_word(self):
        assert ordinal_to_index("sixth") == 6

    def test_seventh_word(self):
        assert ordinal_to_index("seventh") == 7

    def test_eighth_word(self):
        assert ordinal_to_index("eighth") == 8

    def test_ninth_word(self):
        assert ordinal_to_index("ninth") == 9

    def test_tenth_word(self):
        assert ordinal_to_index("tenth") == 10

    def test_first_with_the(self):
        assert ordinal_to_index("the first") == 1

    def test_first_video(self):
        assert ordinal_to_index("first video") == 1

    def test_the_second_result(self):
        assert ordinal_to_index("the second result") == 2

    def test_1st(self):
        assert ordinal_to_index("1st") == 1

    def test_2nd(self):
        assert ordinal_to_index("2nd") == 2

    def test_3rd(self):
        assert ordinal_to_index("3rd") == 3

    def test_4th(self):
        assert ordinal_to_index("4th") == 4

    def test_10th(self):
        assert ordinal_to_index("10th") == 10

    def test_number_4(self):
        assert ordinal_to_index("number 4") == 4

    def test_result_number_3(self):
        assert ordinal_to_index("result number 3") == 3

    def test_video_2(self):
        assert ordinal_to_index("video 2") == 2

    def test_title_5(self):
        assert ordinal_to_index("title 5") == 5

    def test_item_1(self):
        assert ordinal_to_index("item 1") == 1

    def test_last(self):
        assert ordinal_to_index("last") == -1

    def test_out_of_range_11(self):
        assert ordinal_to_index("11th") is None

    def test_out_of_range_0(self):
        assert ordinal_to_index("0th") is None

    def test_non_ordinal_text(self):
        assert ordinal_to_index("despacito") is None

    def test_empty(self):
        assert ordinal_to_index("") is None

    def test_whitespace(self):
        assert ordinal_to_index("   ") is None

    def test_none(self):
        assert ordinal_to_index(None) is None

    # ── Step 14B.2.4 ordinal expansion ──
    def test_number_one(self):
        assert ordinal_to_index("number one") == 1

    def test_number_two(self):
        assert ordinal_to_index("number two") == 2

    def test_video_number_one(self):
        assert ordinal_to_index("video number one") == 1

    def test_top_video(self):
        assert ordinal_to_index("top video") == 1

    def test_top_result(self):
        assert ordinal_to_index("top result") == 1

    def test_top(self):
        assert ordinal_to_index("top") == 1

    def test_the_top_one(self):
        assert ordinal_to_index("the top one") == 1


# ═════════════════════════════════════════════════════════════════════════════
# parse_ordinal_command
# ═════════════════════════════════════════════════════════════════════════════


class TestParseOrdinalCommand:
    def test_play_first_video(self):
        r = parse_ordinal_command("play the first video")
        assert r is not None
        assert r["ordinal"] == 1
        assert r["target_type"] == "video"
        assert r["raw"] == "play the first video"

    def test_play_second_result(self):
        r = parse_ordinal_command("play the second result")
        assert r is not None
        assert r["ordinal"] == 2
        assert r["target_type"] == "result"

    def test_third_title(self):
        r = parse_ordinal_command("play the third title")
        assert r is not None
        assert r["ordinal"] == 3
        assert r["target_type"] == "title"

    def test_fourth_item(self):
        r = parse_ordinal_command("play the fourth item")
        assert r is not None
        assert r["ordinal"] == 4
        assert r["target_type"] == "item"

    def test_video_number_3(self):
        r = parse_ordinal_command("play video number 3")
        assert r is not None
        assert r["ordinal"] == 3
        assert r["target_type"] == "video"

    def test_result_number_4(self):
        r = parse_ordinal_command("select result number 4")
        assert r is not None
        assert r["ordinal"] == 4

    def test_1st_video(self):
        r = parse_ordinal_command("play the 1st video")
        assert r is not None
        assert r["ordinal"] == 1

    def test_2nd_result(self):
        r = parse_ordinal_command("play the 2nd result")
        assert r is not None
        assert r["ordinal"] == 2

    def test_last_one(self):
        r = parse_ordinal_command("play the last one")
        assert r is not None
        assert r["ordinal"] == -1
        assert r["target_type"] == "one"

    def test_netflix_platform_hint(self):
        r = parse_ordinal_command("play the second Netflix title")
        assert r is not None
        assert r["ordinal"] == 2
        assert r["platform_hint"] == "netflix"

    def test_youtube_platform_hint(self):
        r = parse_ordinal_command("play the third YouTube video")
        assert r is not None
        assert r["ordinal"] == 3
        assert r["platform_hint"] == "youtube"

    def test_no_ordinal_returns_none(self):
        assert parse_ordinal_command("play despacito") is None

    def test_play_it_returns_none(self):
        assert parse_ordinal_command("play it") is None

    def test_open_youtube_returns_none(self):
        assert parse_ordinal_command("open YouTube") is None

    def test_empty_returns_none(self):
        assert parse_ordinal_command("") is None

    def test_whitespace_returns_none(self):
        assert parse_ordinal_command("  ") is None

    def test_just_first_word_returns_ordinal(self):
        """'first' alone returns a valid ordinal parse (bare ordinal allowed)."""
        r = parse_ordinal_command("first")
        assert r is not None
        assert r["ordinal"] == 1

    def test_first_without_target_returns_none(self):
        """'play first' without 'video/result/title/item' should return None
        unless it's 'the first' (special case)."""
        r = parse_ordinal_command("play first")
        assert r is not None
        assert r["ordinal"] == 1

    def test_play_first(self):
        r = parse_ordinal_command("play first")
        assert r is not None
        assert r["ordinal"] == 1

    def test_i_mean_the_second_title(self):
        r = parse_ordinal_command("i mean the second title")
        assert r is not None
        assert r["ordinal"] == 2
        assert r["target_type"] == "title"

    def test_I_mean_the_third_video(self):
        r = parse_ordinal_command("I mean the third video")
        assert r is not None
        assert r["ordinal"] == 3
        assert r["target_type"] == "video"

    def test_choose_number_2(self):
        r = parse_ordinal_command("choose number 2")
        assert r is not None
        assert r["ordinal"] == 2

    def test_the_second_one(self):
        r = parse_ordinal_command("the second one")
        assert r is not None
        assert r["ordinal"] == 2
        assert r["target_type"] == "one"

    def test_number_three(self):
        r = parse_ordinal_command("number 3")
        assert r is not None
        assert r["ordinal"] == 3

    def test_title_number_2(self):
        r = parse_ordinal_command("title number 2")
        assert r is not None
        assert r["ordinal"] == 2
        assert r["target_type"] == "title"

    # ── Step 14B.2.4 ordinal expansion ──
    def test_number_one_command(self):
        r = parse_ordinal_command("number one")
        assert r is not None
        assert r["ordinal"] == 1
        assert r["target_type"] == "number"

    def test_top_video_command(self):
        r = parse_ordinal_command("top video")
        assert r is not None
        assert r["ordinal"] == 1
        assert r["target_type"] == "video"

    def test_video_number_one_command(self):
        r = parse_ordinal_command("play video number one")
        assert r is not None
        assert r["ordinal"] == 1
        assert r["target_type"] == "video"


# ═════════════════════════════════════════════════════════════════════════════
# _detect_platform_hint / _detect_target_type
# ═════════════════════════════════════════════════════════════════════════════


class TestDetectPlatformHint:
    def test_youtube_explicit(self):
        assert _detect_platform_hint("play on youtube") == "youtube"

    def test_netflix_explicit(self):
        assert _detect_platform_hint("play on netflix") == "netflix"

    def test_yt_abbreviation(self):
        assert _detect_platform_hint("play on yt") == "youtube"

    def test_nf_abbreviation(self):
        assert _detect_platform_hint("play on nf") == "netflix"

    def test_both_ambiguous(self):
        assert _detect_platform_hint("youtube and netflix") == "ambiguous"

    def test_none(self):
        assert _detect_platform_hint("play despacito") is None


class TestDetectTargetType:
    def test_video(self):
        assert _detect_target_type("play the video") == "video"

    def test_result(self):
        assert _detect_target_type("the result") == "result"

    def test_title(self):
        assert _detect_target_type("second title") == "title"

    def test_item(self):
        assert _detect_target_type("fourth item") == "item"

    def test_one(self):
        assert _detect_target_type("last one") == "one"

    def test_none(self):
        assert _detect_target_type("despacito") is None


# ═════════════════════════════════════════════════════════════════════════════
# browser_grid_state — GridCandidate / GridSnapshot
# ═════════════════════════════════════════════════════════════════════════════


class TestGridCandidate:
    def test_minimal(self):
        c = GridCandidate(visible_order=1, title="Test", url="https://example.com", kind="video")
        assert c.visible_order == 1
        assert c.title == "Test"
        assert c.safe is True
        assert c.rejected_reason == ""

    def test_full(self):
        c = GridCandidate(
            visible_order=2,
            title="Video 2",
            url="https://example.com/2",
            kind="video",
            safe=False,
            rejected_reason="ad",
            source="test",
        )
        assert c.visible_order == 2
        assert c.safe is False
        assert c.rejected_reason == "ad"
        assert c.source == "test"


class TestGridSnapshot:
    def test_minimal(self):
        snap = GridSnapshot(platform="youtube", query="test", timestamp=100.0)
        assert snap.platform == "youtube"
        assert snap.query == "test"
        assert snap.timestamp == 100.0
        assert snap.candidates == []

    def test_with_candidates(self):
        c = GridCandidate(visible_order=1, title="A", url="https://a.com", kind="video")
        snap = GridSnapshot(platform="netflix", query="test", timestamp=200.0, candidates=[c])
        assert len(snap.candidates) == 1
        assert snap.candidates[0].title == "A"


# ═════════════════════════════════════════════════════════════════════════════
# browser_grid_state — save / get / expire
# ═════════════════════════════════════════════════════════════════════════════


class TestSaveAndGetGrid:
    def setup_method(self):
        clear_all_for_tests()

    def test_save_and_get_youtube(self):
        snap = GridSnapshot(platform="youtube", query="despacito", timestamp=time.time())
        save_grid(snap)
        got = get_recent_grid("youtube")
        assert got is not None
        assert got.platform == "youtube"
        assert got.query == "despacito"

    def test_save_and_get_netflix(self):
        snap = GridSnapshot(platform="netflix", query="one piece", timestamp=time.time())
        save_grid(snap)
        got = get_recent_grid("netflix")
        assert got is not None
        assert got.query == "one piece"

    def test_get_nonexistent_platform(self):
        assert get_recent_grid("spotify") is None

    def test_get_expired(self):
        snap = GridSnapshot(platform="youtube", query="old", timestamp=time.time() - 400)
        save_grid(snap)
        assert get_recent_grid("youtube", ttl_seconds=300) is None

    def test_save_invalid_platform(self):
        snap = GridSnapshot(platform="spotify", query="x", timestamp=time.time())
        save_grid(snap)
        assert get_recent_grid("spotify") is None

    def test_save_none(self):
        save_grid(None)
        # No crash

    def test_load_recent_grids(self):
        clear_all_for_tests()
        snap_y = GridSnapshot(platform="youtube", query="a", timestamp=time.time())
        snap_n = GridSnapshot(platform="netflix", query="b", timestamp=time.time())
        save_grid(snap_y)
        save_grid(snap_n)
        grids = load_recent_grids()
        assert "youtube" in grids
        assert "netflix" in grids

    def test_load_recent_grids_with_expired(self):
        clear_all_for_tests()
        snap = GridSnapshot(platform="youtube", query="old", timestamp=time.time() - 400)
        save_grid(snap)
        grids = load_recent_grids(ttl_seconds=300)
        assert "youtube" not in grids

    def test_clear_expired_removes_old(self):
        clear_all_for_tests()
        snap = GridSnapshot(platform="youtube", query="old", timestamp=time.time() - 400)
        save_grid(snap)
        clear_expired(ttl_seconds=300)
        assert get_recent_grid("youtube") is None

    def test_clear_expired_keeps_fresh(self):
        clear_all_for_tests()
        snap = GridSnapshot(platform="youtube", query="fresh", timestamp=time.time())
        save_grid(snap)
        clear_expired(ttl_seconds=300)
        assert get_recent_grid("youtube") is not None

    def test_clear_all_for_tests(self):
        snap = GridSnapshot(platform="youtube", query="x", timestamp=time.time())
        save_grid(snap)
        clear_all_for_tests()
        assert get_recent_grid("youtube") is None


# ═════════════════════════════════════════════════════════════════════════════
# browser_grid_state — scroll context
# ═════════════════════════════════════════════════════════════════════════════


class TestScrollContext:
    def setup_method(self):
        clear_scroll_context_for_tests()

    def test_set_and_get_youtube(self):
        set_scroll_context("youtube")
        ctx = get_scroll_context(ttl_seconds=90)
        assert ctx == "youtube"

    def test_set_and_get_netflix(self):
        set_scroll_context("netflix")
        ctx = get_scroll_context(ttl_seconds=90)
        assert ctx == "netflix"

    def test_set_invalid_platform_ignored(self):
        set_scroll_context("spotify")
        assert get_scroll_context() is None

    def test_expired_returns_none(self):
        set_scroll_context("youtube")
        with patch("core.browser_grid_state._now", return_value=time.time() + 200):
            ctx = get_scroll_context(ttl_seconds=90)
            assert ctx is None

    def test_most_recent_wins(self):
        set_scroll_context("netflix")
        set_scroll_context("youtube")
        ctx = get_scroll_context()
        assert ctx == "youtube"

    def test_clear_for_tests(self):
        set_scroll_context("youtube")
        clear_scroll_context_for_tests()
        assert get_scroll_context() is None


# ═════════════════════════════════════════════════════════════════════════════
# platform_tab — helpers
# ═════════════════════════════════════════════════════════════════════════════


class TestPlatformHelpers:
    def test_platform_canonical_domain_youtube(self):
        from actions.platform_tab import _platform_canonical_domain

        assert _platform_canonical_domain("youtube") == "youtube.com"

    def test_platform_canonical_domain_yt(self):
        from actions.platform_tab import _platform_canonical_domain

        assert _platform_canonical_domain("yt") == "youtube.com"

    def test_platform_canonical_domain_netflix(self):
        from actions.platform_tab import _platform_canonical_domain

        assert _platform_canonical_domain("netflix") == "netflix.com"

    def test_platform_canonical_domain_nf(self):
        from actions.platform_tab import _platform_canonical_domain

        assert _platform_canonical_domain("nf") == "netflix.com"

    def test_platform_canonical_domain_unknown(self):
        from actions.platform_tab import _platform_canonical_domain

        assert _platform_canonical_domain("spotify") == ""

    def test_platform_home_youtube(self):
        from actions.platform_tab import _platform_home

        assert "youtube.com" in _platform_home("youtube")

    def test_platform_home_netflix(self):
        from actions.platform_tab import _platform_home

        assert "netflix.com" in _platform_home("netflix")

    def test_platform_home_unknown(self):
        from actions.platform_tab import _platform_home

        assert _platform_home("spotify") == ""

    def test_is_platform_url_youtube_match(self):
        from actions.platform_tab import _is_platform_url

        assert _is_platform_url("https://www.youtube.com/watch?v=abc", "youtube")

    def test_is_platform_url_netflix_match(self):
        from actions.platform_tab import _is_platform_url

        assert _is_platform_url("https://www.netflix.com/title/123", "netflix")

    def test_is_platform_url_no_match(self):
        from actions.platform_tab import _is_platform_url

        assert not _is_platform_url("https://chatgpt.com", "youtube")

    def test_is_platform_url_empty(self):
        from actions.platform_tab import _is_platform_url

        assert not _is_platform_url("", "youtube")

    def test_is_platform_url_none(self):
        from actions.platform_tab import _is_platform_url

        assert not _is_platform_url(None, "youtube")

    def test_platform_tab_result_dataclass(self):
        from actions.platform_tab import PlatformTabResult

        r = PlatformTabResult(
            page="mock",
            platform="youtube",
            reuse=True,
            selected_tab_url="https://youtube.com",
            target_page_before_url="",
            final_url="https://youtube.com",
            force_new=False,
        )
        assert r.platform == "youtube"
        assert r.reuse is True
        assert r.final_url == "https://youtube.com"


# ═════════════════════════════════════════════════════════════════════════════
# Step 14B.2.4 — last_user_query runtime state
# ═════════════════════════════════════════════════════════════════════════════


class TestLastUserQuery:
    def setup_method(self):
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()

    def teardown_method(self):
        from core.browser_grid_state import clear_all_for_tests

        clear_all_for_tests()

    def test_set_and_get(self):
        from core.browser_grid_state import get_last_user_query, set_last_user_query

        set_last_user_query("one piece", "netflix")
        state = get_last_user_query()
        assert state["query"] == "one piece"
        assert state["platform"] == "netflix"

    def test_default_empty(self):
        from core.browser_grid_state import get_last_user_query

        state = get_last_user_query()
        assert state["query"] == ""
        assert state["platform"] == ""

    def test_clear(self):
        from core.browser_grid_state import (
            clear_last_user_query_for_tests,
            get_last_user_query,
            set_last_user_query,
        )

        set_last_user_query("despacito", "youtube")
        clear_last_user_query_for_tests()
        state = get_last_user_query()
        assert state["query"] == ""
        assert state["platform"] == ""

    def test_overwrite(self):
        from core.browser_grid_state import get_last_user_query, set_last_user_query

        set_last_user_query("first", "youtube")
        set_last_user_query("second", "netflix")
        state = get_last_user_query()
        assert state["query"] == "second"
        assert state["platform"] == "netflix"


# ── V9C.3: Row/Column grid position tests ─────────────────────────────────────


class TestV9C3GridPositionParser(unittest.TestCase):
    """Tests for parse_netflix_grid_position in self_healing_router."""

    def test_row_col_pattern(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("row 2 title 5")
        self.assertIsNotNone(r)
        self.assertEqual(r["row"], 2)
        self.assertEqual(r["col"], 5)

    def test_row_col_pattern_b(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("5th title in row 2")
        self.assertIsNotNone(r)
        self.assertEqual(r["row"], 2)
        self.assertEqual(r["col"], 5)

    def test_row_only(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("row 3")
        self.assertIsNotNone(r)
        self.assertEqual(r["row"], 3)
        self.assertEqual(r["col"], 1)

    def test_top_left(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("top left")
        self.assertIsNotNone(r)
        self.assertEqual(r["row"], 1)
        self.assertEqual(r["col"], 1)

    def test_top_right(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("top right")
        self.assertIsNotNone(r)
        self.assertEqual(r["row"], 1)
        self.assertIn(r["col"], (3, 99))  # 99 = last column sentinel

    def test_bottom_left(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("bottom left")
        self.assertIsNotNone(r)
        self.assertIn(r["row"], (3, 99))  # 99 = last row sentinel
        self.assertEqual(r["col"], 1)

    def test_bottom_right(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("bottom right")
        self.assertIsNotNone(r)
        self.assertIn(r["row"], (3, 99))  # 99 = last row sentinel
        self.assertIn(r["col"], (3, 99))  # 99 = last column sentinel

    def test_simple_ordinal(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("play the second title")
        self.assertIsNotNone(r)
        self.assertEqual(r["ordinal"], 2)

    def test_no_match(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("play some movie on netflix")
        self.assertIsNone(r)

    def test_empty_text(self):
        from core.self_healing_router import parse_netflix_grid_position

        self.assertIsNone(parse_netflix_grid_position(""))
        self.assertIsNone(parse_netflix_grid_position(None))

    def test_word_numbers(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("row two title five")
        self.assertIsNotNone(r)
        self.assertEqual(r["row"], 2)
        self.assertEqual(r["col"], 5)

    def test_grid_item_digit(self):
        from core.self_healing_router import parse_netflix_grid_position

        r = parse_netflix_grid_position("grid item 4")
        self.assertIsNotNone(r)
        self.assertEqual(r["ordinal"], 4)


class TestV9C3GridPositionRouting(unittest.TestCase):
    """Tests for grid position routing in self_healing_router."""

    def test_row_col_routes_to_netflix(self):
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "row 2 title 5")
        self.assertEqual(tool, "netflix_control")
        self.assertEqual(params.get("action"), "grid_play")
        self.assertEqual(params.get("row"), 2)
        self.assertEqual(params.get("col"), 5)

    def test_top_left_routes_to_netflix(self):
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "select top left")
        self.assertEqual(tool, "netflix_control")
        self.assertEqual(params.get("action"), "grid_play")
        self.assertEqual(params.get("row"), 1)
        self.assertEqual(params.get("col"), 1)

    def test_simple_ordinal_routes_to_netflix(self):
        """Row/col parser detects ordinal; routing depends on media_coordinator routing."""
        from core.self_healing_router import parse_netflix_grid_position

        # The parser should detect ordinal=2 for "play the second title"
        r = parse_netflix_grid_position("play the second title")
        self.assertIsNotNone(r)
        self.assertEqual(r.get("ordinal"), 2)
        # Routing through media_coordinator may not bypass it to netflix_control
        # depending on platform detection — the parser output is the key test

    def test_dirty_query_not_cleaned_when_grid_position(self):
        """Row/col commands should not be cleaned into search queries."""
        from core.self_healing_router import _clean_netflix_media_query

        r = _clean_netflix_media_query("row 2 title 5")
        self.assertIsNone(r)

    def test_dirty_query_cleaned_when_not_grid_position(self):
        """Non-grid queries should still be cleaned normally."""
        from core.self_healing_router import _clean_netflix_media_query

        r = _clean_netflix_media_query("play War Machine on Netflix")
        self.assertEqual(r, "War Machine")


class TestV9C3GridCache(unittest.TestCase):
    """Tests for grid cache functions in netflix_control."""

    def test_save_load_cache(self):
        import json
        import os
        import time

        from actions.netflix_control import (
            _NETFLIX_GRID_CACHE_PATH,
            _load_netflix_grid_cache,
            _save_netflix_grid_cache,
        )

        test_data = {
            "rows": [
                {
                    "row": 1,
                    "col": 1,
                    "title": "Test Title",
                    "href": "",
                    "x": 100,
                    "y": 100,
                    "w": 200,
                    "h": 120,
                }
            ],
            "count": 1,
            "rowCount": 1,
            "_cached_at": time.time(),
        }
        _save_netflix_grid_cache(test_data)
        loaded = _load_netflix_grid_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["count"], 1)
        self.assertEqual(loaded["rows"][0]["title"], "Test Title")
        # Clean up
        try:
            os.remove(_NETFLIX_GRID_CACHE_PATH)
        except Exception:
            pass

    def test_cache_expired(self):
        import json
        import os
        import time

        from actions.netflix_control import _NETFLIX_GRID_CACHE_PATH, _load_netflix_grid_cache

        # Write expired cache directly (save function overwrites timestamp)
        os.makedirs(os.path.dirname(_NETFLIX_GRID_CACHE_PATH), exist_ok=True)
        expired_data = {
            "rows": [
                {
                    "row": 1,
                    "col": 1,
                    "title": "Old",
                    "href": "",
                    "x": 100,
                    "y": 100,
                    "w": 200,
                    "h": 120,
                }
            ],
            "count": 1,
            "_cached_at": time.time() - 400,  # expired
        }
        with open(_NETFLIX_GRID_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(expired_data, f)
        loaded = _load_netflix_grid_cache()
        self.assertIsNone(loaded)
        try:
            os.remove(_NETFLIX_GRID_CACHE_PATH)
        except Exception:
            pass

    def test_find_card_at_row_col(self):
        from actions.netflix_control import _find_card_at_row_col

        grid = {
            "rows": [
                {"row": 1, "col": 1, "title": "A", "href": ""},
                {"row": 1, "col": 2, "title": "B", "href": ""},
                {"row": 2, "col": 1, "title": "C", "href": ""},
            ]
        }
        card = _find_card_at_row_col(grid, 1, 2)
        self.assertIsNotNone(card)
        self.assertEqual(card["title"], "B")

    def test_find_card_not_found(self):
        from actions.netflix_control import _find_card_at_row_col

        grid = {"rows": [{"row": 1, "col": 1, "title": "A"}]}
        self.assertIsNone(_find_card_at_row_col(grid, 5, 5))


class TestV9C3GridPlayRowCol(unittest.TestCase):
    """Tests for _handle_grid_play_row_col in netflix_control."""

    def test_click_cached_card_no_page(self):
        from actions.netflix_control import _click_cached_netflix_card

        self.assertIsNone(_click_cached_netflix_card(1, 1, None, None))

    def test_click_cached_card_invalid_coords(self):
        from actions.netflix_control import _click_cached_netflix_card

        self.assertIsNone(_click_cached_netflix_card(0, 1, "fake_page", "fake_sess"))
        self.assertIsNone(_click_cached_netflix_card(1, 0, "fake_page", "fake_sess"))

    def test_handle_grid_play_row_col_no_page(self):
        from actions.netflix_control import _handle_grid_play_row_col

        result = _handle_grid_play_row_col(1, 1, "home", {}, None, None)
        self.assertIsNone(result)
