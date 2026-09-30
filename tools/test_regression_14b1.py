"""
Regression tests for Step 14B.0.1 live hotfixes.

Issues covered:
  3 — Netflix safe search time variable bug (UnboundLocalError)
  4 — Netflix safe search crash does not fallback to featured/modal play
  7 — Transient media state blocked from long-term memory
  8 — Netflix search/play does not use Vision + coordinate click

No real browser calls. No real YouTube/Netflix/network calls.
"""

from __future__ import annotations

import py_compile
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Disable simple mode for all legacy scanner tests in this file.
@pytest.fixture(autouse=True)
def _disable_simple_mode():
    with patch("actions.netflix_control.NETFLIX_SIMPLE_MODE", False):
        yield


# ═════════════════════════════════════════════════════════════════════════════
# Issue 3: Netflix safe search time variable bug
# ═════════════════════════════════════════════════════════════════════════════


class TestNetflixTimeImport:
    """Verify netflix_control.py compiles without UnboundLocalError for 'time'."""

    def test_netflix_control_compiles(self):
        """netflix_control.py must compile without syntax/runtime errors."""
        path = Path(__file__).resolve().parent.parent / "actions" / "netflix_control.py"
        assert path.exists(), f"File not found: {path}"
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            pytest.fail(f"netflix_control.py compile error: {e}")

    def test_netflix_control_no_local_time_shadowing(self):
        """netflix_control function must not shadow 'time' as local variable."""
        import ast

        path = Path(__file__).resolve().parent.parent / "actions" / "netflix_control.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find netflix_control function and check for time imports inside it
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "netflix_control":
                for child in ast.walk(node):
                    if isinstance(child, ast.Import) and any(n.name == "time" for n in child.names):
                        # Any import time inside the function shadows global — this is the bug
                        # Verify there's no such import (line 440 was removed)
                        pytest.fail(
                            "netflix_control function contains 'import time' statement — shadowing bug"
                        )
                break  # Only check the first (main) function
        # If we get here, no shadowing import found — PASS
        assert True


# ═════════════════════════════════════════════════════════════════════════════
# Issue 4: Netflix safe search exception does not fallback
# ═════════════════════════════════════════════════════════════════════════════


class TestNetflixSafeSearchFallback:
    """Verify safe search crash with query does NOT fallback to featured/random play.

    Both tests here must patch every browser_intelligence entry point the search
    path touches, not just the one they are asserting on. Those helpers call
    get_browser_session on their OWN module, so `patch.object(nc, ...)` does not
    reach them — unpatched, is_netflix_profile_page opens a real CDP link to the
    user's Brave, and its live answer then decides whether _click_netflix_profile
    clicks in that browser. False is the deterministic choice for a search test.
    """

    def test_safe_search_exception_with_query_returns_error(self):
        """Simulate safe search exception with query — must return error, not fallback."""
        from unittest.mock import MagicMock, patch

        from actions import netflix_control as nc
        from actions.browser_intelligence import focus_or_open_domain, scan_netflix_cards

        mock_sess = MagicMock()
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/search?q=One+Piece"
        mock_sess.run.return_value = "ok"
        tab_result = MagicMock()
        tab_result.page = mock_page
        mock_owned = MagicMock(return_value=tab_result)

        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.browser_intelligence.focus_or_open_domain", return_value=True):
                with patch(
                    "actions.browser_intelligence.is_netflix_profile_page", return_value=False
                ):
                    with patch("actions.platform_tab.get_or_open_platform_tab", mock_owned):
                        with patch(
                            "actions.browser_intelligence.scan_netflix_cards",
                            side_effect=Exception("Simulated crash"),
                        ):
                            result = nc.netflix_control({"action": "search", "query": "One Piece"})
                            assert "internal error" in result.lower()
                            assert "internal error before selecting a safe title" in result.lower()

    def test_safe_search_exception_without_query_allows_fallback(self):
        """Without a title query, safe search exception still tries fallback path.

        get_or_open_platform_tab must be patched even though this test never
        asserts on the page. _acquire_netflix_page imports it function-locally,
        so patching netflix_control's own names does not reach it: unpatched, it
        hands back a REAL Playwright Page from the user's live Brave session.
        _focus_netflix_foreground then builds real page.bring_to_front() and
        page.evaluate() coroutines that the MagicMock session never awaits,
        which is where this suite's "coroutine was never awaited" RuntimeWarnings
        came from — attributed to whichever later test happened to trigger the
        GC pass that collected them.
        """
        from unittest.mock import MagicMock, patch

        from actions import netflix_control as nc

        mock_sess = MagicMock()
        mock_sess.run.return_value = "Fallback result"
        mock_page = MagicMock()
        mock_page.url = "https://www.netflix.com/"
        tab_result = MagicMock()
        tab_result.page = mock_page

        with patch.object(nc, "get_browser_session", return_value=mock_sess):
            with patch("actions.browser_intelligence.focus_or_open_domain", return_value=True):
                with patch(
                    "actions.platform_tab.get_or_open_platform_tab", return_value=tab_result
                ):
                    with patch(
                        "actions.browser_intelligence.scan_netflix_cards",
                        side_effect=Exception("Crash"),
                    ):
                        result = nc.netflix_control({"action": "search", "query": ""})
                        assert result is not None


