"""The listening session: one wake word, then a rolling window (core/session.py).

Pinned here because three separate timers used to answer the same question:
the media-only attention window, the standby wake listener, and the Gemini
reconnect. One window, one rule, and it is owner-adjustable by voice.
"""

from __future__ import annotations

import json

import pytest

from core import session
from core.attention import ListeningState


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "CONFIG_PATH", tmp_path / "session.json")


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ── config ──────────────────────────────────────────────────────────────────


def test_the_default_window_is_two_minutes_and_standby_is_off():
    cfg = session.get_config()
    assert cfg["window_s"] == 120.0
    assert cfg["standby_enabled"] is False


def test_the_window_is_clamped_to_a_usable_range():
    session.CONFIG_PATH.write_text(json.dumps({"window_s": 1}))
    assert session.get_config()["window_s"] == session.MIN_WINDOW_S
    session.CONFIG_PATH.write_text(json.dumps({"window_s": 10**9}))
    assert session.get_config()["window_s"] == session.MAX_WINDOW_S


def test_a_broken_config_falls_back_to_the_default_window():
    session.CONFIG_PATH.write_text("{not json")
    assert session.window_seconds() == session.DEFAULT_WINDOW_S
    assert session.standby_enabled() is False


def test_setting_the_window_persists_and_describes_itself():
    message = session.set_window_from_text(300)
    assert session.window_seconds() == 300.0
    assert "5 minute" in message
    assert "5 minute" in session.describe()


def test_standby_can_be_turned_on_again_later():
    assert session.standby_enabled() is False
    session.set_standby_enabled(True)
    assert session.standby_enabled() is True
    session.set_standby_enabled(False)
    assert session.standby_enabled() is False


# ── spoken durations ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spoken", "seconds"),
    [
        ("2 min", 120),
        ("2 minutes", 120),
        ("90 seconds", 90),
        ("1 minute 30 seconds", 90),
        ("5 mins", 300),
        ("1 hour", 3600),
        ("30 s", 30),
        ("set the session time to 3 minutes", 180),
    ],
)
def test_a_spoken_duration_is_understood(spoken, seconds):
    assert session.parse_duration(spoken) == seconds


@pytest.mark.parametrize("spoken", ["", "soon", "a while", "maybe later"])
def test_a_phrase_without_a_duration_changes_nothing(spoken):
    before = session.window_seconds()
    message = session.set_window_from_text(spoken)
    assert "Not changed" in message
    assert session.window_seconds() == before


def test_human_duration_reads_naturally():
    assert session.human_duration(120) == "2 minutes"
    assert session.human_duration(90) == "1 min 30 s"
    assert session.human_duration(3600) == "1 hour"
    assert session.human_duration(1) == "1 second"


# ── the rolling window ──────────────────────────────────────────────────────


def test_inside_the_window_bare_commands_work():
    clock = Clock()
    window = session.SessionWindow(clock=clock, window_s=120)
    clock.t += 60
    assert window.open() is True


def test_after_the_window_the_name_is_required_again():
    clock = Clock()
    window = session.SessionWindow(clock=clock, window_s=120)
    clock.t += 121
    assert window.open() is False


def test_every_accepted_turn_re_anchors_the_window():
    clock = Clock()
    window = session.SessionWindow(clock=clock, window_s=120)
    for _ in range(4):
        clock.t += 100
        assert window.open() is True
        window.note_accepted()
    clock.t += 121
    assert window.open() is False


def test_closing_the_session_requires_the_wake_word():
    clock = Clock()
    window = session.SessionWindow(clock=clock, window_s=120)
    window.close()
    assert window.open() is False
    assert window.remaining_s() == 0.0
    window.note_accepted()
    assert window.open() is True


# ── attention integration ───────────────────────────────────────────────────


def test_listening_state_uses_the_configured_window():
    session.set_window_from_text(300)
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 299
    assert state.window_open() is True
    clock.t += 2
    assert state.window_open() is False


def test_an_accepted_utterance_keeps_the_session_open():
    session.set_window_from_text(60)
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 50
    state.note_activity()  # a bare follow-up was accepted
    clock.t += 50
    assert state.window_open() is True
    clock.t += 11
    assert state.window_open() is False


def test_standby_style_close_needs_the_name_but_her_reply_does_not_reopen():
    session.set_window_from_text(60)
    clock = Clock()
    state = ListeningState(clock=clock)
    state.close_session()
    assert state.window_open() is False
    state.era_finished_speaking(interrupted=False)
    assert state.window_open() is False
    state.note_transcript("Era, play next track")
    assert state.window_open() is True
