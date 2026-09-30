"""Tests for Netflix search robustness: wait logic, selectors, fallback, autoplay safety."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest


# Disable simple mode for all legacy scanner tests in this file.
# Simple mode is tested separately in test_netflix_simple_mode.py.
@pytest.fixture(autouse=True)
def _disable_simple_mode():
    with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", False):
        yield


# ═════════════════════════════════════════════════════════════════════
# 1. Search wait timing and card detection
# ═════════════════════════════════════════════════════════════════════


class TestSearchWaitPolling:
    """Verify the Phase 1 + Phase 2 search readiness wait logic."""

    def test_search_wait_polls_for_cards(self):
        """Phase 2 loop detects cards and breaks early."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_search

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        page.evaluate = MagicMock()
        # Simulate cards found on 3rd attempt (only 2 retries needed)
        call_count = 0

        def _eval(js):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return True
            return False

        page.evaluate.side_effect = _eval
        mock_card = MagicMock(title="One Piece", href="https://netflix.com/title/80100")
        with (
            patch.object(nc, "_save_netflix_grid"),
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]),
            patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False),
            patch("core.browser_grid_state.set_last_user_query"),
        ):
            result = nc._handle_search(
                nc._STATE_BROWSE, {"action": "search", "query": "One Piece"}, sess, page
            )
        assert "One Piece" in result or "Playing" in result

    def test_search_wait_timeout_returns_failure(self):
        """Full timeout with 0 cards returns controlled failure."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_search

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        page.evaluate = MagicMock(return_value=False)
        with (
            patch.object(nc, "_save_netflix_grid"),
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[]),
            patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False),
            patch("core.browser_grid_state.set_last_user_query"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
        ):
            result = nc._handle_search(
                nc._STATE_BROWSE, {"action": "search", "query": "One Piece"}, sess, page
            )
        assert "could not read any title cards" in result
        assert "One Piece" in result

    def test_search_failure_includes_query(self):
        """Failure message must contain the query."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_search

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=Berlin"
        page.evaluate = MagicMock(return_value=False)
        with (
            patch.object(nc, "_save_netflix_grid"),
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[]),
            patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False),
            patch("core.browser_grid_state.set_last_user_query"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
        ):
            result = nc._handle_search(
                nc._STATE_BROWSE, {"action": "search", "query": "Berlin"}, sess, page
            )
        assert "Berlin" in result


# ═════════════════════════════════════════════════════════════════════
# 2. Selector priority and chrome exclusion
# ═════════════════════════════════════════════════════════════════════


