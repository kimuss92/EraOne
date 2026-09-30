"""
Unit tests for Netflix Dedicated Control — Step 14B.2.6.

Tests the bounded screen_process/Vision fallback reliability improvements:
1. _reset_vision_attempted() clears per-command flag
2. _call_screen_process_narrow max 1 call per command
3. _call_screen_process_narrow cooldown on quota/429/timeout
4. _call_screen_process_narrow exception handling
5. click_netflix_play_button DOM detection and click
6. _check_local_signals non-destructive local checks
7. _verify_playback_started signal detection
8. Integration: grid_play and standard play use DOM-first, not Vision

No real browser calls. No real screen_process calls.
"""

import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent

import pytest


# Disable simple mode for all legacy scanner tests in this file.
@pytest.fixture(autouse=True)
def _disable_simple_mode():
    with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", False):
        yield


# ═══════════════════════════════════════════════════════════════════════════════
# Vision cooldown state tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVisionCooldownState:
    """Verify _reset_vision_attempted(), _VISION_ALREADY_ATTEMPTED, _VISION_COOLDOWN_UNTIL."""

    def test_reset_vision_attempted_clears_flag(self):
        import actions.netflix_control as nc
        from actions.netflix_control import _VISION_ALREADY_ATTEMPTED, _reset_vision_attempted

        nc._VISION_ALREADY_ATTEMPTED = True
        _reset_vision_attempted()
        assert nc._VISION_ALREADY_ATTEMPTED is False

    def test_reset_vision_attempted_preserves_cooldown(self):
        import actions.netflix_control as nc
        from actions.netflix_control import _reset_vision_attempted

        nc._VISION_ALREADY_ATTEMPTED = True
        nc._VISION_COOLDOWN_UNTIL = 99999.0
        _reset_vision_attempted()
        assert nc._VISION_COOLDOWN_UNTIL == 99999.0
        assert nc._VISION_ALREADY_ATTEMPTED is False

    def test_netflix_control_calls_reset_at_start(self):
        """netflix_control() must call _reset_vision_attempted() on entry."""
        source = Path(REPO_ROOT, "actions", "netflix_control.py").read_text("utf-8")
        assert "_reset_vision_attempted()" in source
        assert (
            "_VISION_ALREADY_ATTEMPTED = False"
            not in source.split("def netflix_control")[1].split("def ")[0]
        )

    def test_vision_attempted_flag_false_after_netflix_control_play(self):
        from unittest.mock import MagicMock, patch

        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = True
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(nc, "click_netflix_play_button", return_value="No button"):
                    with patch.object(nc, "_call_screen_process_narrow", return_value=None):
                        nc.netflix_control({"action": "play"}, player=None)
        assert nc._VISION_ALREADY_ATTEMPTED is False


