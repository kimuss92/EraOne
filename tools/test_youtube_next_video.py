"""
Tests for YouTube next_video Shift+N keyboard action — Step 14B.2.3.

Covers:
  * next_video calls page.keyboard.press("Shift+N") on owned YouTube tab
  * Router routes "next video" / "next on YouTube" correctly

No real browser calls. No real YouTube/Netflix/network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestYouTubeNextVideoRouting:
    """Router tests for next-video routing."""

    @patch("core.self_healing_router.get_scroll_context")
    def test_next_video_routes_to_youtube(self, mock_scroll_ctx):
        """'next video' → youtube_video next_video with YouTube scroll context."""
        from core.self_healing_router import route_tool_call

        mock_scroll_ctx.return_value = "youtube"
        tool, params = route_tool_call(
            tool="media_coordinator",
            parameters={"action": "play"},
            user_text="next video",
        )
        assert tool == "youtube_video"
        assert params.get("action") == "next_video"

    @patch("core.self_healing_router.get_scroll_context")
    def test_next_on_youtube(self, mock_scroll_ctx):
        """'next on YouTube' → youtube_video next_video."""
        from core.self_healing_router import route_tool_call

        mock_scroll_ctx.return_value = "youtube"
        tool, params = route_tool_call(
            tool="media_coordinator",
            parameters={"action": "play"},
            user_text="next on YouTube",
        )
        assert tool == "youtube_video"
        assert params.get("action") == "next_video"

    @patch("core.self_healing_router.get_scroll_context")
    def test_next_episode_on_netflix_not_youtube(self, mock_scroll_ctx):
        """'next episode on Netflix' must NOT route to YouTube."""
        from core.self_healing_router import route_tool_call

        mock_scroll_ctx.return_value = "netflix"
        tool, params = route_tool_call(
            tool="media_coordinator",
            parameters={"action": "play"},
            user_text="next episode on Netflix",
        )
        assert tool != "youtube_video"

    def test_skip_to_next_video(self):
        """'skip to next video' routes to youtube_video next_video if YouTube context."""
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call(
            tool="media_coordinator",
            parameters={"action": "play"},
            user_text="skip to next video",
        )
        assert tool == "youtube_video"
        assert params.get("action") == "next_video"


class TestYouTubeNextVideoAction:
    """Tests for next_video action handler in youtube_video.py."""

    @patch("actions.platform_tab.get_or_open_platform_tab")
    @patch("actions.browser_control.get_browser_session")
    @patch("core.media_state.enforce_single_playback")
    def test_next_video_calls_shift_n(self, mock_enforce, mock_sess, mock_tab):
        """next_video calls page.keyboard.press('Shift+N')."""
        mock_page = MagicMock()
        mock_tab.return_value.page = mock_page
        from actions.youtube_video import youtube_video

        youtube_video({"action": "next_video"}, player=None)
        assert mock_page.bring_to_front.called
        assert mock_page.keyboard.press.called

    @patch("actions.platform_tab.get_or_open_platform_tab")
    @patch("actions.browser_control.get_browser_session")
    @patch("core.media_state.enforce_single_playback")
    def test_next_video_shift_n_argument(self, mock_enforce, mock_sess, mock_tab):
        """next_video Shift+N argument is correct."""
        mock_page = MagicMock()
        mock_tab.return_value.page = mock_page
        from actions.youtube_video import youtube_video

        youtube_video({"action": "next_video"}, player=None)
        mock_page.keyboard.press.assert_called_with("Shift+N")
