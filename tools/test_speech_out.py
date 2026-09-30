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
