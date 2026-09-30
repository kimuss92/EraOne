"""
Voice gate — only the enrolled owner's voice reaches Era.

Two enforcement points, both backed by `core.speaker_recognition.SpeakerVerifier`:

1. `UtteranceGate` sits at the single mic → Gemini choke point
   (`AudioIOMixin._send_realtime`). Gemini transcribes and acts on whatever
   audio it receives, so the check has to happen BEFORE audio leaves the
   machine; checking a transcript or a tool call afterwards is too late.
   Each utterance is held locally until enough voiced audio is buffered to
   judge it, then either flushed in order (owner) or discarded (anyone else).
   Silence is forwarded as digital zeros so Gemini's server-side turn
   endpointing keeps working; rejected speech is also replaced by zeros, so no
   stranger's words ever reach the model. Long utterances are re-verified on a
   rolling window so a second voice cannot take over an accepted turn.

2. `verify_wake_utterance` runs after Vosk has matched the wake word in standby:
   the exact audio of the wake phrase (from Vosk's word timings) must also be
   the owner's voice, or Era stays asleep.

FAIL-CLOSED: when verification is enabled but not ready (no model, no enrolled
profile), every utterance is refused. Nothing here raises into the audio path.

Short replies ("ok", "yeah", "no") hold too little speech for any voiceprint
to judge. They pass only as a FOLLOW-UP: within `followup_window_s` of a fully
verified owner utterance (the window is renewed after Era's reply to it), never
while a confirmation is pending, never chained off another follow-up, and still
refused if what little audio there is scores as a clearly different voice.

Cost: ~`verify_window_s` of extra latency at the start of each utterance
(audio is delayed, not lost).
"""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import numpy as np

from core.speaker_recognition import (
    SAMPLE_RATE,
    SpeakerVerifier,
    VerifyResult,
    pcm_to_float,
    voiced_segment,
)

IDLE, PENDING, ACCEPTED, REJECTED = "idle", "pending", "accepted", "rejected"

_PREROLL_S = 0.3
_MAX_PENDING_S = 8.0  # a verdict that never arrives must not hold audio forever
_STREAM_GAP_S = 0.5  # mic frames stopped (Era speaking / muted) → start fresh


def _rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    arr = np.frombuffer(frame[: len(frame) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(arr * arr)))


