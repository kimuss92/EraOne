"""Gemini Live session stability, G0 (docs/era-enhancement/GEMINI_SESSION_STABILITY.md).

Pure, bounded helpers; the live hooks in audio.py and main.py stay a few lines.

1. Keepalive while a PRIVACY gate (mute, mic off, standby) leaves the socket
   idle. 246 of 255 observed closes were Google's 120 s client-inactivity 1008
   while muted/standby; with audio flowing the same connection lived to its
   ~10 min GoAway. The keepalive frame is constant zeros built here, never mic
   data. UNVERIFIED: whether a sparse silent burst resets Google's idle timer
   is not documented. Each close therefore logs how many keepalive frames that
   connection sent, so a soak can prove or refute it. `ERA_LIVE_KEEPALIVE_S`
   (0 disables) and `ERA_LIVE_KEEPALIVE_FRAMES` tune it. Cost is NOT zero:
   Live bills audio input by duration, so the default (5 x 40 ms every 10 s)
   sends about 2 % of a continuous stream while gated.
4. Name the fault: a 1008 is split into inactivity / lifetime / other, using the
   connection age and whether a GoAway arrived.
5. Utterance stash: the last <= 3 s of owner audio that WAS sent, kept across a
   drop and replayed ONCE into the next session, only when the server produced
   nothing (no audio, text or tool call) after that utterance began and no tool
   was running. So a request that was acted on is never repeated.
"""

from __future__ import annotations

import collections
import math
import os
import struct

from app.audio_config import CHUNK_SIZE, SEND_SAMPLE_RATE


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return default if math.isnan(value) else min(hi, max(lo, value))


FRAME_S = CHUNK_SIZE / SEND_SAMPLE_RATE  # 0.04 s
SILENT_FRAME = bytes(CHUNK_SIZE * 2)  # int16 mono zeros: carries no microphone data
KEEPALIVE_INTERVAL_S = _env_float("ERA_LIVE_KEEPALIVE_S", 10.0, 0.0, 110.0)
KEEPALIVE_FRAMES = int(_env_float("ERA_LIVE_KEEPALIVE_FRAMES", 5, 1, 50))

# Reasons the send path drops audio for PRIVACY. Only these earn a keepalive:
# "reconnecting" has no socket to keep alive.
PRIVACY_GATES = frozenset({"muted", "mic muted by voice", "mic off", "standby without wake word"})


def keepalive_due(gate_reason: str | None, now: float, last_sent_at: float) -> bool:
    """Send a silent burst now? Only while a privacy gate holds the mic shut."""
    if KEEPALIVE_INTERVAL_S <= 0 or gate_reason not in PRIVACY_GATES:
        return False
    return now - last_sent_at >= KEEPALIVE_INTERVAL_S


def keepalive_frames() -> list[bytes]:
    return [SILENT_FRAME] * KEEPALIVE_FRAMES


# ── 4. Name the fault ────────────────────────────────────────────────────────

INACTIVITY_WINDOW_S = (110.0, 130.0)  # observed 119.8-120.0 s on 246 closes


def close_kind(detail: str, conn_age_s: float | None, had_go_away: bool) -> str | None:
    """Diagnostic name of a 1008 close, or None when it is not a 1008.

    "1008-lifetime": the server announced the end (GoAway) or the connection
    reached its ~10 min life. "1008-inactivity": Google's client-inactivity
    close (the wording, or the 120 s clock with no GoAway). Else "1008-other".
    """
    text = (detail or "").lower()
    if "1008" not in text and "policy violation" not in text:
        return None
    if had_go_away or (conn_age_s is not None and conn_age_s >= 480.0):
        return "1008-lifetime"
    if "inactiv" in text or (
        conn_age_s is not None and INACTIVITY_WINDOW_S[0] <= conn_age_s <= INACTIVITY_WINDOW_S[1]
    ):
        return "1008-inactivity"
    return "1008-other"


def age_bucket(conn_age_s: float | None) -> str:
    if conn_age_s is None:
        return "unknown"
    for limit, name in ((30, "<30s"), (110, "30-110s"), (130, "110-130s"), (480, "130-480s")):
        if conn_age_s < limit:
            return name
    return ">=480s"


# ── 5. Utterance stash ───────────────────────────────────────────────────────

VOICED_RMS = 0.01  # about -40 dBFS; the gates' substituted silence is exact zeros


def frame_rms(frame: bytes) -> float:
    n = len(frame) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", frame[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n) / 32768.0


class UtteranceStash:
    """The last few seconds of SENT owner audio, replayable once after a drop."""

    def __init__(self, window_s: float = 3.0) -> None:
        self.window_s = window_s
        self._frames: collections.deque[tuple[float, bytes]] = collections.deque(
            maxlen=max(1, int(round(window_s / FRAME_S)))
        )
        self._utterance_start: float | None = None
        self._last_voiced: float | None = None
        self._stash: list[bytes] | None = None

    def record(self, frame: bytes, now: float) -> None:
        """A frame the send path actually transmitted (after every gate)."""
        self._frames.append((now, frame))
        if frame_rms(frame) >= VOICED_RMS:
            if self._utterance_start is None:
                self._utterance_start = now
            self._last_voiced = now

    def note_model_output(self) -> None:
        """The server answered (audio, text or a tool call): the utterance it
        answered is handled and must never be replayed."""
        self._utterance_start = None
        self._last_voiced = None
        self._frames.clear()

    def capture_on_drop(self, now: float, *, tools_in_flight: int = 0) -> int:
        """Keep the unanswered utterance for one replay. Returns frames kept."""
        start, last = self._utterance_start, self._last_voiced
        self._utterance_start = self._last_voiced = None
        frames, self._frames = list(self._frames), collections.deque(maxlen=self._frames.maxlen)
        if tools_in_flight or start is None or last is None or now - last > self.window_s:
            self._stash = None
            return 0
        kept = [f for t, f in frames if t >= start - 0.2]  # small pre-roll for the onset
        self._stash = kept or None
        return len(kept)

    def take(self) -> list[bytes]:
        """The stash, exactly once."""
        stash, self._stash = self._stash or [], None
        return stash
