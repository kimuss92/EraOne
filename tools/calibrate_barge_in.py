#!/usr/bin/env python3
"""Barge-in calibration self-test — tune the interruption threshold to YOUR room.

WHY THIS EXISTS
    Barge-in (core/vad.py::BargeInDetector) stops Era's voice when the user talks
    over her. Its one tunable that matters is ``amplitude_threshold``: too low and
    Era's own speaker output, bleeding back into the mic, interrupts *her*; too
    high and the user has to shout. The right value depends on the mic, the
    speakers, the volume and the room — so it cannot be a hard-coded default. This
    tool measures both levels on THIS machine and recommends a value.

    Unlike ``tools/era_doctor.py`` (read-only), this test actively PLAYS a tone
    and RECORDS the mic, so it lives on its own and is run on demand:

        python tools/calibrate_barge_in.py

    Two phases:
      1. Echo test  — plays a voice-band tone through the speakers while recording
                      the mic, with no one talking. Whatever the mic hears is
                      speaker bleed. If the current detector fires on it, Era would
                      interrupt herself.
      2. Speech test — asks you to talk normally, and measures how loud real speech
                       lands. The gap between the two is the headroom the threshold
                       sits in.

    It NEVER writes config. It prints a recommended ``barge_in`` block and how to
    apply it, leaving the decision to you.

    ``--self-check`` runs the pure analysis on synthetic audio with no hardware,
    so the logic can be tested in CI.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio_config import RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE  # noqa: E402
from core.vad import (  # noqa: E402
    _VAD_AVAILABLE,
    BargeInDetector,
    _frame_rms,
    get_barge_in_config,
)

# One incoming mic buffer = the size _listen_audio delivers (1024 samples @ 16 kHz
# ≈ 64 ms), so calibration feeds the detector exactly as production does.
_MIC_CHUNK = 1024

# Safety factors turning measured levels into a recommendation.
_ECHO_MARGIN = 1.6  # sit this far above the loudest echo frame
_SPEECH_CEILING = 0.5  # keep the threshold this far below real speech
_MIN_THRESHOLD = 0.02  # never recommend a floor a quiet room can cross


# ──────────────────────────────────────────────────────────────────────────────
# Pure analysis (no hardware — unit-testable)
# ──────────────────────────────────────────────────────────────────────────────
def frame_levels(pcm: bytes, chunk_samples: int = _MIC_CHUNK) -> list[float]:
    """Per-chunk RMS (0..1) of an int16 PCM capture, chunked like the mic loop."""
    if not pcm:
        return []
    chunk_bytes = chunk_samples * 2
    levels: list[float] = []
    for i in range(0, len(pcm) - 1, chunk_bytes):
        frame = pcm[i : i + chunk_bytes]
        if len(frame) >= 2:
            levels.append(_frame_rms(frame))
    return levels


def detector_fires_on(
    pcm: bytes, detector: BargeInDetector, chunk_samples: int = _MIC_CHUNK
) -> bool:
    """Replay a capture through the detector as SPEAKING audio. True if it fires.

    Bypasses the onset grace (onset stamped in the past) so the whole capture is
    eligible — the question is "does any of this bleed look like an interrupt",
    not "does it in the first 300 ms".
    """
    detector.note_speaking(True)
    with detector._lock:
        detector._onset_ts = 0.0  # retire the grace window for the replay
    chunk_bytes = chunk_samples * 2
    for i in range(0, len(pcm) - 1, chunk_bytes):
        frame = pcm[i : i + chunk_bytes]
        if len(frame) >= 2 and detector.feed(frame):
            return True
    return False


def recommend_threshold(echo_peak: float, speech_peak: float | None) -> tuple[float, str]:
    """Recommend an amplitude_threshold from measured echo and (optional) speech.

    Returns (threshold, advice). The threshold sits a margin above the loudest
    echo frame; when a speech level is known it is also held below speech so real
    talking still clears it. Poor separation (echo as loud as speech) is called
    out — that is a headset-or-disable situation, not a tuning one.
    """
    want = max(echo_peak * _ECHO_MARGIN, _MIN_THRESHOLD)

    if speech_peak is None:
        return round(min(want, 0.95), 3), "No speech sample taken; based on echo only."

    ceiling = speech_peak * _SPEECH_CEILING
    if want >= ceiling:
        # The echo is nearly as loud as speech — no safe threshold separates them.
        midpoint = max(_MIN_THRESHOLD, (echo_peak + speech_peak) / 2.0)
        return (
            round(min(midpoint, 0.95), 3),
            "POOR separation: speaker echo is nearly as loud as your voice. "
            "Use a headset, lower the speaker volume, or set barge_in.enabled=false.",
        )
    return round(min(want, ceiling, 0.95), 3), "Good separation between echo and speech."


def build_probe_tone(seconds: float, sample_rate: int) -> np.ndarray:
    """A voice-band warble (int16 mono) — a better echo proxy than a pure sine.

    Sums a few tones across 220–2600 Hz so the spectral energy roughly matches
    speech, which is what the mic-side VAD and RMS actually respond to.
    """
    n = int(seconds * sample_rate)
    t = np.arange(n, dtype=np.float32) / sample_rate
    tone = np.zeros(n, dtype=np.float32)
    for freq, amp in ((220.0, 0.5), (700.0, 0.3), (1500.0, 0.2), (2600.0, 0.1)):
        tone += amp * np.sin(2 * np.pi * freq * t)
    # Gentle fade in/out so the burst does not click.
    fade = min(n, int(0.02 * sample_rate))
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
    tone /= max(1e-6, np.max(np.abs(tone)))
    return (tone * 0.35 * 32767).astype("<i2")


def summarize(levels: list[float]) -> dict:
    """Mean/peak of a level list, guarded for an empty capture."""
    if not levels:
        return {"mean": 0.0, "peak": 0.0, "frames": 0}
    arr = np.asarray(levels, dtype=np.float64)
    return {"mean": float(arr.mean()), "peak": float(arr.max()), "frames": int(arr.size)}


# ──────────────────────────────────────────────────────────────────────────────
# Hardware capture (thin wrappers)
# ──────────────────────────────────────────────────────────────────────────────
def _record_while(action, seconds: float) -> bytes:
    """Record the mic for `seconds`, running `action()` once recording is live.

    Returns raw int16 mono PCM at SEND_SAMPLE_RATE. Raises on device error so the
    caller can print a clean message — this is an interactive tool, not the audio
    hot path.
    """
    import sounddevice as sd

    frames: list[bytes] = []

    def cb(indata, _frames, _t, _status):
        frames.append(bytes(indata))

    with sd.InputStream(
        samplerate=SEND_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=_MIC_CHUNK,
        callback=cb,
    ):
        if action is not None:
            action()
        time.sleep(seconds)
    return b"".join(frames)


def _play_tone(seconds: float) -> None:
    import sounddevice as sd

    tone = build_probe_tone(seconds, RECEIVE_SAMPLE_RATE)
    sd.play(tone, samplerate=RECEIVE_SAMPLE_RATE)


# ──────────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────────
def _print_header() -> None:
    print()
    print("=" * 60)
    print("  Era Barge-in Calibration")
    print("=" * 60)


def run_calibration(echo_seconds: float, speech_seconds: float, do_speech: bool) -> int:
    try:
        import sounddevice  # noqa: F401
    except Exception as e:
        print(f"[FAIL] sounddevice unavailable ({e}); cannot run the audio test.")
        print("       Install it (pip install sounddevice) and retry.")
        return 1

    cfg = get_barge_in_config()
    current = float(cfg["amplitude_threshold"])
    _print_header()
    print(f"  Current amplitude_threshold: {current}")
    vad_state = "available" if _VAD_AVAILABLE else "NOT installed (amplitude-only)"
    print(f"  webrtcvad: {vad_state}")

    # ── Phase 1: echo ──
    print("\n  [1/2] Echo test — playing a tone. DO NOT speak.")
    try:
        echo_pcm = _record_while(lambda: _play_tone(echo_seconds), echo_seconds + 0.4)
    except Exception as e:
        print(f"[FAIL] Audio device error during echo test: {e}")
        return 1
    echo = summarize(frame_levels(echo_pcm))
    self_fires = detector_fires_on(echo_pcm, BargeInDetector(**cfg))
    print(
        f"        echo level: mean {echo['mean']:.3f}, peak {echo['peak']:.3f} "
        f"({echo['frames']} frames)"
    )
    if self_fires:
        print("        ⚠  the CURRENT threshold self-triggers on echo — Era would cut herself off.")
    else:
        print("        ✓  current threshold does not self-trigger on echo.")

    # ── Phase 2: speech ──
    speech_peak = None
    if do_speech:
        print(f"\n  [2/2] Speech test — speak normally for {speech_seconds:.0f}s after the beep.")
        try:
            _countdown()
            speech_pcm = _record_while(None, speech_seconds)
        except Exception as e:
            print(f"[FAIL] Audio device error during speech test: {e}")
            return 1
        speech = summarize(frame_levels(speech_pcm))
        speech_peak = speech["peak"]
        print(
            f"        speech level: mean {speech['mean']:.3f}, peak {speech['peak']:.3f} "
            f"({speech['frames']} frames)"
        )
    else:
        print("\n  [2/2] Speech test skipped (--no-speech).")

    # ── Recommendation ──
    threshold, advice = recommend_threshold(echo["peak"], speech_peak)
    print("\n" + "-" * 60)
    print(f"  Recommended amplitude_threshold: {threshold}")
    print(f"  {advice}")
    print("-" * 60)
    print("  Apply it by adding this to your main config JSON:")
    print('      "barge_in": {')
    print('          "enabled": true,')
    print(f'          "amplitude_threshold": {threshold}')
    print("      }")
    print(f"  …or set the env var:  ERA_BARGE_IN_AMP={threshold}")
    print("  To turn barge-in off entirely: barge_in.enabled=false (or ERA_BARGE_IN=false)")
    print()
    return 0


def _countdown() -> None:
    for n in (3, 2, 1):
        print(f"        {n}...", end="", flush=True)
        time.sleep(1.0)
    print(" go.")


def _self_check() -> int:
    """Hardware-free sanity pass over the pure logic."""
    quiet = build_probe_tone(0.2, SEND_SAMPLE_RATE).tobytes()  # reused as a signal
    levels = frame_levels(quiet)
    assert levels and max(levels) > 0.0, "frame_levels produced nothing"

    # A loud echo peak with no speech → echo-only recommendation above the peak.
    t1, _ = recommend_threshold(0.10, None)
    assert t1 >= 0.10, t1
    # Good separation: quiet echo, loud speech → threshold between them.
    t2, msg2 = recommend_threshold(0.03, 0.40)
    assert 0.03 < t2 < 0.40 and "Good" in msg2, (t2, msg2)
    # Poor separation: echo as loud as speech → warned.
    _, msg3 = recommend_threshold(0.30, 0.35)
    assert "POOR" in msg3, msg3
    print("[OK] calibrate_barge_in self-check passed")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Calibrate Era's barge-in threshold.")
    p.add_argument("--echo-seconds", type=float, default=2.5, help="tone/echo capture length")
    p.add_argument("--speech-seconds", type=float, default=3.0, help="speech capture length")
    p.add_argument("--no-speech", action="store_true", help="skip the speech phase")
    p.add_argument("--self-check", action="store_true", help="run logic only, no audio hardware")
    args = p.parse_args()

    if args.self_check:
        sys.exit(_self_check())
    sys.exit(run_calibration(args.echo_seconds, args.speech_seconds, not args.no_speech))


if __name__ == "__main__":
    main()