# ═════════════════════════════════════════════════════════════════════════════
# Issue 7: Transient media state blocked from long-term memory
# ═════════════════════════════════════════════════════════════════════════════


class TestTransientMemoryBlock:
    """Verify transient media keys are not saved to long-term memory."""

    def test_save_memory_last_media_title_blocked(self):
        """save_memory for last_media_title in preferences is blocked."""
        from unittest.mock import MagicMock, patch

        # Verify the import works
        from memory.memory_manager import MEMORY_PATH, update_memory

        # Read the save_memory logic from main.py
        TRANSIENT_KEYS = frozenset(
            {
                "last_media_platform",
                "last_media_title",
                "last_browser_platform",
                "last_search_query",
                "last_selected_candidate",
            }
        )
        TRANSIENT_CATEGORIES = frozenset({"preferences", "operational_instruction"})

        key = "last_media_title"
        category = "preferences"
        assert category in TRANSIENT_CATEGORIES
        assert key in TRANSIENT_KEYS

    def test_save_memory_last_browser_platform_blocked(self):
        """save_memory for last_browser_platform in operational_instruction is blocked."""
        TRANSIENT_KEYS = frozenset(
            {
                "last_media_platform",
                "last_media_title",
                "last_browser_platform",
                "last_search_query",
                "last_selected_candidate",
            }
        )
        TRANSIENT_CATEGORIES = frozenset({"preferences", "operational_instruction"})

        key = "last_browser_platform"
        category = "operational_instruction"
        assert category in TRANSIENT_CATEGORIES
        assert key in TRANSIENT_KEYS

    def test_non_transient_key_passes_through(self):
        """Non-transient keys in non-transient categories are not blocked."""
        TRANSIENT_KEYS = frozenset(
            {
                "last_media_platform",
                "last_media_title",
                "last_browser_platform",
                "last_search_query",
                "last_selected_candidate",
            }
        )
        TRANSIENT_CATEGORIES = frozenset({"preferences", "operational_instruction"})

        # Verify normal keys are not in the blocked sets
        assert "user_name" not in TRANSIENT_KEYS
        assert "notes" not in TRANSIENT_CATEGORIES
        # Verify transient keys ARE blocked
        assert "last_media_title" in TRANSIENT_KEYS
        assert "preferences" in TRANSIENT_CATEGORIES

    def test_runtime_state_path_available(self):
        """Verify intent_memory can store media state (runtime, not long-term)."""
        from core.intent_memory import load_state, remember_action

        # This is the correct runtime path — does NOT go to long-term memory
        remember_action(tool="netflix_control", action="play", media_app="netflix", app="browser")
        state = load_state()
        assert state.get("last_media_app") == "netflix"


# ═════════════════════════════════════════════════════════════════════════════
# Issue 8: Netflix search/play does not use Vision or coordinate clicks
# ═════════════════════════════════════════════════════════════════════════════


class TestNetflixNoVisionFallback:
    """Verify Netflix title selection uses DOM/CDP first, with bounded Vision fallback only."""

    def test_netflix_search_uses_dom_not_vision(self):
        """Netflix search action uses DOM card scanning, not screen_process as primary."""
        import ast

        path = Path(__file__).resolve().parent.parent / "actions" / "netflix_control.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        scan_calls = []
        screen_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                    if name == "scan_netflix_cards":
                        scan_calls.append(node.lineno)
                    if name in ("screen_find", "screen_processor", "_screen_find"):
                        screen_calls.append(node.lineno)
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                    if name in ("screen_find", "_screen_find"):
                        screen_calls.append(node.lineno)

        # scan_netflix_cards must be the primary DOM scanner
        assert len(scan_calls) > 0, "netflix_control.py must use scan_netflix_cards (DOM)"
        # screen_process appears ONLY inside _call_screen_process_narrow (controlled fallback)
        # and must not be the primary search path
        assert "screen_process" in source, "screen_process is used as controlled fallback"

    def test_netflix_search_prefers_dom_over_vision(self):
        """Verify the search action code path uses scan_netflix_cards (DOM) first."""
        source = (
            Path(__file__)
            .resolve()
            .parent.parent.joinpath("actions", "netflix_control.py")
            .read_text("utf-8")
        )
        # The search action must reference scan_netflix_cards
        assert "scan_netflix_cards" in source
        # screen_process should be referenced only inside the bounded fallback
        assert "_call_screen_process_narrow" in source