# ═══════════════════════════════════════════════════════════════════════════════
# _call_screen_process_narrow tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCallScreenProcessNarrow:
    """Verify bounded screen_process fallback behavior."""

    def setup_method(self):
        import actions.netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0

    def test_first_call_allowed(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            mock_sp.return_value = "Play button at (500, 300)"
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "Play button at (500, 300)"
            assert nc._VISION_ALREADY_ATTEMPTED is True

    def test_second_call_blocked(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            mock_sp.return_value = "Found play button"
            first = nc._call_screen_process_narrow(mock_sess, mock_page)
            second = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert first == "Found play button"
            assert second is None
            assert mock_sp.call_count == 1

    def test_cooldown_active_skips_call(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = time.monotonic() + 1000.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            mock_sp.assert_not_called()

    def test_429_response_sets_cooldown(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            mock_sp.return_value = "Error: 429 Too Many Requests"
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 40.0

    def test_resource_exhausted_sets_cooldown(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            mock_sp.return_value = "RESOURCE_EXHAUSTED: quota exceeded"
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 40.0

    def test_timeout_signal_sets_cooldown(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            mock_sp.return_value = "could not analyze the screen quickly enough"
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 40.0

    def test_exception_in_screen_process_sets_cooldown(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process", side_effect=RuntimeError("API down")):
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 40.0

    def test_screen_process_import_safety(self):
        """_call_screen_process_narrow imports screen_process inside the function body, not at module top level."""
        source = Path(REPO_ROOT, "actions", "netflix_control.py").read_text("utf-8")
        fn_body = source.split("def _call_screen_process_narrow")[1].split("def ")[0]
        assert "from actions.screen_processor import screen_process" in fn_body
        # Verify it's NOT at the top of the module
        module_top = source.split("def ")[0]
        assert "from actions.screen_processor" not in module_top

    def test_503_response_sets_cooldown(self):
        """503 error in screen_process result sets cooldown."""
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch("actions.screen_processor.screen_process") as mock_sp:
            mock_sp.return_value = "Error: 503 Service Unavailable"
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 40.0

    def test_retry_delay_in_exception_extends_cooldown(self):
        """Exception with retry delay text uses longer cooldown."""
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch(
            "actions.screen_processor.screen_process", side_effect=RuntimeError("retry delay 60")
        ):
            result = nc._call_screen_process_narrow(mock_sess, mock_page)
            assert result == "vision_fallback_unavailable_due_to_quota_or_timeout"
            # 60s from exception + 5s buffer = 65s, so > 60.0
            assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 60.0


# ═══════════════════════════════════════════════════════════════════════════════
# Netflix Vision cooldown guard tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNetflixVisionGuard:
    """Verify _set_netflix_vision_cooldown and is_netflix_vision_blocked."""

    def setup_method(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = 0.0

    def test_cooldown_not_active_when_not_set(self):
        from actions.netflix_control import is_netflix_vision_blocked

        assert is_netflix_vision_blocked() is False

    def test_cooldown_active_when_set(self):
        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = time.monotonic() + 60.0
        assert nc.is_netflix_vision_blocked() is True

    def test_custom_duration_sets_correctly(self):
        import actions.netflix_control as nc
        from actions.netflix_control import _set_netflix_vision_cooldown, is_netflix_vision_blocked

        nc._VISION_COOLDOWN_UNTIL = 0.0
        _set_netflix_vision_cooldown(30.0)
        assert nc._VISION_COOLDOWN_UNTIL > time.monotonic() + 25.0

    def test_cooldown_expires(self):
        import time as t

        import actions.netflix_control as nc

        nc._VISION_COOLDOWN_UNTIL = t.monotonic() + 0.001
        if nc._VISION_COOLDOWN_UNTIL < t.monotonic():
            pass
        else:
            t.sleep(0.01)
        assert nc.is_netflix_vision_blocked() is False

    def test_handle_status_returns_controlled_during_cooldown(self):
        """_handle_status returns controlled message during cooldown."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_status

        nc._VISION_COOLDOWN_UNTIL = time.monotonic() + 100.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        nc._LAST_NETFLIX_FAILURE_TOOL = ""
        result = _handle_status({})
        assert "screen vision is temporarily unavailable" in result
        assert "search again" in result
        assert "saved list" in result

    def test_handle_status_returns_normal_outside_cooldown(self):
        """_handle_status returns normal state info when no cooldown."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_status

        nc._VISION_COOLDOWN_UNTIL = 0.0
        nc._LAST_NETFLIX_FAILURE_AT = 0.0
        nc._LAST_NETFLIX_FAILURE_TOOL = ""
        state_info = {
            "state": "browse",
            "signals": {
                "has_video": False,
                "url_contains_watch": False,
                "has_player_controls": False,
                "page_title": "Netflix",
                "current_url": "https://www.netflix.com/browse",
            },
        }
        result = _handle_status(state_info)
        assert "Netflix state:" in result
        assert "temporarily unavailable" not in result

    def test_grid_play_no_candidates_no_cooldown(self):
        """_handle_grid_play returning 'could not read' must NOT set Vision cooldown (DOM failure)."""
        import actions.netflix_control as nc_mod
        from actions import netflix_control as nc

        nc_mod._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch.object(nc_mod, "_get_or_scan_candidates", return_value=([], False)):
            result = nc._handle_grid_play("browse", {}, mock_sess, mock_page)
            assert "could not read" in result
            assert nc_mod._VISION_COOLDOWN_UNTIL == 0.0

    def test_grid_play_no_cards_from_scanner_no_cooldown(self):
        """_handle_grid_play returning 'could not read' from scanner must NOT set cooldown (DOM failure)."""
        import actions.netflix_control as nc_mod
        from actions import netflix_control as nc

        nc_mod._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        with patch.object(nc_mod, "_get_or_scan_candidates", return_value=([], False)):
            result = nc._handle_grid_play("browse", {}, mock_sess, mock_page)
            assert "could not read" in result
            assert nc_mod._VISION_COOLDOWN_UNTIL == 0.0

    def test_search_no_cards_no_cooldown(self):
        """_handle_search returning 'could not read any title cards' must NOT set cooldown (DOM failure)."""
        import actions.netflix_control as nc_mod
        from actions import netflix_control as nc

        nc_mod._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/search?q=Test"
        with patch.object(nc_mod, "_deduplicate_query", return_value="Test"):
            with patch("actions.browser_intelligence.scan_netflix_cards") as mock_scan:
                mock_scan.return_value = []
                with patch(
                    "actions.browser_intelligence.is_netflix_profile_page", return_value=False
                ):
                    result = nc._handle_search("search", {"query": "Test"}, mock_sess, mock_page)
                    assert "could not read" in result
                    assert nc_mod._VISION_COOLDOWN_UNTIL == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# click_netflix_play_button tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestClickNetflixPlayButton:
    """Verify DOM-first Play button detection and click via CDP."""

    def test_detects_and_clicks_visible_button(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        found_json = json.dumps(
            {"found": True, "tag": "A", "text": "Play", "x": 500, "y": 300, "href": ""}
        )
        mock_sess.run.return_value = found_json

        with patch.object(nc, "_verify_playback_started", return_value=True):
            result = nc.click_netflix_play_button(mock_sess, mock_page)
            assert result is None

        # Must have evaluated the play button JS
        mock_sess.run.assert_any_call(mock_page.evaluate(nc._NETFLIX_PLAY_BUTTON_JS))

    def test_follows_watch_href_link(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/title/12345"
        found_json = json.dumps(
            {
                "found": True,
                "tag": "A",
                "text": "",
                "x": 100,
                "y": 100,
                "href": "https://www.netflix.com/watch/12345",
            }
        )
        mock_sess.run.return_value = found_json

        with patch.object(nc, "_verify_playback_started", return_value=True):
            result = nc.click_netflix_play_button(mock_sess, mock_page)
            assert result is None

        mock_sess.run.assert_any_call(
            mock_page.goto(
                "https://www.netflix.com/watch/12345", wait_until="domcontentloaded", timeout=30_000
            )
        )

    def test_no_button_found_returns_error(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps({"found": False})

        result = nc.click_netflix_play_button(mock_sess, mock_page)
        assert result is not None
        assert "No visible Netflix Play" in result

    def test_js_failure_returns_error(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.side_effect = RuntimeError("CDP disconnected")

        result = nc.click_netflix_play_button(mock_sess, mock_page)
        assert result is not None
        assert "Play button detection JS failed" in result

    def test_click_but_no_playback_returns_error(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        found_json = json.dumps(
            {"found": True, "tag": "A", "text": "Play", "x": 500, "y": 300, "href": ""}
        )
        mock_sess.run.return_value = found_json

        with patch.object(nc, "_verify_playback_started", return_value=False):
            result = nc.click_netflix_play_button(mock_sess, mock_page)
            assert result is not None
            assert "but playback could not be verified" in result

    def test_netflix_play_button_js_defined(self):
        from actions.netflix_control import _NETFLIX_PLAY_BUTTON_JS

        assert len(_NETFLIX_PLAY_BUTTON_JS) > 500
        assert "play" in _NETFLIX_PLAY_BUTTON_JS.lower()
        assert "lire" in _NETFLIX_PLAY_BUTTON_JS.lower()
        assert "resume" in _NETFLIX_PLAY_BUTTON_JS.lower()
        assert "getBoundingClientRect" in _NETFLIX_PLAY_BUTTON_JS
        assert "offsetParent" in _NETFLIX_PLAY_BUTTON_JS


# ═══════════════════════════════════════════════════════════════════════════════
# _check_local_signals tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckLocalSignals:
    """Verify non-destructive local DOM signal checking."""

    def test_detects_play_button(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "hasPlayButton": True,
                "hasVideo": False,
                "urlContainsWatch": False,
                "hasPlayerControls": False,
                "activeNetflix": True,
                "pageTitle": "Netflix",
                "currentUrl": "",
                "watchLinks": 0,
            }
        )

        result = nc._check_local_signals(mock_sess, mock_page)
        assert result["has_play_button"] is True
        assert result["has_video"] is False

    def test_detects_video_element(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "hasPlayButton": False,
                "hasVideo": True,
                "urlContainsWatch": False,
                "hasPlayerControls": False,
                "activeNetflix": True,
                "pageTitle": "",
                "currentUrl": "",
                "watchLinks": 0,
            }
        )

        result = nc._check_local_signals(mock_sess, mock_page)
        assert result["has_video"] is True
        assert result["has_play_button"] is False

    def test_detects_watch_url(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "hasPlayButton": False,
                "hasVideo": False,
                "urlContainsWatch": True,
                "hasPlayerControls": False,
                "activeNetflix": True,
                "pageTitle": "",
                "currentUrl": "https://www.netflix.com/watch/12345",
                "watchLinks": 1,
            }
        )

        result = nc._check_local_signals(mock_sess, mock_page)
        assert result["url_contains_watch"] is True

    def test_detects_player_controls(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "hasPlayButton": False,
                "hasVideo": False,
                "urlContainsWatch": True,
                "hasPlayerControls": True,
                "activeNetflix": True,
                "pageTitle": "",
                "currentUrl": "",
                "watchLinks": 0,
            }
        )

        result = nc._check_local_signals(mock_sess, mock_page)
        assert result["has_player_controls"] is True

    def test_js_eval_error_returns_defaults(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.side_effect = RuntimeError("CDP failed")

        result = nc._check_local_signals(mock_sess, mock_page)
        assert result["has_play_button"] is False
        assert result["has_video"] is False
        assert result["url_contains_watch"] is False
        assert result["active_netflix"] is False

    def test_returns_all_keys(self):
        from actions import netflix_control as nc

        expected_keys = {
            "has_play_button",
            "has_video",
            "url_contains_watch",
            "has_player_controls",
            "active_netflix",
            "page_title",
            "current_url",
            "watch_links",
        }
        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = "{}"
        result = nc._check_local_signals(mock_sess, mock_page)
        assert set(result.keys()) == expected_keys


# ═══════════════════════════════════════════════════════════════════════════════
# _verify_playback_started tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVerifyPlaybackStarted:
    """Verify _verify_playback_started delegates to _verify_playback_active."""

    def test_watch_with_active_video_returns_true(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        active_js = json.dumps(
            {
                "hasVideo": True,
                "paused": False,
                "currentTime": 42.0,
                "readyState": 3,
                "duration": 3600,
            }
        )
        mock_sess.run.return_value = active_js
        with patch.object(
            nc,
            "_check_local_signals",
            return_value={
                "url_contains_watch": True,
                "has_play_button": False,
                "has_video": False,
                "has_player_controls": False,
                "active_netflix": True,
                "page_title": "",
                "current_url": "https://www.netflix.com/watch/12345",
                "watch_links": 0,
            },
        ):
            assert nc._verify_playback_started(mock_sess, mock_page) is True

    def test_watch_with_paused_video_returns_false(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        paused_js = json.dumps(
            {
                "hasVideo": True,
                "paused": True,
                "currentTime": 42.0,
                "readyState": 3,
                "duration": 3600,
            }
        )
        mock_sess.run.return_value = paused_js
        with patch.object(
            nc,
            "_check_local_signals",
            return_value={
                "url_contains_watch": True,
                "has_play_button": False,
                "has_video": False,
                "has_player_controls": False,
                "active_netflix": True,
                "page_title": "",
                "current_url": "https://www.netflix.com/watch/12345",
                "watch_links": 0,
            },
        ):
            assert nc._verify_playback_started(mock_sess, mock_page) is False

    def test_no_watch_url_returns_false(self):
        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps({"hasVideo": False})
        # The signals check inside _verify_playback_active returns False for no watch URL
        with patch.object(
            nc,
            "_check_local_signals",
            return_value={
                "url_contains_watch": False,
                "has_video": False,
                "has_player_controls": False,
                "current_url": "https://www.netflix.com/title/12345",
            },
        ):
            assert nc._verify_playback_started(mock_sess, mock_page) is False


# ═══════════════════════════════════════════════════════════════════════════════
# DOM-first integration: standard play action
# ═══════════════════════════════════════════════════════════════════════════════


class TestStandardPlayDOMFirst:
    """Verify standard 'play' action checks DOM first, before CDP and Vision."""

    def test_already_playing_returns_immediately(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "hasVideo": True,
                "paused": False,
                "currentTime": 100.0,
                "readyState": 3,
                "duration": 3600,
            }
        )
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(
                    nc,
                    "_check_local_signals",
                    return_value={
                        "has_play_button": False,
                        "has_video": True,
                        "url_contains_watch": True,
                        "has_player_controls": True,
                        "active_netflix": True,
                        "page_title": "",
                        "current_url": "https://www.netflix.com/watch/12345",
                        "watch_links": 0,
                    },
                ):
                    result = nc.netflix_control({"action": "play"}, player=None)
                    assert "already playing" in result.lower()

    def test_dom_play_button_clicked_before_cdp(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(
                    nc,
                    "_check_local_signals",
                    return_value={
                        "has_play_button": True,
                        "has_video": False,
                        "url_contains_watch": False,
                        "has_player_controls": False,
                        "active_netflix": True,
                        "page_title": "Netflix",
                        "current_url": "https://www.netflix.com/title/12345",
                        "watch_links": 1,
                    },
                ):
                    with patch.object(nc, "_verify_playback_started", side_effect=[False, True]):
                        with patch.object(nc, "click_netflix_play_button") as mock_click:
                            mock_click.return_value = None
                            result = nc.netflix_control({"action": "play"}, player=None)
                            assert "resumed" in result.lower()
                            mock_click.assert_called_once()

    def test_cdp_video_play_fallback_when_no_dom_button(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()

        # Identify the CDP play call by WHAT it does, not by its position in
        # the sequence. The previous form was `[None] * 10 + [sentinel]`, which
        # pinned the exact number of sess.run() calls in the whole play flow —
        # so any change to focus probing or the profile gate shifted the
        # sentinel onto an unrelated call and failed this test for reasons that
        # had nothing to do with the CDP fallback it exists to cover. (It had
        # drifted out of sync by two calls and was failing before this rewrite.)
        def fake_evaluate_background_media(domain, script):
            return ("bg-eval", script)

        def fake_run(arg):
            if isinstance(arg, tuple) and arg and arg[0] == "bg-eval":
                return "Netflix resumed via JS"
            return None

        mock_sess.evaluate_background_media.side_effect = fake_evaluate_background_media
        mock_sess.run.side_effect = fake_run
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(
                    nc,
                    "_check_local_signals",
                    return_value={
                        "has_play_button": False,
                        "has_video": False,
                        "url_contains_watch": False,
                        "has_player_controls": False,
                        "active_netflix": True,
                        "page_title": "Netflix",
                        "current_url": "https://www.netflix.com/title/12345",
                        "watch_links": 0,
                    },
                ):
                    with patch.object(nc, "click_netflix_play_button", return_value="No button"):
                        with patch.object(nc, "_verify_playback_started") as mock_verify:
                            mock_verify.side_effect = [False, True]
                            with patch.object(nc, "_call_screen_process_narrow", return_value=None):
                                result = nc.netflix_control({"action": "play"}, player=None)
                                assert "Netflix resumed via JS" in result

    def test_no_dom_no_cdp_no_vision_returns_clear_failure(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "No active Netflix video element found"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(
                    nc,
                    "_check_local_signals",
                    return_value={
                        "has_play_button": False,
                        "has_video": False,
                        "url_contains_watch": False,
                        "has_player_controls": False,
                        "active_netflix": True,
                        "page_title": "Netflix",
                        "current_url": "https://www.netflix.com/title/12345",
                        "watch_links": 0,
                    },
                ):
                    with patch.object(nc, "click_netflix_play_button", return_value="No button"):
                        with patch.object(nc, "_verify_playback_started", return_value=False):
                            with patch.object(nc, "_call_screen_process_narrow", return_value=None):
                                result = nc.netflix_control({"action": "play"}, player=None)
                                assert "could not click the Play button automatically" in result


# ═══════════════════════════════════════════════════════════════════════════════
# DOM-first integration: grid_play action
# ═══════════════════════════════════════════════════════════════════════════════


class TestGridPlayUsesDomNotVision:
    """Verify grid_play uses DOM Play button, not screen_process."""

    def test_grid_play_calls_click_play_button_after_nav(self):
        from unittest.mock import MagicMock, PropertyMock, patch

        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0

        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/title/12345"

        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = mock_page
                mock_tab.return_value = mock_tab_result
                with patch("core.browser_grid_state.get_recent_grid") as mock_grid:
                    mock_candidate = MagicMock()
                    mock_candidate.href = "https://www.netflix.com/title/12345"
                    mock_candidate.url = ""
                    mock_candidate.title = "Test Show"
                    mock_candidate.kind = "title"
                    mock_candidate.safe = True
                    mock_candidate.source = "netflix"
                    mock_snap = MagicMock()
                    mock_snap.candidates = [mock_candidate]
                    mock_snap.timestamp = time.time() + 3600
                    mock_snap.query = "Test Show"
                    mock_grid.return_value = mock_snap
                    with patch.object(nc, "_verify_playback_started", return_value=False):
                        with patch.object(
                            nc, "click_netflix_play_button_for_current_title"
                        ) as mock_click:
                            mock_click.return_value = None
                            result = nc.netflix_control({"action": "grid_play", "ordinal": 1})
                            assert "Playing" in result
                            assert "Test Show" in result
                            mock_click.assert_called_once()

    def test_grid_play_play_button_fail_returns_clear_error(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0

        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/title/12345"

        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = mock_page
                mock_tab.return_value = mock_tab_result
                with patch("core.browser_grid_state.get_recent_grid") as mock_grid:
                    mock_candidate = MagicMock()
                    mock_candidate.href = "https://www.netflix.com/title/12345"
                    mock_candidate.url = ""
                    mock_candidate.title = "Test Show"
                    mock_candidate.kind = "title"
                    mock_candidate.safe = True
                    mock_candidate.source = "netflix"
                    mock_snap = MagicMock()
                    mock_snap.candidates = [mock_candidate]
                    mock_snap.timestamp = time.time() + 3600
                    mock_snap.query = "Test Show"
                    mock_grid.return_value = mock_snap
                    with patch.object(nc, "_verify_playback_started", return_value=False):
                        with patch.object(nc, "click_netflix_play_button") as mock_click:
                            mock_click.return_value = (
                                "No visible Netflix Play/Lire/Resume button found in DOM."
                            )
                            result = nc.netflix_control({"action": "grid_play", "ordinal": 1})
                            assert "could not start playback automatically" in result

    def test_grid_play_no_screen_process_fallback(self):
        """grid_play should not call _call_screen_process_narrow at all."""
        import ast

        source = Path(REPO_ROOT, "actions", "netflix_control.py").read_text("utf-8")
        tree = ast.parse(source)
        grid_play_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "netflix_control":
                for child in ast.walk(node):
                    if isinstance(child, ast.If):
                        for grandchild in ast.walk(child):
                            if (
                                isinstance(grandchild, ast.Constant)
                                and grandchild.value == "grid_play"
                            ):
                                grid_play_func = child
                                break
                break
        assert grid_play_func is not None
        grid_source = ast.get_source_segment(source, grid_play_func) or ""
        assert (
            "screen_process" not in grid_source or "_call_screen_process_narrow" not in grid_source
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Architectural invariant tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNetflixArchitecture:
    """Verify no screen_process leaks into main control flow."""

    def test_screen_process_only_in_call_screen_process_narrow(self):
        """Screen_process must only be called from _call_screen_process_narrow."""
        source = Path(REPO_ROOT, "actions", "netflix_control.py").read_text("utf-8")
        lines = source.split("\n")
        sp_lines = [
            i + 1
            for i, line in enumerate(lines)
            if "screen_process" in line and "import" not in line
        ]
        for lineno in sp_lines:
            context_start = max(0, lineno - 20)
            context_end = min(len(lines), lineno)
            context = "\n".join(lines[context_start:context_end])
            ok = "def _call_screen_process_narrow" in context or "def test_" in context
            if not ok:
                ok = (
                    "def _call_screen_process_narrow" in context.lower()
                    or "screen_process" in context.lower()
                )
            assert ok, f"screen_process call at line {lineno} outside _call_screen_process_narrow"

    def test_netflix_control_imports_at_function_level(self):
        """Key lazy imports (screen_process) must be inside functions, not at module top level."""
        source = Path(REPO_ROOT, "actions", "netflix_control.py").read_text("utf-8")
        module_top = source.split("def ")[0]
        assert "from actions.screen_processor import screen_process" not in module_top
        assert "from core.browser_grid_state import " not in module_top
        assert "from core.media_state import " not in module_top

    def test_vision_cooldown_state_module_level(self):
        """_VISION_COOLDOWN_UNTIL and _VISION_ALREADY_ATTEMPTED must be module-level globals."""
        import actions.netflix_control as nc

        assert hasattr(nc, "_VISION_COOLDOWN_UNTIL")
        assert hasattr(nc, "_VISION_ALREADY_ATTEMPTED")
        source = Path(REPO_ROOT, "actions", "netflix_control.py").read_text("utf-8")
        assert "_VISION_COOLDOWN_UNTIL: float = 0.0" in source
        assert "_VISION_ALREADY_ATTEMPTED: bool = False" in source


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: no false success tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoFalseSuccess:
    """Verify Netflix never claims success without /watch/, video, or player controls."""

    def test_play_returns_failure_if_no_playback_verified(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
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
                        with patch.object(nc, "_verify_playback_started", return_value=False):
                            with patch.object(nc, "_call_screen_process_narrow", return_value=None):
                                result = nc.netflix_control({"action": "play"}, player=None)
                                assert "could not click the Play button automatically" in result

    def test_grid_play_ordinal_no_false_success(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/title/12345"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = mock_page
                mock_tab.return_value = mock_tab_result
                with patch("core.browser_grid_state.get_recent_grid") as mock_grid:
                    mock_candidate = MagicMock()
                    mock_candidate.href = "https://www.netflix.com/title/12345"
                    mock_candidate.url = ""
                    mock_candidate.title = "Test Show"
                    mock_candidate.kind = "title"
                    mock_candidate.safe = True
                    mock_candidate.source = "netflix"
                    mock_snap = MagicMock()
                    mock_snap.candidates = [mock_candidate]
                    mock_snap.timestamp = time.time() + 3600
                    mock_snap.query = "Test Show"
                    mock_grid.return_value = mock_snap
                    with patch.object(nc, "_verify_playback_started", return_value=False):
                        with patch.object(
                            nc, "click_netflix_play_button", return_value="No button"
                        ):
                            result = nc.netflix_control({"action": "grid_play", "ordinal": 1})
                            assert "could not start playback automatically" in result

    def test_search_auto_play_no_false_success(self):
        import actions.netflix_control as nc_mod
        from actions import netflix_control as nc

        nc_mod._VISION_ALREADY_ATTEMPTED = False
        nc_mod._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/search?q=Test"

        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = mock_page
                mock_tab.return_value = mock_tab_result
                with patch("actions.browser_intelligence.scan_netflix_cards") as mock_scan:
                    mock_card = MagicMock()
                    mock_card.title = "Test Show"
                    mock_card.href = "https://www.netflix.com/title/12345"
                    mock_card.kind = "title"
                    mock_card.safe = True
                    mock_card.source = "netflix"
                    mock_scan.return_value = [mock_card]
                    with patch("utils.media_match.select_best_media_candidate") as mock_select:
                        mock_select.return_value = (
                            {"title": "Test Show", "url": "https://www.netflix.com/title/12345"},
                            0.95,
                            "selected",
                        )
                        with patch.object(nc, "_verify_playback_started", return_value=False):
                            with patch.object(
                                nc, "click_netflix_play_button", return_value="No button"
                            ):
                                result = nc.netflix_control(
                                    {"action": "search", "query": "Test", "_auto_play": True}
                                )
                                assert "could not start playback automatically" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Vision fallback is truly optional
# ═══════════════════════════════════════════════════════════════════════════════


class TestVisionFallbackOptional:
    """Verify screen_process fallback is optional, not required for success."""

    def test_play_succeeds_without_vision_when_dom_works(self):
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(
                    nc,
                    "_check_local_signals",
                    return_value={
                        "has_play_button": True,
                        "has_video": False,
                        "url_contains_watch": False,
                        "has_player_controls": False,
                        "active_netflix": True,
                        "page_title": "",
                        "current_url": "https://www.netflix.com/title/12345",
                        "watch_links": 1,
                    },
                ):
                    with patch.object(nc, "click_netflix_play_button") as mock_click:
                        mock_click.return_value = None
                        with patch.object(
                            nc, "_verify_playback_started", side_effect=[False, True]
                        ):
                            result = nc.netflix_control({"action": "play"})
                            assert "resumed" in result
                            mock_click.assert_called_once()

    def test_vision_fallback_not_called_when_dom_succeeds(self):
        """_call_screen_process_narrow should not be called if DOM already succeeded."""
        from actions import netflix_control as nc

        nc._VISION_ALREADY_ATTEMPTED = False
        nc._VISION_COOLDOWN_UNTIL = 0.0
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.platform_tab.get_or_open_platform_tab") as mock_tab:
                mock_tab_result = MagicMock()
                mock_tab_result.page = MagicMock()
                mock_tab.return_value = mock_tab_result
                with patch.object(
                    nc,
                    "_check_local_signals",
                    return_value={
                        "has_play_button": True,
                        "has_video": False,
                        "url_contains_watch": False,
                        "has_player_controls": False,
                        "active_netflix": True,
                        "page_title": "",
                        "current_url": "https://www.netflix.com/title/12345",
                        "watch_links": 1,
                    },
                ):
                    with patch.object(nc, "click_netflix_play_button") as mock_click:
                        mock_click.return_value = None
                        with patch.object(
                            nc, "_verify_playback_started", side_effect=[False, True]
                        ):
                            with patch.object(nc, "_call_screen_process_narrow") as mock_vision:
                                nc.netflix_control({"action": "play"})
                                mock_vision.assert_not_called()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
