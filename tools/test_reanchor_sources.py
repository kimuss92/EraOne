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


# ── PF-038: a real conversation keeps the window open; lyrics never do ──


def _music_state(clock):
    return ListeningState(clock=clock, media_active=lambda: True, window_s=120)


def test_a_verified_conversation_with_music_on_does_not_lapse_mid_chat():
    # Live 2026-11-16 21:49-21:53: "Era" once, Spotify playing, the owner talked
    # every 10-20 s and the window still closed mid-conversation.
    clock = Clock()
    state = _music_state(clock)
    state.note_activity(explicit=True)  # "Era, play music"
    state.era_finished_speaking(interrupted=False)
    for _ in range(10):  # 10 unnamed follow-ups, 20 s apart (200 s > window)
        clock.t += 15
        state.note_owner_turn(verified=True)
        clock.t += 5
        state.era_finished_speaking(interrupted=False)
        assert state.window_open() is True
    clock.t += 121  # then real silence
    assert state.window_open() is False


def test_an_unverified_voice_turn_during_music_never_extends_the_window():
    # Review F2 / owner 2026-11-16: lyrics and film lines must never keep Era
    # listening. With the voice lock off a transcribed lyric looks like the owner.
    clock = Clock()
    state = _music_state(clock)
    state.note_activity(explicit=True)
    state.era_finished_speaking(interrupted=False)
    for _ in range(3):
        clock.t += 50
        state.note_owner_turn(verified=False)  # a lyric Gemini transcribed
        clock.t += 3
        state.era_finished_speaking(interrupted=False)  # ...and answered
    assert state.window_open() is False  # 159 s after the last real address


def test_without_music_an_unnamed_turn_continues_the_conversation():
    clock = Clock()
    state = ListeningState(clock=clock, window_s=120)
    clock.t += 100
    state.note_owner_turn(verified=False)
    clock.t += 5
    state.era_finished_speaking(interrupted=False)
    clock.t += 100
    assert state.window_open() is True


def test_a_turn_just_before_expiry_survives_a_reply_just_after_it():
    # Review F1: turn at 119 s, reply finishing at 121 s.
    clock = Clock()
    state = ListeningState(clock=clock, window_s=120)
    clock.t += 119
    state.note_owner_turn(verified=True)
    clock.t += 2
    assert state.window_open() is False
    state.era_finished_speaking(interrupted=False)
    assert state.window_open() is True


def test_a_stale_turn_or_an_explicit_close_is_not_continued():
    clock = Clock()
    state = ListeningState(clock=clock, window_s=120)
    clock.t += 10
    state.note_owner_turn(verified=True)
    clock.t += 200  # no reply for minutes
    state.era_finished_speaking(interrupted=False)
    assert state.window_open() is False
    state = ListeningState(clock=clock, window_s=120)
    state.note_owner_turn(verified=True)
    state.close_session()  # "Era, standby"
    state.era_finished_speaking(interrupted=False)
    assert state.window_open() is False


def test_an_owner_turn_cannot_reopen_a_closed_window():
    clock = Clock()
    state = _music_state(clock)
    clock.t += 121
    state.note_owner_turn(verified=True)  # taken after expiry
    state.era_finished_speaking(interrupted=False)
    assert state.window_open() is False


def test_internal_speech_does_not_spend_an_owner_turn():
    clock = Clock()
    state = ListeningState(clock=clock, window_s=120)
    clock.t += 100
    state.note_owner_turn()
    state.era_finished_speaking(interrupted=False, internal=True)  # a scheduled report
    clock.t += 10
    state.era_finished_speaking(interrupted=False)  # her real reply to the owner
    clock.t += 100
    assert state.window_open() is True


