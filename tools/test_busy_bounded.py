"""PF-040: a busy exemption can never keep Era listening forever.

Live 2026-11-16 22:25-22:31 UTC: window set to 60 s, nobody talking, yet
inactivity never came and the HUD never showed it, while the gate kept sending
room audio to Gemini. A state that never left PROCESSING (or a check that raised
and failed open) held the busy exemption indefinitely.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.era_live import audio
from app.era_live.audio import AudioIOMixin
from core.attention import ListeningState
from core.session_manager import EraState, SessionManager


class _Era(AudioIOMixin):
    def __init__(self, manager):
        self.session_manager = manager
        self._pending_store = None
        self._model_reply_ownership = None
        self._local_turn_task = None
        self._local_speaking = False


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(audio.time, "time", lambda: now[0])
    import core.session_manager as sm

    monkeypatch.setattr(sm.time, "time", lambda: now[0])
    return now


def test_a_fresh_tool_call_still_counts_as_busy(clock):
    manager = SessionManager()
    manager.transition_to(EraState.PROCESSING, reason="tool_call")
    clock[0] += 30
    assert _Era(manager)._attention_keep_open() is True


def test_a_stuck_processing_state_stops_being_busy(clock):
    manager = SessionManager()
    manager.transition_to(EraState.PROCESSING, reason="tool_call")
    clock[0] += audio.BUSY_PROCESSING_MAX_S + 1  # never left PROCESSING
    assert _Era(manager)._attention_keep_open() is False


def test_every_tool_call_renews_the_busy_period(clock):
    manager = SessionManager()
    manager.transition_to(EraState.PROCESSING, reason="tool_call")
    clock[0] += 100
    manager.transition_to(EraState.PROCESSING, reason="tool_call")  # a second tool
    clock[0] += 100
    assert _Era(manager)._attention_keep_open() is True


def test_an_unreadable_state_history_is_not_endless_busy(clock):
    # dev-review: PROCESSING snapshot, but reading the history raises.
    manager = SessionManager()
    manager.transition_to(EraState.PROCESSING, reason="tool_call")

    def broken_history():
        raise RuntimeError("history unavailable")

    manager.get_state_history = broken_history
    assert manager.get_state_snapshot()["current_state"] == EraState.PROCESSING
    assert _Era(manager)._attention_keep_open() is False


def test_an_unreadable_history_is_logged_once(clock, monkeypatch, capsys):
    # dev-review: the protected rule says the error is logged once.
    monkeypatch.setattr(audio, "_HISTORY_ERROR_LOGGED", [False])
    manager = SessionManager()
    manager.transition_to(EraState.PROCESSING, reason="tool_call")

    def broken_history():
        raise RuntimeError("history unavailable")

    manager.get_state_history = broken_history
    era = _Era(manager)
    for _ in range(5):
        assert era._attention_keep_open() is False
    out = capsys.readouterr().out
    assert out.count("state history unreadable") == 1


def test_a_failing_busy_check_lets_the_window_decide():
    class _Broken(_Era):
        @property
        def session_manager(self):
            raise RuntimeError("boom")

        @session_manager.setter
        def session_manager(self, value):
            pass

    assert _Broken(None)._attention_keep_open() is False


def test_hud_reports_inactive_and_logs_why_when_processing_is_stuck(clock, monkeypatch):
    from app.hud import status

    manager = SessionManager()
    manager.transition_to(EraState.PROCESSING, reason="tool_call")
    era = _Era(manager)
    era._standby_active = False
    era._mic_muted = False
    era.mic_effective_active = lambda: True
    era._listening = ListeningState(clock=lambda: clock[0], window_s=60)
    logged = []
    import core.logger as logger

    monkeypatch.setattr(
        logger, "log_event", lambda _l, event, _lvl, **kw: logged.append((event, kw))
    )
    monkeypatch.setattr(status, "_last_mode", {"mode": None, "kind": None})
    assert status.listening_mode(era) == "active"  # window just opened
    clock[0] += 10
    assert status.listening_mode(era) == "active"  # countdown only: not logged again
    clock[0] += 51
    assert status.listening_mode(era) == "active"  # busy: processing (recent)
    clock[0] += audio.BUSY_PROCESSING_MAX_S
    assert status.listening_mode(era) == "inactive"
    reasons = [kw["reason"] for event, kw in logged if event == "listening.mode"]
    # dev-review: the reason change is logged even though the mode stays active.
    assert len(reasons) == 3
    assert reasons[0].startswith("window open")
    assert reasons[1] == "busy: processing"
    assert reasons[2] == "window expired"


def test_hud_logs_only_changes():
    from app.hud import status

    era = SimpleNamespace(_standby_active=True)
    status._last_mode["mode"] = None
    status.listening_mode(era)
    status.listening_mode(era)
    assert status._last_mode["mode"] == "standby"
