"""Speech output — turns the model's text into audio, sentence by sentence.

Active in every SPEECH_OUT mode (fish_audio, kokoro_audio, kokoro_text).
The BACKEND is chosen per sentence by core/tts_router.py: Fish Audio is the
primary, Kokoro serves the local modes. In gemini_audio nothing here runs —
Gemini's own Live audio is played directly, which is also what this module
falls back to when the primary backend fails (see _request_fallback).

**Why sentences.** Voicebox's ``POST /generate/stream`` awaits the whole
synthesis before emitting a byte (backend/routes/generations.py:366-410), so
synthesizing a full reply in one call would make first-audio latency scale with
reply length. Gemini in TEXT mode streams text incrementally, so we cut on
sentence boundaries and synthesize sentence N+1 while N is still playing. First
audio then costs only the first sentence.

The pipelining is implicit: the worker enqueues PCM without blocking, while
_play_audio drains that queue independently, so the worker naturally runs ahead
of playback. `_MAX_BACKLOG_FRAMES` is what stops it running *too* far ahead and
buffering a 50k-character reply into memory.

**Handing off to the existing player.** Frames go onto ``era.audio_in_queue``,
the queue _play_audio already drains — so the UI sphere, ``set_speaking()``, the
half-duplex mic gate and every ``while self._is_speaking`` wait keep working
untouched.

``_turn_done_event`` is the subtle part. _play_audio ends a turn when that event
is set AND the queue has drained (app/era_live/audio.py:378-388). So it must be
set only after the LAST sentence's PCM is on the queue — setting it while a
synthesis is still in flight would end the turn early during the momentary gap
between sentences, dropping Era to LISTENING mid-reply.
"""

from __future__ import annotations

import asyncio
import re

from core import tts_router

# VOICEBOX is re-exported (not called directly any more — tts_router owns
# backend choice) so existing call sites and tests can still reach the
# singleton through this module.
from core.tts_voicebox import VOICEBOX, iter_pcm_frames  # noqa: F401

# Sentence boundary: terminal punctuation followed by whitespace or end. Also
# treat a newline as a boundary — list items and short lines rarely end in a
# period but are natural speech breaks.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")

# Below this, a "sentence" is usually an abbreviation ("Dr. Smith", "e.g. this")
# rather than a real break, so keep accumulating. Small enough that the FIRST
# sentence still goes out fast, which is what governs perceived latency.
_MIN_SENTENCE_CHARS = 12

# A run-on with no punctuation must not stall speech forever; split at a word
# boundary once the buffer gets this long.
_MAX_SENTENCE_CHARS = 320

# Roughly two seconds of 24 kHz audio. Enough that playback never starves
# between sentences, low enough that a long reply is not fully materialised.
_MAX_BACKLOG_FRAMES = 48

_END_OF_TURN = object()


def split_sentences(text: str, *, final: bool = False) -> tuple[list[str], str]:
    """Split `text` into complete sentences plus the unconsumed remainder.

    With ``final=True`` the remainder is flushed as a sentence too — used at
    turn_complete, when no more text is coming.
    """
    if not text:
        return [], ""
    out: list[str] = []
    buf = text
    while True:
        m = _SENTENCE_END.search(buf)
        if m:
            candidate = buf[: m.start()].strip()
            rest = buf[m.end() :]
            # Too short to be a real sentence — likely an abbreviation. Absorb
            # the boundary and keep going rather than emitting a fragment.
            # A NEWLINE is exempt: it is an unambiguous break, so a short line
            # ("Line one") is a real utterance, not a "Dr." false positive.
            is_newline_break = "\n" in m.group(0)
            if not is_newline_break and len(candidate) < _MIN_SENTENCE_CHARS and rest.strip():
                buf = buf[: m.end()] + rest
                nxt = _SENTENCE_END.search(buf, m.end())
                if not nxt:
                    break
                candidate = buf[: nxt.start()].strip()
                rest = buf[nxt.end() :]
            if candidate:
                out.append(candidate)
            buf = rest
            continue
        if len(buf) >= _MAX_SENTENCE_CHARS:
            cut = buf.rfind(" ", 0, _MAX_SENTENCE_CHARS)
            if cut <= 0:
                cut = _MAX_SENTENCE_CHARS
            head = buf[:cut].strip()
            if head:
                out.append(head)
            buf = buf[cut:].lstrip()
            continue
        break
    if final:
        tail = buf.strip()
        if tail:
            out.append(tail)
        buf = ""
    return out, buf