def test_a_voice_input_marks_an_owner_turn_with_the_verification_state(monkeypatch):
    import main

    monkeypatch.setattr(main, "_owner_voice_verified", lambda era=None: True)
    turns = []
    era = SimpleNamespace(
        _model_reply_ownership=None,
        _telegram_speech=None,
        _input_envelope=None,
        _tg_reply_pending=False,
        _listening_state=lambda: SimpleNamespace(
            note_owner_turn=lambda verified=False: turns.append(verified)
        ),
    )
    for source in ("voice", "typed"):
        main.EraLive._accept_input(era, SimpleNamespace(id=source, source=source, received_at=0))
    assert turns == [True]  # typed input is already an explicit address


def test_owner_voice_counts_as_verified_only_for_a_scored_turn_with_the_lock_on(monkeypatch):
    import core.speaker_recognition as sr
    import main

    scored = SimpleNamespace(_voice_gate=SimpleNamespace(last_pass_fully_verified=True))
    unscored = SimpleNamespace(_voice_gate=SimpleNamespace(last_pass_fully_verified=False))
    cases = [
        (False, True, scored, False),  # lock off: nothing can tell owner from song
        (True, False, scored, False),  # lock on but not ready
        (True, True, None, False),  # no gate verdict at all
        (True, True, unscored, False),  # short follow-up passed unscored
        (True, True, scored, True),
    ]
    for enabled, ready, era, expected in cases:
        fake = SimpleNamespace(enabled=enabled, readiness=lambda r=ready: (r, ""))
        monkeypatch.setattr(sr, "get_verifier", lambda f=fake: f)
        assert main._owner_voice_verified(era) is expected


def _speech(seconds):
    n = int(16000 * seconds)
    pcm = (0.3 * np.sin(2 * np.pi * 180 * np.arange(n) / 16000) * 32767).astype("<i2")
    return [pcm[i : i + 640].tobytes() for i in range(0, n, 640)]


def test_an_unscored_follow_up_lyric_during_music_never_extends_the_window(monkeypatch):
    """dev-review 2026-11-16: the REAL speaker gate passes a short follow-up
    unscored; routed through _accept_input and Era's reply while music plays,
    it must not extend the listening window."""
    import core.speaker_recognition as sr
    import main
    from core.speaker_recognition import DEFAULT_SETTINGS, VerifyResult
    from core.voice_gate import UtteranceGate

    settings = dict(DEFAULT_SETTINGS, followup_window_s=20.0)

    def verify(pcm, purpose="command"):
        if purpose == "followup":
            return VerifyResult(True, None, "follow-up (too short to score)")
        return VerifyResult(True, 0.8, "match")

    verifier = SimpleNamespace(
        settings=settings, enabled=True, readiness=lambda: (True, ""), verify=verify
    )
    monkeypatch.setattr(sr, "get_verifier", lambda: verifier)
    gate_clock = Clock()
    gate = UtteranceGate(verifier, synchronous=True, clock=gate_clock)
    silence = [bytes(640)] * 40

    def feed(frames):
        for f in frames:
            gate_clock.t += 0.04
            gate.feed(f)

    feed(_speech(2.0) + silence)  # the owner's full command: scored
    assert gate.last_pass_fully_verified is True
    feed(_speech(0.2) + silence)  # a brief lyric inside the follow-up window
    assert gate.last_pass_fully_verified is False

    clock = Clock()
    state = ListeningState(clock=clock, media_active=lambda: True, window_s=120)
    era = SimpleNamespace(
        _model_reply_ownership=None,
        _telegram_speech=None,
        _input_envelope=None,
        _tg_reply_pending=False,
        _listening_state=lambda: state,
        _voice_gate=gate,
    )
    state.note_activity(explicit=True)
    state.era_finished_speaking(interrupted=False)
    clock.t += 100
    main.EraLive._accept_input(era, SimpleNamespace(id="lyric", source="voice", received_at=0))
    clock.t += 5
    state.era_finished_speaking(interrupted=False)  # Gemini answered the lyric
    clock.t += 20
    assert state.window_open() is False  # 125 s after the last real address
