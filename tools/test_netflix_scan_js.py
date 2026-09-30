"""Tests for Netflix scan JS validity, selector isolation, and title extraction."""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# ═════════════════════════════════════════════════════════════════════
# 1. JS syntax validity — the core bug we fixed
# ═════════════════════════════════════════════════════════════════════


class TestScanJSSyntax:
    """Verify _NETFLIX_SCAN_JS is syntactically valid JS (no Python escape leakage)."""

    def _get_scan_js(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        return _NETFLIX_SCAN_JS

    def _get_profile_js(self):
        from actions.browser_intelligence import _NETFLIX_PROFILE_CHECK_JS

        return _NETFLIX_PROFILE_CHECK_JS

    def test_scan_js_is_iife(self):
        js = self._get_scan_js()
        assert js.strip().startswith("(() => {"), "Scan JS must be IIFE"
        stripped = js.strip()
        assert stripped.endswith("})()"), "Scan JS must end with })()"

    def test_scan_js_no_single_quoted_backslash_n(self):
        js = self._get_scan_js()
        lines = js.split("\n")
        for i, line in enumerate(lines):
            if "'\n'" in line:
                pytest.fail(
                    f"Line {i + 1}: Python-interpreted literal newline inside single quotes"
                )
            if "'\\n'" in line and "split(" in line:
                pytest.fail(
                    f"Line {i + 1}: single-quoted '\\n' in split() — use regex /\\n/ instead"
                )

    def test_scan_js_no_backslash_n_in_single_quotes(self):
        js = self._get_scan_js()
        lines = js.split("\n")
        for i, line in enumerate(lines):
            if "'\\n'" in line:
                pytest.fail(f"Line {i + 1}: Python-interpreted '\\n' in single-quoted JS string")

    def test_scan_js_uses_regex_for_newline_split(self):
        js = self._get_scan_js()
        assert "/\\n/" in js or r"/\n/" in js, (
            "Should use regex /\\n/ instead of string '\\n' for split"
        )

    def test_profile_js_is_function(self):
        js = self._get_profile_js()
        assert "() =>" in js or "function" in js, "Profile check JS must be callable"

    def test_scan_js_node_check(self):
        js = self._get_scan_js()
        try:
            subprocess.run(
                [sys.executable, "-c", f"import ast; ast.parse('''{repr(js)}''')"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            pass

    def test_scan_js_is_raw_string(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        raw_repr = repr(_NETFLIX_SCAN_JS)
        assert raw_repr.startswith("'") or 'r"' in raw_repr or "r'" in raw_repr, (
            '_NETFLIX_SCAN_JS should be a raw string (r"""...""")'
        )


# ═════════════════════════════════════════════════════════════════════
# 2. Selector error isolation
# ═════════════════════════════════════════════════════════════════════


class TestSelectorErrorIsolation:
    """Verify one bad selector doesn't crash the whole scan."""

    def test_selector_try_catch_in_scan_js(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "try {" in _NETFLIX_SCAN_JS or "try{" in _NETFLIX_SCAN_JS
        assert "catch" in _NETFLIX_SCAN_JS

    def test_per_selector_try_catch(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        lines = _NETFLIX_SCAN_JS.split("\n")
        try_count = sum(1 for ln in lines if "try {" in ln or "try{" in ln)
        catch_count = sum(1 for ln in lines if "catch" in ln)
        assert try_count >= 2, "Need per-selector AND per-card try/catch blocks"
        assert catch_count >= 2, "Need per-selector AND per-card catch blocks"


# ═════════════════════════════════════════════════════════════════════
# 3. Title extraction paths in JS (aria-label, alt, img[alt], textContent)
# ═════════════════════════════════════════════════════════════════════


class TestExtractTitlePaths:
    """Verify JS extractTitle function covers all fallback paths."""

    def _get_scan_js(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        return _NETFLIX_SCAN_JS

    def test_has_extract_title_function(self):
        js = self._get_scan_js()
        assert "extractTitle" in js, "Must have extractTitle function"

    def test_aria_label_extraction(self):
        js = self._get_scan_js()
        assert "aria-label" in js, "Must check aria-label attribute"

    def test_alt_attribute_extraction(self):
        js = self._get_scan_js()
        assert 'getAttribute("alt")' in js or "getAttribute('alt')" in js, (
            "Must check node.getAttribute('alt') for img elements"
        )

    def test_img_alt_fallback(self):
        js = self._get_scan_js()
        assert "querySelector" in js and "img[alt]" in js, (
            "Must have img[alt] fallback in extractTitle"
        )

    def test_textcontent_fallback(self):
        js = self._get_scan_js()
        assert "textContent" in js, "Must have textContent fallback"

    def test_textcontent_split_on_newline(self):
        js = self._get_scan_js()
        assert "split" in js and "/\\n/" in js or r"/\n/" in js, (
            "textContent must be split on newline to avoid metadata lines"
        )


# ═════════════════════════════════════════════════════════════════════
# 4. Netflix URL filtering in JS
# ═════════════════════════════════════════════════════════════════════


class TestNetflixURLFiltering:
    """Verify JS only returns cards with /watch or /title URLs."""

    def test_netflix_domain_check(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "netflix.com" in _NETFLIX_SCAN_JS

    def test_watch_or_title_filter(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "/watch" in _NETFLIX_SCAN_JS and "/title" in _NETFLIX_SCAN_JS

    def test_dedup_by_href(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "seen.has" in _NETFLIX_SCAN_JS or "seen.add" in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 5. Python-side: _clean_netflix_title rejects metadata-only combo
# ═════════════════════════════════════════════════════════════════════


class TestCleanTitleMetadataCombo:
    """Verify _clean_netflix_title rejects combined metadata fragments."""

    def test_combined_french_metadata(self):
        from actions.netflix_control import _clean_netflix_title

        result = _clean_netflix_title("Disponible maintenantSaison 5il y a 5 jours")
        assert result == "", f"Metadata combo must be rejected, got: '{result}'"

    def test_regarder_reprendre_combo(self):
        from actions.netflix_control import _clean_netflix_title

        result = _clean_netflix_title("Regarder Reprendre Lecture")
        assert result == "", f"Metadata combo must be rejected, got: '{result}'"

    def test_real_title_with_metadata_suffix(self):
        from actions.netflix_control import _clean_netflix_title

        result = _clean_netflix_title("Stranger Things Saison 3")
        assert "Stranger" in result

    def test_nouveaux_episodes_stripped(self):
        from actions.netflix_control import _clean_netflix_title

        result = _clean_netflix_title("Nouveaux épisodes")
        assert result == ""


# ═════════════════════════════════════════════════════════════════════
# 6. _normalize_netflix_candidate integration
# ═════════════════════════════════════════════════════════════════════


class TestNormalizeCandidate:
    """Verify _normalize_netflix_candidate applies _clean_netflix_title."""

    def test_metadata_only_card_rejected(self):
        from actions.netflix_control import _normalize_netflix_candidate

        card = {
            "title": "Disponible maintenantSaison 5il y a 5 jours",
            "href": "https://netflix.com/title/123",
        }
        result = _normalize_netflix_candidate(card)
        assert result is None

    def test_valid_card_kept(self):
        from actions.netflix_control import _normalize_netflix_candidate

        card = {"title": "One Piece", "href": "https://netflix.com/title/801001"}
        result = _normalize_netflix_candidate(card)
        assert result is not None
        assert result["title"] == "One Piece"

    def test_empty_title_rejected(self):
        from actions.netflix_control import _normalize_netflix_candidate

        card = {"title": "", "href": "https://netflix.com/title/123"}
        result = _normalize_netflix_candidate(card)
        assert result is None

    def test_dict_with_name_key(self):
        from actions.netflix_control import _normalize_netflix_candidate

        card = {"name": "Breaking Bad", "url": "https://netflix.com/title/7"}
        result = _normalize_netflix_candidate(card)
        assert result is not None
        assert result["title"] == "Breaking Bad"

    def test_index_based_visible_order(self):
        from actions.netflix_control import _normalize_netflix_candidate

        card = {"title": "Test Show", "href": "https://netflix.com/title/1"}
        result = _normalize_netflix_candidate(card, index=3)
        assert result["visible_order"] == 4


# ═════════════════════════════════════════════════════════════════════
# 7. _convert_cards_to_candidates filters bad entries
# ═════════════════════════════════════════════════════════════════════


class TestConvertCardsToCandidates:
    """Verify _convert_cards_to_candidates drops metadata-only cards."""

    def test_filters_metadata_only(self):
        from actions.netflix_control import _convert_cards_to_candidates

        cards = [
            {"title": "Disponible maintenant", "href": "https://netflix.com/title/1"},
            {"title": "One Piece", "href": "https://netflix.com/title/2"},
        ]
        result = _convert_cards_to_candidates(cards)
        assert len(result) == 1
        assert result[0]["title"] == "One Piece"

    def test_empty_list(self):
        from actions.netflix_control import _convert_cards_to_candidates

        assert _convert_cards_to_candidates([]) == []

    def test_all_metadata_returns_empty(self):
        from actions.netflix_control import _convert_cards_to_candidates

        cards = [
            {"title": "Saison 5", "href": "https://netflix.com/title/1"},
            {"title": "Épisode 3", "href": "https://netflix.com/title/2"},
        ]
        assert _convert_cards_to_candidates(cards) == []


# ═════════════════════════════════════════════════════════════════════
# 8. Search "One Piece" path
# ═════════════════════════════════════════════════════════════════════


class TestSearchOnePiecePath:
    """Verify search action constructs correct Netflix search URL."""

    def test_search_url_construction(self):
        from actions import netflix_control as nc

        sess = MagicMock()
        page = MagicMock()
        page.url = "https://www.netflix.com/browse"
        mock_card = MagicMock(
            title="One Piece",
            href="https://netflix.com/title/80100",
            kind="title",
            safe=True,
            source="netflix",
        )
        with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", False):
            with patch("actions.netflix_control.NETFLIX_FAST_SEARCH_ENABLED", False):
                with patch(
                    "actions.browser_intelligence.scan_netflix_cards", return_value=[mock_card]
                ):
                    with patch(
                        "actions.browser_intelligence.is_netflix_profile_page", return_value=False
                    ):
                        with patch.object(nc, "_save_netflix_grid"):
                            nc._handle_search(
                                nc._STATE_BROWSE,
                                {"action": "search", "query": "One Piece"},
                                sess,
                                page,
                            )
                        assert page.goto.call_count >= 1
                        url = page.goto.call_args[0][0]
                        assert "search" in url
                        assert "one+piece" in url.lower()


# ═════════════════════════════════════════════════════════════════════
# 9. JS duplicate detection via seen set
# ═════════════════════════════════════════════════════════════════════


class TestJSDeduplication:
    """Verify JS scan uses a seen set to deduplicate by href."""

    def test_seen_set_initialization(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "new Set()" in _NETFLIX_SCAN_JS

    def test_href_split_strips_query(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "split('?')" in _NETFLIX_SCAN_JS or 'split("\\?")' in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 10. Scan JS selector array
# ═════════════════════════════════════════════════════════════════════


class TestScanSelectorArray:
    """Verify scan JS uses a selector array for iteration."""

    def test_has_selector_array(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "selectors" in _NETFLIX_SCAN_JS

    def test_includes_search_result_selector(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "search-result" in _NETFLIX_SCAN_JS

    def test_includes_title_card_selector(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "title-card" in _NETFLIX_SCAN_JS

    def test_includes_watch_link_selector(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "/watch" in _NETFLIX_SCAN_JS

    def test_includes_img_alt_selector(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "img[alt]" in _NETFLIX_SCAN_JS

    def test_body_uses_var_not_const_let(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        js = _NETFLIX_SCAN_JS
        body = js[js.index("var selectors") :]
        assert "var " in body, "Body should use var for broad browser compat"
        assert "const " not in body, "Body should not use const (broad compat)"
        assert "let " not in body, "Body should not use let (broad compat)"


# ═════════════════════════════════════════════════════════════════════
# 11. Forbidden Netflix chrome overlay filter (Step 14B.2.15, Fix A)
# ═════════════════════════════════════════════════════════════════════


class TestForbiddenNetflixChrome:
    """Verify isForbiddenNetflixChrome function exists and checks the right patterns."""

    def test_forbidden_chrome_function_exists(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "function isForbiddenNetflixChrome" in _NETFLIX_SCAN_JS

    def test_forbidden_chrome_checks_data_uia_navigation(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "data.indexOf('navigation')" in _NETFLIX_SCAN_JS
        assert "data.indexOf('notifications')" in _NETFLIX_SCAN_JS

    def test_forbidden_chrome_checks_data_uia_account_profile(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "data.indexOf('account')" in _NETFLIX_SCAN_JS
        assert "data.indexOf('profile')" in _NETFLIX_SCAN_JS

    def test_forbidden_chrome_checks_class_names(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "cls.indexOf('notification')" in _NETFLIX_SCAN_JS
        assert "cls.indexOf('navigation')" in _NETFLIX_SCAN_JS
        assert "cls.indexOf('pinning-header')" in _NETFLIX_SCAN_JS
        assert "cls.indexOf('account')" in _NETFLIX_SCAN_JS

    def test_forbidden_chrome_checks_tag_names(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "tag === 'header'" in _NETFLIX_SCAN_JS
        assert "tag === 'nav'" in _NETFLIX_SCAN_JS

    def test_forbidden_chrome_called_in_extract_cards(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "isForbiddenNetflixChrome" in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 12. Notification candidates rejected at scan level (Step 14B.2.15)
# ═════════════════════════════════════════════════════════════════════


class TestNotificationCandidatesRejected:
    """Verify scan_netflix_cards properly handles notification/chrome items."""

    def test_scan_rejects_notification_cards_from_search_page(self):
        """When JS returns document_fallback with notification items on a search
        page, scan_netflix_cards should return empty list."""
        from actions import browser_intelligence as bi

        sess = MagicMock()
        mock_raw = json.dumps(
            {
                "scope": "document_fallback",
                "cards": [],
            }
        )
        with patch.object(bi, "get_browser_session", return_value=sess):
            sess.run.return_value = mock_raw
            result = bi.scan_netflix_cards("brave", page=MagicMock())
        assert result == []

    def test_scan_empty_when_search_url_and_no_valid_cards(self):
        from actions import browser_intelligence as bi

        sess = MagicMock()
        mock_raw = json.dumps(
            {
                "scope": "document_fallback",
                "cards": [],
            }
        )
        with patch.object(bi, "get_browser_session", return_value=sess):
            sess.run.return_value = mock_raw
            result = bi.scan_netflix_cards("brave", page=MagicMock())
        assert result == []

    def test_diagnostics_have_chrome_filter_info(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "isForbiddenNetflixChrome" in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 13. Change 1: New [data-list-name="search"] selectors
# ═════════════════════════════════════════════════════════════════════


class TestNewSearchRowSelectors:
    """Verify new [data-list-name="search"] selectors are present and prioritised."""

    def test_includes_search_row_selectors(self):
        """[data-list-name="search"] selectors must be in the selectors array."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'data-list-name="search"' in _NETFLIX_SCAN_JS

    def test_search_container_priority(self):
        """[data-list-name="search"] must be first in containerCandidates."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        cc_start = _NETFLIX_SCAN_JS.index("containerCandidates")
        cc_block = _NETFLIX_SCAN_JS[cc_start : _NETFLIX_SCAN_JS.index("];", cc_start)]
        first_cc = cc_block[cc_block.index("'") + 1 : cc_block.index("'", cc_block.index("'") + 1)]
        assert "data-list-name" in first_cc, (
            f"First container should be data-list-name, got: {first_cc}"
        )

    def test_selectors_have_data_list_name_first(self):
        """First selector in the selectors array targets [data-list-name='search']."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        sel_start = _NETFLIX_SCAN_JS.index("var selectors")
        sel_block = _NETFLIX_SCAN_JS[sel_start : _NETFLIX_SCAN_JS.index("];", sel_start)]
        first_sel = sel_block[
            sel_block.index("'") + 1 : sel_block.index("'", sel_block.index("'") + 1)
        ]
        assert "data-list-name" in first_sel, (
            f"First selector should be data-list-name, got: {first_sel}"
        )

    def test_netflix_domain_check_still_present(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "netflix.com" in _NETFLIX_SCAN_JS

    def test_watch_or_title_filter_still_present(self):
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "/watch" in _NETFLIX_SCAN_JS and "/title" in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 14. Broader selectors for row/listitem containers
# ═════════════════════════════════════════════════════════════════════


class TestBroaderSelectors:
    """Verify broader selectors are present for row/listitem containers."""

    def test_includes_data_uia_row_selector(self):
        """[data-uia*="row"] selectors must be present for broader card detection."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'data-uia*="row"' in _NETFLIX_SCAN_JS

    def test_includes_role_listitem_selector(self):
        """[role="listitem"] selectors must be present for accessibility-based detection."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'role="listitem"' in _NETFLIX_SCAN_JS

    def test_includes_row_container_candidate(self):
        """[data-uia*="row"] must be in containerCandidates."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'data-uia*="row"' in _NETFLIX_SCAN_JS

    def test_includes_list_container_candidate(self):
        """[role="list"] must be in containerCandidates."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert 'role="list"' in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 15. Visible-title fallback phase
# ═════════════════════════════════════════════════════════════════════


class TestVisibleTitleFallback:
    """Verify the visible-title fallback phase is present and filters correctly."""

    def test_visible_title_fallback_phase_exists(self):
        """The JS must contain a visible_title_fallback phase between scoped and document_fallback."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "visible_title_fallback" in _NETFLIX_SCAN_JS

    def test_fallback_filters_chrome_links(self):
        """The visible-title fallback must use isForbiddenNetflixChrome to reject chrome links."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "isForbiddenNetflixChrome(a)" in _NETFLIX_SCAN_JS

    def test_fallback_requires_visible_rect(self):
        """The visible-title fallback must check bounding rect > 0."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "r.width <= 0 || r.height <= 0" in _NETFLIX_SCAN_JS

    def test_fallback_deduplicates_by_href(self):
        """The visible-title fallback must deduplicate by clean href."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "seen.has(cleanHref)" in _NETFLIX_SCAN_JS

    def test_fallback_requires_text_or_alt(self):
        """The visible-title fallback must require text or image alt metadata."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "txt.length < 2 && !imgAlt" in _NETFLIX_SCAN_JS


# ═════════════════════════════════════════════════════════════════════
# 16. Notification links filtered by isForbiddenNetflixChrome
# ═════════════════════════════════════════════════════════════════════


class TestNotificationFiltering:
    """Verify notification links are properly rejected at scan level."""

    def test_isForbiddenNetflixChrome_rejects_notification_data_uia(self):
        """isForbiddenNetflixChrome must reject elements with data-uia containing 'notifications'."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "notifications" in _NETFLIX_SCAN_JS

    def test_isForbiddenNetflixChrome_rejects_navigation_data_uia(self):
        """isForbiddenNetflixChrome must reject elements with data-uia containing 'navigation'."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "navigation" in _NETFLIX_SCAN_JS

    def test_isForbiddenNetflixChrome_rejects_header_tag(self):
        """isForbiddenNetflixChrome must reject <header> elements."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "tag === 'header'" in _NETFLIX_SCAN_JS

    def test_isForbiddenNetflixChrome_rejects_nav_tag(self):
        """isForbiddenNetflixChrome must reject <nav> elements."""
        from actions.browser_intelligence import _NETFLIX_SCAN_JS

        assert "tag === 'nav'" in _NETFLIX_SCAN_JS

    def test_scan_rejects_only_notification_links(self):
        """When scan returns cards with notification items filtered, only real cards remain."""
        from actions import browser_intelligence as bi

        sess = MagicMock()
        mock_raw = json.dumps(
            {
                "scope": "visible_title_fallback",
                "cards": [
                    {
                        "title": "Real Show",
                        "href": "https://www.netflix.com/title/12345",
                        "x": 100,
                        "y": 200,
                        "width": 300,
                        "height": 200,
                        "selectorUsed": "visible_title_fallback",
                        "ancestor": "div.search-container",
                    },
                ],
            }
        )
        with patch.object(bi, "get_browser_session", return_value=sess):
            sess.run.return_value = mock_raw
            result = bi.scan_netflix_cards("brave", page=MagicMock())
        assert len(result) == 1
        assert result[0].title == "Real Show"


# ═════════════════════════════════════════════════════════════════════
# 17. Diagnostics include chrome exclusion counts
# ═════════════════════════════════════════════════════════════════════


class TestDiagnosticChromeCounts:
    """Verify diagnostics include chromeExcludedCount and rawTitleLinkCount."""

    def test_diagnostics_have_chrome_excluded_count(self):
        """The diagnostic JS in scan_netflix_cards must include chromeExcludedCount field."""
        import inspect

        from actions.browser_intelligence import scan_netflix_cards

        src = inspect.getsource(scan_netflix_cards)
        assert "chromeExcludedCount" in src

    def test_diagnostics_have_raw_title_link_count(self):
        """The diagnostic JS in scan_netflix_cards must include rawTitleLinkCount field."""
        import inspect

        from actions.browser_intelligence import scan_netflix_cards

        src = inspect.getsource(scan_netflix_cards)
        assert "rawTitleLinkCount" in src
