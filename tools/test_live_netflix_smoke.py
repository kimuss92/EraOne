"""Unit tests for live_netflix_smoke.py — validates script logic with mocks.

These tests do NOT require a real browser, CDP connection, or Netflix account.
They test the diagnostic collection, argument parsing, and result formatting.
"""

from unittest.mock import MagicMock, patch

from tools.live_netflix_smoke import Diagnostics, _parse_args


class TestDiagnostics:
    def test_step_records_message(self):
        d = Diagnostics()
        d.step("hello")
        assert "hello" in d.steps

    def test_warn_records_message(self):
        d = Diagnostics()
        d.warn("bad")
        assert any("WARN: bad" in s for s in d.steps)

    def test_fail_records_message(self):
        d = Diagnostics()
        d.fail("broken")
        assert any("FAIL: broken" in s for s in d.steps)

    def test_report_empty(self):
        d = Diagnostics()
        r = d.report()
        assert "candidates: []" in r
        assert "current_url:" in r

    def test_report_with_candidates(self):
        d = Diagnostics()
        d.candidates = [{"title": "Pokémon", "url": "https://netflix.com/title/123"}]
        d.scan_scope = "search_container"
        d.current_url = "https://www.netflix.com/search?q=Pokémon"
        r = d.report()
        assert "Pokémon" in r
        assert "search_container" in r

    def test_report_with_rejected(self):
        d = Diagnostics()
        d.rejected_chrome = ["Continue Watching"]
        d.rejected_unrelated = ["Random Title"]
        r = d.report()
        assert "Continue Watching" in r
        assert "Random Title" in r


class TestParseArgs:
    def test_query_defaults_to_wild_robot(self):
        with patch("sys.argv", ["live_netflix_smoke.py"]):
            args = _parse_args()
            assert args.query == "The Wild Robot"

    def test_basic_query(self):
        with patch("sys.argv", ["live_netflix_smoke.py", "--query", "Pokémon"]):
            args = _parse_args()
            assert args.query == "Pokémon"
            assert args.timeout == 45
            assert args.dry_run is False

    def test_dry_run_flag(self):
        with patch("sys.argv", ["live_netflix_smoke.py", "--query", "Berlin", "--dry-run"]):
            args = _parse_args()
            assert args.dry_run is True

    def test_timeout_flag(self):
        with patch("sys.argv", ["live_netflix_smoke.py", "--query", "Test", "--timeout", "30"]):
            args = _parse_args()
            assert args.timeout == 30


class TestResultFormatting:
    def test_ok_result_format(self):
        result = "LIVE_NETFLIX_OK\nPlaying: Pokémon"
        assert result.startswith("LIVE_NETFLIX_OK")
        assert "Pokémon" in result

    def test_skipped_cdp_format(self):
        result = "LIVE_NETFLIX_SKIPPED_CDP_UNAVAILABLE"
        assert "SKIPPED" in result
        assert "CDP" in result

    def test_skipped_login_format(self):
        result = "LIVE_NETFLIX_SKIPPED_LOGIN_REQUIRED"
        assert "SKIPPED" in result
        assert "LOGIN" in result

    def test_failed_format(self):
        result = "LIVE_NETFLIX_FAILED\nreason: zero_cards"
        assert "FAILED" in result
        assert "zero_cards" in result

    def test_dry_run_ok_format(self):
        result = "LIVE_NETFLIX_OK_DRY_RUN\nMatched: Berlin"
        assert "DRY_RUN" in result
        assert "Berlin" in result


class TestCheckCdpAvailable:
    @patch("actions.browser_control.get_browser_session")
    def test_cdp_available(self, mock_get):
        mock_sess = MagicMock()
        mock_sess.run.return_value = "ok"
        mock_get.return_value = mock_sess
        from tools.live_netflix_smoke import _check_cdp_available

        ok, reason = _check_cdp_available()
        assert ok is True

    @patch("actions.browser_control.get_browser_session", side_effect=Exception("no browser"))
    def test_cdp_unavailable(self, mock_get):
        from tools.live_netflix_smoke import _check_cdp_available

        ok, reason = _check_cdp_available()
        assert ok is False
        assert "no browser" in reason


class TestCheckNetflixLogin:
    def test_logged_in_on_browse(self):
        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "url": "https://www.netflix.com/browse",
                "hasProfile": False,
                "hasLogin": False,
                "hasBrowse": True,
                "title": "Netflix",
            }
        )
        from tools.live_netflix_smoke import _check_netflix_login

        logged_in, reason = _check_netflix_login(mock_sess, mock_page)
        assert logged_in is True

    def test_login_page_detected(self):
        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "url": "https://www.netflix.com/Login",
                "hasProfile": False,
                "hasLogin": True,
                "hasBrowse": False,
                "title": "Login",
            }
        )
        from tools.live_netflix_smoke import _check_netflix_login

        logged_in, reason = _check_netflix_login(mock_sess, mock_page)
        assert logged_in is False
        assert "login" in reason

    def test_profile_chooser(self):
        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_sess.run.return_value = json.dumps(
            {
                "url": "https://www.netflix.com/browse",
                "hasProfile": True,
                "hasLogin": False,
                "hasBrowse": True,
                "title": "Who's Watching?",
            }
        )
        from tools.live_netflix_smoke import _check_netflix_login

        logged_in, reason = _check_netflix_login(mock_sess, mock_page)
        assert logged_in is False
        assert "profile" in reason


import json
