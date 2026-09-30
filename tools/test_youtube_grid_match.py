"""
Tests for YouTube description/title grid matching — Step 14B.2.3.

Covers:
  * Description query matches strong recent grid title → auto-play
  * Weak match → ask for video number
  * No grid → ask for search first
  * Threshold is 0.60 (not 0.40)

No real browser calls. No real YouTube/Netflix/network calls.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.browser_grid_state import (
    GridCandidate,
    GridSnapshot,
    clear_all_for_tests,
    clear_scroll_context_for_tests,
    save_grid,
)


class TestYouTubeGridMatch:
    """Tests for description/grid matching in youtube_video grid_play path."""

    @patch("actions.platform_tab.get_or_open_platform_tab")
    @patch("actions.browser_control.get_browser_session")
    @patch("core.media_state.enforce_single_playback")
    def test_strong_title_match_auto_plays(self, mock_enforce, mock_sess, mock_tab):
        """Strong description match from recent grid auto-plays the video."""
        clear_all_for_tests()
        clear_scroll_context_for_tests()
        candidates = [
            GridCandidate(
                visible_order=1,
                title="Storyboard AI film tutorial",
                url="https://www.youtube.com/watch?v=aaa111",
                kind="video",
                safe=True,
                source="youtube",
            ),
            GridCandidate(
                visible_order=2,
                title="Top 10 Python tricks",
                url="https://www.youtube.com/watch?v=bbb222",
                kind="video",
                safe=True,
                source="youtube",
            ),
        ]
        snap = GridSnapshot(
            platform="youtube", query="storyboard", timestamp=time.time(), candidates=candidates
        )
        save_grid(snap)
        mock_page = MagicMock()
        mock_page.url = "https://www.youtube.com/watch?v=aaa111"
        mock_tab.return_value.page = mock_page
        from actions.youtube_video import youtube_video

        result = youtube_video({"action": "grid_play", "title": "storyboard AI film"}, player=None)
        assert "Playing:" in result

    @patch("actions.platform_tab.get_or_open_platform_tab")
    @patch("actions.browser_control.get_browser_session")
    @patch("core.media_state.enforce_single_playback")
    def test_weak_match_asks_for_number(self, mock_enforce, mock_sess, mock_tab):
        """Weak/no match asks for video number, does not auto-play."""
        clear_all_for_tests()
        clear_scroll_context_for_tests()
        candidates = [
            GridCandidate(
                visible_order=1,
                title="Cooking pasta carbonara",
                url="https://www.youtube.com/watch?v=ccc333",
                kind="video",
                safe=True,
                source="youtube",
            ),
            GridCandidate(
                visible_order=2,
                title="Guitar tutorial beginner",
                url="https://www.youtube.com/watch?v=ddd444",
                kind="video",
                safe=True,
                source="youtube",
            ),
        ]
        snap = GridSnapshot(
            platform="youtube", query="space", timestamp=time.time(), candidates=candidates
        )
        save_grid(snap)
        from actions.youtube_video import youtube_video

        result = youtube_video({"action": "grid_play", "title": "quantum physics"}, player=None)
        assert "video number" in str(result).lower()

    @patch("actions.platform_tab.get_or_open_platform_tab")
    @patch("actions.browser_control.get_browser_session")
    def test_no_grid_asks_for_search(self, mock_sess, mock_tab):
        """No grid snapshot asks for search first."""
        clear_all_for_tests()
        mock_page = MagicMock()
        mock_page.url = "https://www.youtube.com/"
        mock_tab.return_value.page = mock_page
        mock_sess_instance = MagicMock()
        mock_sess.return_value = mock_sess_instance
        mock_sess_instance.run.return_value = []
        from actions.youtube_video import youtube_video

        result = youtube_video({"action": "grid_play", "title": "storyboard film"}, player=None)
        assert "search" in str(result).lower()

    def test_threshold_is_060(self):
        """Token overlap of 0.60+ is required for strong match."""
        from utils.media_match import token_overlap_score

        score = token_overlap_score("storyboard film", "How to storyboard an AI film")
        assert score < 0.6
        score = token_overlap_score("storyboard AI film", "storyboard AI film tutorial")
        assert score >= 0.6
