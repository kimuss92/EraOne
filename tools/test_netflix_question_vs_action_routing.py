"""
Tests for the question-vs-action guard in core/self_healing_router.py.

Covers C9 (questions/analyses MUST NOT become grid_play) and C10
(imperative orders MUST still flow through to netflix_control grid_play)
from the Netflix selection project.

The guard `_is_grid_question_or_analysis` runs BEFORE `_route_ordinal_command`
so that interrogative or descriptive phrases — even when they contain
"first result" / "second title" / etc. — stay in their source tool
(typically `screen_process`) and are never turned into clicks.

Real imperative orders ("Select the first result.", "Click row 2 title 5.")
remain unaffected.

No real browser calls. No real YouTube/Netflix/network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from core.self_healing_router import (
    _is_grid_question_or_analysis,
    route_tool_call,
)

# ═════════════════════════════════════════════════════════════════════════════
# Pure-function tests on _is_grid_question_or_analysis
# ═════════════════════════════════════════════════════════════════════════════


class TestIsGridQuestionOrAnalysis:
    """Direct unit tests for the guard predicate."""

    # ── C9: Questions / analyses — must return True ────────────────────────

    def test_what_is_the_title_of_the_first_result(self):
        assert _is_grid_question_or_analysis("What is the title of the first result?") is True

    def test_what_is_the_title_of_first_search_result_war_machine(self):
        assert (
            _is_grid_question_or_analysis(
                "What is the title of the first search result shown for War Machine?"
            )
            is True
        )

    def test_tell_me_the_title_of_the_first_result(self):
        assert _is_grid_question_or_analysis("Tell me the title of the first result.") is True

    def test_identify_the_first_card(self):
        assert _is_grid_question_or_analysis("Identify the first card.") is True

    def test_describe_the_first_result(self):
        assert _is_grid_question_or_analysis("Describe the first result.") is True

    def test_what_do_you_see_on_netflix_grid(self):
        assert _is_grid_question_or_analysis("What do you see on the Netflix grid?") is True

    # ── Edge cases still flagged as questions ─────────────────────────────

    def test_any_trailing_question_mark_flagged(self):
        """Any text ending in '?' is treated as a question (even without keywords)."""
        assert _is_grid_question_or_analysis("Is the first result War Machine?") is True

    def test_what_starts_question(self):
        assert _is_grid_question_or_analysis("what is shown first?") is True

    def test_which_starts_question(self):
        assert _is_grid_question_or_analysis("which one is the title?") is True

    def test_look_at_starts_question(self):
        assert _is_grid_question_or_analysis("look at the second card") is True

    def test_analyze_starts_question(self):
        assert _is_grid_question_or_analysis("analyze the first row") is True

    def test_read_starts_question(self):
        assert _is_grid_question_or_analysis("read the third title") is True

    def test_title_of_the_second_pattern(self):
        assert _is_grid_question_or_analysis("give me the title of the second result") is True

    # ── C10: Imperative orders — must return False ────────────────────────

    def test_select_the_first_result(self):
        assert _is_grid_question_or_analysis("Select the first result.") is False

    def test_play_the_first_result(self):
        assert _is_grid_question_or_analysis("Play the first result.") is False

    def test_choose_the_first_title(self):
        assert _is_grid_question_or_analysis("Choose the first title.") is False

    def test_top_left(self):
        assert _is_grid_question_or_analysis("top left") is False

    def test_click_row_2_title_5(self):
        assert _is_grid_question_or_analysis("Click row 2 title 5.") is False

    def test_play_the_5th_title_in_row_2(self):
        assert _is_grid_question_or_analysis("Play the 5th title in row 2.") is False

    def test_i_mean_the_second_title_still_passes(self):
        """'I mean the second title' is a corrective imperative, not a question."""
        assert _is_grid_question_or_analysis("I mean the second title") is False

    def test_play_the_second_one(self):
        assert _is_grid_question_or_analysis("play the second one") is False

    # ── Empty / None / whitespace ──────────────────────────────────────────

    def test_empty_string(self):
        assert _is_grid_question_or_analysis("") is False

    def test_none(self):
        assert _is_grid_question_or_analysis(None) is False

    def test_whitespace(self):
        assert _is_grid_question_or_analysis("   ") is False


# ═════════════════════════════════════════════════════════════════════════════
# End-to-end routing: questions must NOT become grid_play
# ═════════════════════════════════════════════════════════════════════════════


class TestQuestionsDoNotRouteToGridPlay:
    """Integration tests: question phrases (C9) must not be diverted to grid_play.

    Even when the source tool is screen_process / media_coordinator /
    browser_control, the router must NOT translate the question into a
    netflix_control grid_play call.
    """

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        from core.browser_grid_state import GridSnapshot, clear_all_for_tests, save_grid

        clear_all_for_tests()
        # Seed a Netflix grid so ordinal routing COULD match if guard were absent.
        snap = GridSnapshot(
            platform="netflix",
            query="war machine",
            timestamp=__import__("time").time(),
        )
        save_grid(snap)
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_question_from_screen_process_stays_screen_process(self):
        """'What is the title of the first result?' from screen_process
        must stay screen_process, NOT become netflix_control grid_play."""
        tool, params = route_tool_call(
            "screen_process",
            {"text": "What is the title of the first result?"},
            "What is the title of the first result?",
        )
        assert tool != "netflix_control" or params.get("action") != "grid_play", (
            f"Question was diverted to netflix_control grid_play: {tool} {params}"
        )

    def test_question_from_media_coordinator_stays_media_coordinator(self):
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "What is the title of the first result?",
        )
        # Must not be netflix_control grid_play. May fall through to
        # media_coordinator play or stay on screen_process depending on path,
        # but never ordinal-diverted.
        if tool == "netflix_control":
            assert params.get("action") != "grid_play", (
                f"Question diverted to grid_play: {tool} {params}"
            )

    def test_all_c9_questions_do_not_produce_grid_play(self):
        """Every C9 example must NOT yield netflix_control grid_play."""
        c9_examples = [
            "What is the title of the first result?",
            "What is the title of the first search result shown for War Machine?",
            "Tell me the title of the first result.",
            "Identify the first card.",
            "Describe the first result.",
            "What do you see on the Netflix grid?",
        ]
        for text in c9_examples:
            tool, params = route_tool_call("media_coordinator", {"action": "play"}, text)
            assert not (tool == "netflix_control" and params.get("action") == "grid_play"), (
                f"C9 question diverted to grid_play: {text!r} → {tool} {params}"
            )

    def test_question_about_first_search_result_shown_for_war_machine(self):
        """The exact War Machine bug from C9/C10 spec."""
        tool, params = route_tool_call(
            "media_coordinator",
            {"action": "play"},
            "What is the title of the first search result shown for War Machine?",
        )
        assert not (tool == "netflix_control" and params.get("action") == "grid_play"), (
            f"Question about War Machine results diverted: {tool} {params}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# End-to-end routing: imperative orders MUST still become grid_play
# ═════════════════════════════════════════════════════════════════════════════


class TestImperativeOrdersStillRouteToGridPlay:
    """Imperative orders (C10) must still flow through to netflix_control grid_play.

    The guard ONLY blocks questions/analyses — it must NOT block real actions.
    """

    @pytest.fixture(autouse=True)
    def _mock_deps(self):
        from core.browser_grid_state import GridSnapshot, clear_all_for_tests, save_grid

        clear_all_for_tests()
        snap = GridSnapshot(
            platform="netflix",
            query="war machine",
            timestamp=__import__("time").time(),
        )
        save_grid(snap)
        with (
            patch("core.self_healing_router.load_state", return_value={}),
            patch("core.self_healing_router.get_active_title", return_value=""),
        ):
            yield
        clear_all_for_tests()

    def test_select_first_result_routes_to_grid_play(self):
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Select the first result."
        )
        # Either via _route_ordinal_command (ordinal=1) or via grid_position
        # (top-left → row=1 col=1). Both end in netflix_control grid_play.
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    def test_play_first_result_routes_to_grid_play(self):
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Play the first result."
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    def test_choose_first_title_routes_to_grid_play(self):
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Choose the first title."
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"

    def test_top_left_routes_to_grid_play_row_col(self):
        tool, params = route_tool_call("media_coordinator", {"action": "play"}, "Top left.")
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("row") == 1
        assert params.get("col") == 1

    def test_click_row_2_title_5_routes_to_grid_play(self):
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Click row 2 title 5."
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("row") == 2
        assert params.get("col") == 5

    def test_play_5th_title_in_row_2_routes_to_grid_play(self):
        tool, params = route_tool_call(
            "media_coordinator", {"action": "play"}, "Play the 5th title in row 2."
        )
        assert tool == "netflix_control"
        assert params.get("action") == "grid_play"
        assert params.get("row") == 2
        assert params.get("col") == 5

    def test_all_c10_orders_route_to_grid_play(self):
        """Every C10 example must yield netflix_control grid_play."""
        c10_examples = [
            "Select the first result.",
            "Play the first result.",
            "Choose the first title.",
            "Top left.",
            "Click row 2 title 5.",
            "Play the 5th title in row 2.",
        ]
        for text in c10_examples:
            tool, params = route_tool_call("media_coordinator", {"action": "play"}, text)
            assert tool == "netflix_control", (
                f"C10 order did not reach netflix_control: {text!r} → {tool} {params}"
            )
            assert params.get("action") == "grid_play", (
                f"C10 order did not become grid_play: {text!r} → {tool} {params}"
            )


# Note: `_route_ordinal_command` itself has dedicated coverage in
# `tools/test_browser_media_routing.py::TestRouteOrdinalCommand`. We do
# NOT duplicate it here — the guard sits OUTSIDE that helper and only
# short-circuits the call site in `route_tool_call`.
