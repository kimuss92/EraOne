"""
Tests for YouTube generic direct-play fast path — Step 14B.2.3.

Covers:
  * Generic query (chill music) triggers fast path before browser search page
  * Fast path scrape success → direct navigate to watch URL
  * Fast path scrape failure → fallback to owned browser search flow
  * Generic play does not list candidates or ask clarification if safe candidate exists

No real browser calls. No real YouTube/Netflix/network calls.

Every collaborator that would otherwise reach the machine is patched:
  _scrape_first_video_url    → live HTTP GET to youtube.com
  _navigate_in_browser       → drives the owned Brave tab
  _verify_youtube_navigation → reads the live session URL
  _get_page_url              → reads the live session URL
  scan_youtube_cards         → evaluates JS in the user's real browser
  spotify_control            → pauses the user's real Spotify

scan_youtube_cards is the one that used to make this docstring a lie: it is
imported function-locally inside _handle_play, so it slipped past the
actions.youtube_video.* patches and read whatever page the user's Brave
happened to be showing.
"""

from __future__ import annotations

from unittest.mock import patch


class TestYouTubeDirectPlay:
    """Tests for _handle_play generic fast path in actions/youtube_video.py."""

    @patch("actions.spotify_control.spotify_control")
    @patch("actions.browser_intelligence.scan_youtube_cards", return_value=[])
    @patch("actions.youtube_video._get_page_url", return_value="")
    @patch("actions.youtube_video._scrape_first_video_url")
    @patch("actions.youtube_video._navigate_in_browser")
    @patch("actions.youtube_video._verify_youtube_navigation")
    @patch("utils.media_match.is_generic_media_query", return_value=True)
    def test_generic_query_fast_path_success(
        self,
        mock_generic,
        mock_verify,
        mock_navigate,
        mock_scrape,
        mock_page_url,
        mock_scan,
        mock_spotify,
    ):
        """Generic query: fast path scrape succeeds → direct navigate to watch URL."""
        from core.browser_grid_state import clear_all_for_tests, clear_scroll_context_for_tests

        clear_all_for_tests()
        clear_scroll_context_for_tests()

        mock_scrape.return_value = "https://www.youtube.com/watch?v=test123"
        mock_verify.return_value = (True, "https://www.youtube.com/watch?v=test123")
        from actions.youtube_video import _handle_play

        result = _handle_play({"query": "chill music"}, None, "brave")
        assert "Playing:" in result
        assert mock_scrape.called
        assert mock_navigate.called
        # The fast path is "fast" precisely because it skips the search page.
        first_target = str(mock_navigate.call_args_list[0].args[0])
        assert first_target == "https://www.youtube.com/watch?v=test123"

    @patch("actions.spotify_control.spotify_control")
    @patch("actions.browser_intelligence.scan_youtube_cards", return_value=[])
    @patch("actions.youtube_video._get_page_url", return_value="")
    @patch("actions.youtube_video._scrape_first_video_url")
    @patch("actions.youtube_video._navigate_in_browser")
    @patch("actions.youtube_video._verify_youtube_navigation")
    @patch("utils.media_match.is_generic_media_query", return_value=True)
    def test_generic_query_fast_path_fallback(
        self,
        mock_generic,
        mock_verify,
        mock_navigate,
        mock_scrape,
        mock_page_url,
        mock_scan,
        mock_spotify,
    ):
        """Generic query: fast path scrape fails → falls back to the search page."""
        from core.browser_grid_state import clear_all_for_tests, clear_scroll_context_for_tests

        clear_all_for_tests()
        clear_scroll_context_for_tests()

        mock_scrape.return_value = None
        from actions.youtube_video import _handle_play

        _handle_play({"query": "chill music"}, None, "brave")
        assert mock_scrape.called
        targets = [str(c.args[0]) if c.args else "" for c in mock_navigate.call_args_list]
        assert targets, "expected a navigation after the fast path failed"
        assert "search_query=chill" in targets[0], (
            f"expected a fallback to the search page, got: {targets}"
        )

    @patch("actions.spotify_control.spotify_control")
    @patch("actions.browser_intelligence.scan_youtube_cards")
    @patch("actions.youtube_video._get_page_url", return_value="")
    @patch("actions.youtube_video._scrape_first_video_url")
    @patch("actions.youtube_video._navigate_in_browser")
    @patch("actions.youtube_video._verify_youtube_navigation")
    @patch("utils.media_match.is_generic_media_query", return_value=False)
    def test_specific_query_no_fast_path(
        self,
        mock_generic,
        mock_verify,
        mock_navigate,
        mock_scrape,
        mock_page_url,
        mock_scan,
        mock_spotify,
    ):
        """A specific query must not direct-play; it must search first.

        WHY THE ASSERTION IS ON THE *FIRST* NAVIGATION

        _handle_play can navigate to a /watch URL on two legitimately different
        routes, and only one of them is a bug for a specific query:

          * the generic fast path (line ~548) navigates straight to a watch URL
            with no search page in between — this is what must never happen for
            a specific query;
          * the normal route navigates to the search page, scans the cards, and
            then opens the chosen result, which IS a /watch navigation and is
            exactly the intended behaviour.

        An earlier version asserted that no navigation target anywhere contained
        "/watch". That conflates the two routes, so the test's verdict depended
        on whether card selection happened to find anything — and because
        scan_youtube_cards was not patched, "anything" meant whatever page the
        user's real Brave session was on. It went red the moment that browser
        was showing an unrelated YouTube video, with no repo change at all.

        Checking that the FIRST navigation is the search page states the real
        invariant, and holds whether or not a result is subsequently opened.

        mock_scrape must return a URL, not None. With None, entering the fast
        path by mistake produces no navigation at all and the regression slips
        through — verified by forcing the is_generic gate open, which this test
        passed until the scrape was given a distinguishable return value.
        """
        from actions.browser_intelligence import Card

        # Realistic scan output, so the specific-query route is actually
        # exercised (search → scan → select) instead of dead-ending on an
        # empty grid.
        mock_scan.return_value = [
            Card(
                index=0,
                title="Luis Fonsi - Despacito ft. Daddy Yankee",
                href="https://www.youtube.com/watch?v=kJQP7kiw5Fk",
                platform="youtube",
            ),
            Card(
                index=1,
                title="Despacito (Remix) - Luis Fonsi ft. Justin Bieber",
                href="https://www.youtube.com/watch?v=72UO0v5ESUo",
                platform="youtube",
            ),
        ]
        # A watch URL only the fast path can produce: it is what the scrape
        # returns, and the scrape feeds no other navigation on this route.
        fast_path_url = "https://www.youtube.com/watch?v=FASTPATHXXX"
        mock_scrape.return_value = fast_path_url
        mock_verify.return_value = (True, "https://www.youtube.com/watch?v=kJQP7kiw5Fk")
        from actions.youtube_video import _handle_play

        _handle_play({"query": "despacito"}, None, "brave")

        assert mock_navigate.called, "expected a navigation for a specific query"
        targets = [str(c.args[0]) if c.args else "" for c in mock_navigate.call_args_list]
        assert "/watch" not in targets[0], (
            f"a specific query direct-played instead of searching: {targets}"
        )
        assert "search_query" in targets[0] or "/results" in targets[0], (
            f"expected a YouTube search navigation first, got: {targets}"
        )
        assert fast_path_url not in targets, (
            f"the scraped fast-path URL was navigated to for a specific query: {targets}"
        )

    def test_chill_music_is_generic(self):
        """'chill music' is detected as generic."""
        from utils.media_match import is_generic_media_query

        assert is_generic_media_query("chill music")

    def test_lofi_is_generic(self):
        """'lofi' is detected as generic."""
        from utils.media_match import is_generic_media_query

        assert is_generic_media_query("lofi")
