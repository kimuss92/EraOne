"""Sentence streamer: splitting, turn boundaries, backpressure and interrupts.

The subtle one is `_turn_done_event`. _play_audio ends a turn when that event is
set AND the queue has drained (app/era_live/audio.py:378-388). Sentences arrive
with gaps between them while the next is synthesizing, so setting the event when
Gemini stops *talking* would end the turn mid-reply and drop Era to LISTENING
with half her answer unspoken. It must only be set once the LAST sentence's
audio is on the queue.

Synthesis is stubbed throughout — no Voicebox backend is required.
"""

import asyncio

import pytest

import app.era_live.speech_out as so
from app.era_live.speech_out import SpeechOut, split_sentences
from core.tts_fish import FISH as _FISH


# ── Splitting ────────────────────────────────────────────────────────────────
def test_splits_on_terminal_punctuation():
    out, rest = split_sentences("First one here. Second one here! Third?")
    assert out == ["First one here.", "Second one here!"]
    assert rest == "Third?"


def test_incomplete_tail_is_held_back():
    """Streamed text arrives mid-sentence; a fragment must not be synthesized."""
    out, rest = split_sentences("A complete sentence. And a partial")
    assert out == ["A complete sentence."]
    assert rest == "And a partial"


def test_final_flushes_the_tail():
    out, rest = split_sentences("And a partial", final=True)
    assert out == ["And a partial"]
    assert rest == ""


def test_abbreviation_does_not_split():
    """'Dr.' is not the end of a sentence."""
    out, _ = split_sentences("Dr. Smith is here now. Next thing.")
    assert out[0] == "Dr. Smith is here now."


def test_newline_splits_even_when_short():
    """A newline is unambiguous, so the abbreviation guard must not apply."""
    out, _ = split_sentences("Line one\nLine two is longer here\n")
    assert out == ["Line one", "Line two is longer here"]


def test_runaway_text_is_chopped_at_a_word_boundary():
    """No punctuation must never mean no speech."""
    out, rest = split_sentences("word " * 200)
    assert out, "a run-on should still produce speakable chunks"
    assert all(len(s) <= so._MAX_SENTENCE_CHARS for s in out)
    assert not any(s.endswith("wor") for s in out), "split mid-word"


def test_empty_input():
    assert split_sentences("") == ([], "")
    assert split_sentences("", final=True) == ([], "")


# ── Streamer ─────────────────────────────────────────────────────────────────
class _FakeEra:
    def __init__(self):
        self.audio_in_queue = asyncio.Queue()
        self._turn_done_event = asyncio.Event()


@pytest.fixture(autouse=True)
def _pin_fish_mode(monkeypatch):
    """These tests exercise the STREAMING logic, not backend choice.

    tts_router dispatches on the active voice mode, so pin it rather than
    depending on whatever config/voice.json holds on this machine.
    """
    from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG

    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO, raising=False)


@pytest.fixture
def stub_synth(monkeypatch):
    """Stub streaming synthesis: 100 bytes of PCM per sentence, and a call log."""
    calls = []

    async def _fake(text):
        calls.append(text)
        yield b"\x00\x01" * 50

    monkeypatch.setattr(_FISH, "stream", _fake)
    return calls


async def _drain(streamer, era, timeout=2.0):
    """Wait for the worker to finish the queued work."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if streamer._pending.empty() and era._turn_done_event.is_set():
            return True
        await asyncio.sleep(0.01)
    return False


def test_audio_reaches_the_queue(stub_synth):
    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.say("Hello there everyone. How are you?")
        assert await _drain(s, era)
        await s.stop()
        assert era.audio_in_queue.qsize() > 0
        assert stub_synth == ["Hello there everyone.", "How are you?"]

    asyncio.run(_run())


def test_turn_event_waits_for_the_last_sentence(monkeypatch):
    """The turn must not end while a sentence is still being synthesized.

    A slow second sentence stands in for real synthesis latency: if end_turn()
    set the event eagerly, it would already be set here while the worker is
    still mid-call, and _play_audio would cut Era off.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(text):
        if "second" in text:
            started.set()
            await release.wait()
        yield b"\x00\x01" * 50

    monkeypatch.setattr(_FISH, "stream", _slow)

    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.feed("The first sentence here. The second sentence here. ")
        s.end_turn()

        await asyncio.wait_for(started.wait(), timeout=2.0)
        # Gemini has finished sending; synthesis has NOT finished.
        assert not era._turn_done_event.is_set(), "turn ended mid-reply"

        release.set()
        assert await _drain(s, era)
        await s.stop()

    asyncio.run(_run())


def test_interrupt_drops_pending_sentences(stub_synth):
    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.feed("One sentence here. Two sentence here. Three sentence here. ")
        dropped = s.interrupt()
        await asyncio.sleep(0.05)
        await s.stop()
        assert dropped > 0
        assert s._pending.empty()

    asyncio.run(_run())


