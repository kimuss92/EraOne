"""Is the Gemini Live session actually answering? — the verdict that says "switch".

WHY THIS EXISTS (2026-10-03)
  Karim's screen recording: HUD says "Connected", mic waveform moving, state goes
  LISTENING -> READY, and nothing ever comes back. Era's logs showed nothing
  wrong because nothing WAS wrong as far as the socket knew: no error, no close,
  and then — 20 seconds later — Google answered 1011 "service unavailable". Era
  only reacts to a disconnect, so a session that is connected but dead looked
  exactly like a quiet room. Sixteen 1011/1008 drops were logged before 16:10.

THREE FAILURE SHAPES, each decidable from signals Era already sees:

  deaf    The user spoke (mic energy) and Gemini never even transcribed it.
  mute    Gemini transcribed it but never produced an answer (audio, a tool call,
          or the end of the turn).
  flaky   Several upstream service errors in a short window. Each one reconnects
          fine, which is exactly why nobody noticed the pattern.

WHAT THIS CANNOT DO — read before trusting a verdict
  The only "the user spoke" evidence is raw mic energy: Era runs no local VAD
  while capturing for Gemini (core/turn_latency.py explains why). Music, a
  video, or a conversation in the room can look like speech. So:
    * a single short burst never counts (min_voice_s),
    * "deaf" needs the SAME silence to outlast hear_timeout_s, and either two
      separate unanswered episodes or one long one,
    * every verdict is logged with its numbers by the caller, so false positives
      are visible and the thresholds can be tuned from data rather than argued.
  A false verdict costs a few minutes on the Claude brain with an announcement;
  a missed one costs the user speaking to a wall. The thresholds lean toward the
  first.

Pure: a clock is injected, nothing here does I/O or touches asyncio, so the
mic-callback thread and the event loop can both call it (guarded by a lock).
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# Mic energy that counts as "someone is talking". int16 full scale = 1.0; room
# noise on Era's mic sits well under this, normal speech well over it.
VOICE_RMS = 0.015
# Silence longer than this ends one speech episode and starts the next.
EPISODE_GAP_S = 0.9


def enabled() -> bool:
    """Off switch: ERA_LIVE_HEALTH=0."""
    return os.getenv("ERA_LIVE_HEALTH", "1").strip().lower() not in ("0", "false", "off")


@dataclass(frozen=True)
class Verdict:
    kind: str  # "deaf" | "mute" | "flaky"
    detail: str


class LiveHealth:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        min_voice_s: float = 1.2,
        long_voice_s: float = 3.0,
        hear_timeout_s: float = 12.0,
        reply_timeout_s: float = 25.0,
        flaky_errors: int = 3,
        flaky_window_s: float = 600.0,
    ) -> None:
        self._clock = clock
        self._min_voice_s = min_voice_s
        self._long_voice_s = long_voice_s
        self._hear_timeout_s = hear_timeout_s
        self._reply_timeout_s = reply_timeout_s
        self._flaky_errors = flaky_errors
        self._flaky_window_s = flaky_window_s
        self._lock = threading.Lock()
        self._errors: deque[float] = deque()  # survives reset(): it spans connections
        self.reset()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def reset(self) -> None:
        """A new connection starts with a clean slate. Upstream errors are kept:
        they describe the pattern ACROSS connections."""
        with self._lock:
            self._run_s = 0.0  # voice seconds in the current episode
            self._gap_s = 0.0
            self._episode_counted = False
            self._unheard_n = 0  # unanswered speech episodes
            # WHEN the user last made sound. The deaf clock runs from here, not
            # from when they started: on 2026-10-03 a 9-second sentence was judged
            # "deaf" while the user was still finishing it, because Gemini sends
            # the transcript after the user stops, not during.
            self._last_voice_at: float | None = None
            self._longest_unheard_s = 0.0
            self._awaiting_reply_since: float | None = None

    def clear_errors(self) -> None:
        with self._lock:
            self._errors.clear()

    # ── inputs ──────────────────────────────────────────────────────────────
    def note_voice(self, rms: float, dt: float) -> None:
        """One mic block: `rms` as a 0..1 fraction of full scale, `dt` its length
        in seconds. Called only for audio that is being sent to Gemini."""
        now = self._clock()
        with self._lock:
            if rms >= VOICE_RMS:
                self._gap_s = 0.0
                self._run_s += dt
                self._last_voice_at = now
                if not self._episode_counted and self._run_s >= self._min_voice_s:
                    self._episode_counted = True
                    self._unheard_n += 1
                if self._episode_counted:
                    self._longest_unheard_s = max(self._longest_unheard_s, self._run_s)
            else:
                self._gap_s += dt
                if self._gap_s >= EPISODE_GAP_S:
                    self._run_s = 0.0
                    self._episode_counted = False

    def note_heard(self) -> None:
        """Gemini transcribed something the user said — it can hear."""
        with self._lock:
            self._unheard_n = 0
            self._longest_unheard_s = 0.0
            self._awaiting_reply_since = self._clock()

    def note_reply(self) -> None:
        """Gemini produced an answer: audio, a spoken transcript, a tool call,
        or the end of a turn. Anything that means "it responded"."""
        with self._lock:
            self._awaiting_reply_since = None
            self._unheard_n = 0
            self._longest_unheard_s = 0.0

    def note_upstream_error(self) -> None:
        now = self._clock()
        with self._lock:
            self._errors.append(now)
            self._trim(now)

    def _trim(self, now: float) -> None:
        while self._errors and now - self._errors[0] > self._flaky_window_s:
            self._errors.popleft()

    # ── the verdict ─────────────────────────────────────────────────────────
    def verdict(self) -> Verdict | None:
        now = self._clock()
        with self._lock:
            self._trim(now)
            # Silence for hear_timeout_s AFTER the last sound, so a user who is
            # still talking is never judged, however long the sentence.
            if (
                self._unheard_n
                and self._last_voice_at is not None
                and now - self._last_voice_at >= self._hear_timeout_s
            ):
                if self._unheard_n >= 2 or self._longest_unheard_s >= self._long_voice_s:
                    return Verdict(
                        "deaf",
                        f"{self._unheard_n} spoken episode(s), longest "
                        f"{self._longest_unheard_s:.1f}s, no transcript for "
                        f"{now - self._last_voice_at:.0f}s after the user stopped",
                    )
            if (
                self._awaiting_reply_since is not None
                and now - self._awaiting_reply_since >= self._reply_timeout_s
            ):
                return Verdict(
                    "mute",
                    f"heard the user but no answer for {now - self._awaiting_reply_since:.0f}s",
                )
            if len(self._errors) >= self._flaky_errors:
                return Verdict(
                    "flaky",
                    f"{len(self._errors)} upstream errors in {self._flaky_window_s / 60:.0f} min",
                )
        return None
