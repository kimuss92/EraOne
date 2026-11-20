"""PF-026: Gemini Live G0 stability (docs/era-enhancement/GEMINI_SESSION_STABILITY.md).

Mock/guard proof only. Whether a sparse silent burst resets Google's 120 s idle
timer is NOT proven here; that needs the live soak (see handoff.md).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.audio_config import CHUNK_SIZE, SEND_SAMPLE_RATE
from app.era_live import session_stability as stab
from app.era_live.audio import AudioIOMixin
from core.session_manager import DEFAULT_MAX_TOKEN_AGE_SECONDS, SessionManager


def voice(frames: int = 1) -> bytes:
    t = np.arange(CHUNK_SIZE * frames) / SEND_SAMPLE_RATE
    return (6000 * np.sin(2 * np.pi * 220 * t)).astype("<i2").tobytes()


def make_era(snapshot=None, *, sends_before_stop=None, **flags):
    sent: list[bytes] = []
    era = SimpleNamespace(
        _go_away_event=asyncio.Event(),
        out_queue=asyncio.Queue(),
        _mic_muted=flags.get("mic_muted", False),
        _browser_mic_active=flags.get("mic_active", True),
        session_manager=SimpleNamespace(get_state_snapshot=lambda: dict(snapshot or {})),
        _speaker_gate=lambda: SimpleNamespace(feed=lambda frame: [frame]),
        _attention_gate=lambda: SimpleNamespace(feed=lambda frame, keep_open: [frame]),
        _attention_keep_open=lambda: True,
        _note_sent_voice=lambda frame: None,
        _log_audio_drop=lambda reason: None,
    )

    async def send(audio):
        sent.append(audio.data)
        if sends_before_stop is not None and len(sent) >= sends_before_stop:
            era._go_away_event.set()

    era.session = SimpleNamespace(send_realtime_input=send)
    return era, sent


def run(era, seconds=3.0):
    async def go():
        try:
            await asyncio.wait_for(AudioIOMixin._send_realtime(era), seconds)
        except TimeoutError:
            pass

    asyncio.run(go())


# ── G0-1: keepalive while a privacy gate idles the socket ────────────────────


@pytest.fixture
def fast_keepalive(monkeypatch):
    monkeypatch.setattr(stab, "KEEPALIVE_INTERVAL_S", 0.1)
    monkeypatch.setattr(stab, "KEEPALIVE_FRAMES", 2)


@pytest.mark.parametrize(
    "snapshot, flags",
    [
        ({"is_muted": True}, {}),
        ({}, {"mic_muted": True}),
        ({}, {"mic_active": False}),
        ({"standby_active": True}, {}),
    ],
)
def test_a_gated_session_is_kept_alive_with_pure_silence(fast_keepalive, snapshot, flags):
    era, sent = make_era(snapshot, sends_before_stop=4, **flags)
    # Real speech sits in the queue while gated: it must never leave the machine.
    for _ in range(3):
        era.out_queue.put_nowait({"data": voice(), "mime_type": "audio/pcm;rate=16000"})
    run(era)
    assert len(sent) >= 4
    assert all(frame == bytes(CHUNK_SIZE * 2) for frame in sent), "mic audio leaked"
    assert era._keepalive_frames_sent == len(sent)


def test_an_open_mic_gets_no_keepalive(fast_keepalive):
    era, sent = make_era({})
    run(era, seconds=0.6)
    assert sent == []


def test_reconnecting_gets_no_keepalive(fast_keepalive):
    era, sent = make_era({"is_reconnecting": True})
    run(era, seconds=0.6)
    assert sent == []


def test_keepalive_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(stab, "KEEPALIVE_INTERVAL_S", 0.0)
    era, sent = make_era({"is_muted": True})
    run(era, seconds=0.6)
    assert sent == []


def test_default_keepalive_is_sparse_and_well_inside_the_idle_window():
    assert 0 < stab.KEEPALIVE_INTERVAL_S <= 30
    assert stab.KEEPALIVE_FRAMES * stab.FRAME_S <= 0.5


# ── G0-2: 40 ms chunks at 16 kHz ─────────────────────────────────────────────


def test_chunks_are_40_ms_at_16_khz():
    assert SEND_SAMPLE_RATE == 16000
    assert CHUNK_SIZE / SEND_SAMPLE_RATE == pytest.approx(0.040)


# ── G0-3: resumption handle lifetime; the server is authoritative ────────────


def test_handle_lifetime_is_googles_two_hours_with_margin():
    assert 6600 <= DEFAULT_MAX_TOKEN_AGE_SECONDS < 7200


def test_only_a_handle_past_its_lifetime_counts_as_expired():
    sm = SessionManager()
    sm.current_resumption_token = "h"
    sm._token_saved_at = time.time() - 3600
    assert not sm.token_expired()
    sm._token_saved_at = time.time() - 3 * 3600
    assert sm.token_expired()


def test_failed_attempts_alone_never_discard_the_handle():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    block = src.split("# ── Backoff with jitter", 1)[1][:1500]
    assert "self.session_manager.token_expired()" in block
    assert "_reconnect_attempts >= self._stale_token_after" not in block


# ── G0-4: name the fault ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "detail, age, go_away, kind",
    [
        ("1008 None. The operation was aborted", 119.9, False, "1008-inactivity"),
        ("1008 policy violation: client inactivity", 30.0, False, "1008-inactivity"),
        ("1008 None. The operation was aborted", 539.9, False, "1008-lifetime"),
        ("1008 None. The operation was aborted", 60.0, True, "1008-lifetime"),
        ("1008 None. The operation was aborted", 45.0, False, "1008-other"),
        ("1011 internal error", 120.0, False, None),
    ],
)
def test_a_1008_close_is_named(detail, age, go_away, kind):
    assert stab.close_kind(detail, age, go_away) == kind


def test_every_close_record_names_the_kind_and_the_keepalive_count():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    record = src.split('"engine.connection.closed"', 1)[1][:900]
    for field in (
        "close_kind=",
        "conn_age_bucket=",
        "keepalive_frames=",
        "continuity_frames_kept=",
    ):
        assert field in record


# ── G0-5: unanswered speech replayed once, answered speech never ────────────


def stash_with_speech(now=100.0, seconds=1.0):
    s = stab.UtteranceStash()
    for i in range(int(seconds / stab.FRAME_S)):
        s.record(voice(), now + i * stab.FRAME_S)
    return s, now + seconds


def test_unanswered_recent_speech_is_kept_for_exactly_one_replay():
    s, end = stash_with_speech()
    assert s.capture_on_drop(end + 0.5) > 0
    assert len(s.take()) > 0
    assert s.take() == []  # one replay only


def test_answered_speech_is_never_replayed():
    s, end = stash_with_speech()
    s.note_model_output()
    assert s.capture_on_drop(end + 0.5) == 0
    assert s.take() == []


def test_speech_while_a_tool_runs_is_never_replayed():
    s, end = stash_with_speech()
    assert s.capture_on_drop(end + 0.5, tools_in_flight=1) == 0


def test_old_speech_is_not_replayed():
    s, end = stash_with_speech()
    assert s.capture_on_drop(end + 10) == 0


def test_silence_alone_is_not_an_utterance():
    s = stab.UtteranceStash()
    for i in range(50):
        s.record(bytes(CHUNK_SIZE * 2), 100 + i * stab.FRAME_S)
    assert s.capture_on_drop(103) == 0


def test_the_stash_is_bounded_to_three_seconds():
    s, end = stash_with_speech(seconds=10.0)
    assert s.capture_on_drop(end) <= round(3.0 / stab.FRAME_S)


def test_replay_goes_first_and_once_then_live_audio_in_order():
    stashed = [voice(), voice()]
    live = voice(1)[:-2] + b"\x01\x00"
    era, sent = make_era({}, sends_before_stop=3)
    era._utterance_stash = stab.UtteranceStash()
    era._utterance_stash._stash = list(stashed)
    era.out_queue.put_nowait({"data": live, "mime_type": "audio/pcm;rate=16000"})
    run(era)
    assert sent == [*stashed, live]
    assert era._utterance_stash.take() == []


def test_replay_is_discarded_if_the_owner_muted_meanwhile(fast_keepalive):
    era, sent = make_era({"is_muted": True}, sends_before_stop=2)
    era._utterance_stash = stab.UtteranceStash()
    era._utterance_stash._stash = [voice()]
    run(era)
    assert all(frame == bytes(CHUNK_SIZE * 2) for frame in sent)
    assert era._utterance_stash.take() == []
