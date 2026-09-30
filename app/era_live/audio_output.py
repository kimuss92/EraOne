"""Self-healing speaker output for Era's Live voice.

Why this exists: every `_play_audio` entry in engine.reconnect_error was a LOCAL
audio-device fault, never the network — PortAudio's "There is no driver
installed on your system" [MME error 6], raised when the output endpoint
vanished or switched under an open stream (Star Glasses / BT headset connecting
or dropping, a `connect_device` call, a volume/endpoint change). The raise
escaped `_play_audio`, the TaskGroup wrapped it in an ExceptionGroup, and the
whole Gemini Live session was torn down and reconnected — which cannot fix a
speaker, and when the device was still missing, the next session died the same
way.

The contract here: a speaker problem never kills the session.
  - Open/write failures are retried with capped exponential backoff, on the
    preferred device first and then the system default (MME's default follows
    the Windows default endpoint, so it recovers once Windows settles).
  - A short outage loses nothing: the chunk being written is retried inline for
    ~2s while new audio waits in the queue.
  - A longer outage degrades to silence (chunks are dropped so the turn logic
    keeps working) and reopening is retried lazily, never blocking the loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import sounddevice as sd

from core.logger import log_event, main_logger

# Inline retry for a chunk that just failed: total ~1.75s before degrading.
INLINE_RETRY_DELAYS_S = (0.25, 0.5, 1.0)
# Background reopen backoff while degraded (doubles per failure, capped).
DEGRADED_RETRY_BASE_S = 1.0
DEGRADED_RETRY_MAX_S = 8.0

# Errors that mean "the audio device is gone/unusable", as opposed to a bug.
DEVICE_ERRORS: tuple[type[BaseException], ...] = (sd.PortAudioError, OSError)


class ResilientOutputStream:
    """A RawOutputStream that reopens itself instead of raising.

    `opener(device)` must return a started-or-startable stream with
    start/write/stop/close; it is injectable so tests need no audio hardware.
    """

    def __init__(
        self,
        opener: Callable[[Any], Any],
        preferred_device: Any = None,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        inline_delays: tuple[float, ...] = INLINE_RETRY_DELAYS_S,
        on_open: Callable[[Any, bool], None] | None = None,
    ) -> None:
        self._opener = opener
        # Reports (device, on_fallback) every time a stream opens, so something
        # can answer "where is the voice ACTUALLY playing" - a fallback open is
        # otherwise invisible to everyone except this object.
        self._on_open = on_open
        self._preferred = preferred_device
        self._sleep = sleep
        self._clock = clock
        self._inline_delays = inline_delays
        self._stream: Any = None
        self._degraded = False
        self._next_retry_at = 0.0
        self._retry_delay = DEGRADED_RETRY_BASE_S
        self.dropped_chunks = 0
        # True while playing on the default device because the preferred one
        # could not be opened. The preference itself is kept, so the next
        # reopen tries it again and reclaims it once it is back.
        self.on_fallback = False
        # Whether this outage has already been reported (see write()).
        self._lost_logged = False

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def degraded(self) -> bool:
        return self._degraded

    # ── open / close ──────────────────────────────────────────────────────

    def _candidates(self) -> list[Any]:
        return [self._preferred, None] if self._preferred is not None else [None]

    def _try_open(self) -> bool:
        """One pass over preferred-then-default. Never raises."""
        for device in self._candidates():
            stream = None
            try:
                stream = self._opener(device)
                stream.start()
            except Exception as err:
                self._close_quietly(stream)
                print(f"[era] [PLAY] output open failed on device {device!r}: {err}")
                # NOTHING that re-enumerates PortAudio may happen here. Era's
                # microphone InputStream is open for the whole session and
                # Pa_Terminate is PROCESS-wide: it turns every open stream into
                # an invalid pointer (PaErrorCode -9988, verified), _listen_audio
                # re-raises, and the Live session dies. A speaker fault killing
                # the session is the exact thing this class exists to prevent.
                #
                # It is also unnecessary for the common case. A device that was
                # present at startup keeps its PortAudio index across a
                # disconnect/reconnect, so simply retrying `_preferred` - which
                # `_candidates` already does on every reopen - is what recovers
                # flapping Bluetooth. Re-enumeration is only needed for a device
                # that never existed at Pa_Initialize, and that is handled
                # between sessions, where no stream is open (see main.py).
                continue
            # A fallback open is recorded, not destructive. The preference is
            # the user's choice of where Era speaks and outlives one unplug:
            # erasing it here meant a pinned device that came back mid-session
            # was never reclaimed, and the pin looked like it had been ignored.
            self.on_fallback = device != self._preferred
            self._stream = stream
            if self._on_open is not None:
                try:
                    self._on_open(device, self.on_fallback)
                except Exception as cb_err:  # a reporter must never cost audio
                    print(f"[era] [PLAY] route report failed: {cb_err}")
            if self.on_fallback:
                print(
                    f"[era] [PLAY] opened FALLBACK device {device!r}; "
                    f"{self._preferred!r} was unavailable."
                )
                try:
                    log_event(
                        main_logger,
                        "audio.output.fallback",
                        "WARNING",
                        preferred=str(self._preferred),
                        opened=str(device),
                    )
                except Exception:
                    pass
            return True
        return False

    def reclaim_preferred(self) -> None:
        """Go back to the chosen device, which has just reappeared.

        Only meaningful while on the fallback. Dropping the stream sends the
        next write through ``_try_open``, whose candidate list still starts with
        the preferred device. Without this, a Bluetooth device that reconnected
        mid-session was never taken back: nothing FAILS while the fallback plays
        happily, so no reopen was ever triggered.

        Called between chunks, so no audio is in flight and nothing is lost.
        """
        if not self.on_fallback or self._preferred is None:
            return
        print(f"[era] [PLAY] chosen device {self._preferred!r} is back; reclaiming")
        self._drop_stream()
        self._next_retry_at = 0.0
        self._retry_delay = DEGRADED_RETRY_BASE_S

    def set_preferred_device(self, device: Any) -> None:
        """Re-route an OPEN stream to a different device, now.

        Dropping the stream is what makes the change immediate: the next write
        reopens on the new device, mid-sentence, with no session restart. A
        no-op when the device is unchanged, so polling this is free.
        """
        if device == self._preferred and not self.on_fallback:
            return
        print(f"[era] [PLAY] re-routing voice to device {device!r}")
        self._preferred = device
        self._drop_stream()
        # A pending backoff belongs to the OLD device; a deliberate re-route
        # must not have to wait it out.
        self._next_retry_at = 0.0
        self._retry_delay = DEGRADED_RETRY_BASE_S

    @staticmethod
    def _close_quietly(stream: Any) -> None:
        if stream is None:
            return
        # A stream whose device vanished can raise on stop/close.
        for op in ("stop", "close"):
            try:
                getattr(stream, op)()
            except Exception:
                pass

    def _drop_stream(self) -> None:
        self._close_quietly(self._stream)
        self._stream = None

    def open(self) -> bool:
        """Open at session start. On failure, degrade rather than raise."""
        if self._try_open():
            return True
        self._enter_degraded("open failed at session start")
        return False

    def close(self) -> None:
        self._drop_stream()

    # ── degraded state ────────────────────────────────────────────────────

    def _enter_degraded(self, reason: str) -> None:
        if not self._degraded:
            self._degraded = True
            self._retry_delay = DEGRADED_RETRY_BASE_S
            print(f"[era] [PLAY] speaker unavailable ({reason}); voice muted until it returns.")
            try:
                log_event(main_logger, "audio.output.lost", "WARNING", reason=reason[:200])
            except Exception:
                pass
        self._next_retry_at = self._clock() + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, DEGRADED_RETRY_MAX_S)

    def _leave_degraded(self) -> None:
        if self._degraded:
            print(f"[era] [PLAY] speaker recovered ({self.dropped_chunks} chunks skipped).")
            try:
                log_event(
                    main_logger,
                    "audio.output.recovered",
                    "INFO",
                    dropped_chunks=self.dropped_chunks,
                )
            except Exception:
                pass
        self._degraded = False
        self._retry_delay = DEGRADED_RETRY_BASE_S
        self.dropped_chunks = 0

    # ── write ─────────────────────────────────────────────────────────────

    async def _write_once(self, chunk: bytes) -> None:
        await asyncio.to_thread(self._stream.write, chunk)

    async def write(self, chunk: bytes) -> bool:
        """Play `chunk`. Returns True if it reached the speaker. Never raises
        for device faults; cancellation still propagates."""
        if self._stream is None:
            # Degraded: only probe the device when the backoff window has passed,
            # so a missing speaker costs nothing on the hot path.
            if self._clock() < self._next_retry_at:
                self.dropped_chunks += 1
                return False
            if not self._try_open():
                self._enter_degraded("reopen failed")
                self.dropped_chunks += 1
                return False
            self._leave_degraded()

        try:
            await self._write_once(chunk)
            self._lost_logged = False  # a clean write ends the outage episode
            return True
        except DEVICE_ERRORS as err:
            # Once per OUTAGE, not once per chunk. A Bluetooth device that
            # disconnects and reconnects on its own produces this on every
            # buffer, and a flapping headset used to write hundreds of identical
            # "output device lost" lines into the log - which then reads like a
            # cascade of failures instead of one device blinking. The recovery
            # itself is unchanged; only the noise is.
            if not self._lost_logged:
                self._lost_logged = True
                print(f"[era] [PLAY] output device lost ({err}); reopening.")
            self._drop_stream()
            last_err: BaseException = err

        # Short outage (endpoint switching): retry this same chunk inline with
        # backoff. Audio arriving meanwhile waits in the queue, so nothing is lost.
        for delay in self._inline_delays:
            await self._sleep(delay)
            if not self._try_open():
                continue
            try:
                await self._write_once(chunk)
                return True
            except DEVICE_ERRORS as err:
                last_err = err
                self._drop_stream()

        self._enter_degraded(str(last_err))
        self.dropped_chunks += 1
        return False
