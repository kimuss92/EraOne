"""Playback recovery must preserve unknown outcomes and never toggle another tab."""

from unittest.mock import Mock

import pytest

from core import media_state


@pytest.mark.parametrize(
    "reply",
    [
        "Target browser tab matching 'youtube.com' was not found.",
        "No HTML5 video element found on YouTube page",
        "DOM Media Injection Failure: connection closed",
        "YouTube playback was blocked",
        "YouTube pause failed read-back",
        "YouTube video paused cleanly",
    ],
)
def test_youtube_never_replays_dom_result_as_global_key(monkeypatch, reply):
    import pyautogui

    from actions import browser_control, window_control
    from actions.youtube_video import _handle_playback_control

    class Session:
        def _maximize_browser_window_windows(self):
            return None

        def evaluate_background_media(self, *args):
            return "command"

        def run(self, job):
            return reply

    monkeypatch.setattr(browser_control, "get_browser_session", lambda *a, **kw: Session())
    monkeypatch.setattr(window_control, "focus_window", Mock())
    press = Mock()
    monkeypatch.setattr(pyautogui, "press", press)
    assert _handle_playback_control("pause", "brave") == reply
    press.assert_not_called()


def test_youtube_interrupted_command_never_toggles(monkeypatch):
    import pyautogui

    from actions import browser_control, window_control
    from actions.youtube_video import _handle_playback_control

    class Session:
        def _maximize_browser_window_windows(self):
            return None

        def evaluate_background_media(self, *args):
            return "command"

        def run(self, job):
            if job == "command":
                raise TimeoutError("may have executed")

    monkeypatch.setattr(browser_control, "get_browser_session", lambda *a, **kw: Session())
    monkeypatch.setattr(window_control, "focus_window", Mock())
    press = Mock()
    monkeypatch.setattr(pyautogui, "press", press)
    assert "not verified" in _handle_playback_control("resume", "brave")
    press.assert_not_called()


@pytest.fixture
def playback(monkeypatch):
    state = {s: {"status": "playing"} for s in ("spotify", "youtube", "netflix")}
    monkeypatch.setattr(media_state, "MEDIA_STATE", state)
    monkeypatch.setattr(media_state, "log", Mock())
    return state


def test_exclusivity_keeps_failed_pause_state(playback, monkeypatch):
    monkeypatch.setattr(media_state, "_pause_source", lambda source, **kw: source == "netflix")
    media_state.enforce_single_playback("youtube")
    assert playback["spotify"]["status"] == "playing"
    assert playback["netflix"]["status"] == "paused"


def test_sweep_reports_partial_failure(playback, monkeypatch):
    monkeypatch.setattr(media_state, "source_window_present", lambda source: False)
    monkeypatch.setattr(media_state, "_pause_source", lambda source, **kw: source == "netflix")
    result = media_state.post_action_sweep()
    assert "unresolved" in result and "youtube" in result
    assert playback["youtube"]["status"] == "playing"
    assert playback["netflix"]["status"] == "paused"


def test_sweep_reports_confirmed_resolution(playback, monkeypatch):
    monkeypatch.setattr(media_state, "source_window_present", lambda source: False)
    monkeypatch.setattr(media_state, "_pause_source", lambda source, **kw: True)
    assert "resolved" in media_state.post_action_sweep()
    assert playback["youtube"]["status"] == playback["netflix"]["status"] == "paused"


@pytest.mark.parametrize("source,domain", [("youtube", "youtube.com"), ("netflix", "netflix.com")])
def test_browser_pause_uses_targeted_readback(source, domain, monkeypatch):
    pause = Mock(return_value=False)
    monkeypatch.setattr(media_state, "_cdp_pause_video", pause)
    assert media_state._pause_source(source) is False
    pause.assert_called_once_with(domain)


def test_spotify_unknown_does_not_confirm_pause(monkeypatch):
    from core import system_awareness

    monkeypatch.setattr(media_state, "_is_spotify_process_running", lambda: True)
    monkeypatch.setattr(media_state, "is_playing", lambda source: False)
    monkeypatch.setattr(system_awareness, "observed_source_status", lambda source: None)
    assert media_state._pause_source("spotify") is False


@pytest.mark.parametrize(
    "sent,observed,expected",
    [
        (False, "paused", False),
        (True, "playing", False),
        (True, None, False),
        (True, "paused", True),
    ],
)
def test_spotify_sweep_requires_targeted_send_and_readback(monkeypatch, sent, observed, expected):
    from actions import spotify_control
    from core import system_awareness

    monkeypatch.setattr(media_state, "_is_spotify_process_running", lambda: True)
    monkeypatch.setattr(media_state, "is_playing", lambda source: True)
    send = Mock(return_value=sent)
    monkeypatch.setattr(spotify_control, "_send_spotify_command", send)
    toggle = Mock()
    monkeypatch.setattr(spotify_control, "_press_global_media_play_pause", toggle)
    monkeypatch.setattr(system_awareness, "observed_source_status", lambda source: observed)
    assert media_state._pause_source("spotify") is expected
    send.assert_called_once_with(spotify_control.APPCOMMAND_MEDIA_PAUSE)
    toggle.assert_not_called()