class UtteranceGate:
    """Streaming per-utterance speaker gate. `feed()` → frames safe to forward."""

    def __init__(
        self,
        verifier: SpeakerVerifier,
        *,
        synchronous: bool = False,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
        followup_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self._verifier = verifier
        s = verifier.settings
        self.window_s = float(s["verify_window_s"])
        self.min_speech_s = float(s["min_command_speech_s"])
        self.reverify_s = float(s["reverify_every_s"])
        self.hangover_s = float(s["hangover_s"])
        self.followup_window_s = float(s.get("followup_window_s", 0.0))
        self._followup_allowed = followup_allowed or (lambda: True)
        # Follow-up context: until when a short reply counts as part of a
        # conversation the owner verifiably started. Renewed only by FULL
        # verification (and Era's reply to it), so follow-ups cannot chain.
        self._owner_until = -1.0
        self._owner_anchor_full = False
        self._sync = synchronous
        self._clock = clock
        self._on_event = on_event or (lambda msg: print(f"[VOICE-GATE] {msg}"))
        self._executor = None if synchronous else ThreadPoolExecutor(1, "voice-gate")
        self._lock = threading.Lock()
        self._noise = 0.005
        self._last_feed = None
        self._last_block_log = 0.0
        self.last_result: VerifyResult | None = None
        self._reset()
        if self._executor is not None:
            self._executor.submit(verifier.warm_up)

    # ── state ────────────────────────────────────────────────────────────────
    def _reset(self) -> None:
        self.state = IDLE
        self._preroll: collections.deque[bytes] = collections.deque()
        self._preroll_s = 0.0
        self._buf: list[bytes] = []
        self._buf_s = 0.0
        self._voiced_s = 0.0
        self._silence_s = 0.0
        self._window: collections.deque[tuple[bytes, float]] = collections.deque()
        self._window_s = 0.0
        self._since_check_s = 0.0
        self._job: Future | VerifyResult | None = None
        self._purpose = "command"

    def reset(self) -> None:
        with self._lock:
            self._reset()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _is_speech(self, frame: bytes) -> bool:
        level = _rms(frame)
        speech = level > max(0.012, self._noise * 3.0)
        if not speech and self.state == IDLE:
            self._noise = 0.95 * self._noise + 0.05 * level
        return speech

    def _submit(self, pcm: bytes, purpose: str = "command") -> None:
        self._purpose = purpose
        if self._sync:
            self._job = self._verifier.verify(pcm, purpose)
        else:
            self._job = self._executor.submit(self._verifier.verify, pcm, purpose)

    def _mark_owner(self, now: float, full: bool) -> None:
        if full:
            self._owner_until = now + self.followup_window_s
        self._owner_anchor_full = full

    def _followup_open(self, now: float) -> bool:
        if self.followup_window_s <= 0 or now > self._owner_until:
            return False
        try:
            return bool(self._followup_allowed())
        except Exception:
            return False  # fail closed

    def _poll(self) -> VerifyResult | None:
        job = self._job
        if job is None:
            return None
        if isinstance(job, VerifyResult):
            result = job
        elif job.done():
            try:
                result = job.result()
            except Exception as e:  # fail closed
                result = VerifyResult(False, None, f"verification error: {e}")
        else:
            return None
        self._job = None
        self.last_result = result
        return result

    def _push_window(self, frame: bytes, dur: float) -> None:
        self._window.append((frame, dur))
        self._window_s += dur
        while self._window and self._window_s - self._window[0][1] >= self.window_s:
            self._window_s -= self._window.popleft()[1]

    # ── main entry ───────────────────────────────────────────────────────────
    def feed(self, frame: bytes) -> list[bytes]:
        """Feed one mic frame (16 kHz int16). Returns the frames to send, in order."""
        try:
            with self._lock:
                return self._feed(frame)
        except Exception as e:  # never raise into the audio loop; fail closed
            self._on_event(f"gate error, frame dropped: {e}")
            return [bytes(len(frame))]

    def _feed(self, frame: bytes) -> list[bytes]:
        silence = bytes(len(frame))
        now = self._clock()
        if self._last_feed is not None and now - self._last_feed > _STREAM_GAP_S:
            # Mic frames pause while Era speaks. If she started replying inside a
            # fully verified owner conversation, the follow-up window restarts
            # when she finishes, so "ok"/"no" after a long answer still counts.
            if self._owner_anchor_full and self._last_feed <= self._owner_until:
                self._owner_until = now + self.followup_window_s
            self._reset()
        self._last_feed = now

        ready, why = self._verifier.readiness()
        if not ready:
            self._reset()
            if now - self._last_block_log > 30.0:
                self._last_block_log = now
                self._on_event(f"voice input BLOCKED — {why}. Run tools/enroll_speaker.py")
            return [silence]
        if not self._verifier.enabled:
            return [frame]

        dur = len(frame) / 2 / SAMPLE_RATE
        speech = self._is_speech(frame)
        if speech:
            self._silence_s = 0.0
        else:
            self._silence_s += dur
        ended = self._silence_s >= self.hangover_s

        if self.state == IDLE:
            if not speech:
                self._preroll.append(frame)
                self._preroll_s += dur
                while self._preroll_s > _PREROLL_S and len(self._preroll) > 1:
                    self._preroll_s -= len(self._preroll.popleft()) / 2 / SAMPLE_RATE
                return [silence]
            self.state = PENDING
            self._buf = list(self._preroll)
            self._buf_s = self._preroll_s
            self._preroll.clear()
            self._preroll_s = 0.0
            self._voiced_s = 0.0

        if self.state == PENDING:
            self._buf.append(frame)
            self._buf_s += dur
            if speech:
                self._voiced_s += dur
            if self._job is None and (self._voiced_s >= self.window_s or ended):
                # Decide "full check vs short reply" with the VERIFIER's own speech
                # measure. The gate's per-frame energy count is coarser (64 ms
                # frames, looser floor) and used to say "long enough" for a word
                # the verifier then refused as "too little speech to verify" —
                # which skipped the short-reply path entirely.
                voiced_s = len(voiced_segment(pcm_to_float(b"".join(self._buf)))) / SAMPLE_RATE
                if voiced_s < self.min_speech_s:
                    if not self._followup_open(now):
                        self._on_event(
                            "short utterance ignored (short replies are accepted only "
                            "right after a verified command)"
                        )
                        self._reset()
                        return [silence]
                    self._submit(b"".join(self._buf), "followup")
                else:
                    self._submit(b"".join(self._buf))
            result = self._poll()
            if result is None:
                if self._buf_s > _MAX_PENDING_S:
                    self._on_event("verification timed out — utterance ignored")
                    self._reset()
                return []
            if not result.accepted:
                self._on_event(f"rejected utterance ({result.reason})")
                if ended:
                    self._reset()
                else:
                    self.state = REJECTED
                    self._buf = []
                return [silence]
            out = self._buf
            if self._purpose == "followup":
                # Pass the short reply, but it does not renew the conversation.
                self._mark_owner(now, full=False)
                self._reset()
                return out
            self._mark_owner(now, full=True)
            for f in out:
                self._push_window(f, len(f) / 2 / SAMPLE_RATE)
            self._buf, self._buf_s, self._since_check_s = [], 0.0, 0.0
            self.state = IDLE if ended else ACCEPTED
            return out

        if self.state == ACCEPTED:
            result = self._poll()
            if result is not None and not result.accepted:
                self._on_event(f"voice changed mid-utterance ({result.reason}) — cut off")
                self.state = IDLE if ended else REJECTED
                self._window.clear()
                self._window_s = 0.0
                return [silence]
            self._push_window(frame, dur)
            if speech:
                self._since_check_s += dur
            if self._job is None and self._since_check_s >= self.reverify_s:
                self._since_check_s = 0.0
                self._submit(b"".join(f for f, _ in self._window))
                result = self._poll()  # synchronous mode resolves immediately
                if result is not None and not result.accepted:
                    self._on_event(f"voice changed mid-utterance ({result.reason}) — cut off")
                    self.state = IDLE if ended else REJECTED
                    return [silence]
            if ended:
                self._mark_owner(now, full=True)
                self._reset()
            return [frame]

        # REJECTED: swallow until the utterance ends.
        if ended:
            self._reset()
        return [silence]


class WakeAudioTap:
    """Rolling copy of the audio fed to the Vosk wake recognizer.

    Vosk word timings are seconds since the recognizer was created, so the tap
    must be cleared whenever a new recognizer is built.
    """

    def __init__(self, max_s: float = 6.0) -> None:
        self._chunks: collections.deque[tuple[int, bytes]] = collections.deque()
        self._total = 0
        self._max = int(max_s * SAMPLE_RATE)

    def clear(self) -> None:
        self._chunks.clear()
        self._total = 0

    def append(self, data: bytes) -> None:
        n = len(data) // 2
        self._chunks.append((self._total, data[: n * 2]))
        self._total += n
        while self._chunks and self._chunks[0][0] + len(self._chunks[0][1]) // 2 < (
            self._total - self._max
        ):
            self._chunks.popleft()

    def slice(self, start_s: float, end_s: float) -> bytes:
        a, b = int(start_s * SAMPLE_RATE), int(end_s * SAMPLE_RATE)
        out = bytearray()
        for off, data in self._chunks:
            lo, hi = max(a, off), min(b, off + len(data) // 2)
            if hi > lo:
                out += data[(lo - off) * 2 : (hi - off) * 2]
        return bytes(out)

    def tail(self, seconds: float) -> bytes:
        end = self._total / SAMPLE_RATE
        return self.slice(max(0.0, end - seconds), end)


class WakeVerifier:
    """Wake-word owner check with one retry.

    A single wake word is well under a second of speech, where speaker embeddings
    are least reliable, so the owner is sometimes refused. If the wake word is
    refused and said AGAIN (Vosk-matched again) within `wake_retry_window_s`, both
    takes are verified TOGETHER against the command threshold (calibrated on
    longer audio): twice the speech scores far more reliably, and an impostor
    saying it twice is still an impostor.
    """

    def __init__(self, verifier: SpeakerVerifier, clock: Callable[[], float] = time.monotonic):
        self._verifier = verifier
        self._clock = clock
        self._last_rejected: tuple[float, bytes] | None = None

    def check(self, tap: WakeAudioTap, vosk_result: dict) -> VerifyResult:
        try:
            pcm = wake_audio(tap, vosk_result)
            if not pcm:
                return VerifyResult(False, None, "no wake audio captured")
            result = self._verifier.verify(pcm, "wake")
            if result.accepted:
                self._last_rejected = None
                return result
            now = self._clock()
            window = float(self._verifier.settings.get("wake_retry_window_s", 0.0))
            prev = self._last_rejected
            if prev is not None and now - prev[0] <= window:
                self._last_rejected = None
                combined = self._verifier.verify(prev[1] + pcm, "command")
                return VerifyResult(
                    combined.accepted, combined.score, f"on repeat: {combined.reason}"
                )
            self._last_rejected = (now, pcm)
            return result
        except Exception as e:
            return VerifyResult(False, None, f"wake verification error: {e}")


def wake_audio(tap: WakeAudioTap, vosk_result: dict) -> bytes:
    """The audio of the Vosk-matched wake phrase (word timings +/- 150 ms)."""
    words = [
        w
        for w in (vosk_result.get("result") or [])
        if isinstance(w, dict) and "start" in w and "end" in w
    ]
    if words:
        start = min(float(w["start"]) for w in words) - 0.15
        end = max(float(w["end"]) for w in words) + 0.15
        return tap.slice(max(0.0, start), end)
    return tap.tail(2.0)


def verify_wake_utterance(
    verifier: SpeakerVerifier, tap: WakeAudioTap, vosk_result: dict
) -> VerifyResult:
    """Owner check on the exact audio of a Vosk-matched wake phrase. Fail-closed."""
    try:
        pcm = wake_audio(tap, vosk_result)
        if not pcm:
            return VerifyResult(False, None, "no wake audio captured")
        return verifier.verify(pcm, "wake")
    except Exception as e:
        return VerifyResult(False, None, f"wake verification error: {e}")
