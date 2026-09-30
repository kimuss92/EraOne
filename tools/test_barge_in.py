"""Tests for the barge-in (real-time interruption) detector and its config.

Deterministic and hardware-free: every test forces the amplitude-only fallback
(``_vad = None``) so the outcome does not depend on whether webrtcvad is
installed, and builds PCM buffers by hand rather than capturing a mic.
"""

import numpy as np
import pytest

from core.vad import (
    BargeInDetector,
    _frame_rms,
    build_barge_in_detector,
    fade_out_pcm,
    get_barge_in_config,
    get_barge_in_fade_ms,
)

# 16 kHz int16 mono: a 30 ms webrtcvad frame is 480 samples = 960 bytes.
_FRAME_BYTES = 960


def _pcm(amplitude: int, n_frames: int) -> bytes:
    """A loud/quiet PCM buffer of `n_frames` 30 ms frames."""
    samples = np.full(480 * n_frames, amplitude, dtype="<i2")
    return samples.tobytes()


_LOUD = _pcm(8000, 1)  # RMS ~0.244, one frame
_QUIET = _pcm(0, 1)  # RMS 0.0


def _detector(**kw) -> BargeInDetector:
    """A detector with the VAD forced off (amplitude-only) for determinism."""
    det = BargeInDetector(**kw)
    det._vad = None
    return det


def test_frame_rms_bounds():
    assert _frame_rms(b"") == 0.0
    assert _frame_rms(_QUIET) == 0.0
    assert _frame_rms(_LOUD) == pytest.approx(8000 / 32768, rel=1e-3)


def test_disabled_never_fires():
    det = _detector(enabled=False, onset_grace_ms=0, min_speech_ms=30)
    det.note_speaking(True)
    assert det.feed(_pcm(8000, 100)) is False


def test_unarmed_never_fires():
    # No note_speaking(True): the detector is not armed, so even a long, loud
    # buffer is ignored.
    det = _detector(onset_grace_ms=0, min_speech_ms=30)
    assert det.feed(_pcm(8000, 100)) is False


def test_onset_grace_suppresses_immediate_trigger():
    det = _detector(onset_grace_ms=10_000, min_speech_ms=30)
    det.note_speaking(True)
    # Inside the (10 s) grace window a loud buffer must not fire — this is the
    # guard against echo at the very start of playback.
    assert det.feed(_pcm(8000, 100)) is False


def test_sustained_loud_speech_fires_once():
    det = _detector(onset_grace_ms=0, min_speech_ms=250)
    det.note_speaking(True)
    # 10 frames = 300 ms of loud audio in one buffer clears the 250 ms bar.
    assert det.feed(_pcm(8000, 10)) is True
    # One-shot: it disarms after firing so it cannot re-trigger mid-turn.
    assert det.feed(_pcm(8000, 10)) is False


def test_quiet_audio_never_accumulates():
    det = _detector(onset_grace_ms=0, min_speech_ms=60)
    det.note_speaking(True)
    for _ in range(50):
        assert det.feed(_QUIET) is False


def test_brief_blip_does_not_trigger():
    # A single 30 ms loud frame is well under the 250 ms minimum: a stray echo
    # blip must not stop Era.
    det = _detector(onset_grace_ms=0, min_speech_ms=250)
    det.note_speaking(True)
    assert det.feed(_LOUD) is False


def test_amplitude_threshold_respected():
    # Threshold above the buffer's RMS (0.244): it never counts as speech.
    det = _detector(onset_grace_ms=0, min_speech_ms=30, amplitude_threshold=0.5)
    det.note_speaking(True)
    assert det.feed(_pcm(8000, 100)) is False


def test_reset_rearms_for_next_turn():
    det = _detector(onset_grace_ms=0, min_speech_ms=250)
    det.note_speaking(True)
    assert det.feed(_pcm(8000, 10)) is True
    det.reset()
    det.note_speaking(True)  # next turn
    assert det.feed(_pcm(8000, 10)) is True


def test_disarm_stops_detection():
    det = _detector(onset_grace_ms=0, min_speech_ms=30)
    det.note_speaking(True)
    det.note_speaking(False)  # turn ended
    assert det.feed(_pcm(8000, 100)) is False


