"""PF-034: only real conversation re-anchors the listening window.

Live 2026-11-16 (owner voiceprint gate disabled, so all room audio reaches the
attention gate): the window never closed during music, and a scheduled report
(internal model request -> PROCESSING busy exemption) let room audio pass and
re-open an EXPIRED window, so Era answered unnamed speech at 18:04:44 although
the owner's last order was 17:59:28.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from core.attention import AttentionGate, ListeningState

SPEECH = (0.2 * np.sin(2 * np.pi * 220 * np.arange(640) / 16000) * 32767).astype("<i2").tobytes()
QUIET = bytes(len(SPEECH))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _utterance(gate, keep_open=False):
    out = []
    for _ in range(10):
        out += gate.feed(SPEECH, keep_open=keep_open)
    for _ in range(30):  # > 0.7 s of quiet ends the utterance
        out += gate.feed(QUIET, keep_open=keep_open)
    return out


def _gate(media=False):
    clock = Clock()
    state = ListeningState(clock=clock, media_active=lambda: media, window_s=120)
    return clock, state, AttentionGate(state, lambda: None)


def test_a_busy_exemption_never_reopens_an_expired_window():
    clock, state, gate = _gate()
    clock.t += 121
    assert state.window_open() is False
    _utterance(gate, keep_open=True)  # Era busy with a task: audio passes...
    assert state.window_open() is False  # ...but it is not a new conversation


def test_music_cannot_keep_the_window_open():
    clock, state, gate = _gate(media=True)
    for _ in range(5):  # lyrics with pauses, 30 s apart
        clock.t += 30
        _utterance(gate)
    assert state.window_open() is False  # 150 s after the last real turn


def test_unnamed_speech_without_media_still_re_anchors():
    clock, state, gate = _gate(media=False)
    clock.t += 100
    _utterance(gate)
    clock.t += 100
    assert state.window_open() is True  # PF-006: bare follow-ups keep it open


def test_internal_speech_neither_re_anchors_nor_uses_up_an_address():
    clock = Clock()
    state = ListeningState(clock=clock, window_s=120)
    state.note_activity(explicit=True)
    clock.t += 100
    state.era_finished_speaking(interrupted=False, internal=True)  # scheduled report
    clock.t += 30
    assert state.window_open() is False
    state.era_finished_speaking(interrupted=False)  # her real reply to the address
    assert state.window_open() is True


def test_an_internal_request_is_not_busy_with_the_owner():
    from app.era_live.audio import AudioIOMixin

    tracker = SimpleNamespace(active=True, owner=None, reason="unowned_model_request")
    era = SimpleNamespace(
        _pending_store=None,
        session_manager=SimpleNamespace(
            get_state_snapshot=lambda: {"current_state": "EraState.PROCESSING"}
        ),
        _model_reply_ownership=tracker,
    )
    assert AudioIOMixin._attention_keep_open(era) is False
    tracker.reason, tracker.owner = "", SimpleNamespace(id="voice-1")
    assert AudioIOMixin._attention_keep_open(era) is True


def test_speech_for_an_internal_turn_is_recognised():
    import main

    era = SimpleNamespace(
        _model_reply_ownership=SimpleNamespace(
            active=True, owner=None, reason="unowned_model_request"
        )
    )
    assert main._speaking_for_internal_turn(era) is True
    era._model_reply_ownership.reason = "new_input"
    assert main._speaking_for_internal_turn(era) is False
    assert main._speaking_for_internal_turn(SimpleNamespace()) is False
