"""Where a turn's time actually goes — measured, not argued about. (Board item 22.)

WHY THIS EXISTS
  "She responds too late" has at least four candidate causes, and they need
  completely different fixes: the endpointing wait (Era being deliberately
  patient so she does not cut the user off), the model's own think time, a tool
  call in the middle of the turn, and the lead-in before the first audio reaches
  the speakers. A screenshot cannot tell them apart, and the board's rule for
  this item is "measure first — do not optimize on a guess".

THE NUMBER THAT MATCHES THE COMPLAINT is `response_ms`: the user's last word to
  the first audio they hear. Every other field is a breakdown of it.

WHAT THIS CAN AND CANNOT SEE — read before trusting `endpoint_wait_ms`
  Era's local VAD runs only WHILE SHE IS SPEAKING (the barge-in detector in
  app/era_live/audio.py — see the `if era_speaking:` branch). While the user is
  talking, mic frames go straight to Gemini unexamined, so there is no local
  "the user stopped making sound" timestamp to measure from.

  So `endpoint_wait_ms` is the gap between the LAST input-transcript delta and
  Gemini's `finished` mark. That is a PROXY for the configured `silence_ms`
  wait, inflated by however long the server sat on that last delta. It is good
  enough to tell 800ms of deliberate patience from a 4s model stall; it is not a
  calibrated measurement of the VAD, and a shift of a few tens of ms in it means
  nothing. `silence_ms` is logged beside it precisely so the two can be compared
  — if they track each other, the wait is the config and the dial is the fix; if
  `endpoint_wait_ms` runs far above it, the server is slow to transcribe and the
  dial will not help.

  Measuring the wait properly would mean running local VAD on every mic frame
  during normal capture, which is a CPU and behaviour change, not instrumentation.
  That is a separate decision and deliberately not made here.

DURATIONS USE A MONOTONIC CLOCK, never wall time: a turn that straddles an NTP
  correction or a DST change must not report a negative or hour-long latency.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable


def enabled() -> bool:
    """Off switch. On by default: one JSON line per turn is cheap, and the whole
    point of this module is that the numbers exist BEFORE someone needs them."""
    return os.getenv("ERA_TURN_LATENCY", "1").strip().lower() not in ("0", "false", "off")


def _ms(start: float | None, end: float | None) -> int | None:
    """Milliseconds between two marks, or None when either never happened.

    None is deliberately not 0: a turn where the model never produced audio and
    a turn where it produced audio instantly are completely different findings,
    and zero would silently merge them.
    """
    if start is None or end is None:
        return None
    return max(0, int(round((end - start) * 1000)))


class TurnLatency:
    """One turn's marks. Every method is safe to call at any time, in any order,
    any number of times — it sits in the audio receive loop, and instrumentation
    that can throw is instrumentation that takes the voice down with it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._user_first: float | None = None
        self._user_last_delta: float | None = None
        self._user_finished: float | None = None
        self._model_first_out: float | None = None
        self._first_played: float | None = None
        self._turn_complete: float | None = None
        self._tool_ms: int = 0
        self._tool_count: int = 0
        self._interrupted = False

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return 0.0

    # ── marks ────────────────────────────────────────────────────────────────

    def note_user_delta(self) -> None:
        """A piece of the user's transcript arrived. The FIRST starts the turn;
        the LAST is our best proxy for when they stopped speaking."""
        now = self._now()
        if self._user_first is None:
            self._user_first = now
        self._user_last_delta = now

    def note_user_finished(self) -> None:
        if self._user_finished is None:
            self._user_finished = self._now()

    def note_model_output(self) -> None:
        """First audio byte or first text token of the answer — whichever the
        current voice mode produces first. Both are "she started answering"."""
        if self._model_first_out is None:
            self._model_first_out = self._now()

    def note_audio_played(self) -> None:
        """First byte that actually reached the speakers. Distinct from
        note_model_output on purpose: audio received is not audio heard, and the
        gap between them is the only part of the wait the user can hear."""
        if self._first_played is None:
            self._first_played = self._now()

    def note_tool(self, duration_ms: int | float | None) -> None:
        if duration_ms is None:
            return
        try:
            self._tool_ms += max(0, int(duration_ms))
            self._tool_count += 1
        except (TypeError, ValueError):
            pass

    def note_interrupted(self) -> None:
        """The user barged in. Recorded because an interrupted turn's numbers
        describe the interruption, not the latency, and averaging them in would
        quietly drag every statistic toward whatever the user cut short."""
        self._interrupted = True

    def note_turn_complete(self) -> None:
        if self._turn_complete is None:
            self._turn_complete = self._now()

    # ── the reading ──────────────────────────────────────────────────────────

    def summary(self, silence_ms: int | None = None) -> dict | None:
        """The breakdown, or None when this turn measured nothing worth logging.

        None rather than a row of nulls: Era answering a typed message, or a
        session waking up, produces no user speech at all, and a log full of
        empty turns is how a measurement stops being read.
        """
        if self._user_finished is None and self._model_first_out is None:
            return None

        out = {
            # What the user feels: their last word -> the first sound back.
            "response_ms": _ms(self._user_last_delta, self._first_played),
            # Proxy, not a measurement — see the module docstring.
            "endpoint_wait_ms": _ms(self._user_last_delta, self._user_finished),
            # The model thinking, including any tool round trip inside it.
            "model_ms": _ms(self._user_finished, self._model_first_out),
            # Audio received -> audio heard.
            "playback_ms": _ms(self._model_first_out, self._first_played),
            "total_ms": _ms(self._user_first, self._turn_complete),
            "tool_ms": self._tool_ms,
            "tool_count": self._tool_count,
            "interrupted": self._interrupted,
        }
        if silence_ms is not None:
            # Logged beside endpoint_wait_ms so the proxy can be judged against
            # the setting it is meant to reflect.
            out["configured_silence_ms"] = silence_ms
        return out
