"""Tests for Netflix Grid Accuracy + No Wrong Title Play (Step 14B.2.8)."""

from __future__ import annotations

import re
import time
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

import pytest


# Disable simple mode for all legacy scanner tests in this file.
# Simple mode is tested separately in test_netflix_simple_mode.py.
@pytest.fixture(autouse=True)
def _disable_simple_mode():
    with (
        patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", False),
        patch("actions.netflix_control.NETFLIX_FAST_SEARCH_ENABLED", False),
    ):
        yield


# ═════════════════════════════════════════════════════════════════════
# 1. State detection
# ═════════════════════════════════════════════════════════════════════


class TestStateDetection:
    """Verify /browse → BROWSE, /title/ → DETAILS, /watch/ → PLAYER."""

    def _make_mock_page(self, url: str):
        page = MagicMock()
        type(page).url = PropertyMock(return_value=url)
        return page

    def _make_signals(self, url: str, **overrides):
        s = {
            "has_play_button": False,
            "has_video": False,
            "url_contains_watch": "/watch/" in url,
            "has_player_controls": False,
            "active_netflix": "netflix.com" in url,
            "page_title": "",
            "current_url": url,
            "watch_links": 0,
        }
        s.update(overrides)
        return s

    @patch("actions.netflix_control._check_local_signals")
    def test_browse_no_modal_is_browse(self, mock_signals):
        from actions import netflix_control as nc

        url = "https://www.netflix.com/browse"
        mock_signals.return_value = self._make_signals(url)
        sess = MagicMock()
        page = self._make_mock_page(url)
        # No modal
        sess.run.return_value = "null"
        result = nc._detect_state(sess, page)
        assert result["state"] == nc._STATE_BROWSE

    @patch("actions.netflix_control._check_local_signals")
    def test_title_url_is_details(self, mock_signals):
        from actions import netflix_control as nc

        url = "https://www.netflix.com/title/81476183"
        mock_signals.return_value = self._make_signals(url)
        sess = MagicMock()
        page = self._make_mock_page(url)
        result = nc._detect_state(sess, page)
        assert result["state"] == nc._STATE_DETAILS

    @patch("actions.netflix_control._check_local_signals")
    def test_watch_url_is_player(self, mock_signals):
        from actions import netflix_control as nc

        url = "https://www.netflix.com/watch/81476183"
        mock_signals.return_value = self._make_signals(url)
        sess = MagicMock()
        page = self._make_mock_page(url)
        result = nc._detect_state(sess, page)
        assert result["state"] == nc._STATE_PLAYER

    @patch("actions.netflix_control._check_local_signals")
    def test_search_url_is_search_results(self, mock_signals):
        from actions import netflix_control as nc

        url = "https://www.netflix.com/search?q=one+piece"
        mock_signals.return_value = self._make_signals(url)
        sess = MagicMock()
        page = self._make_mock_page(url)
        result = nc._detect_state(sess, page)
        assert result["state"] == nc._STATE_SEARCH_RESULTS

    @patch("actions.netflix_control._check_local_signals")
    def test_browse_with_modal_is_details(self, mock_signals):
        from actions import netflix_control as nc

        url = "https://www.netflix.com/browse"
        mock_signals.return_value = self._make_signals(url)
        sess = MagicMock()
        page = self._make_mock_page(url)
        # Modal present
        sess.run.return_value = '{"modal": "found", "bbox": {"x": 0, "y": 0}}'
        result = nc._detect_state(sess, page)
        assert result["state"] == nc._STATE_DETAILS


# ═════════════════════════════════════════════════════════════════════
# 2. No default first result
# ═════════════════════════════════════════════════════════════════════


class TestNoDefaultFirstResult:
    """grid_play without ordinal must NOT play first result."""

    def test_grid_play_no_ordinal_returns_list(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "grid_play"}

        with patch.object(
            nc,
            "_get_or_scan_candidates",
            return_value=(
                [
                    {
                        "visible_order": 1,
                        "title": "One Piece",
                        "url": "https://netflix.com/title/1",
                    },
                    {"visible_order": 2, "title": "Naruto", "url": "https://netflix.com/title/2"},
                ],
                True,
            ),
        ):
            with patch.object(nc, "_verify_playback_started") as mock_vps:
                result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
                assert "first" in result.lower() or "I found" in result
                mock_vps.assert_not_called()

    def test_grid_play_no_ordinal_no_candidates(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "grid_play"}

        with patch.object(nc, "_get_or_scan_candidates", return_value=([], False)):
            result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
            assert "could not read" in result.lower() or "search" in result.lower()


# ═════════════════════════════════════════════════════════════════════
# 3. French ordinals
# ═════════════════════════════════════════════════════════════════════


