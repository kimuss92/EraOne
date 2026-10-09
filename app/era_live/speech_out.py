"""Speech output — turns the model's text into audio, sentence by sentence.

Active in fish_audio mode, where Fish Audio speaks the transcript.
The BACKEND is chosen per sentence by core/tts_router.py: Fish Audio is the
primary. In gemini_audio nothing here runs —
Gemini's own Live audio is played directly, which is also what this module
falls back to when the primary backend fails (see _request_fallback).

**Why sentences.** A TTS backend awaits the whole synthesis before emitting a
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

from app.era_live.tts_text import open_note_at, sanitize_for_tts, strip_notes
from core import tts_router
from core.audio_pcm import iter_pcm_frames
from core.tts_fish import detect_lang

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

# Turns whose held Gemini audio may wait for Fish at once. Normally one; a few
# covers tool round trips without letting a stuck worker hoard audio.
_MAX_HELD_TURNS = 3


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
            # "!" / "?" are never abbreviations, so a short "Hello!" is a real
            # sentence. Absorbing it made Era wait for the NEXT sentence before
            # speaking at all (+2.2 s measured on 2026-10-09).
            is_exclaim = candidate.rstrip("\"')]").endswith(("!", "?"))
            if (
                not is_newline_break
                and not is_exclaim
                and len(candidate) < _MIN_SENTENCE_CHARS
                and rest.strip()
            ):
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


# ── First-audio fast path ───────────────────────────────────────────────────
# Gemini's transcript arrives at ~1 word per 100 ms, and Fish answers in ~0.34 s,
# so in fish_audio mode the user mostly waits for the FIRST sentence to be
# complete (median 1.46 s, up to 2.6 s, measured 2026-10-09). For the first
# chunk of a turn only, release earlier — every later sentence plays while the
# one before it is still speaking, so it gains nothing from being split.
_FIRST_CLAUSE_MIN_WORDS = 4  # "Sure," alone is too thin to start on
_FIRST_MAX_WORDS = 12  # a run-on opener is cut at a word boundary past this
_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "fig"}
)
_CLAUSE_BREAK = re.compile(r"(?<=[A-Za-z\)\"'])[,;:—–](?=\s|$)")


def _ends_a_sentence(text: str) -> bool:
    """A buffer ending in terminal punctuation that is clearly a sentence end."""
    t = text.rstrip().rstrip("\"')]")
    if not t or t[-1] not in ".!?…":
        return False
    if t[-1] in "!?…":
        return True
    word = t[:-1].split()[-1].lower() if t[:-1].split() else ""
    # "3." may be "3.5" split across chunks; "Dr." / "e.g." are not ends.
    return len(word) >= 3 and word.isalpha() and word not in _ABBREVIATIONS


def release_first(buf: str) -> tuple[str, str]:
    """Release the turn's opening words early. ("", buf) when nothing is ready.

    Called only while a turn has produced no speech yet and split_sentences()
    found no complete sentence. Three cases, in order:
      * the buffer already ends a sentence (the next chunk has not arrived to
        supply the whitespace the regular splitter waits for);
      * a clause break (comma, colon, dash) after at least four words;
      * a run-on past twelve words, cut at the last word boundary.
    """
    text = buf.strip()
    if not text:
        return "", buf
    if _ends_a_sentence(text):
        return text, ""
    for m in _CLAUSE_BREAK.finditer(buf):
        head = buf[: m.end()].strip()
        if len(head.split()) >= _FIRST_CLAUSE_MIN_WORDS:
            return head, buf[m.end() :].lstrip()
    # A word counts only once whitespace follows it: the last one may still be arriving.
    complete = [m for m in re.finditer(r"\S+", buf) if m.end() < len(buf)]
    if len(complete) >= _FIRST_MAX_WORDS:
        end = complete[_FIRST_MAX_WORDS - 1].end()
        return buf[:end].strip(), buf[end:].lstrip()
    return "", buf


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
        # Which turn a queued sentence belongs to, and that turn's held Gemini audio.
        # Gemini finishes a turn seconds before Fish finishes SPEAKING it, so the
        # fallback audio must outlive Gemini's turn_complete: on 2026-10-07 it was
        # dropped there and a late Fish failure "replayed 0 buffered chunk(s)" —
        # a silent reply. Released when the turn's last sentence has played.
        self._turn_no = 0
        self._held_audio: dict[int, list[bytes]] = {}
        self._first_out = False  # has this turn released any text to synthesis yet?

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
        # Model notes go BEFORE splitting (they carry their own full stops), and a
        # note still streaming in is held back whole until its "]" arrives.
        self._buf = strip_notes(self._buf + delta)
        held = ""
        cut = open_note_at(self._buf)
        if cut >= 0:
            self._buf, held = self._buf[:cut], self._buf[cut:]
        sentences, self._buf = split_sentences(self._buf)
        if not sentences and not self._first_out:
            head, self._buf = release_first(self._buf)
            sentences = [head] if head else []
        self._buf += held
        if sentences:
            self._first_out = True
        for s in sentences:
            self._pending.put_nowait((self._generation, self._turn_no, s))

    def pending_text(self) -> str:
        """Text produced but not yet voiced — for barge-in resume context.

        Read-only snapshot (never consumes the queue). Returns the un-split
        buffer, which is the text that has not even reached synthesis yet, so it
        is the part most certainly still unspoken. Empty in gemini_audio, where
        Gemini owns the audio and Era keeps no local text remainder. Best-effort:
        the resume note only uses this as context, so an empty string is fine.
        """
        return (self._buf or "").strip()

    def end_turn(self, held_audio: list[bytes] | None = None) -> None:
        """Flush the tail and mark the end of the turn.

        ``held_audio`` is Gemini's own audio for this turn, kept as the fallback
        until the turn's last sentence has actually been synthesized.
        """
        if not self.start():
            return
        # A "[" still open when the turn ends was a note cut short: never voice it.
        cut = open_note_at(self._buf)
        if cut >= 0:
            self._buf = self._buf[:cut]
        sentences, self._buf = split_sentences(self._buf, final=True)
        for s in sentences:
            self._pending.put_nowait((self._generation, self._turn_no, s))
        self._pending.put_nowait((self._generation, self._turn_no, _END_OF_TURN))
        if held_audio:
            self._held_audio[self._turn_no] = list(held_audio)
            while len(self._held_audio) > _MAX_HELD_TURNS:  # bounded: oldest goes first
                self._held_audio.pop(min(self._held_audio))
        self._turn_no += 1
        self._first_out = False

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
        sentences, _ = split_sentences(strip_notes(text), final=True)
        for s in sentences:
            self._pending.put_nowait((self._generation, self._turn_no, s))
        self._pending.put_nowait((self._generation, self._turn_no, _END_OF_TURN))
        self._turn_no += 1

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
        self._held_audio.clear()  # an interrupted reply must never replay later
        self._first_out = False
        dropped = 0
        while not self._pending.empty():
            try:
                self._pending.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    # ── Fallback ─────────────────────────────────────────────────────────────
    def _request_fallback(self, turn_no: int | None = None) -> None:
        """Ask the engine to finish this turn with Gemini's own audio.

        Routed through the engine (not done here) because the recovery needs the
        voice-mode flip, which the streamer does not own. When the failing
        sentence's turn has already completed on Gemini's side, its held audio
        lives HERE (see end_turn) and is handed over; otherwise the engine still
        has it in its live buffer. getattr-guarded so SpeechOut stays usable
        standalone and in tests, where there is no engine to call back into.
        """
        self._buf = ""
        held = self._held_audio.pop(turn_no, None) if turn_no is not None else None
        cb = getattr(self.era, "_tts_fallback", None)
        if cb is None:
            return
        try:
            if held:
                cb("fish synthesis failed", held_audio=held)
            else:
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
                generation, turn_no, item = await self._pending.get()
                if generation != self._generation:
                    continue  # orphaned by an interrupt

                if item is _END_OF_TURN:
                    # Every sentence of this turn has been voiced: its fallback
                    # audio can no longer be needed.
                    self._held_audio.pop(turn_no, None)
                    # Only now is the turn really over: every sentence ahead of
                    # this sentinel has already been synthesized and enqueued,
                    # because the worker is strictly sequential.
                    event = getattr(self.era, "_turn_done_event", None)
                    if event is not None:
                        event.set()
                    continue

                # Only speech reaches the voice: no notes, paths, tags or markup
                # (app/era_live/tts_text.py). A sentence that cleans to nothing is
                # skipped — it is not a synthesis failure, so no fallback.
                item = sanitize_for_tts(item)
                if not item:
                    continue
                # Logged so a leak is visible in eraone.log; spoken text was
                # otherwise never recorded anywhere.
                print(f"[TTS] say: {item[:160]}")

                await self._await_backlog()
                if generation != self._generation:
                    continue

                # PRIMARY: stream Fish Audio frames onto the queue AS THEY ARRIVE
                # so playback starts on the first frame (~hundreds of ms) instead
                # of after the whole sentence is synthesized (~seconds on a long
                # reply). This is the latency fix — see core/tts_fish.stream().
                produced = False
                event = getattr(self.era, "_turn_done_event", None)
                async for chunk in tts_router.stream(item, detect_lang(item)):
                    if generation != self._generation:
                        break
                    q = self._queue()
                    if q is None:
                        break
                    # Clear a stale turn event as soon as real audio flows, so a
                    # set() from the previous turn cannot end this one early.
                    if event is not None and event.is_set():
                        event.clear()
                    for frame in iter_pcm_frames(chunk):
                        q.put_nowait(frame)
                    produced = True
                    # Throttle mid-stream too, so a long reply is not buffered in
                    # full — the whole point of streaming.
                    await self._await_backlog()

                if produced and generation == self._generation:
                    # Every frame of this sentence is on the speaker queue, which
                    # is the only transcript/audio boundary we can actually name.
                    # LiveTurnState drops the most recent one when building a
                    # recovery note, so a drop right here repeats a sentence
                    # rather than skipping one. See core/live_turn_state.py.
                    try:
                        self.era.live_turn.note_sentence_committed(item)
                    except Exception:
                        pass

                if not produced and generation == self._generation:
                    # FALLBACK: the primary produced nothing for this sentence.
                    # Hand the turn to Gemini's own Live audio rather than drop
                    # it. Only trip this when NO audio came — a mid-stream cut
                    # (some frames already played) must not double up with a
                    # Gemini replay of the same words.
                    self._request_fallback(turn_no)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[TTS] speech worker error: {e}")
                await asyncio.sleep(0.1)
