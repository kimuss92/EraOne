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
        patch("actions.spotify_search_worker.search_ui", return_value=(True, "clicked")) as inv,
    ):
        msg = sc._spotify_search_and_play("Snoop Dogg")
    assert "could not start" in msg and "Sunroof" in msg
    # retried once with a real click
    assert inv.call_count == 2 and inv.call_args.kwargs.get("retry") is True

    from core.tool_result import looks_like_failure

    assert looks_like_failure(msg)


def test_search_reports_the_track_that_actually_started(_search_env):
    readings = iter([("Sunroof", "Nicky Youre"), ("Gin and Juice", "Snoop Dogg")])
    with (
        patch.object(sc, "_now_playing", side_effect=lambda: next(readings)),
        patch("actions.spotify_search_worker.search_ui", return_value=(True, "clicked")),
    ):
        msg = sc._spotify_search_and_play("Snoop Dogg")
    assert msg.startswith("Playing 'Gin and Juice' by Snoop Dogg")


def test_research_of_current_song_counts_as_success(_search_env):
    same = ("Gin and Juice", "Snoop Dogg")
    with (
        patch.object(sc, "_now_playing", return_value=same),
        patch("actions.spotify_search_worker.search_ui", return_value=(True, "clicked")),
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


# ── Liked Songs / New Releases: named control, killable worker (2026-10-16) ──
class TestPrimaryContextPlay:
    def test_the_named_context_beats_the_stale_pages_first_play(self, monkeypatch):
        """Right after navigation the first 'Play <x>' can be the page being left."""
        stale, liked = _Btn("Play سواد الغيم"), _Btn("Play Liked Songs")
        pressed = []
        stale.invoke = lambda: pressed.append("stale")
        liked.invoke = lambda: pressed.append("liked")
        monkeypatch.setattr(spotify_ui, "Desktop", lambda backend: _FakeDesktop())
        monkeypatch.setattr(spotify_ui, "_wait_for_tree", lambda win, t: [stale, liked])
        ok, _ = spotify_ui.invoke_primary_play(1, timeout_s=1.0, prefer="Liked Songs")
        assert ok and pressed == ["liked"]

    def test_pause_on_the_named_context_means_already_playing(self, monkeypatch):
        monkeypatch.setattr(spotify_ui, "Desktop", lambda backend: _FakeDesktop())
        monkeypatch.setattr(
            spotify_ui, "_wait_for_tree", lambda win, t: [_Btn("Play X"), _Btn("Pause Liked Songs")]
        )
        assert spotify_ui.invoke_primary_play(1, 1.0, prefer="Liked Songs") == (
            True,
            "already playing",
        )

    def test_a_missing_named_control_fails_honestly_and_presses_nothing(self, monkeypatch):
        """New Releases' dead shortcut left Liked Songs on screen; its first Play ran."""
        other = _Btn("Play Liked Songs")
        monkeypatch.setattr(spotify_ui, "Desktop", lambda backend: _FakeDesktop())
        monkeypatch.setattr(spotify_ui, "_wait_for_tree", lambda win, t: [other])
        monkeypatch.setattr(spotify_ui, "_buttons", lambda win: [other])
        ok, detail = spotify_ui.invoke_primary_play(1, 0.05, prefer="Release Radar")
        assert not ok and "Release Radar" in detail
        assert other.invoked == other.clicked == 0

    def test_new_releases_opens_release_radar_from_the_sidebar(self, monkeypatch):
        monkeypatch.setattr(sc.threading.Thread, "start", lambda *a: None)
        monkeypatch.setattr(sc, "_spotify_window_exists", lambda: True)
        monkeypatch.setattr(sc, "_wait_for_spotify_ui_ready", lambda cold: (1, "Spotify"))
        monkeypatch.setattr(sc, "_get_spotify_hwnd_and_title", lambda: (1, "Spotify"))
        monkeypatch.setattr(sc, "_verify_playback_started", lambda timeout_s: True)
        monkeypatch.setattr(sc, "_mark_spotify_playing_state", lambda: None)
        monkeypatch.setattr(
            sc.pyautogui, "hotkey", lambda *a: pytest.fail("dead Alt+Shift+N shortcut")
        )
        calls = []
        monkeypatch.setattr(
            "actions.spotify_search_worker.primary_ui",
            lambda label, **kw: calls.append((label, kw.get("open_row"))) or (True, "invoked"),
        )
        assert "Release Radar" in sc._spotify_play_new_releases()
        assert calls == [("Release Radar", "Release Radar")]

    def test_the_sidebar_row_is_opened_not_its_play_button(self, monkeypatch):
        play, row = _Btn("Play Release Radar"), _Btn("Release Radar Playlist • Made for K")
        monkeypatch.setattr(spotify_ui, "Desktop", lambda backend: _FakeDesktop())
        monkeypatch.setattr(spotify_ui, "_buttons", lambda win: [play, row])
        assert spotify_ui.open_library_row(1, "Release Radar", timeout_s=0.5)
        assert row.clicked == 1 and play.clicked == play.invoked == 0

    def test_context_play_runs_in_the_killable_worker_not_in_process(self, monkeypatch):
        monkeypatch.setattr(sc.threading.Thread, "start", lambda *a: None)
        monkeypatch.setattr(sc, "_spotify_window_exists", lambda: True)
        monkeypatch.setattr(sc, "_open_spotify_uri", lambda *a: True)
        monkeypatch.setattr(sc, "_wait_for_spotify_ui_ready", lambda cold: (1, "Spotify"))
        monkeypatch.setattr(sc, "_get_spotify_hwnd_and_title", lambda: (1, "Spotify"))
        monkeypatch.setattr(sc, "_verify_playback_started", lambda timeout_s: True)
        monkeypatch.setattr(sc, "_mark_spotify_playing_state", lambda: None)
        monkeypatch.setattr(
            spotify_ui, "invoke_primary_play", lambda *a, **k: pytest.fail("in-process UIA")
        )
        calls = []
        monkeypatch.setattr(
            "actions.spotify_search_worker.primary_ui",
            lambda label, **kw: calls.append((label, kw)) or (True, "invoked"),
        )
        assert sc._spotify_play_liked_songs() == "Playing your Liked Songs, sir."
        assert calls and calls[0][0] == "Liked Songs"

    def test_primary_ui_asks_the_worker_for_the_named_context(self, monkeypatch):
        import json
        from unittest.mock import Mock

        from actions import spotify_search_worker as worker

        process = Mock()
        process.poll.return_value = 0
        process.communicate.return_value = ('ERA_SPOTIFY_RESULT:{"ok": true, "detail": "x"}', "")
        monkeypatch.setattr(worker.subprocess, "Popen", Mock(return_value=process))
        assert worker.primary_ui("Liked Songs", cold=False, timeout_s=5.0) == (True, "x")
        payload = json.loads(process.communicate.call_args_list[0].args[0])
        assert payload["mode"] == "primary" and payload["label"] == "Liked Songs"


class _FakeDesktop:
    def window(self, handle):
        return object()


# ── Stale / off-screen results and late verification (2026-10-16 live run) ──
class _Win:
    def __init__(self, top=0, bottom=1000):
        self._r = type("R", (), {"top": top, "bottom": bottom})()

    def rectangle(self):
        return self._r


class TestSearchAfterNavigation:
    def test_a_generic_shared_word_is_not_a_match(self):
        """'Konoha Music Station' shares only 'music' with the query: no match."""
        rows = [_Btn("Play Konoha Music Station 🍃")]
        assert spotify_ui._best_search_match(rows, "سواد الغيم Mikasa Music") == (None, 0)

    def test_the_real_result_still_matches_without_by(self):
        rows = [_Btn("Play Konoha Music Station"), _Btn("Play سواد الغيم by Mikasa Music")]
        target, level = spotify_ui._best_search_match(rows, "سواد الغيم Mikasa Music")
        assert target is rows[1] and level >= 4

    def test_off_screen_rows_are_dropped(self):
        rows = [_Btn("Play A", top=300), _Btn("Play B", top=2043)]
        assert spotify_ui._on_screen(rows, _Win()) == [rows[0]]

    def test_the_previous_page_is_never_clicked(self, monkeypatch):
        old = _Btn("Play سواد الغيم by Mikasa Music")  # also on the stale page
        new = _Btn("Play سواد الغيم by Mikasa Music", top=200)
        pages = iter([[old], [old], [new, _Btn("Play Other")]])
        last = {"page": [old]}

        def buttons(win):
            last["page"] = next(pages, last["page"])
            return last["page"]

        monkeypatch.setattr(spotify_ui, "Desktop", lambda backend: _FakeWinDesktop())
        monkeypatch.setattr(spotify_ui, "_buttons", buttons)
        monkeypatch.setattr(spotify_ui, "sleep_cancellable", lambda s: None)
        ok, _ = spotify_ui.invoke_search_result(
            1,
            "سواد الغيم by Mikasa Music",
            timeout_s=2.0,
            stale_names=spotify_ui.play_names([old]),
        )
        assert ok and new.invoked + new.clicked == 1 and old.invoked + old.clicked == 0

    def test_a_track_that_starts_late_is_still_success(self, monkeypatch):
        """'Konoba On Our Knees' was playing while Era reported it could not start."""
        reads = iter([("Bad Decisions", "benny blanco")] + [("On Our Knees", "Konoba")] * 50)
        monkeypatch.setattr(sc.threading.Thread, "start", lambda *a: None)
        monkeypatch.setattr(sc, "_spotify_window_exists", lambda: True)
        monkeypatch.setattr(sc, "_spotify_is_playing", lambda: True)
        monkeypatch.setattr(sc, "_mark_spotify_playing_state", lambda: None)
        monkeypatch.setattr(sc, "_now_playing", lambda: next(reads))
        waits = []

        def wait(before, query, timeout_s=5.0):
            waits.append(timeout_s)
            now = sc._now_playing()
            return now if sc._track_matches(query, now) else None

        monkeypatch.setattr(sc, "_wait_for_new_track", wait)
        monkeypatch.setattr(
            "actions.spotify_search_worker.search_ui", lambda *a, **k: (True, "clicked")
        )
        out = sc._spotify_search_and_play("Konoba On Our Knees")
        assert out.startswith("Playing 'On Our Knees' by Konoba")
        assert waits[0] >= 3.5  # long enough for Spotify's late media-session update


class _FakeWinDesktop:
    def window(self, handle):
        return _Win()


def test_a_slow_first_load_is_retried_once_by_itself(monkeypatch):
    """'Try again' always worked: the first search raced Spotify still loading."""
    monkeypatch.setattr(sc.threading.Thread, "start", lambda *a: None)
    monkeypatch.setattr(sc, "_spotify_window_exists", lambda: True)
    monkeypatch.setattr(sc, "_spotify_is_playing", lambda: True)
    monkeypatch.setattr(sc, "_mark_spotify_playing_state", lambda: None)
    tracks = {"now": ("Old", "Someone")}
    monkeypatch.setattr(sc, "_now_playing", lambda: tracks["now"])
    monkeypatch.setattr(
        sc,
        "_wait_for_new_track",
        lambda before, q, timeout_s=5.0: (
            tracks["now"] if tracks["now"][0] == "سواد الغيم" else None
        ),
    )
    attempts = []

    def search_ui(query, **kw):
        attempts.append(kw)
        if len(attempts) == 1:
            return False, "Spotify's window timed out; its UI worker was stopped"
        tracks["now"] = ("سواد الغيم", "Mikasa Music")
        return True, "clicked"

    monkeypatch.setattr("actions.spotify_search_worker.search_ui", search_ui)
    out = sc._spotify_search_and_play("سواد الغيم by Mikasa Music")
    assert out == "Playing 'سواد الغيم' by Mikasa Music, sir."
    assert len(attempts) == 2 and attempts[1]["cold"] is False
