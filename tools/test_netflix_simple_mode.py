#!/usr/bin/env python3
"""Unit tests for Netflix simple mode (direct URL + DOM text matching).

Covers:
  - Title matching (exact, contains, tokens, rejection)
  - Safety (no screen_process, no computer_control click, no random Enter)
  - Controlled failure on no match
  - Playback verification required before "Playing"
  - List-only mode behavior
  - Query routing (language stripping)
  - Fix 1-12: href guard, candidate lock, identity verification, etc.

Run:
    python -m pytest tools/test_netflix_simple_mode.py -v --tb=short
"""

import json
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# 1. Helpers: build a mock page/sess that returns canned JS results
# ---------------------------------------------------------------------------


def _make_page(
    candidates_js=None,
    scored_js=None,
    title_links_count=5,
    current_url="https://www.netflix.com/search?q=One+Piece",
    has_links=True,
    playback_ok=True,
):
    """Create a mock page object that returns predefined JS evaluation results."""
    page = MagicMock()
    page.url = current_url

    def _eval_side_effect(js_or_fn, *args):
        js_str = str(js_or_fn) if not isinstance(js_or_fn, str) else js_or_fn
        if (
            "_SIMPLE_MODE_CANDIDATE_JS" in js_str
            or "primarySelectors" in js_str
            or "a[href" in js_str
        ):
            if candidates_js is not None:
                return json.dumps(candidates_js)
            return "[]"
        if "_SIMPLE_MODE_TITLE_MATCH_JS" in js_str or "scored" in js_str or "qNorm" in js_str:
            if scored_js is not None:
                return json.dumps(scored_js)
            return "[]"
        if "title/" in js_str or "watch/" in js_str:
            return has_links
        if "window.location.href" in js_str:
            return current_url
        return "[]"

    page.evaluate = MagicMock(side_effect=_eval_side_effect)
    page.keyboard = MagicMock()
    page.keyboard.press = MagicMock()
    page.keyboard.type = MagicMock()
    page.goto = MagicMock()
    page.bring_to_front = MagicMock()
    return page


def _make_sess(playback_ok=True):
    """Create a mock session object."""
    sess = MagicMock()
    sess.run = MagicMock(side_effect=lambda x, **kw: x if not isinstance(x, str) else x)
    sess.get_url = MagicMock(return_value="https://www.netflix.com/search?q=One+Piece")
    sess.evaluate_background_media = MagicMock(return_value="ok")
    return sess


# ---------------------------------------------------------------------------
# 2. Import the functions under test
# ---------------------------------------------------------------------------