class SpeechOut:
    """Buffers streamed text, synthesizes it locally, feeds Era's audio queue.

    Owned by EraLive. The audio queue and turn event are read lazily off the
    engine on every use because both are recreated per Live session — caching
    them would leave the streamer writing into a dead session's queue after a
    reconnect.
    """

    def __init__(self, era) -> None:
        self.era = era
        self._buf = ""
        self._pending: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._generation = 0  # bumped on interrupt to orphan in-flight work

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        """Ensure the synthesis worker is running. True if it is (or now is).

        Returns False instead of raising when there is no running loop in THIS
        thread. Every entry point here is reachable from non-loop threads (the
        UI text command, the Telegram bridge, tool dispatch), and
        asyncio.create_task() requires a running loop in the calling thread —
        so this used to raise "RuntimeError: no running event loop" straight
        through speak() and out of whatever thread called it. The voice layer's
        rule is degrade, never raise: no worker means no local speech, which
        must never be fatal to the caller.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
            # Without a done-callback a worker that dies raises only the
            # "Task exception was never retrieved" warning at GC time, which is
            # how a silently-dead speech worker would look identical to Era
            # simply having nothing to say.
            self._worker.add_done_callback(self._on_worker_done)
        return True

    @staticmethod
    def _on_worker_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            print(f"[TTS] speech worker died: {type(exc).__name__}: {exc}")

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        self._worker = None

    # ── Input ────────────────────────────────────────────────────────────────
    def feed(self, delta: str) -> None:
        """Accept a chunk of streamed model text."""
        if not delta:
            return
        if not self.start():
            return
        self._buf += delta
        sentences, self._buf = split_sentences(self._buf)
        for s in sentences:
            self._pending.put_nowait((self._generation, s))

    def pending_text(self) -> str:
        """Text produced but not yet voiced — for barge-in resume context.

        Read-only snapshot (never consumes the queue). Returns the un-split
        buffer, which is the text that has not even reached synthesis yet, so it
        is the part most certainly still unspoken. Empty in gemini_audio, where
        Gemini owns the audio and Era keeps no local text remainder. Best-effort:
        the resume note only uses this as context, so an empty string is fine.
        """
        return (self._buf or "").strip()

    def end_turn(self) -> None:
        """Flush the tail and mark the end of the turn."""
        if not self.start():
            return
        sentences, self._buf = split_sentences(self._buf, final=True)
        for s in sentences:
            self._pending.put_nowait((self._generation, s))
        self._pending.put_nowait((self._generation, _END_OF_TURN))

    def say(self, text: str) -> None:
        """Speak a complete utterance. Used by speak(); needs no Live session.

        Needs no SESSION, but it does need the event LOOP — the worker that
        synthesizes and the queue it feeds both live there. speak() normally
        marshals here via call_soon_threadsafe; the direct call is the no-loop
        fallback, and start() is what keeps that case from raising.
        """
        text = (text or "").strip()
        if not text:
            return
        if not self.start():
            print("[TTS] no running loop — dropping local utterance")
            return
        sentences, _ = split_sentences(text, final=True)
        for s in sentences:
            self._pending.put_nowait((self._generation, s))
        self._pending.put_nowait((self._generation, _END_OF_TURN))

    # ── Interrupt ────────────────────────────────────────────────────────────
    def interrupt(self) -> int:
        """Drop everything queued and orphan any in-flight synthesis.

        Bumping the generation is what makes this safe: a synthesis already in
        flight cannot be cancelled mid-HTTP-call, so instead its result is
        discarded when it comes back with a stale generation. Without that, a
        sentence from the previous turn would play after a wake or a mode switch.
        """
        self._generation += 1
        self._buf = ""
        dropped = 0
        while not self._pending.empty():
            try:
                self._pending.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    # ── Fallback ─────────────────────────────────────────────────────────────
    def _request_fallback(self) -> None:
        """Ask the engine to finish this turn with Gemini's own audio.

        Routed through the engine (not done here) because the recovery needs the
        buffered cloud audio and the voice-mode flip, neither of which the
        streamer owns. getattr-guarded so SpeechOut stays usable standalone and
        in tests, where there is no engine to call back into.
        """
        self._buf = ""
        cb = getattr(self.era, "_tts_fallback", None)
        if cb is None:
            return
        try:
            cb("fish synthesis failed")
        except Exception as e:
            print(f"[TTS] fallback request failed: {e}")

    # ── Worker ───────────────────────────────────────────────────────────────
    def _queue(self):
        return getattr(self.era, "audio_in_queue", None)

    async def _await_backlog(self) -> None:
        """Throttle synthesis so a long reply is not fully buffered up front."""
        q = self._queue()
        while q is not None and q.qsize() > _MAX_BACKLOG_FRAMES:
            await asyncio.sleep(0.05)
            q = self._queue()

    async def _run(self) -> None:
        while True:
            try:
                generation, item = await self._pending.get()
                if generation != self._generation:
                    continue  # orphaned by an interrupt

                if item is _END_OF_TURN:
                    # Only now is the turn really over: every sentence ahead of
                    # this sentinel has already been synthesized and enqueued,
                    # because the worker is strictly sequential.
                    event = getattr(self.era, "_turn_done_event", None)
                    if event is not None:
                        event.set()
                    continue

                await self._await_backlog()
                if generation != self._generation:
                    continue

                # PRIMARY: Fish Audio (or Kokoro in the local modes). The
                # router picks by voice mode; an empty result means that backend
                # could not speak this sentence.
                pcm, backend = await tts_router.synthesize(item)
                if generation != self._generation:
                    continue
                if not pcm:
                    # FALLBACK: hand the turn to Gemini's own Live audio rather
                    # than dropping the sentence. The engine owns that switch —
                    # it has the buffered cloud audio and the mode flip.
                    if backend == tts_router.BACKEND_FISH:
                        self._request_fallback()
                    continue

                q = self._queue()
                if q is None:
                    continue
                # Clear the turn event as soon as real audio starts flowing, so
                # a stale set() from the previous turn cannot end this one early.
                event = getattr(self.era, "_turn_done_event", None)
                if event is not None and event.is_set():
                    event.clear()
                for frame in iter_pcm_frames(pcm):
                    q.put_nowait(frame)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[TTS] speech worker error: {e}")
                await asyncio.sleep(0.1)