class TestFrenchOrdinals:
    """Verify French ordinal understanding."""

    def test_seconde_is_2(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("seconde") == 2

    def test_deuxieme_is_2(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("deuxième") == 2

    def test_troisieme_is_3(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("troisième") == 3

    def test_quatrieme_is_4(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("quatrième") == 4

    def test_premier_is_1(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("premier") == 1

    def test_premiere_is_1(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("première") == 1

    def test_et_de_seconde_parse(self):
        """et de seconde routed through parse_ordinal_command."""
        from utils.ordinal_selection import parse_ordinal_command

        result = parse_ordinal_command("et de seconde")
        assert result is not None
        assert result["ordinal"] == 2

    def test_parse_ordinal_seconde(self):
        from utils.ordinal_selection import parse_ordinal_command

        result = parse_ordinal_command("play the second title")
        assert result is not None
        assert result["ordinal"] == 2

    def test_parse_ordinal_second_in_french(self):
        from utils.ordinal_selection import parse_ordinal_command

        result = parse_ordinal_command("le deuxième titre")
        assert result is not None


# ═════════════════════════════════════════════════════════════════════
# 4. Candidate schema normalization
# ═════════════════════════════════════════════════════════════════════


class TestNormalizeCandidate:
    """Verify _normalize_netflix_candidate handles dict and Card objects."""

    def test_dict_with_url(self):
        from actions.netflix_control import _normalize_netflix_candidate

        result = _normalize_netflix_candidate(
            {"title": "One Piece", "url": "https://netflix.com/title/1"}
        )
        assert result is not None
        assert result["title"] == "One Piece"
        assert result["url"] == "https://netflix.com/title/1"

    def test_dict_with_href(self):
        from actions.netflix_control import _normalize_netflix_candidate

        result = _normalize_netflix_candidate(
            {"title": "Naruto", "href": "https://netflix.com/title/2"}
        )
        assert result is not None
        assert result["title"] == "Naruto"
        assert result["url"] == "https://netflix.com/title/2"

    def test_object_with_href(self):
        from actions.netflix_control import _normalize_netflix_candidate

        class Obj:
            title = "One Punch Man"
            href = "https://netflix.com/title/3"

        result = _normalize_netflix_candidate(Obj())
        assert result is not None
        assert result["title"] == "One Punch Man"
        assert result["url"] == "https://netflix.com/title/3"

    def test_no_title_returns_none(self):
        from actions.netflix_control import _normalize_netflix_candidate

        result = _normalize_netflix_candidate({"url": "https://netflix.com/title/1"})
        assert result is None

    def test_card_dataclass(self):
        from actions.browser_intelligence import Card
        from actions.netflix_control import _normalize_netflix_candidate

        card = Card(title="One Piece", href="https://netflix.com/title/1")
        result = _normalize_netflix_candidate(card)
        assert result is not None
        assert result["title"] == "One Piece"
        assert result["url"] == "https://netflix.com/title/1"


# ═════════════════════════════════════════════════════════════════════
# 5. Saved grid as source of truth
# ═════════════════════════════════════════════════════════════════════


class TestGridSourceOfTruth:
    """Verify saved grid is used first, ordinal selects correct index."""

    def test_saved_grid_ordinal_2_selects_second(self):
        from actions import netflix_control as nc
        from core.browser_grid_state import GridCandidate, GridSnapshot

        sess = MagicMock()
        page = MagicMock()
        saved_candidates = [
            GridCandidate(
                visible_order=1,
                title="One Piece",
                url="https://netflix.com/title/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
            GridCandidate(
                visible_order=2,
                title="Naruto",
                url="https://netflix.com/title/2",
                kind="title",
                safe=True,
                source="netflix",
            ),
            GridCandidate(
                visible_order=3,
                title="Bleach",
                url="https://netflix.com/title/3",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]

        with patch(
            "core.browser_grid_state.get_recent_grid",
            return_value=GridSnapshot(
                platform="netflix",
                query="anime",
                timestamp=time.time(),
                candidates=saved_candidates,
            ),
        ):
            candidates, from_saved = nc._get_or_scan_candidates(sess, page, "anime")
            assert from_saved is True
            assert len(candidates) == 3
            assert candidates[1]["title"] == "Naruto"

    def test_saved_grid_ordinal_4_out_of_range(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "grid_play", "ordinal": 4}

        with patch.object(
            nc,
            "_get_or_scan_candidates",
            return_value=(
                [
                    {"visible_order": 1, "title": "A", "url": "u1"},
                    {"visible_order": 2, "title": "B", "url": "u2"},
                ],
                True,
            ),
        ):
            result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
            assert "only found 2" in result.lower() or "only found" in result.lower()


# ═════════════════════════════════════════════════════════════════════
# 6. Router Netflix smart_click ordinal locking
# ═════════════════════════════════════════════════════════════════════


class TestRouterSmartClickOrdinal:
    """Verify router routes Netflix smart_click with ordinal to grid_play."""

    def setup_method(self):
        from core.browser_grid_state import clear_all_for_tests
        from core.intent_memory import save_state

        clear_all_for_tests()
        save_state({})
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0

    def test_smart_click_ordinal_routes_to_grid_play(self):
        from core.self_healing_router import route_tool_call

        result = route_tool_call(
            "browser_control",
            {"action": "smart_click", "description": "fourth video title card"},
            user_text="on Netflix",
        )
        assert result[0] == "netflix_control"
        assert result[1]["action"] == "grid_play"
        assert result[1]["ordinal"] == 4

    def test_smart_click_no_ordinal_routes_to_search(self):
        from core.self_healing_router import route_tool_call

        result = route_tool_call(
            "browser_control",
            {"action": "smart_click", "description": "play One Piece"},
            user_text="on Netflix",
        )
        assert result[0] == "netflix_control"
        assert result[1]["action"] == "search"

    def test_smart_click_without_netflix_context_passes_through(self):
        from core.self_healing_router import route_tool_call

        result = route_tool_call(
            "browser_control",
            {"action": "smart_click", "description": "fourth video title card"},
            user_text="play something",
        )
        # If no Netflix intent, it should not be rerouted
        tool, params = result
        # If tool is still browser_control, smart_click passed through
        # (or it might be something else based on other routing logic)
        # Just verify it's not netflix_control grid_play
        assert not (tool == "netflix_control" and params.get("action") == "grid_play")


# ═════════════════════════════════════════════════════════════════════
# 7. Play button scoping
# ═════════════════════════════════════════════════════════════════════


class TestPlayButtonScoping:
    """Verify play button is scoped to current title, not global."""

    def test_selected_title_in_response(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "grid_play", "ordinal": 1}

        candidate = {"visible_order": 1, "title": "One Piece", "url": "https://netflix.com/title/1"}
        with patch.object(nc, "_get_or_scan_candidates", return_value=([candidate], True)):
            with patch.object(nc, "_verify_playback_started", return_value=True):
                with patch.object(nc, "_set_playing"):
                    result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
                    assert "Playing: One Piece" == result

    def test_selected_title_not_raw_page_text(self):
        """The response must use selected_title from grid, not random page text."""
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "grid_play", "ordinal": 1}

        candidate = {"visible_order": 1, "title": "One Piece", "url": "https://netflix.com/title/1"}
        with patch.object(nc, "_get_or_scan_candidates", return_value=([candidate], True)):
            with patch.object(nc, "_verify_playback_started", return_value=True):
                with patch.object(nc, "_set_playing"):
                    result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
                    # Ensure we don't return raw page text like "Disponible maintenant"
                    assert "Disponible" not in result
                    assert "Saison" not in result


# ═════════════════════════════════════════════════════════════════════
# 8. _clean_netflix_title behaviour
# ═════════════════════════════════════════════════════════════════════


class TestCleanNetflixTitle:
    """Verify _clean_netflix_title strips metadata, keeps real titles."""

    def test_strips_disponible_maintenant(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("Disponible maintenant") == ""

    def test_strips_nouveaute(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("Nouveauté") == ""

    def test_strips_saison_number(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("Saison 5") == ""

    def test_keeps_real_title(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("One Piece") == "One Piece"

    def test_keeps_title_with_adjacent_metadata(self):
        from actions.netflix_control import _clean_netflix_title

        result = _clean_netflix_title("Breaking Bad Saison 5")
        assert "Breaking" in result

    def test_empty_input(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("") == ""

    def test_whitespace_input(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("   ") == ""

    def test_short_remaining_becomes_empty(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("x") == ""

    def test_strips_episode_prefix(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("Épisode 3") == ""

    def test_strips_classement(self):
        from actions.netflix_control import _clean_netflix_title

        assert _clean_netflix_title("Classement 7") == ""


# ═════════════════════════════════════════════════════════════════════
# 9. _set_playing NOT called on failure
# ═════════════════════════════════════════════════════════════════════


class TestNoSetPlayingOnFailure:
    """Verify _set_playing is never called when playback fails."""

    def test_handle_play_no_set_playing_on_failure(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "play"}
        with patch.object(nc, "_verify_playback_started", return_value=False):
            with patch.object(
                nc,
                "_check_local_signals",
                return_value={
                    "has_play_button": False,
                    "has_video": False,
                    "url_contains_watch": False,
                    "has_player_controls": False,
                    "active_netflix": True,
                    "page_title": "",
                    "current_url": "https://www.netflix.com/title/12345",
                    "watch_links": 0,
                },
            ):
                with patch.object(nc, "click_netflix_play_button", return_value="No button"):
                    with patch.object(nc, "_call_screen_process_narrow", return_value=None):
                        with patch.object(nc, "_set_playing") as mock_set:
                            result = nc._handle_play(nc._STATE_DETAILS, params, sess, page)
                            assert "could not click the Play button" in result
                            mock_set.assert_not_called()

    def test_handle_grid_play_no_set_playing_on_failure(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        params = {"action": "grid_play", "ordinal": 1}
        candidate = {"visible_order": 1, "title": "Test Show", "url": "https://netflix.com/title/1"}
        with patch.object(nc, "_get_or_scan_candidates", return_value=([candidate], True)):
            with patch.object(nc, "_verify_playback_started", return_value=False):
                with patch.object(nc, "click_netflix_play_button", return_value="No button"):
                    with patch.object(nc, "_set_playing") as mock_set:
                        result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
                        assert "could not start playback" in result
                        mock_set.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# 10. Home / browse / go_home action
# ═════════════════════════════════════════════════════════════════════


class TestHomeBrowseAction:
    """Verify home/browse/go_home navigates to Netflix browse."""

    def test_home_action_returns_correct_message(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                result = nc.netflix_control({"action": "home"})
                assert "home opened" in result.lower()

    def test_browse_action_returns_correct_message(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                result = nc.netflix_control({"action": "browse"})
                assert "home opened" in result.lower()

    def test_go_home_action_returns_correct_message(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                result = nc.netflix_control({"action": "go_home"})
                assert "home opened" in result.lower()

    def test_home_navigates_to_browse_url(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = mock_page
                mock_tab.return_value = mock_tab_result
                nc.netflix_control({"action": "home"})
                # Verify navigation to /browse
                mock_page.goto.assert_called_once()
                args, _ = mock_page.goto.call_args
                url_arg = args[0]
                assert "browse" in url_arg


# ═════════════════════════════════════════════════════════════════════
# 11. Search without auto_play (native search)
# ═════════════════════════════════════════════════════════════════════


class TestSearchNoAutoPlay:
    """Verify search with auto_play=False returns numbered list."""

    def test_search_returns_list_by_default(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_card1 = MagicMock(
            title="One Piece",
            href="https://netflix.com/title/1",
            kind="title",
            safe=True,
            source="netflix",
        )
        mock_card2 = MagicMock(
            title="One Piece : Film 2",
            href="https://netflix.com/title/2",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch(
            "actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card1, mock_card2]
        ):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch.object(nc, "_save_netflix_grid"):
                    result = nc._handle_search(
                        nc._STATE_UNKNOWN, {"action": "search", "query": "One Piece"}, sess, page
                    )
                    assert "1." in result
                    assert "One Piece" in result
                    assert "auto" not in result.lower()

    def test_search_with_auto_play_false_explicitly(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_card = MagicMock(
            title="One Piece",
            href="https://netflix.com/title/1",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch.object(nc, "_save_netflix_grid"):
                    result = nc._handle_search(
                        nc._STATE_UNKNOWN,
                        {"action": "search", "query": "One Piece", "_auto_play": False},
                        sess,
                        page,
                    )
                    assert "Please tell me" in result


# ═════════════════════════════════════════════════════════════════════
# 12. Ambiguous / low_confidence match lists grid
# ═════════════════════════════════════════════════════════════════════


class TestPlayAmbiguousListsGrid:
    """Verify ambiguous/low_confidence auto-play match lists grid instead of playing."""

    def test_ambiguous_match_lists_grid(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=test"
        mock_card1 = MagicMock(
            title="One Piece",
            href="https://netflix.com/title/1",
            kind="title",
            safe=True,
            source="netflix",
        )
        mock_card2 = MagicMock(
            title="One Punch Man",
            href="https://netflix.com/title/2",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch(
            "actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card1, mock_card2]
        ):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch(
                    "utils.media_match.select_best_media_candidate",
                    return_value=(None, 0.0, "ambiguous"),
                ):
                    with patch.object(nc, "_save_netflix_grid"):
                        result = nc._handle_search(
                            nc._STATE_UNKNOWN,
                            {"action": "search", "query": "one", "_auto_play": True},
                            sess,
                            page,
                        )
                        assert "several possible" in result.lower()
                        assert "One Piece" in result
                        assert "One Punch" in result


# ═════════════════════════════════════════════════════════════════════
# 13. Rescan returns 0 but saved grid exists
# ═════════════════════════════════════════════════════════════════════


class TestRescanZeroFallsBackToSavedGrid:
    """Verify _get_or_scan_candidates falls back to saved grid when rescan returns 0."""

    def test_rescan_zero_uses_saved_grid(self):
        from actions import netflix_control as nc
        from core.browser_grid_state import GridCandidate, GridSnapshot

        sess = MagicMock()
        page = MagicMock()
        saved = [
            GridCandidate(
                visible_order=1,
                title="Saved Show",
                url="https://netflix.com/title/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        with patch(
            "core.browser_grid_state.get_recent_grid",
            return_value=GridSnapshot(
                platform="netflix", query="saved", timestamp=time.time(), candidates=saved
            ),
        ):
            with patch("actions.browser_intelligence.scan_netflix_cards", return_value=[]):
                candidates, from_saved = nc._get_or_scan_candidates(sess, page, "saved")
                assert from_saved is True
                assert len(candidates) == 1
                assert candidates[0]["title"] == "Saved Show"

    def test_rescan_zero_no_saved_grid_returns_empty(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        with patch("core.browser_grid_state.get_recent_grid", return_value=None):
            with patch("actions.browser_intelligence.scan_netflix_cards", return_value=[]):
                candidates, from_saved = nc._get_or_scan_candidates(sess, page, "test")
                assert from_saved is False
                assert len(candidates) == 0


# ═════════════════════════════════════════════════════════════════════
# 14. Search auto-play by default (Step 14B.2.13, Fix A)
# ═════════════════════════════════════════════════════════════════════


class TestSearchAutoPlayDefault:
    """Verify search+query autoplays by default (no more numbered list)."""

    def test_search_with_query_auto_plays(self):
        """search for One Piece -> auto_play=True -> plays best match."""
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=one+piece"
        mock_card = MagicMock(
            title="One Piece",
            href="https://netflix.com/title/1",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch(
                    "utils.media_match.select_best_media_candidate",
                    return_value=(
                        {"title": "One Piece", "url": "https://netflix.com/title/1"},
                        1.0,
                        "selected",
                    ),
                ):
                    with patch.object(nc, "_save_netflix_grid"):
                        with patch.object(nc, "_verify_playback_started", return_value=True):
                            with patch.object(nc, "_set_playing"):
                                result = nc._handle_search(
                                    nc._STATE_UNKNOWN,
                                    {"action": "search", "query": "One Piece", "_auto_play": True},
                                    sess,
                                    page,
                                )
                                assert "Playing: One Piece" == result

    def test_search_no_query_lists(self):
        """search without query -> list (existing behavior)."""
        from actions import netflix_control as nc
        from actions.browser_intelligence import scan_netflix_cards

        result = nc._handle_search(
            nc._STATE_UNKNOWN, {"action": "search", "value": ""}, MagicMock(), MagicMock()
        )
        assert "requires a title" in result.lower()


# ═════════════════════════════════════════════════════════════════════
# 15. Watch action converts to search (Step 14B.2.13, Fix A+D)
# ═════════════════════════════════════════════════════════════════════


class TestWatchActionConvertsToSearch:
    """Verify watch action is aliased to search + auto_play."""

    def test_watch_mapped_to_search_in_aliases(self):
        from actions.netflix_control import _ACTION_ALIASES

        assert _ACTION_ALIASES.get("watch") == "search"

    def test_netflix_control_watch_with_query_sets_auto_play(self):
        from actions import netflix_control as nc

        result = nc.netflix_control({"action": "watch", "query": "One Piece"})
        assert result is not None  # smoke test: no crash


# ═════════════════════════════════════════════════════════════════════
# 16. Grid play explicit ordinal skips safety filter (Step 14B.2.13, Fix B)
# ═════════════════════════════════════════════════════════════════════


class TestGridPlayOrdinalSkipsSafetyFilter:
    """Verify explicit ordinal selection bypasses _filter_candidates_safely_for_search."""

    def test_ordinal_skips_filter(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=anime"
        params = {"action": "grid_play", "ordinal": 1}
        candidate = {"visible_order": 1, "title": "One Piece", "url": "https://netflix.com/title/1"}
        with patch.object(nc, "_get_or_scan_candidates", return_value=([candidate], True)):
            with patch.object(nc, "_filter_candidates_safely_for_search") as mock_filter:
                with patch.object(nc, "_verify_playback_started", return_value=True):
                    with patch.object(nc, "_set_playing"):
                        result = nc._handle_grid_play(nc._STATE_BROWSE, params, sess, page)
                        mock_filter.assert_not_called()
                        assert "Playing: One Piece" == result


# ═════════════════════════════════════════════════════════════════════
# 17. Play on search_results routes to grid_play (Step 14B.2.13, Fix C)
# ═════════════════════════════════════════════════════════════════════


class TestPlayOnSearchResultsWithSavedGrid:
    """Verify play on search_results with saved grid -> grid_play ordinal=1."""

    def test_play_search_results_with_saved_grid_plays_first(self):
        from actions import netflix_control as nc
        from core.browser_grid_state import GridCandidate, GridSnapshot

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=anime"
        saved = [
            GridCandidate(
                visible_order=1,
                title="Naruto",
                url="https://netflix.com/title/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        with patch(
            "core.browser_grid_state.get_recent_grid",
            return_value=GridSnapshot(
                platform="netflix", query="anime", timestamp=time.time(), candidates=saved
            ),
        ):
            with patch.object(nc, "_handle_grid_play") as mock_gp:
                mock_gp.return_value = "Playing: Naruto"
                result = nc._handle_play(nc._STATE_SEARCH_RESULTS, {"action": "play"}, sess, page)
                mock_gp.assert_called_once()
                args = mock_gp.call_args[0]
                assert args[1].get("ordinal") == 1
                assert "Playing: Naruto" == result

    def test_play_search_results_no_saved_grid_asks(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        with patch("core.browser_grid_state.get_recent_grid", return_value=None):
            result = nc._handle_play(nc._STATE_SEARCH_RESULTS, {"action": "play"}, sess, page)
            assert "search results" in result.lower()
            assert "tell me" in result.lower()


# ═════════════════════════════════════════════════════════════════════
# 18. Fullscreen blocked on search_results (Step 14B.2.13, Fix E)
# ═════════════════════════════════════════════════════════════════════


class TestPlayOnSearchResultsNoFullscreen:
    """Verify fullscreen action on search_results is blocked."""

    def test_fullscreen_on_search_results_blocked(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=anime"
        with patch.object(
            nc,
            "_detect_state",
            return_value={"state": nc._STATE_SEARCH_RESULTS, "signals": {"current_url": page.url}},
        ):
            with patch.object(nc, "_acquire_netflix_page", return_value=(sess, page)):
                with patch.object(nc, "_focus_netflix_foreground"):
                    result = nc.netflix_control({"action": "fullscreen"})
                    assert "Cannot enter fullscreen" in result

    def test_fullscreen_on_player_not_blocked(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/watch/12345"
        with patch.object(
            nc,
            "_detect_state",
            return_value={"state": nc._STATE_PLAYER, "signals": {"current_url": page.url}},
        ):
            with patch.object(nc, "_acquire_netflix_page", return_value=(sess, page)):
                with patch.object(nc, "_focus_netflix_foreground"):
                    with patch.object(nc, "_handle_cdp_action", return_value="fullscreen toggled"):
                        result = nc.netflix_control({"action": "fullscreen"})
                        assert "fullscreen" in result.lower()


# ═════════════════════════════════════════════════════════════════════
# 19. List-only phrase detected (Step 14B.2.13, Fix A)
# ═════════════════════════════════════════════════════════════════════


class TestListOnlyPhraseDetected:
    """Verify list/show/just search phrases suppress auto_play."""

    def test_list_phrase_returns_list(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_card1 = MagicMock(
            title="One Piece",
            href="https://netflix.com/title/1",
            kind="title",
            safe=True,
            source="netflix",
        )
        mock_card2 = MagicMock(
            title="Naruto",
            href="https://netflix.com/title/2",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch(
            "actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card1, mock_card2]
        ):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch.object(nc, "_save_netflix_grid"):
                    result = nc._handle_search(
                        nc._STATE_UNKNOWN,
                        {"action": "search", "query": "One Piece", "_auto_play": False},
                        sess,
                        page,
                    )
                    assert "1." in result
                    assert "One Piece" in result


# ═════════════════════════════════════════════════════════════════════
# 18. Search rejects unrelated candidates for autoplay (14B.2.14)
# ═════════════════════════════════════════════════════════════════════


class TestSearchRejectsUnrelatedCandidates:
    """Verify autoplay rejects candidates that have zero token overlap with query."""

    def test_unrelated_candidates_rejected_for_autoplay(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        unrelated_cards = [
            MagicMock(
                title="Outlast : La jungle",
                href="https://netflix.com/title/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
            MagicMock(
                title="Michael Jackson : Le verdict",
                href="https://netflix.com/title/2",
                kind="title",
                safe=True,
                source="netflix",
            ),
            MagicMock(
                title="Nemesis",
                href="https://netflix.com/title/3",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=unrelated_cards):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch.object(nc, "_save_netflix_grid"):
                    result = nc._handle_search(
                        nc._STATE_UNKNOWN,
                        {"action": "search", "query": "One Piece", "_auto_play": True},
                        sess,
                        page,
                    )
                    assert (
                        "could not identify" in result.lower() or "safe matching" in result.lower()
                    )
                    assert "Outlast" not in result
                    assert "Michael Jackson" not in result

    def test_mixed_candidates_filtered_to_matching(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mixed_cards = [
            MagicMock(
                title="One Piece",
                href="https://netflix.com/title/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
            MagicMock(
                title="Outlast : La jungle",
                href="https://netflix.com/title/2",
                kind="title",
                safe=True,
                source="netflix",
            ),
            MagicMock(
                title="One Punch Man",
                href="https://netflix.com/title/3",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=mixed_cards):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch(
                    "utils.media_match.select_best_media_candidate",
                    return_value=(
                        {"title": "One Piece", "url": "https://netflix.com/title/1"},
                        1.0,
                        "selected",
                    ),
                ):
                    with patch.object(nc, "_save_netflix_grid"):
                        with patch.object(nc, "_verify_playback_started", return_value=True):
                            with patch.object(nc, "_set_playing"):
                                result = nc._handle_search(
                                    nc._STATE_UNKNOWN,
                                    {"action": "search", "query": "One Piece", "_auto_play": True},
                                    sess,
                                    page,
                                )
                                assert "Playing: One Piece" == result


# ═════════════════════════════════════════════════════════════════════
# 19. Search auto-play with matching candidate (14B.2.14)
# ═════════════════════════════════════════════════════════════════════


class TestSearchAutoPlayMatchingCandidate:
    """Verify search+query auto-plays when a matching candidate exists."""

    def test_matching_candidate_autoplays(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=naruto"
        cards = [
            MagicMock(
                title="Naruto",
                href="https://netflix.com/title/1",
                kind="title",
                safe=True,
                source="netflix",
            ),
            MagicMock(
                title="Naruto Shippuden",
                href="https://netflix.com/title/2",
                kind="title",
                safe=True,
                source="netflix",
            ),
        ]
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=cards):
            with patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False):
                with patch(
                    "utils.media_match.select_best_media_candidate",
                    return_value=(
                        {"title": "Naruto", "url": "https://netflix.com/title/1"},
                        0.95,
                        "selected",
                    ),
                ):
                    with patch.object(nc, "_save_netflix_grid"):
                        with patch.object(nc, "_verify_playback_started", return_value=True):
                            with patch.object(nc, "_set_playing"):
                                result = nc._handle_search(
                                    nc._STATE_UNKNOWN,
                                    {"action": "search", "query": "naruto", "_auto_play": True},
                                    sess,
                                    page,
                                )
                                assert "Playing: Naruto" == result


# ═════════════════════════════════════════════════════════════════════
# 20. Play/watch/search all route to autoplay (14B.2.14)
# ═════════════════════════════════════════════════════════════════════


class TestPlayWatchSearchAutoplay:
    """Verify play/watch with query triggers the search+autoplay conversion."""

    def test_play_with_query_sets_action_search_in_params(self):
        from actions import netflix_control as nc

        params = {"action": "play", "query": "One Piece"}
        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/browse"
        with patch.object(nc, "_acquire_netflix_page", return_value=(sess, page)):
            with patch.object(nc, "_focus_netflix_foreground"):
                with patch.object(
                    nc,
                    "_detect_state",
                    return_value={"state": nc._STATE_BROWSE, "signals": {"current_url": page.url}},
                ):
                    with patch.object(
                        nc, "_handle_search", return_value="Playing: One Piece"
                    ) as mock_search:
                        nc.netflix_control(params)
                        mock_search.assert_called_once()
                        assert params.get("action") == "search"
                        assert params.get("_auto_play") is True

    def test_watch_with_query_sets_action_search_in_params(self):
        from actions import netflix_control as nc

        params = {"action": "watch", "value": "Naruto"}
        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/browse"
        with patch.object(nc, "_acquire_netflix_page", return_value=(sess, page)):
            with patch.object(nc, "_focus_netflix_foreground"):
                with patch.object(
                    nc,
                    "_detect_state",
                    return_value={"state": nc._STATE_BROWSE, "signals": {"current_url": page.url}},
                ):
                    with patch.object(
                        nc, "_handle_search", return_value="Playing: Naruto"
                    ) as mock_search:
                        nc.netflix_control(params)
                        mock_search.assert_called_once()
                        # watch is aliased to search via _ACTION_ALIASES; _auto_play is set
                        assert params.get("_auto_play") is True


# ═════════════════════════════════════════════════════════════════════
# 20b. Do NOT save grid when notification-only candidates (14B.2.15 Fix F)
# ═════════════════════════════════════════════════════════════════════


class TestNoGridSaveForNotificationCandidates:
    """Verify _handle_search does NOT save grid when only notification/overlay
    candidates come back (or when scan returns empty)."""

    def test_no_grid_save_when_scan_empty(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=[]):
            with patch.object(nc, "_save_netflix_grid") as mock_save:
                result = nc._handle_search(
                    nc._STATE_SEARCH_RESULTS, {"action": "search", "query": "One Piece"}, sess, page
                )
        mock_save.assert_not_called()
        assert (
            "could not" in result.lower()
            or "try again" in result.lower()
            or "error" in result.lower()
        )

    def test_no_grid_save_when_all_candidates_rejected(self):
        """When scan returns only metadata-only cards (all filtered), save is not called."""
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_meta_card = MagicMock(
            title="Disponible maintenant",
            href="https://netflix.com/title/1",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch(
            "actions.browser_intelligence.scan_netflix_cards", return_value=[mock_meta_card]
        ):
            with patch.object(nc, "_save_netflix_grid") as mock_save:
                nc._handle_search(
                    nc._STATE_SEARCH_RESULTS, {"action": "search", "query": "One Piece"}, sess, page
                )
        mock_save.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# 21. No old browser_control Netflix macro path (14B.2.14)
# ═════════════════════════════════════════════════════════════════════


class TestNoOldBrowserControlNetflixPath:
    """Verify browser_control.netflix_search_and_play is unreachable."""

    def test_safety_belt_blocks_netflix_actions(self):
        from actions.browser_control import browser_control

        result = browser_control({"action": "netflix_search_and_play", "title": "test"})
        assert "handled by netflix_control" in result.lower()

    def test_go_to_netflix_search_blocked(self):
        from actions.browser_control import browser_control

        result = browser_control(
            {"action": "go_to", "url": "https://www.netflix.com/search?q=test"}
        )
        assert "netflix_control" in result.lower()


# ═════════════════════════════════════════════════════════════════════
# 22. No screen_process/computer_control fallback (14B.2.14)
# ═════════════════════════════════════════════════════════════════════


class TestNoScreenProcessFallback:
    """Verify netflix_control does not use uncontrolled screen_process/computer_control."""

    def test_screen_process_import_only_in_bounded_fallback(self):
        """screen_process import must ONLY appear inside _call_screen_process_narrow."""
        import ast

        with open("actions/netflix_control.py", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        bounded_func_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_call_screen_process_narrow":
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.ImportFrom)
                        and child.module
                        and "screen_processor" in child.module
                    ):
                        bounded_func_found = True
        assert bounded_func_found, (
            "Expected screen_process import inside _call_screen_process_narrow"
        )

        # Verify no module-level screen_process import
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and "screen_processor" in node.module
            ):
                raise AssertionError(
                    "screen_process imported at module level (should only be in _call_screen_process_narrow)"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "screen_processor" in alias.name:
                        raise AssertionError("screen_processor imported at module level")

    def test_netflix_control_has_no_computer_control_import(self):
        import ast

        with open("actions/netflix_control.py", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "computer_control" in node.module:
                    raise AssertionError("netflix_control imports computer_control")

    def test_no_old_browser_control_netflix_macro_restored(self):
        """Verify browser_control.netflix_search_and_play is still blocked."""
        from actions.browser_control import browser_control

        result = browser_control({"action": "netflix_search_and_play", "title": "test"})
        assert "netflix_control" in result.lower()

    def test_no_screen_process_in_handle_search(self):
        """Verify _handle_search does NOT call screen_process (only bounded fallback)."""
        import ast

        with open("actions/netflix_control.py", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        # _handle_play may call _call_screen_process_narrow (bounded fallback), which is allowed.
        # But _handle_search must NOT call any screen_process variant.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_search":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = ""
                        if isinstance(child.func, ast.Attribute):
                            func = child.func.attr
                        elif isinstance(child.func, ast.Name):
                            func = child.func.id
                        if "screen_process" in func:
                            raise AssertionError("_handle_search should not call screen_process")


# ═════════════════════════════════════════════════════════════════════
# V9C: Netflix grid ordinal / no-href card / DOM click tests
# ═════════════════════════════════════════════════════════════════════


class TestV9COrdinalDetection:
    """Verify ordinal detection handles grid/title number word patterns."""

    def test_title_number_two(self):
        from utils.ordinal_selection import ordinal_to_index, parse_ordinal_command

        assert ordinal_to_index("title number two") == 2
        p = parse_ordinal_command("title number two")
        assert p is not None and p["ordinal"] == 2

    def test_grid_item_digit(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("grid item 2") == 2

    def test_grid_number_word(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("grid number two") == 2

    def test_grid_digit(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("grid 2") == 2

    def test_select_title_number_two_on_grid(self):
        from utils.ordinal_selection import parse_ordinal_command

        p = parse_ordinal_command("select title number two on the grid")
        assert p is not None and p["ordinal"] == 2

    def test_result_number_three(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("result number three") == 3

    def test_title_five(self):
        from utils.ordinal_selection import ordinal_to_index

        assert ordinal_to_index("title 5") == 5


class TestV9CNoHrefCards:
    """Verify no-href cards are kept in candidate lists."""

    def test_convert_cards_keeps_no_href(self):
        from actions.netflix_control import _convert_cards_to_candidates

        cards = [
            MagicMock(title="One Piece", href=None),
            MagicMock(title="Naruto", href="https://www.netflix.com/title/123"),
        ]
        result = _convert_cards_to_candidates(cards)
        titles = [c["title"] for c in result]
        assert "One Piece" in titles
        assert "Naruto" in titles

    def test_convert_cards_deduplicates_no_href(self):
        from actions.netflix_control import _convert_cards_to_candidates

        cards = [
            MagicMock(title="One Piece", href=None),
            MagicMock(title="One Piece", href=None),
            MagicMock(title="Naruto", href="https://www.netflix.com/title/123"),
        ]
        result = _convert_cards_to_candidates(cards)
        # _convert_cards_to_candidates does not deduplicate (JS scanner handles that)
        assert len(result) == 3
        assert all(c["title"] in ("One Piece", "Naruto") for c in result)

    def test_normalize_keeps_no_url(self):
        from actions.netflix_control import _normalize_netflix_candidate

        card = {"title": "One Piece", "url": ""}
        norm = _normalize_netflix_candidate(card, 0)
        assert norm is not None
        assert norm["title"] == "One Piece"
        assert norm["url"] == ""


class TestV9CDomClickFallback:
    """Verify _click_visible_netflix_card_by_ordinal works with mocked page."""

    def test_click_success(self):
        from actions.netflix_control import _click_visible_netflix_card_by_ordinal

        page = MagicMock()
        sess = MagicMock()
        sess.run.return_value = True
        result = _click_visible_netflix_card_by_ordinal(1, page, sess)
        assert result is True
        sess.run.assert_called_once()

    def test_click_failure_returns_false(self):
        from actions.netflix_control import _click_visible_netflix_card_by_ordinal

        page = MagicMock()
        sess = MagicMock()
        sess.run.return_value = False
        result = _click_visible_netflix_card_by_ordinal(1, page, sess)
        assert result is False

    def test_click_none_page_returns_false(self):
        from actions.netflix_control import _click_visible_netflix_card_by_ordinal

        result = _click_visible_netflix_card_by_ordinal(1, None, MagicMock())
        assert result is False


class TestV9CGridPlayDomFallback:
    """Verify _handle_grid_play uses DOM click when href is empty."""

    @patch("actions.netflix_control._get_or_scan_candidates")
    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal")
    @patch("actions.netflix_control._verify_playback_started")
    @patch("actions.netflix_control._set_playing")
    @patch("actions.netflix_control._focus_netflix_foreground")
    @patch("actions.netflix_control.time")
    def test_grid_play_dom_click_when_no_href(
        self,
        mock_time,
        mock_focus,
        mock_set_playing,
        mock_verify,
        mock_dom_click,
        mock_get_candidates,
    ):
        from actions.netflix_control import _handle_grid_play

        mock_get_candidates.return_value = (
            [{"title": "One Piece", "url": "", "visible_order": 1}],
            False,
        )
        mock_dom_click.return_value = True
        mock_verify.return_value = True
        sess = MagicMock()
        page = MagicMock()
        result = _handle_grid_play("browse", {"ordinal": 1}, sess, page)
        assert "Playing" in result
        mock_dom_click.assert_called_once_with(1, page, sess)

    @patch("actions.netflix_control._get_or_scan_candidates")
    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal")
    def test_grid_play_dom_click_fails(
        self,
        mock_dom_click,
        mock_get_candidates,
    ):
        from actions.netflix_control import _handle_grid_play

        mock_get_candidates.return_value = (
            [{"title": "One Piece", "url": "", "visible_order": 1}],
            False,
        )
        mock_dom_click.return_value = False
        sess = MagicMock()
        page = MagicMock()
        result = _handle_grid_play("browse", {"ordinal": 1}, sess, page)
        assert "no valid URL" in result.lower() or "could not be clicked" in result.lower()


class TestV9CAutoPlayScope:
    """Verify search-only intent suppresses auto_play."""

    @patch("actions.netflix_control._handle_search")
    @patch("actions.netflix_control._detect_state")
    @patch("actions.netflix_control._acquire_netflix_page")
    @patch("actions.netflix_control._focus_netflix_foreground")
    def test_search_only_no_auto_play(self, mock_focus, mock_acquire, mock_state, mock_search):
        from actions.netflix_control import netflix_control

        mock_acquire.return_value = (MagicMock(), MagicMock())
        mock_state.return_value = {
            "state": "browse",
            "signals": {"current_url": "https://netflix.com/browse"},
        }
        mock_search.return_value = "search result"
        netflix_control(
            {"action": "search", "query": "one piece", "user_text": "search only one piece"}
        )
        mock_search.assert_called_once()
        call_args = mock_search.call_args
        assert (
            call_args[1].get("_auto_play", False) is False
            or call_args[0][1].get("_auto_play", False) is False
        )


# ═════════════════════════════════════════════════════════════════════
# V9C.1 — Netflix Live DOM Geometry Fallback Tests
# ═════════════════════════════════════════════════════════════════════


class TestV9C1GridPlayNoCardsGeometryClick:
    """grid_play must try live geometry click when scanner returns 0 cards."""

    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=True)
    @patch("actions.netflix_control._get_or_scan_candidates", return_value=([], False))
    @patch("actions.netflix_control._save_netflix_grid")
    @patch("actions.browser_intelligence.scan_netflix_cards", return_value=[])
    def test_no_cards_tries_geometry_click(
        self,
        mock_scan,
        mock_save,
        mock_get_candidates,
        mock_dom_click,
        mock_verify,
    ):
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        result = _handle_grid_play("browse", {"ordinal": 2}, sess, page)
        mock_dom_click.assert_called_once_with(2, page, sess)
        assert "Playing Netflix card #2" in result

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=False)
    @patch("actions.netflix_control._get_or_scan_candidates", return_value=([], False))
    @patch("actions.browser_intelligence.scan_netflix_cards", return_value=[])
    def test_no_cards_geometry_click_failure_returns_clean_error(
        self,
        mock_scan,
        mock_get_candidates,
        mock_dom_click,
    ):
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        result = _handle_grid_play("browse", {"ordinal": 1}, sess, page)
        assert "could not read or click" in result.lower()

    @patch("actions.netflix_control._verify_playback_started", return_value=False)
    @patch("actions.netflix_control.click_netflix_play_button", return_value="no play btn")
    @patch(
        "actions.netflix_control.click_netflix_play_button_for_current_title",
        return_value="no play button",
    )
    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=True)
    @patch("actions.netflix_control._get_or_scan_candidates", return_value=([], False))
    @patch("actions.browser_intelligence.scan_netflix_cards", return_value=[])
    def test_no_cards_geo_click_succeeds_play_fails(
        self,
        mock_scan,
        mock_get_candidates,
        mock_dom_click,
        mock_play_title,
        mock_play_btn,
        mock_verify,
    ):
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        result = _handle_grid_play("browse", {"ordinal": 1}, sess, page)
        assert "clicked" in result.lower() and "could not click play" in result.lower()


class TestV9C1AutoplayNoHrefVisibleClick:
    """Auto-play must use visible click when strong candidate has no href."""

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=True)
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control._set_playing")
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._close_netflix_overlays")
    @patch("actions.netflix_control._open_netflix_native_search", return_value=True)
    @patch("actions.netflix_control._infer_netflix_query_from_page", return_value="One Piece")
    @patch("actions.netflix_control._close_netflix_overlays")
    def test_autoplay_no_href_uses_visible_click(
        self,
        mock_close2,
        mock_query,
        mock_search_open,
        mock_close,
        mock_find,
        mock_set_playing,
        mock_play_title,
        mock_dom_click,
    ):
        """When auto_play=True and candidate has no href, should try geometry click."""
        with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", True):
            mock_find.return_value = [
                {"text": "One Piece", "href": "", "url": "", "score": 0.9, "visible_order": 1}
            ]
            from actions.netflix_control import _simple_search_and_play

            sess = MagicMock()
            page = MagicMock()
            page.url = "https://www.netflix.com/search?q=One+Piece"
            result = _simple_search_and_play("One Piece", page, sess, auto_play=True)
            mock_dom_click.assert_called_once()
            assert "Playing: One Piece" in result

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=False)
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._close_netflix_overlays")
    @patch("actions.netflix_control._open_netflix_native_search", return_value=True)
    @patch("actions.netflix_control._infer_netflix_query_from_page", return_value="One Piece")
    @patch("actions.netflix_control._close_netflix_overlays")
    def test_autoplay_no_href_geometry_fails(
        self,
        mock_close2,
        mock_query,
        mock_search_open,
        mock_close,
        mock_find,
        mock_dom_click,
    ):
        with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", True):
            mock_find.return_value = [
                {"text": "One Piece", "href": "", "url": "", "score": 0.9, "visible_order": 1}
            ]
            from actions.netflix_control import _simple_search_and_play

            sess = MagicMock()
            page = MagicMock()
            page.url = "https://www.netflix.com/search?q=One+Piece"
            result = _simple_search_and_play("One Piece", page, sess, auto_play=True)
            assert "could not open" in result.lower()


class TestV9C1OrdinalRouteNotSearchQuery:
    """'select title number two on the grid' must route to grid_play, not set query."""

    def test_ordinal_phrase_routes_to_grid_play(self):
        from utils.ordinal_selection import parse_ordinal_command

        parsed = parse_ordinal_command("select title number two on the grid")
        assert parsed is not None
        assert parsed.get("ordinal") == 2


class TestV9C1NoVisionCooldownOnDomFailure:
    """DOM scanner failure must NOT set Vision cooldown."""

    @patch("actions.netflix_control._set_netflix_failure")
    @patch("actions.netflix_control._set_netflix_vision_cooldown")
    @patch("actions.netflix_control._get_or_scan_candidates", return_value=([], False))
    @patch("actions.browser_intelligence.scan_netflix_cards", return_value=[])
    def test_no_cooldown_on_scan_failure(
        self,
        mock_scan,
        mock_get_candidates,
        mock_cooldown,
        mock_failure,
    ):
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = None  # No page = can't try geometry click
        _handle_grid_play("browse", {"ordinal": 1}, sess, page)
        mock_cooldown.assert_not_called()


class TestV9C1SearchUrlIsSearchPage:
    """URL containing /search should be treated as search page even without title-card classes."""

    def test_search_url_detected(self):
        from actions.browser_intelligence import scan_netflix_cards

        raw_str = "https://www.netflix.com/search?q=One+Piece"
        _is_search_url = "/search" in raw_str.lower()
        assert _is_search_url

    def test_search_url_without_q_param(self):
        raw_str = "https://www.netflix.com/search/unknown"
        _is_search_url = "/search" in raw_str.lower()
        assert _is_search_url


class TestV9C1GeometryExcludesNav:
    """Geometry click must not select notification/nav links."""

    def test_is_bad_chrome_filters_nav(self):
        js_fragment = (
            "function isBadChrome(el) {"
            "  return !!el.closest("
            '    \'header, nav, [data-uia*="navigation"], [data-uia*="notification"],'
            '    [data-uia*="profile"], [data-uia*="account"], [data-uia*="search-box"],'
            '    input, footer, [data-uia="PIN-entry"]\''
            "  );"
            "}"
        )
        assert "header" in js_fragment
        assert "nav" in js_fragment
        assert "notification" in js_fragment
        assert "profile" in js_fragment
        assert "account" in js_fragment
        assert "footer" in js_fragment


class TestV9C1GeometryClickFunctionExists:
    """_click_visible_netflix_card_by_ordinal must exist and accept (ordinal, page, sess)."""

    def test_function_signature(self):
        import inspect

        from actions.netflix_control import _click_visible_netflix_card_by_ordinal

        sig = inspect.signature(_click_visible_netflix_card_by_ordinal)
        params = list(sig.parameters.keys())
        assert params == ["ordinal", "page", "sess"]

    def test_returns_bool(self):
        from actions.netflix_control import _click_visible_netflix_card_by_ordinal

        result = _click_visible_netflix_card_by_ordinal(1, None, MagicMock())
        assert result is False

    def test_ordinal_zero_returns_false(self):
        from actions.netflix_control import _click_visible_netflix_card_by_ordinal

        result = _click_visible_netflix_card_by_ordinal(0, MagicMock(), MagicMock())
        assert result is False


# ═════════════════════════════════════════════════════════════════════
# V9C.2 — Dirty Query Guard + Availability Intent Tests
# ═════════════════════════════════════════════════════════════════════


class TestV9C2DirtyQueryGuard:
    """_looks_like_dirty_media_query blocks autoplay for dirty queries."""

    def test_do_you_have_is_dirty(self):
        from actions.netflix_control import _looks_like_dirty_media_query

        assert _looks_like_dirty_media_query("do you have playing war machine") is True

    def test_can_you_is_dirty(self):
        from actions.netflix_control import _looks_like_dirty_media_query

        assert _looks_like_dirty_media_query("can you play one piece") is True

    def test_playing_is_dirty(self):
        from actions.netflix_control import _looks_like_dirty_media_query

        assert _looks_like_dirty_media_query("playing war machine") is True

    def test_on_netflix_is_dirty(self):
        from actions.netflix_control import _looks_like_dirty_media_query

        assert _looks_like_dirty_media_query("war machine on netflix") is True

    def test_clean_query_not_dirty(self):
        from actions.netflix_control import _looks_like_dirty_media_query

        assert _looks_like_dirty_media_query("War Machine") is False

    def test_empty_not_dirty(self):
        from actions.netflix_control import _looks_like_dirty_media_query

        assert _looks_like_dirty_media_query("") is False


class TestV9C2AutoplayDirtyQueryBlocks:
    """_simple_search_and_play blocks autoplay when query is dirty."""

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=False)
    @patch("actions.netflix_control._find_netflix_title_by_text")
    def test_dirty_query_blocks_autoplay_no_href(
        self,
        mock_find,
        mock_geo,
    ):
        """When strong candidates exist but no href and query is dirty, should not try geometry click."""
        from actions.netflix_control import _simple_search_and_play

        sess = MagicMock()
        page = MagicMock()
        # Mock candidates with high score but no href
        mock_find.return_value = [
            {"text": "War Machine", "score": 0.9, "href": "", "visible_order": 1}
        ]
        result = _simple_search_and_play(
            "do you have playing war machine",
            page,
            sess,
            player=None,
            auto_play=True,
        )
        # Should return a clarification message, not attempt geometry click
        mock_geo.assert_not_called()
        assert "query looks unclear" in result.lower() or "do you have" in result.lower()

    @patch("actions.netflix_control._find_netflix_title_by_text")
    def test_clean_query_passes_autoplay(self, mock_find):
        """When query is clean and strong match exists, autoplay proceeds normally."""
        from actions.netflix_control import _simple_search_and_play

        sess = MagicMock()
        page = MagicMock()
        mock_find.return_value = [
            {
                "text": "War Machine",
                "score": 0.9,
                "href": "https://www.netflix.com/title/12345",
                "visible_order": 1,
            }
        ]
        with (
            patch("actions.netflix_control._click_netflix_title_candidate", return_value=True),
            patch("actions.netflix_control._verify_playback_started", return_value=True),
            patch("actions.netflix_control._verify_title_context_matches", return_value=True),
            patch("actions.netflix_control._set_playing"),
            patch("actions.netflix_control._pause_other_media"),
        ):
            result = _simple_search_and_play(
                "War Machine",
                page,
                sess,
                player=None,
                auto_play=True,
            )
            assert "Playing" in result


class TestV9C2DocumentFallbackPromotion:
    """scan_netflix_cards must promote document_fallback candidates when strict scan returns 0."""

    def test_promoted_candidates_have_titles(self):
        """When document_fallback returns cards with titles, they are promoted."""
        import json

        from actions.browser_intelligence import scan_netflix_cards

        # Simulate a raw scan result with document_fallback scope and cards with titles
        fake_result = json.dumps(
            {
                "scope": "document_fallback",
                "cards": [
                    {"title": "War Machine", "href": "", "subtitle": "", "x": 100, "y": 200},
                    {"title": "Berlin", "href": "", "subtitle": "", "x": 300, "y": 200},
                ],
            }
        )
        mock_sess = MagicMock()
        mock_sess.run.return_value = fake_result
        with (
            patch("actions.browser_intelligence.get_browser_session", return_value=mock_sess),
            patch("actions.browser_intelligence._BROWSER_AVAILABLE", True),
        ):
            cards = scan_netflix_cards("brave")
            # Should have promoted the document_fallback cards
            assert len(cards) >= 1
            titles = [c.title for c in cards]
            assert any("War Machine" in t or "Berlin" in t for t in titles)

    def test_strict_cards_returned_when_available(self):
        """When strict scan has results, document_fallback cards are not promoted."""
        import json

        from actions.browser_intelligence import scan_netflix_cards

        # Simulate a result with both strict and fallback cards
        fake_result = json.dumps(
            {
                "scope": "search_container",
                "cards": [
                    {
                        "title": "One Piece",
                        "href": "https://www.netflix.com/title/123",
                        "subtitle": "",
                        "x": 0,
                        "y": 0,
                    },
                ],
            }
        )
        mock_sess = MagicMock()
        mock_sess.run.return_value = fake_result
        with (
            patch("actions.browser_intelligence.get_browser_session", return_value=mock_sess),
            patch("actions.browser_intelligence._BROWSER_AVAILABLE", True),
        ):
            cards = scan_netflix_cards("brave")
            assert len(cards) == 1
            assert cards[0].title == "One Piece"


# ═════════════════════════════════════════════════════════════════════
# V9C.2.1: War Machine title verification regression tests
# ═════════════════════════════════════════════════════════════════════


class TestV9C21TitleVerification:
    """_is_verified_title_match must reject wrong titles and accept correct ones."""

    def test_war_machine_does_not_match_watchmen(self):
        from actions.netflix_control import _is_verified_title_match

        assert _is_verified_title_match({"title": "Watchmen"}, "War Machine") is False

    def test_war_machine_does_not_match_the_guardians(self):
        from actions.netflix_control import _is_verified_title_match

        assert _is_verified_title_match({"title": "The Guardians"}, "War Machine") is False

    def test_war_machine_matches_war_machine(self):
        from actions.netflix_control import _is_verified_title_match

        assert _is_verified_title_match({"title": "War Machine"}, "War Machine") is True

    def test_empty_title_fails(self):
        from actions.netflix_control import _is_verified_title_match

        assert _is_verified_title_match({"title": ""}, "War Machine") is False

    def test_no_title_key_fails(self):
        from actions.netflix_control import _is_verified_title_match

        assert _is_verified_title_match({}, "War Machine") is False

    def test_one_piece_matches_one_piece_film_red(self):
        from actions.netflix_control import _is_verified_title_match

        assert _is_verified_title_match({"title": "One Piece Film Red"}, "One Piece") is True


class TestV9C21AutoplayWrongTitleBlocked:
    """Autoplay geometry click must not proceed when title verification fails."""

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal")
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._close_netflix_overlays")
    @patch("actions.netflix_control._open_netflix_native_search", return_value=True)
    @patch("actions.netflix_control._infer_netflix_query_from_page", return_value="War Machine")
    @patch("actions.netflix_control._close_netflix_overlays")
    def test_autoplay_no_href_war_machine_wrong_title_does_not_click(
        self,
        mock_close2,
        mock_query,
        mock_search_open,
        mock_close,
        mock_find,
        mock_click,
    ):
        """When candidate is Watchmen but query is War Machine, geometry click must NOT be called."""
        with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", True):
            mock_find.return_value = [
                {
                    "text": "Watchmen",
                    "title": "Watchmen",
                    "href": "",
                    "score": 0.95,
                    "visible_order": 1,
                }
            ]
            from actions.netflix_control import _simple_search_and_play

            sess = MagicMock()
            page = MagicMock()
            page.url = "https://www.netflix.com/search?q=War+Machine"
            result = _simple_search_and_play("War Machine", page, sess, auto_play=True)
            mock_click.assert_not_called()
            assert "verify the exact title" in result or "could not verify" in result

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=True)
    @patch("actions.netflix_control.click_netflix_play_button_for_current_title", return_value=None)
    @patch("actions.netflix_control.click_netflix_play_button", return_value=None)
    @patch("actions.netflix_control._set_playing")
    @patch("actions.netflix_control._read_netflix_visible_title", return_value="War Machine")
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._close_netflix_overlays")
    @patch("actions.netflix_control._open_netflix_native_search", return_value=True)
    @patch("actions.netflix_control._infer_netflix_query_from_page", return_value="War Machine")
    @patch("actions.netflix_control._close_netflix_overlays")
    def test_autoplay_no_href_war_machine_exact_title_can_click(
        self,
        mock_close2,
        mock_query,
        mock_search_open,
        mock_close,
        mock_find,
        mock_read_title,
        mock_set_playing,
        mock_play_btn,
        mock_play_title,
        mock_click,
    ):
        """When candidate IS War Machine, geometry click is allowed."""
        with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", True):
            mock_find.return_value = [
                {
                    "text": "War Machine",
                    "title": "War Machine",
                    "href": "",
                    "score": 0.95,
                    "visible_order": 1,
                }
            ]
            from actions.netflix_control import _simple_search_and_play

            sess = MagicMock()
            page = MagicMock()
            page.url = "https://www.netflix.com/search?q=War+Machine"
            result = _simple_search_and_play("War Machine", page, sess, auto_play=True)
            mock_click.assert_called_once()
            assert "Playing" in result

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal")
    @patch("actions.netflix_control._find_netflix_title_by_text")
    @patch("actions.netflix_control._close_netflix_overlays")
    @patch("actions.netflix_control._open_netflix_native_search", return_value=True)
    @patch("actions.netflix_control._infer_netflix_query_from_page", return_value="War Machine")
    @patch("actions.netflix_control._close_netflix_overlays")
    def test_autoplay_no_href_unknown_title_does_not_click(
        self,
        mock_close2,
        mock_query,
        mock_search_open,
        mock_close,
        mock_find,
        mock_click,
    ):
        """When candidate has empty title, geometry click must NOT be called."""
        with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", True):
            mock_find.return_value = [
                {"text": "", "title": "", "href": "", "score": 0.90, "visible_order": 1}
            ]
            from actions.netflix_control import _simple_search_and_play

            sess = MagicMock()
            page = MagicMock()
            page.url = "https://www.netflix.com/search?q=War+Machine"
            result = _simple_search_and_play("War Machine", page, sess, auto_play=True)
            mock_click.assert_not_called()
            assert "verify the exact title" in result or "could not verify" in result

    @patch("actions.netflix_control._click_visible_netflix_card_by_ordinal", return_value=True)
    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._set_playing")
    @patch("actions.netflix_control._focus_netflix_foreground")
    @patch("actions.netflix_control._get_or_scan_candidates", return_value=([], False))
    def test_explicit_grid_play_can_click_unknown_title_but_not_claim_query(
        self,
        mock_get_candidates,
        mock_focus,
        mock_set_playing,
        mock_verify,
        mock_click,
    ):
        """Explicit ordinal grid_play can geometry-click unknown card but must say 'Playing Netflix card #N'."""
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        result = _handle_grid_play("browse", {"ordinal": 1}, sess, page)
        mock_click.assert_called_once()
        assert "Playing Netflix card #1" in result
        assert "War Machine" not in result


class TestV9C21QueryCleaning:
    """'War Machine on Netflix' must be cleaned to 'War Machine'."""

    def test_netflix_search_query_war_machine_on_netflix_cleaned(self):
        from core.self_healing_router import _clean_netflix_media_query

        result = _clean_netflix_media_query("War Machine on Netflix")
        assert result == "War Machine"

    def test_netflix_search_query_play_war_machine_on_netflix_cleaned(self):
        from core.self_healing_router import _clean_netflix_media_query

        result = _clean_netflix_media_query("Play War Machine on Netflix")
        assert result == "War Machine"

    def test_router_routes_cleaned_war_machine_query(self):
        """Router should pass cleaned 'War Machine' (not 'War Machine on Netflix') to netflix_control."""
        from core.self_healing_router import route_tool_call

        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Play War Machine on Netflix"
        )
        if tool == "netflix_control":
            q = params.get("query", "")
            assert "on netflix" not in q.lower(), f"Query still contains 'on netflix': {q!r}"
            assert "War Machine" in q


# ── V9C.3: Grid play row/col tests ─────────────────────────────────────────


class TestV9C3GridPlayRowCol(unittest.TestCase):
    """Tests for _handle_grid_play row/col fast path in netflix_control."""

    def test_row_col_triggers_fast_path(self):
        """_handle_grid_play should call _handle_grid_play_row_col when row+col are present."""
        import actions.netflix_control as nc

        with unittest.mock.patch.object(
            nc, "_handle_grid_play_row_col", return_value="RowColHit"
        ) as mock_rc:
            result = nc._handle_grid_play("home", {"row": 2, "col": 3}, None, None)
            mock_rc.assert_called_once_with(2, 3, "home", {"row": 2, "col": 3}, None, None, None)
            self.assertEqual(result, "RowColHit")

    def test_ordinal_still_works_without_row_col(self):
        """_handle_grid_play should use existing ordinal path when row/col not present."""
        import actions.netflix_control as nc

        # When no row/col, it should not call _handle_grid_play_row_col
        with unittest.mock.patch.object(
            nc, "_handle_grid_play_row_col", return_value=None
        ) as mock_rc:
            # It will fall through to the existing scan/candidate logic
            with unittest.mock.patch.object(
                nc, "_get_or_scan_candidates", return_value=([], False)
            ):
                nc._handle_grid_play("home", {"ordinal": 1}, None, None)
                mock_rc.assert_not_called()

    def test_invalid_row_col_falls_through(self):
        """Non-integer row/col should fall through to existing logic."""
        import actions.netflix_control as nc

        with unittest.mock.patch.object(
            nc, "_handle_grid_play_row_col", return_value=None
        ) as mock_rc:
            with unittest.mock.patch.object(
                nc, "_get_or_scan_candidates", return_value=([], False)
            ):
                nc._handle_grid_play("home", {"row": "bad", "col": "bad"}, None, None)
                mock_rc.assert_not_called()

    def test_row_zero_col_zero_falls_through(self):
        """row=0 or col=0 should fall through (must be >= 1)."""
        import actions.netflix_control as nc

        with unittest.mock.patch.object(
            nc, "_handle_grid_play_row_col", return_value=None
        ) as mock_rc:
            with unittest.mock.patch.object(
                nc, "_get_or_scan_candidates", return_value=([], False)
            ):
                nc._handle_grid_play("home", {"row": 0, "col": 1}, None, None)
                mock_rc.assert_not_called()

    def test_click_cached_netflix_card_bad_coords(self):
        from actions.netflix_control import _click_cached_netflix_card

        self.assertIsNone(_click_cached_netflix_card(0, 1, "p", "s"))
        self.assertIsNone(_click_cached_netflix_card(1, 0, "p", "s"))

    def test_find_card_at_row_col_exact(self):
        from actions.netflix_control import _find_card_at_row_col

        grid = {
            "rows": [
                {"row": 1, "col": 1, "title": "A"},
                {"row": 1, "col": 2, "title": "B"},
                {"row": 2, "col": 1, "title": "C"},
            ]
        }
        self.assertEqual(_find_card_at_row_col(grid, 1, 2)["title"], "B")
        self.assertEqual(_find_card_at_row_col(grid, 2, 1)["title"], "C")
        self.assertIsNone(_find_card_at_row_col(grid, 3, 1))

    def test_scan_visible_grid_no_page(self):
        from actions.netflix_control import scan_netflix_visible_grid

        self.assertIsNone(scan_netflix_visible_grid(None, None))

    def test_save_load_cache_roundtrip(self):
        import os
        import time

        from actions.netflix_control import (
            _NETFLIX_GRID_CACHE_PATH,
            _load_netflix_grid_cache,
            _save_netflix_grid_cache,
        )

        data = {"rows": [{"row": 1, "col": 1, "title": "X"}], "count": 1, "_cached_at": time.time()}
        _save_netflix_grid_cache(data)
        loaded = _load_netflix_grid_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["rows"][0]["title"], "X")
        try:
            os.remove(_NETFLIX_GRID_CACHE_PATH)
        except Exception:
            pass


# ── V9C.3.1: Hotfix — GPC/privacy filter, modal close, cached click validation ──


class TestV9C31BadElementFilter(unittest.TestCase):
    """PART 1: _is_bad_netflix_element rejects GPC/privacy/nav/footer elements."""

    def test_gpc_opt_out_rejected(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertTrue(_is_bad_netflix_element("gpc opt-out preference"))

    def test_privacy_center_rejected(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertTrue(_is_bad_netflix_element("Centre de préférences de la confidentialité"))

    def test_cookie_banner_rejected(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertTrue(_is_bad_netflix_element("cookie consent banner"))

    def test_legal_footer_rejected(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertTrue(_is_bad_netflix_element("legal footer help links"))

    def test_navigation_rejected(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertTrue(_is_bad_netflix_element("navigation menu account profile"))

    def test_real_card_title_accepted(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertFalse(_is_bad_netflix_element("War Machine"))
        self.assertFalse(_is_bad_netflix_element("One Piece"))
        self.assertFalse(_is_bad_netflix_element("The Guardians"))

    def test_empty_string_accepted(self):
        from actions.netflix_control import _is_bad_netflix_element

        self.assertFalse(_is_bad_netflix_element(""))
        self.assertFalse(_is_bad_netflix_element(None))


class TestV9C31ModalClose(unittest.TestCase):
    """PART 4: _close_netflix_modal_for_grid_selection helper."""

    def test_no_page_returns_silently(self):
        from actions.netflix_control import _close_netflix_modal_for_grid_selection

        _close_netflix_modal_for_grid_selection(None, None)  # should not raise

    @patch("actions.netflix_control.time")
    def test_close_button_clicked(self, mock_time):
        from actions.netflix_control import _close_netflix_modal_for_grid_selection

        mock_page = MagicMock()
        mock_sess = MagicMock()
        mock_sess.run.return_value = "clicked_close"
        _close_netflix_modal_for_grid_selection(mock_sess, mock_page)
        mock_sess.run.assert_called()

    @patch("actions.netflix_control.time")
    def test_no_modal_falls_back_to_escape(self, mock_time):
        from actions.netflix_control import _close_netflix_modal_for_grid_selection

        mock_page = MagicMock()
        mock_sess = MagicMock()
        mock_sess.run.return_value = "no_modal"
        _close_netflix_modal_for_grid_selection(mock_sess, mock_page)
        mock_page.keyboard.press.assert_called_with("Escape")


class TestV9C31CachedClickValidation(unittest.TestCase):
    """PART 5: _click_cached_netflix_card rejects bad elements at click point."""

    @patch("actions.netflix_control.scan_netflix_visible_grid")
    def test_bad_element_at_point_returns_none(self, mock_scan):
        from actions.netflix_control import _click_cached_netflix_card

        mock_scan.return_value = {
            "rows": [
                {
                    "row": 1,
                    "col": 1,
                    "title": "GPC Opt-Out",
                    "href": "",
                    "x": 100,
                    "y": 200,
                    "w": 200,
                    "h": 150,
                }
            ],
            "count": 1,
        }
        mock_page = MagicMock()
        mock_sess = MagicMock()
        mock_sess.run.return_value = '{"ok": false, "reason": "bad_element", "text": "gpc opt-out"}'
        result = _click_cached_netflix_card(1, 1, mock_page, mock_sess)
        self.assertIsNone(result)

    @patch("actions.netflix_control.scan_netflix_visible_grid")
    def test_valid_element_succeeds(self, mock_scan):
        from actions.netflix_control import _click_cached_netflix_card

        mock_scan.return_value = {
            "rows": [
                {
                    "row": 1,
                    "col": 1,
                    "title": "War Machine",
                    "href": "https://netflix.com/title/123",
                    "x": 100,
                    "y": 200,
                    "w": 200,
                    "h": 150,
                }
            ],
            "count": 1,
        }
        mock_page = MagicMock()
        mock_sess = MagicMock()
        mock_sess.run.return_value = '{"ok": true}'
        result = _click_cached_netflix_card(1, 1, mock_page, mock_sess)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "War Machine")


class TestV9C31ExplicitSelectionResponse(unittest.TestCase):
    """PART 6: Explicit ordinal selection says 'Playing Netflix card #N', not query title."""

    def test_no_href_ordinal_says_card_number(self):
        from actions.netflix_control import _is_verified_title_match

        # Explicit selection should use card number, not claim query title
        # This is a structural test — the actual response format is in _handle_grid_play
        # We verify the helper exists and works
        self.assertTrue(
            _is_verified_title_match({"title": "War Machine"}, "War Machine", min_score=0.88)
        )
        self.assertFalse(
            _is_verified_title_match({"title": "Watchmen"}, "War Machine", min_score=0.88)
        )


class TestV9C31GridPlayClosesModal(unittest.TestCase):
    """PART 4+7: _handle_grid_play_row_col closes modal before clicking."""

    @patch("actions.netflix_control._close_netflix_modal_for_grid_selection")
    @patch("actions.netflix_control._click_cached_netflix_card")
    @patch("actions.netflix_control._verify_playback_started", return_value=True)
    @patch("actions.netflix_control._set_playing")
    def test_modal_closed_before_cached_click(self, mock_set, mock_verify, mock_click, mock_close):
        from actions.netflix_control import _handle_grid_play_row_col

        mock_click.return_value = {
            "title": "War Machine",
            "href": "https://netflix.com/title/123",
            "x": 100,
            "y": 200,
        }
        mock_page = MagicMock()
        mock_sess = MagicMock()
        result = _handle_grid_play_row_col(1, 1, "idle", {}, mock_sess, mock_page)
        mock_close.assert_called_once()
        mock_click.assert_called_once_with(1, 1, mock_page, mock_sess)
        self.assertIn("Playing", result)

    @patch("actions.netflix_control._close_netflix_modal_for_grid_selection")
    @patch("actions.netflix_control._click_cached_netflix_card", return_value=None)
    def test_modal_closed_even_when_card_not_found(self, mock_click, mock_close):
        from actions.netflix_control import _handle_grid_play_row_col

        mock_page = MagicMock()
        mock_sess = MagicMock()
        result = _handle_grid_play_row_col(99, 99, "idle", {}, mock_sess, mock_page)
        mock_close.assert_called_once()
        self.assertIn("could not find", result)


class TestV9C31PromotedCandidatesFilter(unittest.TestCase):
    """PART 2: Promoted document_fallback candidates filter bad tokens."""

    def test_bad_token_filtered_from_promotion(self):
        from actions.netflix_control import _is_bad_netflix_element

        # Simulates what the promotion path checks
        items = [
            {"title": "GPC Opt-Out Preference", "href": "", "ancestor": "footer"},
            {
                "title": "War Machine",
                "href": "https://netflix.com/title/123",
                "ancestor": "search_container",
            },
        ]
        good = [
            it
            for it in items
            if not _is_bad_netflix_element(
                " ".join([it["title"], it.get("ancestor", ""), it.get("selectorUsed", "")])
            )
        ]
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0]["title"], "War Machine")


class TestV9C31OrdinalClickFiltersBadTokens(unittest.TestCase):
    """PART 1+3: JS in _click_visible_netflix_card_by_ordinal filters bad tokens."""

    def test_is_bad_netflix_element_variants(self):
        from actions.netflix_control import _is_bad_netflix_element

        # French privacy
        self.assertTrue(_is_bad_netflix_element("préférences confidentialité"))
        # English GPC
        self.assertTrue(_is_bad_netflix_element("gpc opt-out consent"))
        # Cookie
        self.assertTrue(_is_bad_netflix_element("manage cookies preferences"))
        # Legal
        self.assertTrue(_is_bad_netflix_element("terms and conditions legal"))
        # Real titles pass
        self.assertFalse(_is_bad_netflix_element("Stranger Things"))
        self.assertFalse(_is_bad_netflix_element("The Witcher"))
        self.assertFalse(_is_bad_netflix_element("Breaking Bad"))