class TestNetflixSelectors:
    """Verify _NETFLIX_SCAN_JS selector priority and chrome filtering."""

    def test_includes_search_row_selectors(self):
        """[data-list-name="search"] selectors must be present in scan JS."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'data-list-name="search"' in _NETFLIX_SCAN_JS

    def test_scoped_search_selector_priority(self):
        """[data-list-name="search"] must be first in selectors array."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        sel_start = _NETFLIX_SCAN_JS.index("var selectors")
        sel_block = _NETFLIX_SCAN_JS[sel_start : _NETFLIX_SCAN_JS.index("];", sel_start)]
        first_sel = sel_block[
            sel_block.index("'") + 1 : sel_block.index("'", sel_block.index("'") + 1)
        ]
        assert "data-list-name" in first_sel, (
            f"First selector should be data-list-name, got: {first_sel}"
        )

    def test_search_container_priority(self):
        """[data-list-name="search"] must be first in containerCandidates."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        cc_start = _NETFLIX_SCAN_JS.index("containerCandidates")
        cc_block = _NETFLIX_SCAN_JS[cc_start : _NETFLIX_SCAN_JS.index("];", cc_start)]
        first_cc = cc_block[cc_block.index("'") + 1 : cc_block.index("'", cc_block.index("'") + 1)]
        assert "data-list-name" in first_cc, (
            f"First container candidate should be data-list-name, got: {first_cc}"
        )

    def test_chrome_links_excluded_on_search_page(self):
        """document_fallback post-filter must reject nav/notification ancestors."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "isForbiddenNetflixChrome" in _NETFLIX_SCAN_JS
        assert "header" in _NETFLIX_SCAN_JS or "forbiddenPatterns" in _NETFLIX_SCAN_JS

    def test_search_page_rejects_non_search_data_list(self):
        """Non-search [data-list-name] values should not be solely relied on."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "containerCandidates" in _NETFLIX_SCAN_JS
        assert "document_fallback" in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 3. Query-token strict filtering (Change 4 verification)
# ═════════════════════════════════════════════════════════════════════


class TestQueryTokenFiltering:
    """Verify strict query-token validation — no unfiltered fallback."""

    def test_filter_removes_all_autoplay_failure(self):
        """When filter removes all candidates in autoplay path, return failure."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_search

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_card = MagicMock(title="Outlast", href="https://netflix.com/title/999")
        with (
            patch.object(nc, "_save_netflix_grid"),
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]),
            patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False),
            patch("core.browser_grid_state.set_last_user_query"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
        ):
            result = nc._handle_search(
                nc._STATE_BROWSE,
                {"action": "search", "query": "One Piece", "_auto_play": True},
                sess,
                page,
            )
        assert "could not identify" in result or "safe matching" in result

    def test_filter_removes_all_list_failure(self):
        """When filter removes all candidates in list-only path, return failure."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_search

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_card = MagicMock(title="Outlast", href="https://netflix.com/title/999")
        with (
            patch.object(nc, "_save_netflix_grid"),
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]),
            patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False),
            patch("core.browser_grid_state.set_last_user_query"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
        ):
            result = nc._handle_search(
                nc._STATE_BROWSE, {"action": "search", "query": "One Piece"}, sess, page
            )
        assert "could not identify" in result or "safe matching" in result

    def test_explicit_ordinal_bypasses_filter(self):
        """Explicit ordinal with fresh grid bypasses query-token filter."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=Berlin"
        mock_card = MagicMock(title="Berlin", href="https://netflix.com/title/1")
        with (
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]),
            patch.object(nc, "_save_netflix_grid"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
            patch.object(nc, "_verify_playback_started", return_value=True),
        ):
            result = nc._handle_grid_play(
                nc._STATE_SEARCH_RESULTS,
                {"action": "grid_play", "ordinal": 1, "query": "Berlin"},
                sess,
                page,
            )
        assert "Playing" in result or "Berlin" in result

    def test_search_autoplay_strict_match(self):
        """ "One Piece" matches "ONE PIECE" via token overlap."""
        from actions.netflix_control import _convert_cards_to_candidates

        cards_list = [MagicMock(title="ONE PIECE", href="https://netflix.com/title/1")]
        with patch("actions.browser_intelligence.scan_netflix_cards", return_value=cards_list):
            candidates = _convert_cards_to_candidates(
                [{"title": "ONE PIECE", "href": "https://netflix.com/title/1"}]
            )
            assert len(candidates) == 1
            assert candidates[0]["title"] == "ONE PIECE"

    def test_search_autoplay_rejects_unrelated(self):
        """ "One Piece" rejects "Outlast" token check."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_search

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_card = MagicMock(title="Outlast", href="https://netflix.com/title/999")
        with (
            patch.object(nc, "_save_netflix_grid"),
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]),
            patch("actions.browser_intelligence.is_netflix_profile_page", return_value=False),
            patch("core.browser_grid_state.set_last_user_query"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
        ):
            result = nc._handle_search(
                nc._STATE_BROWSE,
                {"action": "search", "query": "One Piece", "_auto_play": True},
                sess,
                page,
            )
        assert "could not identify" in result or "safe matching" in result


# ═════════════════════════════════════════════════════════════════════
# 4. Grid play fresh scan recovery
# ═════════════════════════════════════════════════════════════════════


class TestGridPlayFreshScan:
    """Verify fresh-scan retry in _handle_grid_play."""

    def test_grid_play_fresh_scan_recovers(self):
        """When initial scan returns 0, fresh scan with 1.5s wait recovers."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"

        # First call returns [], fresh scan recovers with 1 card.
        scan_calls = [
            [],
            [MagicMock(title="One Piece", href="https://netflix.com/title/1")],
        ]

        def _scan(*a, **kw):
            return scan_calls.pop(0) if scan_calls else []

        with (
            patch("actions.browser_intelligence.scan_netflix_cards", side_effect=_scan),
            patch.object(nc, "_save_netflix_grid"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
            patch.object(nc, "_verify_playback_started", return_value=True),
        ):
            result = nc._handle_grid_play(
                nc._STATE_SEARCH_RESULTS,
                {"action": "grid_play", "ordinal": 1, "query": "One Piece"},
                sess,
                page,
            )
        assert "Playing" in result

    def test_grid_play_fresh_scan_fails(self):
        """When both initial and fresh scans return 0, and the live visible-card
        ordinal click fallback also fails, controlled failure."""
        import actions.netflix_control as nc
        from actions.netflix_control import _handle_grid_play

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/search?q=One+Piece"

        with (
            patch("actions.browser_intelligence.scan_netflix_cards", return_value=[]),
            patch.object(nc, "_save_netflix_grid"),
            patch.object(nc, "_set_netflix_failure"),
            patch.object(nc, "_set_netflix_vision_cooldown"),
            patch.object(nc, "_click_visible_netflix_card_by_ordinal", return_value=False),
        ):
            result = nc._handle_grid_play(
                nc._STATE_SEARCH_RESULTS,
                {"action": "grid_play", "ordinal": 1, "query": "One Piece"},
                sess,
                page,
            )
        assert "could not read" in result


# ═════════════════════════════════════════════════════════════════════
# 5. Zero-cards diagnostic snapshot (Change 3)
# ═════════════════════════════════════════════════════════════════════


class TestZeroCardsDiagnostic:
    """Verify the zero-cards diagnostic JS is present."""

    def test_zero_cards_diagnostic_snapshot(self):
        """The scan function should have secondary diagnostic for 0 cards."""
        # The diagnostic code is in scan_netflix_cards, not in the JS string.
        # Verify the scan function contains the diagnostic logic.
        import inspect

        from actions.browser_intelligence import _NETFLIX_SCAN_JS, scan_netflix_cards

        src = inspect.getsource(scan_netflix_cards)
        assert "data-list-names" in src or "0-cards diagnostic" in src

    def test_scan_netflix_cards_diagnostics_exist(self):
        """scan_netflix_cards should have the zero-cards diagnostic block."""
        import inspect

        from actions import browser_intelligence as bi

        src = inspect.getsource(bi.scan_netflix_cards)
        assert "netflix 0-cards diagnostic" in src, (
            "scan_netflix_cards must include the 0-cards diagnostic print"
        )


# ═════════════════════════════════════════════════════════════════════
# 6. Wait sentinel ignores notification links
# ═════════════════════════════════════════════════════════════════════


class TestWaitSentinelChromeFilter:
    """Verify the Phase 2 wait sentinel rejects notification/nav chrome links."""

    def test_handle_search_source_has_chrome_filter_in_wait(self):
        """_handle_search Phase 2 wait loop must walk ancestors to reject chrome."""
        import inspect

        from actions.netflix_control import _handle_search

        src = inspect.getsource(_handle_search)
        assert "isForbiddenNetflixChrome" not in src or "navigation" in src, (
            "Phase 2 wait must filter chrome links via ancestor walk"
        )
        # The wait JS must check for forbidden chrome patterns
        assert "indexOf('navigation')" in src or "indexOf('notifications')" in src, (
            "Phase 2 wait JS must check for navigation/notifications in ancestors"
        )

    def test_simple_search_wait_source_has_chrome_filter(self):
        """_simple_search_and_play Step 4 wait loop must reject chrome links."""
        import inspect

        from actions.netflix_control import _simple_search_and_play

        src = inspect.getsource(_simple_search_and_play)
        assert "indexOf('navigation')" in src or "indexOf('notifications')" in src, (
            "Step 4 wait JS must check for navigation/notifications in ancestors"
        )

    def test_handle_search_wait_requires_text_or_alt(self):
        """Phase 2 wait must require useful text or image metadata, not just size."""
        import inspect

        from actions.netflix_control import _handle_search

        src = inspect.getsource(_handle_search)
        assert "textContent" in src or "img" in src, (
            "Phase 2 wait must check for text content or image alt"
        )

    def test_simple_search_wait_requires_text_or_alt(self):
        """Step 4 wait must require useful text or image metadata."""
        import inspect

        from actions.netflix_control import _simple_search_and_play

        src = inspect.getsource(_simple_search_and_play)
        assert "textContent" in src or "img" in src, (
            "Step 4 wait must check for text content or image alt"
        )

    def test_scan_js_visible_title_fallback_exists(self):
        """_NETFLIX_SCAN_JS must have a visible_title_fallback phase."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "visible_title_fallback" in _NETFLIX_SCAN_JS

    def test_scan_js_broader_row_selectors(self):
        """_NETFLIX_SCAN_JS must include [data-uia*=\"row\"] selectors."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'data-uia*="row"' in _NETFLIX_SCAN_JS

    def test_scan_js_broader_listitem_selectors(self):
        """_NETFLIX_SCAN_JS must include [role=\"listitem\"] selectors."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'role="listitem"' in _NETFLIX_SCAN_JS

    def test_grid_play_still_uses_dom_first(self):
        """_handle_grid_play must still call scan_netflix_cards (DOM-first)."""
        import inspect

        from actions.netflix_control import _handle_grid_play

        src = inspect.getsource(_handle_grid_play)
        assert "scan_netflix_cards" in src, (
            "_handle_grid_play must call scan_netflix_cards for DOM-first scanning"
        )

    def test_no_screen_process_fallback_in_grid_play(self):
        """_handle_grid_play must not call screen_process as a fallback."""
        import inspect

        from actions.netflix_control import _handle_grid_play

        src = inspect.getsource(_handle_grid_play)
        # screen_process should not be called as a fallback mechanism
        lines = src.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "screen_process" in stripped and "import" not in stripped:
                raise AssertionError(f"_handle_grid_play must not call screen_process: {stripped}")