from actions.netflix_control import (
    AUTOPLAY_MIN_SCORE,
    NETFLIX_SIMPLE_MODE,
    _click_netflix_title_candidate,
    _close_netflix_overlays,
    _deduplicate_query,
    _extract_netflix_title_id,
    _find_netflix_title_by_text,
    _is_valid_netflix_href,
    _open_netflix_native_search,
    _simple_search_and_play,
    _verify_title_context_matches,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Test class: _find_netflix_title_by_text
# ═══════════════════════════════════════════════════════════════════════════════


class TestFindNetflixTitleByText:
    """Tests for _find_netflix_title_by_text — text-based title matching."""

    def test_exact_title_match_accepted(self):
        """Exact normalized title match should score 1.0."""
        candidates = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=candidates, scored_js=scored)
        sess = _make_sess()

        result = _find_netflix_title_by_text(page, sess, "One Piece")

        assert len(result) == 1
        assert result[0]["score"] == 1.0
        assert result[0]["reason"] == "exact"
        assert result[0]["text"] == "One Piece"

    def test_full_query_contained_in_title(self):
        """Title containing the full query should score 0.9."""
        candidates = [
            {
                "text": "One Piece: East Blue",
                "href": "https://www.netflix.com/title/81254224",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        scored = [
            {
                "text": "One Piece: East Blue",
                "href": "https://www.netflix.com/title/81254224",
                "score": 0.9,
                "reason": "contain",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=candidates, scored_js=scored)
        sess = _make_sess()

        result = _find_netflix_title_by_text(page, sess, "One Piece")

        assert len(result) == 1
        assert result[0]["score"] == 0.9
        assert result[0]["reason"] == "contain"
        assert result[0]["text"] == "One Piece: East Blue"

    def test_all_query_tokens_present(self):
        """Title containing all query tokens should score 0.7."""
        candidates = [
            {
                "text": "The One Piece Adventure",
                "href": "https://www.netflix.com/title/81254224",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        scored = [
            {
                "text": "The One Piece Adventure",
                "href": "https://www.netflix.com/title/81254224",
                "score": 0.7,
                "reason": "all_tokens",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=candidates, scored_js=scored)
        sess = _make_sess()

        result = _find_netflix_title_by_text(page, sess, "One Piece")

        assert len(result) == 1
        assert result[0]["score"] == 0.7
        assert result[0]["reason"] == "all_tokens"

    def test_unrelated_title_rejected(self):
        """Title unrelated to query should not appear in results."""
        page = _make_page(candidates_js=[], scored_js=[])
        sess = _make_sess()

        result = _find_netflix_title_by_text(page, sess, "One Piece")

        assert len(result) == 0

    def test_notification_header_account_profile_rejected(self):
        """Candidates from notification/header/account/profile chrome should be excluded."""
        candidates_with_chrome = [
            {
                "text": "Notifications",
                "href": "https://www.netflix.com/account",
                "rect": {"x": 0, "y": 0, "w": 100, "h": 50},
            },
            {
                "text": "Account Settings",
                "href": "https://www.netflix.com/account",
                "rect": {"x": 0, "y": 0, "w": 100, "h": 50},
            },
            {
                "text": "Profile Selection",
                "href": "https://www.netflix.com/profiles",
                "rect": {"x": 0, "y": 0, "w": 100, "h": 50},
            },
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=candidates_with_chrome, scored_js=scored)
        sess = _make_sess()

        result = _find_netflix_title_by_text(page, sess, "One Piece")

        result_texts = [r["text"] for r in result]
        assert "One Piece" in result_texts
        assert "Notifications" not in result_texts
        assert "Account Settings" not in result_texts
        assert "Profile Selection" not in result_texts


# ═══════════════════════════════════════════════════════════════════════════════
# Test class: _simple_search_and_play — safety checks
# ═══════════════════════════════════════════════════════════════════════════════


class TestSimpleSearchAndPlaySafety:
    """Verify that simple mode does not use forbidden mechanisms."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_no_screen_process(
        self, mock_set_play, mock_click_play, mock_pause, mock_verify_ctx, mock_verify
    ):
        """_simple_search_and_play must not call screen_process."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        with patch("actions.netflix_control.screen_process", create=True) as mock_sp:
            _simple_search_and_play("One Piece", page, sess, auto_play=True)
            mock_sp.assert_not_called()

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_no_computer_control_click(
        self, mock_set_play, mock_click_play, mock_pause, mock_verify_ctx, mock_verify
    ):
        """_simple_search_and_play must not use computer_control click."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        with patch("actions.netflix_control.computer_control", create=True) as mock_cc:
            _simple_search_and_play("One Piece", page, sess, auto_play=True)
            if hasattr(mock_cc, "click"):
                mock_cc.click.assert_not_called()
            if hasattr(mock_cc, "screen_click"):
                mock_cc.screen_click.assert_not_called()

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_no_random_enter(
        self, mock_set_play, mock_click_play, mock_pause, mock_verify_ctx, mock_verify
    ):
        """_simple_search_and_play must not randomly press Enter."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        _simple_search_and_play("One Piece", page, sess, auto_play=True)

        for c in page.keyboard.press.call_args_list:
            args = [str(a).lower() for a in c[0]]
            assert "enter" not in args, f"Unexpected Enter press: {c}"

    def test_no_old_browser_control_netflix_search_and_play(self):
        """The old browser_control.netflix_search_and_play route must not exist."""
        try:
            from actions import browser_control

            assert not hasattr(browser_control, "netflix_search_and_play"), (
                "Old browser_control.netflix_search_and_play still exists!"
            )
        except ImportError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Test class: _simple_search_and_play — behavior
# ═══════════════════════════════════════════════════════════════════════════════


class TestSimpleSearchAndPlayBehavior:
    """Verify search-and-play orchestration logic."""

    def test_no_match_returns_controlled_failure(self):
        """When no candidates match, return a controlled failure message."""
        page = _make_page(candidates_js=[], scored_js=[])
        sess = _make_sess()

        result = _simple_search_and_play("Nonexistent Show XYZ", page, sess, auto_play=True)

        assert "could not identify" in result.lower() or "no matching" in result.lower()

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_match_opens_title_page(
        self, mock_set_play, mock_click_play, mock_pause, mock_verify_ctx, mock_verify
    ):
        """When a match is found and auto_play=True, navigation to /title/ should occur."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)

        page.goto.assert_called()
        nav_url = page.goto.call_args_list[-1][0][0]
        assert "/title/" in nav_url or "/watch/" in nav_url
        assert "Playing" in result

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_playback_verification_required(
        self, mock_set_play, mock_click_play, mock_pause, mock_verify_ctx, mock_verify
    ):
        """Must verify playback before returning 'Playing'."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)

        mock_verify.assert_called()
        assert result.startswith("Playing:")

    @patch("actions.netflix_control._verify_playback_started", return_value=False)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch(
        "actions.netflix_control.click_netflix_play_button_for_current_title",
        return_value="Play button not found",
    )
    @patch("actions.netflix_control._set_playing")
    def test_playback_failure_returns_error(
        self, mock_set, mock_click_play, mock_pause, mock_verify_ctx, mock_verify
    ):
        """When playback verification fails after play button click, return error."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)

        assert "could not start playback" in result.lower()

    def test_list_only_mode_returns_numbered_list(self):
        """auto_play=False should return a numbered list, not play."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
            {
                "text": "One Piece Film: Red",
                "href": "https://www.netflix.com/title/81633902",
                "score": 0.9,
                "reason": "startswith_query",
                "rect": {"x": 10, "y": 240, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=False)

        assert "1." in result
        assert "2." in result
        assert "Playing" not in result
        assert "tell me which one" in result.lower()

    def test_list_only_does_not_list_unrelated(self):
        """List-only mode should not list candidates below the safety threshold."""
        scored = [
            {
                "text": "Random Show",
                "href": "https://www.netflix.com/title/99999",
                "score": 0.1,
                "reason": "partial_tokens",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=False)

        assert "Random Show" not in result
        assert "could not identify" in result.lower() or "try a different" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Test class: Query routing (language stripping)
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueryRouting:
    """Verify that natural-language query prefixes are stripped correctly."""

    def test_serie_champis_routing(self):
        """'Serie Champis sur Netflix' should route query='Champis'."""
        result = _deduplicate_query("Serie Champis sur Netflix")
        assert "Champis" in result or "champis" in result.lower()

    def test_film_berlin_routing(self):
        """'Film Berlin sur Netflix' should route query='Berlin'."""
        result = _deduplicate_query("Film Berlin sur Netflix")
        assert "Berlin" in result or "berlin" in result.lower()

    def test_regarde_one_piece_routing(self):
        """'Regarde One Piece sur Netflix' should route query='One Piece'."""
        result = _deduplicate_query("Regarde One Piece sur Netflix")
        assert "One Piece" in result or "one piece" in result.lower()

    def test_deduplicate_removes_consecutive_duplicates(self):
        """Consecutive duplicate words should be removed."""
        result = _deduplicate_query("One One Piece Piece")
        assert result.lower() == "one piece"


# ═══════════════════════════════════════════════════════════════════════════════
# Test class: Config flag
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigFlag:
    """Verify the simple mode flag is set correctly."""

    def test_simple_mode_is_default(self):
        """NETFLIX_SIMPLE_MODE should be True by default."""
        assert NETFLIX_SIMPLE_MODE is True


# ═══════════════════════════════════════════════════════════════════════════════
# Test class: _close_netflix_overlays
# ═══════════════════════════════════════════════════════════════════════════════


class TestCloseNetflixOverlays:
    """Verify overlay dismissal presses Escape multiple times."""

    def test_presses_escape_three_times(self):
        """_close_netflix_overlays should press Escape 3 times."""
        page = MagicMock()
        sess = MagicMock()
        sess.run = MagicMock()

        _close_netflix_overlays(page, sess)

        assert page.keyboard.press.call_count == 3
        for c in page.keyboard.press.call_args_list:
            assert c[0][0] == "Escape"


# ═══════════════════════════════════════════════════════════════════════════════
# Test class: _open_netflix_native_search
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenNetflixNativeSearch:
    """Verify native search UI interaction."""

    def test_returns_false_when_no_launcher(self):
        """Should return False when search launcher is not found."""
        page = MagicMock()
        page.evaluate = MagicMock(return_value="not_found")
        sess = MagicMock()
        sess.run = MagicMock(return_value="not_found")

        result = _open_netflix_native_search(page, sess, "One Piece")

        assert result is False

    def test_returns_true_when_launcher_found(self):
        """Should return True when search input is found and text entered."""
        page = MagicMock()

        call_count = [0]

        def _eval_side_effect(js):
            call_count[0] += 1
            if call_count[0] == 1:
                return "clicked"
            return "found"

        page.evaluate = MagicMock(side_effect=_eval_side_effect)
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)

        result = _open_netflix_native_search(page, sess, "One Piece")

        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: page.evaluate() call signature (2026-06-23 bug)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvaluateCallSignature:
    """Verify page.evaluate() is called with exactly 2 positional args (JS + one dict)."""

    def test_find_title_calls_evaluate_with_two_args(self):
        """_find_netflix_title_by_text must pass JS + one dict to page.evaluate()."""
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"

        candidates = [
            {
                "text": "One Piece",
                "href": "https://netflix.com/title/1",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        scored = [
            {
                "text": "One Piece",
                "href": "https://netflix.com/title/1",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]

        call_count = [0]

        def _eval_side_effect(*args):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps(candidates)
            return json.dumps(scored)

        page.evaluate = MagicMock(side_effect=_eval_side_effect)
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x if not isinstance(x, str) else x)

        _find_netflix_title_by_text(page, sess, "One Piece")

        second_call = page.evaluate.call_args_list[1]
        assert len(second_call[0]) == 2, (
            f"page.evaluate() called with {len(second_call[0])} positional args, expected 2. "
            f"Args: {second_call[0]}"
        )
        assert isinstance(second_call[0][0], str)
        assert isinstance(second_call[0][1], dict)
        assert "candidates" in second_call[0][1]
        assert "query" in second_call[0][1]

    def test_find_title_rejects_three_args(self):
        """If page.evaluate is called with >2 positional args, it should raise."""

        def strict_evaluate(*args):
            if len(args) > 2:
                raise TypeError(
                    f"Page.evaluate() takes from 2 to 3 positional arguments but {len(args) + 1} were given"
                )
            return "[]"

        page = MagicMock()
        page.evaluate = strict_evaluate
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)

        result = _find_netflix_title_by_text(page, sess, "One Piece")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Matching rules — one-word exact only, multi-word startswith
# ═══════════════════════════════════════════════════════════════════════════════


class TestMatchingRules:
    """Verify title matching rules for autoplay safety."""

    def _make_scored_eval(self, scored):
        call_count = [0]

        def _eval_side_effect(*args):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps(
                    [{"text": s["text"], "href": s["href"], "rect": s["rect"]} for s in scored]
                )
            return json.dumps(scored)

        return _eval_side_effect

    def test_one_word_exact_match_autoplay_allowed(self):
        """One-word query 'Berlin' → title 'Berlin' gets score 1.0."""
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=Berlin"
        scored = [
            {
                "text": "Berlin",
                "href": "https://netflix.com/title/1",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        page.evaluate = MagicMock(side_effect=self._make_scored_eval(scored))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        result = _find_netflix_title_by_text(page, sess, "Berlin")
        assert len(result) == 1
        assert result[0]["score"] == 1.0
        assert result[0]["reason"] == "exact"

    def test_one_word_partial_match_rejected_for_autoplay(self):
        """One-word query 'Berlin' → title 'Berlin et La Dame' gets score 0.5 (below AUTOPLAY_MIN_SCORE)."""
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=Berlin"
        scored = [
            {
                "text": "Berlin et La Dame a l'hermine",
                "href": "https://netflix.com/title/2",
                "score": 0.5,
                "reason": "partial_one_word",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        page.evaluate = MagicMock(side_effect=self._make_scored_eval(scored))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        result = _find_netflix_title_by_text(page, sess, "Berlin")
        assert len(result) == 1
        assert result[0]["score"] == 0.5
        assert result[0]["reason"] == "partial_one_word"

    def test_multi_word_startswith_allowed(self):
        """Multi-word query 'One Piece' → title 'One Piece Film: Red' gets score 0.9."""
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        scored = [
            {
                "text": "One Piece Film: Red",
                "href": "https://netflix.com/title/3",
                "score": 0.9,
                "reason": "startswith_query",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        page.evaluate = MagicMock(side_effect=self._make_scored_eval(scored))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        result = _find_netflix_title_by_text(page, sess, "One Piece")
        assert len(result) == 1
        assert result[0]["score"] == 0.9
        assert result[0]["reason"] == "startswith_query"

    def test_multi_word_suffix_rejected_for_autoplay(self):
        """Multi-word query 'One Piece' → title 'John Bishop: One Piece' gets score 0.5 (below AUTOPLAY_MIN_SCORE)."""
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        scored = [
            {
                "text": "John Bishop: One Piece",
                "href": "https://netflix.com/title/4",
                "score": 0.5,
                "reason": "all_tokens_suffix",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        page.evaluate = MagicMock(side_effect=self._make_scored_eval(scored))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        result = _find_netflix_title_by_text(page, sess, "One Piece")
        assert len(result) == 1
        assert result[0]["score"] == 0.5
        assert result[0]["reason"] == "all_tokens_suffix"

    def test_multi_word_exact_match_autoplay(self):
        """Multi-word query 'One Piece' → title 'ONE PIECE' gets score 1.0."""
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        scored = [
            {
                "text": "ONE PIECE",
                "href": "https://netflix.com/title/5",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        page.evaluate = MagicMock(side_effect=self._make_scored_eval(scored))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        result = _find_netflix_title_by_text(page, sess, "One Piece")
        assert len(result) == 1
        assert result[0]["score"] == 1.0
        assert result[0]["reason"] == "exact"


# ═══════════════════════════════════════════════════════════════════════════════
# Playback verification — polling, player containers, blocking states
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlaybackVerification:
    """Verify _verify_playback_active polls and detects player containers."""

    def test_watch_page_waits_for_video(self):
        """On /watch/ page, waits up to timeout for video element."""
        from actions.netflix_control import _verify_playback_active

        page = MagicMock()
        page.url = "https://www.netflix.com/watch/12345"
        call_count = [0]

        def _eval_side_effect(js):
            call_count[0] += 1
            if call_count[0] <= 3:
                return json.dumps(
                    {
                        "url": "https://www.netflix.com/watch/12345",
                        "hasVideo": False,
                        "hasPlayer": False,
                        "blocked": False,
                    }
                )
            return json.dumps(
                {
                    "url": "https://www.netflix.com/watch/12345",
                    "hasVideo": True,
                    "paused": False,
                    "currentTime": 5.0,
                    "readyState": 4,
                    "duration": 120.0,
                    "hasPlayer": True,
                    "blocked": False,
                }
            )

        page.evaluate = MagicMock(side_effect=_eval_side_effect)
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with patch(
            "actions.netflix_control._check_local_signals",
            return_value={
                "url_contains_watch": True,
                "current_url": "https://www.netflix.com/watch/12345",
            },
        ):
            result = _verify_playback_active(sess, page)
        assert result is True

    def test_watch_page_with_video_verifies(self):
        """On /watch/ with video element playing, returns True."""
        from actions.netflix_control import _verify_playback_active

        page = MagicMock()
        page.url = "https://www.netflix.com/watch/12345"
        page.evaluate = MagicMock(
            return_value=json.dumps(
                {
                    "url": "https://www.netflix.com/watch/12345",
                    "hasVideo": True,
                    "paused": False,
                    "currentTime": 10.0,
                    "readyState": 4,
                    "duration": 120.0,
                    "hasPlayer": True,
                    "blocked": False,
                }
            )
        )
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with patch(
            "actions.netflix_control._check_local_signals",
            return_value={
                "url_contains_watch": True,
                "current_url": "https://www.netflix.com/watch/12345",
            },
        ):
            result = _verify_playback_active(sess, page)
        assert result is True

    def test_watch_page_with_player_container_verifies(self):
        """On /watch/ with player container but no video yet, keeps polling."""
        from actions.netflix_control import _verify_playback_active

        page = MagicMock()
        page.url = "https://www.netflix.com/watch/12345"
        call_count = [0]

        def _eval_side_effect(js):
            call_count[0] += 1
            if call_count[0] <= 10:
                return json.dumps(
                    {
                        "url": "https://www.netflix.com/watch/12345",
                        "hasVideo": False,
                        "hasPlayer": True,
                        "blocked": False,
                    }
                )
            return json.dumps(
                {
                    "url": "https://www.netflix.com/watch/12345",
                    "hasVideo": True,
                    "paused": False,
                    "currentTime": 2.0,
                    "readyState": 4,
                    "duration": 120.0,
                    "hasPlayer": True,
                    "blocked": False,
                }
            )

        page.evaluate = MagicMock(side_effect=_eval_side_effect)
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with patch(
            "actions.netflix_control._check_local_signals",
            return_value={
                "url_contains_watch": True,
                "current_url": "https://www.netflix.com/watch/12345",
            },
        ):
            result = _verify_playback_active(sess, page)
        assert result is True

    def test_watch_page_no_video_no_player_timeout(self):
        """On /watch/ without video or player after timeout, returns False."""
        from actions.netflix_control import _verify_playback_active

        page = MagicMock()
        page.url = "https://www.netflix.com/watch/12345"
        page.evaluate = MagicMock(
            return_value=json.dumps(
                {
                    "url": "https://www.netflix.com/watch/12345",
                    "hasVideo": False,
                    "hasPlayer": False,
                    "blocked": False,
                }
            )
        )
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with patch(
            "actions.netflix_control._check_local_signals",
            return_value={
                "url_contains_watch": True,
                "current_url": "https://www.netflix.com/watch/12345",
            },
        ):
            result = _verify_playback_active(sess, page)
        assert result is False

    def test_blocked_profile_selection_returns_false(self):
        """Profile selection blocking state returns False immediately."""
        from actions.netflix_control import _verify_playback_active

        page = MagicMock()
        page.url = "https://www.netflix.com/watch/12345"
        page.evaluate = MagicMock(
            return_value=json.dumps(
                {
                    "url": "https://www.netflix.com/watch/12345",
                    "hasVideo": False,
                    "hasPlayer": False,
                    "blocked": True,
                    "blockReason": "profile_selection",
                }
            )
        )
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with patch(
            "actions.netflix_control._check_local_signals",
            return_value={
                "url_contains_watch": True,
                "current_url": "https://www.netflix.com/watch/12345",
            },
        ):
            result = _verify_playback_active(sess, page)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-play safety — _set_playing only after verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestAutoPlaySafety:
    """Verify _simple_search_and_play only calls _set_playing after verification."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._set_playing")
    def test_set_playing_only_after_verification(
        self, mock_set_playing, mock_find, mock_pause, mock_verify_ctx, mock_verify
    ):
        """_set_playing is only called when playback is verified."""
        from actions.netflix_control import _simple_search_and_play

        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=Berlin"
        candidates = [
            {
                "text": "Berlin",
                "href": "https://netflix.com/title/1",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        mock_find.return_value = candidates
        page.evaluate = MagicMock(return_value=json.dumps(candidates))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with (
            patch("actions.netflix_control._close_netflix_overlays"),
            patch(
                "actions.netflix_control.click_netflix_play_button_for_current_title",
                return_value=None,
            ),
        ):
            _simple_search_and_play("Berlin", page, sess, auto_play=True)
            mock_set_playing.assert_called_once_with("Berlin")

    @patch("actions.netflix_control._verify_playback_started", return_value=False)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._set_playing")
    def test_set_playing_not_called_on_verification_failure(
        self, mock_set_playing, mock_find, mock_pause, mock_verify_ctx, mock_verify
    ):
        """_set_playing is NOT called when playback verification fails."""
        from actions.netflix_control import _simple_search_and_play

        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=Berlin"
        candidates = [
            {
                "text": "Berlin",
                "href": "https://netflix.com/title/1",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            }
        ]
        mock_find.return_value = candidates
        page.evaluate = MagicMock(return_value=json.dumps(candidates))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x)
        with (
            patch("actions.netflix_control._close_netflix_overlays"),
            patch(
                "actions.netflix_control.click_netflix_play_button_for_current_title",
                return_value="No button",
            ),
        ):
            result = _simple_search_and_play("Berlin", page, sess, auto_play=True)
            mock_set_playing.assert_not_called()
            assert "Playing" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 1: Valid href guard
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidHrefGuard:
    """Verify candidates with invalid href are not autoplayed."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    def test_candidate_invalid_href_not_autoplayed(self, *mocks):
        """Candidate with invalid href (no /title/ or /watch/) should not autoplay."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/account",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)

        assert "stopped for safety" in result.lower() or "no valid" in result.lower()

    def test_is_valid_netflix_href_rejects_empty(self):
        """_is_valid_netflix_href returns False for empty string."""
        assert _is_valid_netflix_href("") is False

    def test_is_valid_netflix_href_rejects_account(self):
        """_is_valid_netflix_href returns False for /account/ URL."""
        assert _is_valid_netflix_href("https://www.netflix.com/account") is False

    def test_is_valid_netflix_href_accepts_title(self):
        """_is_valid_netflix_href returns True for /title/<digits>."""
        assert _is_valid_netflix_href("https://www.netflix.com/title/81254224") is True

    def test_is_valid_netflix_href_accepts_watch(self):
        """_is_valid_netflix_href returns True for /watch/<digits>."""
        assert _is_valid_netflix_href("https://www.netflix.com/watch/81254224") is True


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 3: _click_netflix_title_candidate returns bool
# ═══════════════════════════════════════════════════════════════════════════════


class TestClickNetflixTitleCandidate:
    """Verify _click_netflix_title_candidate returns bool and validates URL."""

    def test_returns_false_for_empty_href(self):
        """Returns False for empty href."""
        sess = MagicMock()
        page = MagicMock()
        result = _click_netflix_title_candidate(page, sess, {"href": ""})
        assert result is False

    def test_returns_false_for_wrong_href(self):
        """Returns False for href that is not a valid Netflix title URL."""
        sess = MagicMock()
        page = MagicMock()
        result = _click_netflix_title_candidate(
            page, sess, {"href": "https://www.netflix.com/account"}
        )
        assert result is False

    def test_returns_true_for_valid_href(self):
        """Returns True for valid Netflix /title/ href."""
        sess = MagicMock()
        page = MagicMock()
        page.goto = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x if not isinstance(x, str) else x)
        result = _click_netflix_title_candidate(
            page, sess, {"href": "https://www.netflix.com/title/81254224"}
        )
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 4: No generic Play after candidate nav fails
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoGenericPlayAfterNavFailure:
    """Verify generic Play button is NOT clicked after candidate nav fails."""

    @patch("actions.netflix_control._verify_playback_started")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title")
    def test_no_play_after_invalid_href(self, mock_click_play, mock_verify):
        """Generic play button must not be called after invalid candidate href."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/account",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        _simple_search_and_play("One Piece", page, sess, auto_play=True)

        mock_click_play.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 5+6: Identity verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestVerifyTitleContextMatches:
    """Verify _verify_title_context_matches checks page identity."""

    def test_accepts_correct_title_page(self):
        """Accepts page with matching /title/<id> URL."""
        page = MagicMock()
        page.evaluate = MagicMock(return_value="https://www.netflix.com/title/81254224")
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x if not isinstance(x, str) else x)
        result = _verify_title_context_matches(page, sess, "One Piece", "81254224")
        assert result is True

    def test_rejects_wrong_title_page(self):
        """Rejects page with wrong /title/<id>."""
        page = MagicMock()
        page.evaluate = MagicMock(return_value="https://www.netflix.com/title/99999999")
        sess = MagicMock()
        sess.run = MagicMock(side_effect=lambda x, **kw: x if not isinstance(x, str) else x)
        result = _verify_title_context_matches(page, sess, "One Piece", "81254224")
        assert result is False

    def test_returns_false_on_exception(self):
        """Returns False on exception instead of True."""
        page = MagicMock()
        page.evaluate = MagicMock(side_effect=Exception("evaluate failed"))
        sess = MagicMock()
        sess.run = MagicMock(side_effect=Exception("run failed"))
        result = _verify_title_context_matches(page, sess, "Test")
        assert result is False

    def test_accepts_title_via_og_title(self):
        """Accepts when og:title matches expected title."""
        page = MagicMock()
        sess = MagicMock()

        def _side_effect(fn, *args):
            s = str(fn)
            if "window.location.href" in s:
                return "https://www.netflix.com/title/123"
            if "og:title" in s:
                return "One Piece"
            return ""

        sess.run = MagicMock(side_effect=_side_effect)
        page.evaluate = MagicMock(side_effect=lambda *args: str(args[0]) if args else "")
        result = _verify_title_context_matches(page, sess, "One Piece")
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 7: Verify played identity not only video active
# ═══════════════════════════════════════════════════════════════════════════════


class TestVerifyPlaybackIdentity:
    """Verify playback checks both active video AND identity."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=False)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control._set_playing")
    def test_active_video_wrong_identity_does_not_return_playing(
        self, mock_set, mock_pause, mock_ctx, mock_verify
    ):
        """Active video but wrong/unknown identity does not return Playing."""
        scored = [
            {
                "text": "Wrong Show",
                "href": "https://www.netflix.com/title/999",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(
            candidates_js=scored,
            scored_js=scored,
            current_url="https://www.netflix.com/search?q=Wrong",
        )
        sess = _make_sess()

        result = _simple_search_and_play("Wrong Show", page, sess, auto_play=True)

        assert "Playing" not in result
        assert "stopped for safety" in result.lower()

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control._set_playing")
    def test_active_video_verified_identity_returns_playing(
        self, mock_set, mock_pause, mock_ctx, mock_verify
    ):
        """Active video + verified identity returns Playing: <expected_title>."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)
        assert "Playing:" in result
        assert "One Piece" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 8: Stop success being overwritten
# ═══════════════════════════════════════════════════════════════════════════════


class TestSuccessNotOverwritten:
    """Once playback is verified active and identity is safe, return immediately."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control._set_playing")
    def test_early_return_on_verified_playback(self, mock_set, mock_pause, mock_ctx, mock_verify):
        """If playback verified active immediately after nav, return Playing right away."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)

        assert result.startswith("Playing:")
        mock_set.assert_called_once_with("One Piece")


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 9: Pause other media before Netflix playback
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseOtherMedia:
    """Verify _pause_other_media is called before Netflix playback starts."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_pause_called_before_playback(
        self, mock_set, mock_click, mock_pause, mock_ctx, mock_verify
    ):
        """_pause_other_media() must be called before opening the final Play/Watch."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        _simple_search_and_play("One Piece", page, sess, auto_play=True)

        mock_pause.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 10: Scoring/autoplay threshold
# ═══════════════════════════════════════════════════════════════════════════════


class TestAutoplayThreshold:
    """Verify AUTOPLAY_MIN_SCORE enforces correct autoplay behavior."""

    def test_autoplay_min_score_is_0_85(self):
        """AUTOPLAY_MIN_SCORE should be 0.85."""
        assert AUTOPLAY_MIN_SCORE == 0.85

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_exact_title_can_autoplay(self, *mocks):
        """Exact title candidate with valid /title/<id> can autoplay."""
        scored = [
            {
                "text": "Lupin",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("Lupin", page, sess, auto_play=True)
        assert "Playing:" in result
        assert "Lupin" in result

    @patch("actions.netflix_control._verify_playback_started")
    @patch("actions.netflix_control._verify_title_context_matches")
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title")
    @patch("actions.netflix_control._set_playing")
    def test_one_word_partial_not_autoplayed(self, *mocks):
        """One-word partial candidate (score 0.5) is listed but not autoplayed."""
        scored = [
            {
                "text": "Berlin et La Dame a l'hermine",
                "href": "https://netflix.com/title/2",
                "score": 0.5,
                "reason": "partial_one_word",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("Berlin", page, sess, auto_play=True)
        assert "Playing" not in result
        assert "not confident" in result.lower() or "tell me which" in result.lower()

    @patch("actions.netflix_control._verify_playback_started")
    @patch("actions.netflix_control._verify_title_context_matches")
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title")
    @patch("actions.netflix_control._set_playing")
    def test_multi_word_suffix_not_autoplayed(self, *mocks):
        """Multi-word suffix candidate (score 0.5) is listed but not autoplayed."""
        scored = [
            {
                "text": "John Bishop: One Piece",
                "href": "https://netflix.com/title/4",
                "score": 0.5,
                "reason": "all_tokens_suffix",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)
        assert "Playing" not in result

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._verify_title_context_matches", return_value=True)
    @patch("actions.netflix_control._pause_other_media")
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    def test_score_0_9_startswith_autoplays(self, *mocks):
        """Score 0.9 (startswith multi-word query) is >= 0.85 and can autoplay."""
        scored = [
            {
                "text": "One Piece: East Blue",
                "href": "https://www.netflix.com/title/81254224",
                "score": 0.9,
                "reason": "startswith_query",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()

        result = _simple_search_and_play("One Piece", page, sess, auto_play=True)
        assert "Playing:" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 11: getHref hardening — tested via _SIMPLE_MODE_CANDIDATE_JS
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetHrefHardening:
    """Verify the getHref function uses depth-limited climbing."""

    def test_get_href_uses_depth_limit(self):
        """_SIMPLE_MODE_CANDIDATE_JS getHref is depth-limited (max 3)."""
        js = __import__(
            "actions.netflix_control", fromlist=["_SIMPLE_MODE_CANDIDATE_JS"]
        )._SIMPLE_MODE_CANDIDATE_JS
        assert "depth" in js, "getHref should use depth-limited climbing"
        assert "depth < 3" in js or "depth <= 3" in js, "getHref should limit depth to 3"


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 12: Saved grid safety
# ═══════════════════════════════════════════════════════════════════════════════


class TestSavedGridSafety:
    """Verify saved grid is discarded when stale or query mismatched."""

    def test_extract_title_id_from_title_url(self):
        """_extract_netflix_title_id extracts id from /title/<id>."""
        assert _extract_netflix_title_id("https://www.netflix.com/title/81254224") == "81254224"

    def test_extract_title_id_from_watch_url(self):
        """_extract_netflix_title_id extracts id from /watch/<id>."""
        assert _extract_netflix_title_id("https://www.netflix.com/watch/81254224") == "81254224"

    def test_extract_title_id_returns_none(self):
        """_extract_netflix_title_id returns None for non-matching URL."""
        assert _extract_netflix_title_id("https://www.netflix.com/browse") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Forbidden mechanisms tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestForbiddenMechanisms:
    """Verify forbidden mechanisms are never used in simple mode."""

    def test_no_vision_usage(self):
        """No Vision import or call within simple mode flow."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()
        with patch("actions.netflix_control.screen_process", create=True) as mock_sp:
            with patch("actions.netflix_control._verify_playback_started", return_value=True):
                with patch(
                    "actions.netflix_control._verify_title_context_matches", return_value=True
                ):
                    with patch("actions.netflix_control._pause_other_media"):
                        with patch(
                            "actions.netflix_control.click_netflix_play_button_for_current_title",
                            return_value=None,
                        ):
                            with patch("actions.netflix_control._set_playing"):
                                _simple_search_and_play("One Piece", page, sess, auto_play=True)
            mock_sp.assert_not_called()

    def test_no_computer_control_usage(self):
        """No computer_control click within simple mode flow."""
        scored = [
            {
                "text": "One Piece",
                "href": "https://www.netflix.com/title/81254224",
                "score": 1.0,
                "reason": "exact",
                "rect": {"x": 10, "y": 20, "w": 200, "h": 300},
            },
        ]
        page = _make_page(candidates_js=scored, scored_js=scored)
        sess = _make_sess()
        with patch("actions.netflix_control.computer_control", create=True) as mock_cc:
            with patch("actions.netflix_control._verify_playback_started", return_value=True):
                with patch(
                    "actions.netflix_control._verify_title_context_matches", return_value=True
                ):
                    with patch("actions.netflix_control._pause_other_media"):
                        with patch(
                            "actions.netflix_control.click_netflix_play_button_for_current_title",
                            return_value=None,
                        ):
                            with patch("actions.netflix_control._set_playing"):
                                _simple_search_and_play("One Piece", page, sess, auto_play=True)
            if hasattr(mock_cc, "click"):
                mock_cc.click.assert_not_called()
            if hasattr(mock_cc, "screen_click"):
                mock_cc.screen_click.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