def test_malformed_frame_does_not_raise():
    det = _detector(onset_grace_ms=0, min_speech_ms=30)
    det.note_speaking(True)
    # Odd byte count / garbage must degrade to False, never raise.
    assert det.feed(b"\x01\x02\x03") in (True, False)


def _isolate_config(monkeypatch):
    """Make config resolution read env+defaults only, never the user's real
    config/api_keys.json (which may now carry a `barge_in` block that would
    otherwise override the env vars these tests set)."""
    monkeypatch.setattr("core.utils.load_config", lambda: {}, raising=False)


def test_config_defaults_and_env_override(monkeypatch):
    _isolate_config(monkeypatch)
    for var in (
        "ERA_BARGE_IN",
        "ERA_BARGE_IN_AGGR",
        "ERA_BARGE_IN_AMP",
        "ERA_BARGE_IN_MIN_MS",
        "ERA_BARGE_IN_GRACE_MS",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = get_barge_in_config()
    assert cfg["enabled"] is True
    assert 0.0 <= cfg["amplitude_threshold"] <= 1.0
    assert cfg["min_speech_ms"] >= 30

    monkeypatch.setenv("ERA_BARGE_IN", "false")
    monkeypatch.setenv("ERA_BARGE_IN_AMP", "0.2")
    monkeypatch.setenv("ERA_BARGE_IN_MIN_MS", "400")
    cfg2 = get_barge_in_config()
    assert cfg2["enabled"] is False
    assert cfg2["amplitude_threshold"] == pytest.approx(0.2)
    assert cfg2["min_speech_ms"] == 400


def test_responsive_defaults(monkeypatch):
    # The out-of-the-box defaults favour a quick, easy cut-in: a 0.05 amplitude
    # floor (normal-volume speech, not only a raised voice) and a 150 ms sustain.
    # This pins that intent so a refactor cannot silently drift back to the old
    # conservative 0.08 / 250 ms without a failing test to justify it.
    _isolate_config(monkeypatch)
    for var in ("ERA_BARGE_IN_AMP", "ERA_BARGE_IN_MIN_MS"):
        monkeypatch.delenv(var, raising=False)
    cfg = get_barge_in_config()
    assert cfg["amplitude_threshold"] == pytest.approx(0.05)
    assert cfg["min_speech_ms"] == 150


def test_default_fires_faster_than_old_floor():
    # 150 ms of loud audio (5 frames) now trips the default detector; the old
    # 250 ms floor required nearly twice as much before Era would yield.
    det = _detector(onset_grace_ms=0)  # min_speech_ms defaults to 150
    det.note_speaking(True)
    assert det.feed(_pcm(8000, 5)) is True


def test_build_from_config(monkeypatch):
    _isolate_config(monkeypatch)
    monkeypatch.setenv("ERA_BARGE_IN", "true")
    monkeypatch.setenv("ERA_BARGE_IN_MIN_MS", "120")
    det = build_barge_in_detector()
    assert isinstance(det, BargeInDetector)
    assert det.enabled is True
    assert det.min_speech_ms == 120


def test_clamps_bad_config_values():
    # Out-of-range values are clamped at the boundary rather than trusted.
    det = BargeInDetector(amplitude_threshold=5.0, min_speech_ms=1, onset_grace_ms=-10)
    assert det.amplitude_threshold == 1.0
    assert det.min_speech_ms >= BargeInDetector._VAD_FRAME_MS
    assert det.onset_grace_ms == 0


# ── Calibration self-test logic (tools/calibrate_barge_in.py) ──────────────────
from tools.calibrate_barge_in import (  # noqa: E402
    build_probe_tone,
    detector_fires_on,
    frame_levels,
    recommend_threshold,
    summarize,
)


def test_frame_levels_and_summary():
    levels = frame_levels(_pcm(8000, 200))  # ~200 frames of loud audio
    assert levels and all(lv > 0.0 for lv in levels)
    s = summarize(levels)
    assert s["frames"] == len(levels)
    assert s["peak"] == pytest.approx(8000 / 32768, rel=1e-2)
    assert summarize([]) == {"mean": 0.0, "peak": 0.0, "frames": 0}


def test_recommend_echo_only():
    threshold, msg = recommend_threshold(0.10, None)
    assert threshold >= 0.10  # sits above the echo peak
    assert "echo only" in msg.lower()


def test_recommend_good_separation():
    threshold, msg = recommend_threshold(0.03, 0.40)
    assert 0.03 < threshold < 0.40  # between echo and speech
    assert "good" in msg.lower()


def test_recommend_poor_separation_warns():
    threshold, msg = recommend_threshold(0.30, 0.35)
    assert "POOR" in msg
    assert 0.0 < threshold <= 0.95


def test_recommend_never_below_floor():
    # A dead-silent room must not yield a hair-trigger threshold.
    threshold, _ = recommend_threshold(0.0, None)
    assert threshold >= 0.02


def test_detector_fires_on_loud_capture():
    det = _detector(min_speech_ms=250)  # grace is retired inside detector_fires_on
    assert detector_fires_on(_pcm(8000, 200), det) is True


def test_detector_does_not_fire_on_quiet_capture():
    det = _detector(min_speech_ms=250, amplitude_threshold=0.08)
    assert detector_fires_on(_pcm(0, 200), det) is False


def test_probe_tone_shape():
    tone = build_probe_tone(0.1, 24000)
    assert tone.dtype == np.dtype("<i2")
    assert tone.size == int(0.1 * 24000)
    assert int(np.max(np.abs(tone))) > 0


# ── Phase 1: fade-out duck on interruption ─────────────────────────────────────
def test_fade_out_ends_at_zero_and_is_short():
    pcm = _pcm(8000, 4)  # 4 frames of constant-loud audio
    tail = fade_out_pcm(pcm, 24000, 20)
    arr = np.frombuffer(tail, dtype="<i2")
    assert arr.size == int(24000 * 20 / 1000)  # exactly fade_ms of samples
    assert abs(int(arr[-1])) <= 1  # lands on (near) zero — no click
    assert abs(int(arr[0])) > abs(int(arr[-1]))  # ramps downward


def test_fade_out_ramp_is_monotonic_nonincreasing():
    tail = fade_out_pcm(_pcm(8000, 8), 24000, 30)
    arr = np.abs(np.frombuffer(tail, dtype="<i2").astype(np.int32))
    assert np.all(np.diff(arr) <= 0)  # magnitude never rises across the ramp


def test_fade_out_disabled_or_empty_returns_empty():
    assert fade_out_pcm(_pcm(8000, 4), 24000, 0) == b""  # 0 ms = hard stop
    assert fade_out_pcm(b"", 24000, 20) == b""
    assert fade_out_pcm(_pcm(8000, 4), 0, 20) == b""


def test_fade_out_shorter_than_window_uses_whole_buffer():
    pcm = _pcm(8000, 1)  # 480 samples; window would be 480 at 20 ms/24k
    tail = fade_out_pcm(pcm, 24000, 100)  # 2400-sample window > buffer
    arr = np.frombuffer(tail, dtype="<i2")
    assert arr.size == 480  # capped at what is available, never over-reads


def test_fade_out_tolerates_odd_length():
    # Must degrade, never raise on a misaligned buffer (audio hot path).
    assert isinstance(fade_out_pcm(b"\x01\x02\x03", 24000, 20), bytes)


def test_fade_ms_config_default_and_env(monkeypatch):
    _isolate_config(monkeypatch)
    monkeypatch.delenv("ERA_BARGE_IN_FADE_MS", raising=False)
    assert get_barge_in_fade_ms() == 20
    monkeypatch.setenv("ERA_BARGE_IN_FADE_MS", "35")
    assert get_barge_in_fade_ms() == 35
    monkeypatch.setenv("ERA_BARGE_IN_FADE_MS", "garbage")
    assert get_barge_in_fade_ms() == 20  # bad value → safe default
    monkeypatch.setenv("ERA_BARGE_IN_FADE_MS", "-5")
    assert get_barge_in_fade_ms() == 0  # clamped to a hard stop, never negative
