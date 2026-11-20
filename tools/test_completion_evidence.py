"""PF-030: "couldn't confirm it took effect" only when there really was an effect to confirm.

Live 2026-11-16 10:33: "new releases" -> "Playing your Release Radar ... Now:
Spotify ... is playing" was tagged NOT_VERIFIED: playlist actions had no expected
state, so fresh telemetry showing playback could never confirm them.
Live 2026-11-16 10:44: "What's news about Protocol One?" (action=report, a read)
was spoken "... sir — though I couldn't confirm it took effect."
Window move/focus (11 calls in two days) were never confirmed though the handler
read the monitor back from Windows.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from actions import window_evidence
from app.era_live.speak_once import spoken_outcome
from core import live_app_state as live
from core.completion_contract import annotate, is_read_only, is_unconfirmed, verdict
from core.tool_result import ToolResult


def state(app="spotify", running=True, windows=("visible",), playback="unknown"):
    return {
        "app": app,
        "observed_at": 0.0,
        "source": "live_os",
        "process_running": running,
        "pids": [1] if running else [],
        "windows": None if windows is None else [{"title": app, "state": s} for s in windows],
        "audio_session": "active" if playback == "playing" else "inactive",
        "playback": playback,
        "errors": [],
    }


def refresh(monkeypatch, tool, params, observed, raw="Done."):
    monkeypatch.setattr(live, "collect_live_state", lambda target: observed)
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    return asyncio.run(live.refresh_after_command(tool, params, raw))


# ── 1. A read has nothing to confirm ────────────────────────────────────────


def test_the_live_protocol_one_report_is_never_called_unconfirmed():
    result = annotate(
        ToolResult.ok("Protocol One report protocol_one_x.md, written 14 h ago."),
        "protocol_one",
        "report",
    )
    assert verdict(result, "protocol_one", "report") == "informational"
    said = spoken_outcome("protocol_one", result)
    assert "couldn't confirm" not in said
    assert said.startswith("Protocol One report")


@pytest.mark.parametrize(
    "tool, action",
    [
        ("protocol_one", "status"),
        ("protocol_one", "recent"),
        ("netflix_control", "status"),
        ("spotify_control", "current_track_data"),
        ("scheduled_task", "list"),
        ("audio_control", "list_devices"),
        ("gmail_control", "search"),
        ("code_mode", "status"),
        ("set_microphone", None),
        ("shutdown_era", None),
    ],
)
def test_reads_and_eras_own_switches_are_informational(tool, action):
    result = annotate(ToolResult.ok("Answer."), tool, action)
    assert is_read_only(tool, action)
    assert not is_unconfirmed(result)


@pytest.mark.parametrize(
    "tool, action",
    [
        ("protocol_one", "launch"),
        ("spotify_control", "search"),  # search STARTS playback on Spotify
        ("netflix_control", "search"),
        ("send_message", "send"),
    ],
)
def test_real_actions_still_need_evidence(tool, action):
    result = annotate(ToolResult.ok("Done."), tool, action)
    assert not is_read_only(tool, action)
    assert is_unconfirmed(result)


# ── 2. Spotify playlists are checked against fresh playback ─────────────────


def test_release_radar_confirmed_by_fresh_playback(monkeypatch):
    raw = "Playing your Release Radar — this week's new releases, sir."
    r = refresh(
        monkeypatch,
        "spotify_control",
        {"action": "new_releases"},
        state(windows=("minimized",), playback="playing"),
        raw,
    )
    assert r.verified and r.verification_status == "VERIFIED"
    said = spoken_outcome("spotify_control", annotate(r, "spotify_control", "new_releases"))
    assert said == "Spotify is playing your Release Radar, sir."


def test_a_playlist_that_did_not_start_says_so(monkeypatch):
    r = refresh(monkeypatch, "spotify_control", {"action": "liked_songs"}, state(playback="paused"))
    assert not r.verified
    said = spoken_outcome("spotify_control", annotate(r, "spotify_control", "liked_songs"))
    assert "your Liked Songs" in said and "paused" in said
    assert "couldn't confirm it took effect" not in said


# ── 3. Window read-backs are evidence ───────────────────────────────────────


@pytest.mark.parametrize(
    "action, message",
    [
        ("move_to_monitor", "Moved 'Netflix - Brave' to screen 1."),
        ("move_to_monitor", "'Netflix - Brave' is already on screen 2."),
        ("minimize", "Minimized: Spotify"),
        ("maximize", "Window 'Brave' is already maximized."),
    ],
)
def test_a_read_back_window_result_is_verified(action, message):
    r = window_evidence.with_evidence(action, {"title": "x"}, message)
    assert isinstance(r, ToolResult) and r.verified and r.success
    assert r.verifier_name == "window_native"
    assert r.message == message


@pytest.mark.parametrize(
    "action, message",
    [
        ("move_to_monitor", "Tried to move 'Brave' to screen 1, but it stayed where it was."),
        ("minimize", "Failed to minimize 'Brave'."),
        ("focus", "No open window matching 'Brave'."),
    ],
)
def test_a_failure_stays_the_handlers_own_string(action, message):
    # A failed ToolResult is falsy, and the dispatcher does `r or "Done."`.
    assert window_evidence.with_evidence(action, {"title": "Brave"}, message) == message


class _FakeWin32Gui:
    """Two Brave windows (HWND 100 and 200, same process) and Notepad (300)."""

    captions = {100: "Brave - Inbox", 200: "Brave - Netflix", 300: "notes about Brave.txt"}

    def __init__(self, foreground):
        self.foreground = foreground

    def GetForegroundWindow(self):  # noqa: N802 - mirrors pywin32
        return self.foreground

    def GetAncestor(self, hwnd, flag):  # noqa: N802
        return hwnd  # top-level windows: their own root owner

    def GetWindowText(self, hwnd):  # noqa: N802
        return self.captions.get(hwnd, "")


def _focus_with(monkeypatch, foreground, acted):
    import sys

    monkeypatch.setitem(sys.modules, "win32gui", _FakeWin32Gui(foreground))
    monkeypatch.setattr(window_evidence.time, "sleep", lambda s: None)
    return window_evidence.with_evidence(
        "focus", {"title": "Brave"}, "Focused Brave - Netflix.", hwnd=acted
    )


def test_focus_is_verified_when_the_acted_window_is_in_front(monkeypatch):
    r = _focus_with(monkeypatch, foreground=200, acted=200)
    assert isinstance(r, ToolResult) and r.verified
    assert "Brave - Netflix" in r.verification_message


@pytest.mark.parametrize(
    "foreground, acted",
    [
        (100, 200),  # another window of the SAME Brave process is in front
        (300, 200),  # a different app whose caption mentions "Brave" is in front
        (200, None),  # the action never recorded which window it acted on
    ],
)
def test_focus_never_confirms_a_different_window(monkeypatch, foreground, acted):
    r = _focus_with(monkeypatch, foreground=foreground, acted=acted)
    assert r == "Focused Brave - Netflix."


def test_window_control_reads_back_the_window_it_acted_on(monkeypatch):
    from actions import window_control as wc

    seen = {}

    def fake_focus(title):
        wc._note_acted(200)
        return "Focused Brave - Netflix."

    def fake_evidence(action, params, message, hwnd=None):
        seen["hwnd"] = hwnd
        return message

    wc._note_acted(999)  # a previous call's window must never leak in
    monkeypatch.setitem(wc.ACTION_MAP, "focus", fake_focus)
    monkeypatch.setattr(window_evidence, "with_evidence", fake_evidence)
    wc.window_control({"action": "focus", "title": "Brave"})
    assert seen["hwnd"] == 200

    monkeypatch.setitem(wc.ACTION_MAP, "focus", lambda title: "No open window matching 'x'.")
    wc.window_control({"action": "focus", "title": "x"})
    assert seen["hwnd"] is None


def test_native_proof_survives_a_probe_that_finds_no_process(monkeypatch):
    raw = window_evidence.with_evidence(
        "move_to_monitor", {"title": "ERA ONE"}, "Moved 'ERA ONE' to screen 1."
    )
    r = refresh(
        monkeypatch,
        "window_control",
        {"action": "move_to_monitor", "title": "ERA ONE", "screen": "primary"},
        state(app="era one", running=False, windows=[]),
        raw,
    )
    assert r.verified and r.verification_status == "VERIFIED"
    assert r.message == "Moved 'ERA ONE' to screen 1."
    assert "not running" not in r.message and "don't see" not in r.message
    assert not is_unconfirmed(annotate(r, "window_control", "move_to_monitor"))


def test_native_proof_keeps_the_probe_as_context_when_the_app_is_seen(monkeypatch):
    raw = window_evidence.with_evidence(
        "move_to_monitor", {"title": "Brave"}, "Moved 'Brave' to screen 1."
    )
    r = refresh(
        monkeypatch,
        "window_control",
        {"action": "move_to_monitor", "title": "Brave", "screen": "primary"},
        state(app="brave"),
        raw,
    )
    assert r.verified
    assert r.message.startswith("Moved 'Brave' to screen 1. Now: Brave is running")


# ── 4. Spotify volume keystrokes are read back from the Windows session ─────


def _volumes(monkeypatch, *readings):
    from actions import spotify_exact

    seq = iter(readings)
    last = {}

    def read():
        nonlocal last
        last = next(seq, last)
        return last

    monkeypatch.setattr(spotify_exact, "get_volume_exact", read)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return spotify_exact


def test_volume_up_confirmed_by_the_session_volume(monkeypatch):
    ex = _volumes(monkeypatch, {"ok": True, "percent": 40}, {"ok": True, "percent": 50})
    r = ex.confirm_volume_step("up", lambda: "Spotify volume increased, sir.")
    assert isinstance(r, ToolResult) and r.verified and r.verifier_name == "spotify_native"
    assert r.message == "Spotify volume up to 50%, sir."


def test_volume_that_did_not_move_keeps_the_macros_text(monkeypatch):
    ex = _volumes(monkeypatch, {"ok": True, "percent": 40}, {"ok": True, "percent": 40})
    r = ex.confirm_volume_step("down", lambda: "Spotify volume decreased, sir.", timeout_s=0)
    assert r == "Spotify volume decreased, sir."


def test_volume_with_no_session_keeps_the_macros_text(monkeypatch):
    ex = _volumes(monkeypatch, {"ok": False, "error": "spotify not running"})
    r = ex.confirm_volume_step("up", lambda: "Spotify is not running or visible, sir.")
    assert r == "Spotify is not running or visible, sir."


# ── 5. Audio device changes carry the endpoint's own read-back ──────────────


def _audio(ok, message, data):
    from core.audio_endpoints import AudioResult

    return AudioResult(ok, message, data)


def test_a_device_level_read_back_is_verified():
    from actions.audio_control import _evidence

    res = _audio(True, "Star glass set to 40%, sir.", {"device": {}, "volume": 40, "requested": 40})
    r = _evidence(res)
    assert isinstance(r, ToolResult) and r.verified and r.verifier_name == "audio_endpoint"
    assert not is_unconfirmed(annotate(r, "audio_control", "glass_volume_set"))


def test_a_switched_default_device_is_verified_with_its_note():
    from actions.audio_control import _evidence

    res = _audio(True, "Speakers is now the default output, sir.", {"device": {}})
    r = _evidence(res, " Spotify is held on the glasses.")
    assert r.verified and r.message.endswith("held on the glasses.")


@pytest.mark.parametrize(
    "res",
    [
        _audio(
            True,
            "Glass is at 30% - it wouldn't go to 40%, sir.",
            {"device": {}, "volume": 30, "requested": 40},
        ),
        _audio(
            False,
            "Windows didn't switch Speakers for: console, sir.",
            {"device": {}, "not_switched": ["console"]},
        ),
        _audio(False, "I couldn't find that device, sir.", {}),
    ],
)
def test_an_unmatched_or_failed_audio_change_stays_text(res):
    from actions.audio_control import _evidence

    assert _evidence(res) == res.message


def test_pinning_eras_own_voice_output_is_her_own_state():
    assert is_read_only("audio_control", "set_voice_output")
    assert not is_read_only("audio_control", "set_default")


# ── 6. Netflix fullscreen is confirmed by the browser's own window state ────


def test_cdp_confirmed_fullscreen_is_verified_through_the_probe(monkeypatch):
    from actions.netflix_control import _browser_window_proof

    raw = _browser_window_proof("Netflix is fullscreen.", "fullscreen")
    r = refresh(
        monkeypatch,
        "netflix_control",
        {"action": "fullscreen"},
        state(app="netflix", playback="playing"),
        raw,
    )
    assert r.verified and r.message.startswith("Netflix is fullscreen.")
    assert not is_unconfirmed(annotate(r, "netflix_control", "fullscreen"))


def test_the_blind_f11_fallback_stays_unconfirmed(monkeypatch):
    r = refresh(
        monkeypatch,
        "netflix_control",
        {"action": "fullscreen"},
        state(app="netflix"),
        "Fullscreen toggled.",
    )
    assert is_unconfirmed(annotate(r, "netflix_control", "fullscreen"))


class _Tab:
    def __init__(self, url):
        self.url = url

    def evaluate(self, script):
        return self.url

    def goto(self, url, **kwargs):
        return None  # a navigation that never lands: the address stays put


class _Sess:
    def run(self, value):
        return value


def _open(monkeypatch, url, status):
    from actions import netflix_control as nc

    monkeypatch.setattr(nc, "_focus_netflix_foreground", lambda page, sess: None)
    monkeypatch.setattr(
        nc,
        "_clear_profile_gate",
        lambda *a, **k: {"status": status, "profile": "Nesri", "message": "Not sure."},
    )
    return nc._handle_open(_Sess(), _Tab(url), params={})


def test_open_netflix_is_confirmed_by_the_tabs_own_address(monkeypatch):
    r = _open(monkeypatch, "https://www.netflix.com/browse", "selected")
    assert isinstance(r, ToolResult) and r.verified
    assert r.message == "Netflix is open on the Nesri profile."


@pytest.mark.parametrize(
    "url, status",
    [
        ("https://www.netflix.com/browse", "selected-unsure"),  # profile not settled
        ("about:blank", "no-gate"),  # never reached Netflix
    ],
)
def test_open_netflix_unproven_stays_text(monkeypatch, url, status):
    assert isinstance(_open(monkeypatch, url, status), str)