def test_interrupt_orphans_in_flight_audio(monkeypatch):
    """Synthesis already in flight cannot be cancelled — its result is discarded.

    Without the generation check, a sentence from before a wake or a mode switch
    would play into the new context.
    """
    release = asyncio.Event()

    async def _slow(text):
        await release.wait()
        yield b"\x00\x01" * 50

    monkeypatch.setattr(_FISH, "stream", _slow)

    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.say("A sentence that is being spoken.")
        await asyncio.sleep(0.05)  # let the worker enter synthesize()

        s.interrupt()
        release.set()
        await asyncio.sleep(0.1)
        await s.stop()

        assert era.audio_in_queue.qsize() == 0, "orphaned audio reached the speakers"

    asyncio.run(_run())


def test_say_works_with_no_session(stub_synth):
    """speak() must work without a Live session.

    _FakeEra deliberately has no `session` attribute at all.
    """

    async def _run():
        era = _FakeEra()
        assert not hasattr(era, "session")
        s = SpeechOut(era)
        s.say("Quota is exhausted, sir.")
        assert await _drain(s, era)
        await s.stop()
        assert era.audio_in_queue.qsize() > 0

    asyncio.run(_run())


def test_failed_synthesis_does_not_stall_the_turn(monkeypatch):
    """A synthesis failure must still end the turn, or Era hangs in SPEAKING."""

    async def _fail(text):
        return
        yield  # make this an async generator that produces nothing

    monkeypatch.setattr(_FISH, "stream", _fail)

    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.say("This will fail to synthesize.")
        assert await _drain(s, era), "turn never completed after a synthesis failure"
        await s.stop()
        assert era.audio_in_queue.qsize() == 0

    asyncio.run(_run())


# ── Held Gemini audio outlives Gemini's turn_complete (2026-10-07 silent turn) ──
class _FallbackEra(_FakeEra):
    def __init__(self):
        super().__init__()
        self.fallbacks = []

    def _tts_fallback(self, reason, held_audio=None):
        self.fallbacks.append((reason, held_audio))


def test_a_fish_failure_after_gemini_finished_still_gets_the_held_audio(monkeypatch):
    async def _fail(text):
        return
        yield  # pragma: no cover - an empty stream is Fish's failure signal

    monkeypatch.setattr(_FISH, "stream", _fail)

    async def _run():
        era = _FallbackEra()
        s = SpeechOut(era)
        s.feed("A sentence Fish cannot voice. ")
        # Gemini's turn_complete arrives BEFORE the worker reaches the sentence.
        s.end_turn(held_audio=[b"g1", b"g2"])
        assert await _drain(s, era)
        await s.stop()
        assert era.fallbacks and era.fallbacks[0][1] == [b"g1", b"g2"]

    asyncio.run(_run())


def test_held_audio_is_released_once_the_turn_is_voiced(stub_synth):
    async def _run():
        era = _FallbackEra()
        s = SpeechOut(era)
        s.feed("Fish voices this one fine. ")
        s.end_turn(held_audio=[b"g1"])
        assert await _drain(s, era)
        await s.stop()
        assert era.fallbacks == []
        assert s._held_audio == {}

    asyncio.run(_run())


def test_interrupt_discards_held_audio():
    s = SpeechOut(_FallbackEra())
    s._held_audio[0] = [b"g1"]
    s.interrupt()
    assert s._held_audio == {}


# ── First-audio fast path (measured 2026-10-09: median wait 1,455 -> 586 ms) ──
from app.era_live.speech_out import release_first  # noqa: E402


def test_a_short_exclamation_is_its_own_sentence():
    out, rest = split_sentences("Hello! One interesting fact is")
    assert out == ["Hello!"]
    assert rest == "One interesting fact is"


def test_first_sentence_released_without_waiting_for_the_next_chunk():
    assert release_first("I'm unable to access your email account.") == (
        "I'm unable to access your email account.",
        "",
    )


@pytest.mark.parametrize("tail", ["We met Dr.", "It costs 3.", "Use one, e.g."])
def test_ambiguous_full_stops_are_not_released_early(tail):
    assert release_first(tail) == ("", tail)


def test_first_clause_is_released_after_four_words():
    head, rest = release_first("A kilobyte is a unit of digital information, typically")
    assert head == "A kilobyte is a unit of digital information,"
    assert rest == "typically"


def test_a_short_leading_clause_waits():
    assert release_first("Sure, I")[0] == ""


def test_a_number_with_a_comma_is_not_a_clause_break():
    assert release_first("roughly 1,000 bytes in")[0] == ""


def test_a_run_on_opener_is_cut_at_a_complete_word():
    text = " ".join(f"w{i}" for i in range(20)) + " partia"
    head, rest = release_first(text)
    assert head.split() == [f"w{i}" for i in range(12)]
    assert rest.endswith("partia")


def test_only_the_first_chunk_of_a_turn_is_released_early(stub_synth):
    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.feed("A kilobyte is a unit of digital information,")
        s.feed(" roughly a thousand bytes, give or take")
        s.end_turn()
        assert await _drain(s, era)
        # Next turn gets the fast path again.
        s.feed("While I'm unable to open your email,")
        await asyncio.sleep(0.05)
        await s.stop()
        assert stub_synth[0] == "A kilobyte is a unit of digital information,"
        assert stub_synth[1] == "roughly a thousand bytes, give or take"
        assert stub_synth[2] == "While I'm unable to open your email,"

    asyncio.run(_run())
