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

    async def _fake(text, lang=""):
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

    async def _slow(text, lang=""):
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

    async def _slow(text, lang=""):
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

    async def _fail(text, lang=""):
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
    async def _fail(text, lang=""):
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


# ── Only speech reaches the voice (2026-10-09: Fish read system notes aloud) ──
from app.era_live.tts_text import open_note_at, sanitize_for_tts, strip_notes  # noqa: E402
from app.text_intents import _say_exactly  # noqa: E402


def test_a_say_exactly_wrapper_becomes_its_payload():
    assert strip_notes(_say_exactly("System offline.")).strip() == "System offline."


def test_the_cm_brand_question_note_becomes_its_payload():
    note = (
        "[System note: Ask the user aloud, exactly this and nothing "
        'more: "Which brand is this for?" Do not guess the brand or '
        "use any tool — just ask and wait for their reply.]"
    )
    assert strip_notes(note).strip() == "Which brand is this for?"


def test_other_model_notes_are_dropped():
    assert sanitize_for_tts("Done. [System note: the user was away.]") == "Done."
    assert sanitize_for_tts(r"[User uploaded a file saved at C:\x\y.pdf]") == ""


@pytest.mark.parametrize(
    "raw, spoken",
    [
        (r"Saved to C:\Users\nesri\Desktop\report.pdf, sir.", "Saved to report.pdf, sir."),
        ("See https://www.example.com/a/b?c=1 for more.", "See example.com for more."),
        ("The file_controller tool is ready.", "The file controller tool is ready."),
        ("**Done**, `sir`.", "Done, sir."),
        ("## Summary", "Summary"),
        ('Result {"ok": true} received.', "Result received."),
        ("SYS: Voice switched to Fish.", "Voice switched to Fish."),
        ("[FISH] request failed.", "request failed."),
        ("C'est fait, monsieur. Très bien.", "C'est fait, monsieur. Très bien."),
        ("تم يا سيدي.", "تم يا سيدي."),
    ],
)
def test_sentences_are_cleaned_for_speech(raw, spoken):
    assert sanitize_for_tts(raw) == spoken


def test_markup_only_text_is_not_spoken():
    assert sanitize_for_tts("**") == "" and sanitize_for_tts("[System note: x]") == ""


def test_an_unclosed_note_is_found():
    assert open_note_at("Done. [System note: the") == 6
    assert open_note_at("Done. [a] fine") == -1


def test_a_streamed_note_split_across_chunks_is_never_voiced(stub_synth):
    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.feed("All set, sir. [System note: Say nothing.")
        s.feed(" The user is away. Do not reply.] Anything else?")
        s.end_turn()
        assert await _drain(s, era)
        await s.stop()
        spoken = " ".join(stub_synth)
        assert "System" not in spoken and "Do not reply" not in spoken
        assert "All set, sir." in spoken and "Anything else?" in spoken

    asyncio.run(_run())


def test_say_unwraps_before_splitting(stub_synth):
    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.say(_say_exactly("System offline. Back soon."))
        assert await _drain(s, era)
        await s.stop()
        assert stub_synth == ["System offline.", "Back soon."]

    asyncio.run(_run())


def test_a_turn_ending_inside_a_note_drops_the_note(stub_synth):
    async def _run():
        era = _FakeEra()
        s = SpeechOut(era)
        s.feed("Right away, sir. [System note: cut off")
        s.end_turn()
        assert await _drain(s, era)
        await s.stop()
        assert stub_synth == ["Right away, sir."]

    asyncio.run(_run())


def _fake_engine_for_speak():
    from types import SimpleNamespace

    said, logs = [], []
    eng = SimpleNamespace(
        session=object(),  # a live session: skip the local-fallback branch
        _loop=None,
        speech_out=SimpleNamespace(say=said.append),
        ui=SimpleNamespace(write_log=logs.append),
        _tg_ack=lambda text: None,
        _note_era_question=lambda text: None,
        local_speak=lambda text: False,
    )
    return eng, said, logs


def test_speak_hands_fish_the_payload_not_the_wrapper(monkeypatch):
    import main
    from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG

    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO, raising=False)
    eng, said, _ = _fake_engine_for_speak()
    main.EraLive.speak(eng, _say_exactly("System offline."))
    assert said == ["System offline."]


def test_speak_error_never_voices_the_raw_error_in_fish_mode(monkeypatch):
    import main
    from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG

    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO, raising=False)
    eng, said, logs = _fake_engine_for_speak()
    eng.speak = lambda text: said.append(text)
    main.EraLive.speak_error(eng, "file_controller", r"[WinError 5] Access denied: C:\x\y.txt")
    assert said == ["Sir, the file controller tool ran into a problem. The details are in the log."]
    assert "WinError 5" in logs[0]  # the detail is kept, in the log
