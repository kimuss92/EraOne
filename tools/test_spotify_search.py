"""Spotify search: artist matching, verified playback, and the computer_settings bridge.

Regression for the 2026-09-28 trace: "Play some Snoop Dogg" answered "Playing
'Snoop Dogg'" while the previous song kept playing, and every
computer_settings(spotify_search, value=...) answered "No song name provided".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# `pytest.importorskip("pywinauto")` was not enough, and the difference matters:
# importorskip only converts ImportError into a skip. pywinauto imports comtypes,
# which calls CoInitializeEx() at import time, and if anything earlier in the same
# process already picked the other COM apartment model that raises
#
#     OSError: [WinError -2147417850] Cannot change thread mode after it is set
#
# which is NOT an ImportError, so it escaped the guard and became a COLLECTION
# ERROR — aborting the entire run before a single test executed.
#
# It is invisible in a normal `pytest` run only by luck of ordering: testpaths is
# "tools .", so this file is imported before the root-level test that sets the
# other mode. Selecting a subset in a different order (tools/era_gate.py does
# exactly that) reproduces it every time — test_memory_guard.py then this file
# errors; this file then test_memory_guard.py is clean.
#
# A machine-environment condition must cost this one file, never the run.
try:
    import pywinauto  # noqa: F401 - imported for availability only
except Exception as exc:  # noqa: BLE001 - ImportError, or comtypes' OSError above
    pytest.skip(f"pywinauto unavailable here: {exc}", allow_module_level=True)

from actions import spotify_control as sc  # noqa: E402
from actions import spotify_ui  # noqa: E402


class _Btn:
    def __init__(self, name, top=100):
        self._name, self._top = name, top
        self.invoked = self.clicked = 0

    def window_text(self):
        return self._name

    def rectangle(self):
        class R:
            pass

        r = R()
        r.top = self._top
        return r

    def invoke(self):
        self.invoked += 1

    def click_input(self):
        self.clicked += 1


# ── matching ────────────────────────────────────────────────────────────────


def test_artist_query_matches_track_rows_by_artist():
    rows = [
        _Btn("Play Sunroof by Nicky Youre", top=50),
        _Btn("Play Drop It Like It's Hot by Snoop Dogg, Pharrell Williams", top=200),
    ]
    target, level = spotify_ui._best_search_match(rows, "Snoop Dogg")
    assert target is rows[1]
    assert level >= 1


def test_exact_title_beats_artist_match():
    rows = [
        _Btn("Play Gin and Juice by Snoop Dogg", top=100),
        _Btn("Play Snoop Dogg", top=300),  # the artist card
    ]
    target, _ = spotify_ui._best_search_match(rows, "Snoop Dogg")
    assert target is rows[1]


def test_no_shared_word_is_still_no_match():
    rows = [_Btn("Play Sunroof by Nicky Youre")]
    assert spotify_ui._best_search_match(rows, "Snoop Dogg") == (None, 0)


# ── verified playback ───────────────────────────────────────────────────────


@pytest.fixture
def _search_env():
    """Stub everything around the search so only the verification logic runs."""
    with (
        patch.object(sc, "_enforce_spotify_priority"),
        patch.object(sc, "_spotify_window_exists", return_value=True),
        patch.object(sc, "_wait_for_spotify_ui_ready", return_value=(1, "Spotify")),
        patch.object(sc, "_get_spotify_hwnd_and_title", return_value=(1, "Spotify")),
        patch.object(sc, "_focus_spotify_window"),
        patch.object(sc, "_open_search"),
        patch.object(sc, "_mark_spotify_playing_state"),
        patch.object(sc, "_spotify_is_playing", return_value=True),
        patch.object(sc, "_wait_until", side_effect=lambda pred, *a, **k: pred()),
    ):
        yield


def test_search_reports_failure_when_track_never_changes(_search_env):
    old = ("Sunroof", "Nicky Youre")
    with (
        patch.object(sc, "_now_playing", return_value=old),
        patch.object(spotify_ui, "invoke_search_result", return_value=(True, "invoked")) as inv,
    ):
        msg = sc._spotify_search_and_play("Snoop Dogg")
    assert "could not start" in msg and "Sunroof" in msg
    # retried once with a real click
    assert inv.call_count == 2 and inv.call_args.kwargs.get("prefer_click") is True

    from core.tool_result import looks_like_failure

    assert looks_like_failure(msg)


def test_search_reports_the_track_that_actually_started(_search_env):
    readings = iter([("Sunroof", "Nicky Youre"), ("Gin and Juice", "Snoop Dogg")])
    with (
        patch.object(sc, "_now_playing", side_effect=lambda: next(readings)),
        patch.object(spotify_ui, "invoke_search_result", return_value=(True, "invoked")),
    ):
        msg = sc._spotify_search_and_play("Snoop Dogg")
    assert msg.startswith("Playing 'Gin and Juice' by Snoop Dogg")


def test_research_of_current_song_counts_as_success(_search_env):
    same = ("Gin and Juice", "Snoop Dogg")
    with (
        patch.object(sc, "_now_playing", return_value=same),
        patch.object(spotify_ui, "invoke_search_result", return_value=(True, "invoked")),
    ):
        msg = sc._spotify_search_and_play("gin and juice")
    assert msg.startswith("Playing 'Gin and Juice'")


def test_empty_query_is_a_failure():
    from core.tool_result import looks_like_failure

    assert looks_like_failure(sc._spotify_search_and_play(""))


# ── routing ─────────────────────────────────────────────────────────────────


def test_search_value_with_music_phrase_is_not_hijacked_to_play():
    with (
        patch.object(sc, "_spotify_search_and_play", return_value="searched") as srch,
        patch.object(sc, "_spotify_play", return_value="resumed"),
        patch("core.user_prefs.is_post_action_minimize", return_value=False),
    ):
        out = sc.spotify_control({"action": "search", "value": "chill music please"})
    assert out == "searched"
    srch.assert_called_once_with("chill music please")


def test_computer_settings_bridge_forwards_the_query():
    from actions import computer_settings as cs

    with patch("actions.spotify_control.spotify_control", return_value="ok") as ctl:
        cs.computer_settings({"action": "spotify_search", "value": "Snoop Dogg"})
    sent = ctl.call_args.args[0]
    assert sent == {"action": "search", "value": "Snoop Dogg"}
