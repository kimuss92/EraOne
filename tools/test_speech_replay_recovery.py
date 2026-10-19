"""Sentence failure must not replay a whole turn or poison another turn."""

import asyncio
from types import SimpleNamespace

import pytest

from app.era_live.speech_out import SpeechOut
from core import tts_router
from core.speech_frames import EnginePCM, SpeechFrame


@pytest.mark.parametrize("raises", [False, True])
def test_late_failure_stops_only_its_turn_without_whole_reply_replay(monkeypatch, raises):
    async def run():
        attempted, fallbacks, notices = [], [], []
        finished = asyncio.Event()

        async def synth(text, lang):
            attempted.append(text)
            if text == "A sentence that fails.":
                if raises:
                    # Even a provider prefix must be discarded on exception.
                    yield EnginePCM(b"\x02\x00" * 32, "fish")
                    raise TimeoutError("provider disconnected")
                return
            yield EnginePCM(b"\x01\x00" * 32, "fish")

        monkeypatch.setattr(tts_router, "stream", synth)
        era = SimpleNamespace(
            audio_in_queue=asyncio.Queue(),
            _turn_done_event=finished,
            _tts_fallback=lambda *a, **kw: fallbacks.append((a, kw)),
            _tts_partial_failure=notices.append,
        )
        worker = SpeechOut(era)
        try:
            worker.feed("An already voiced sentence. ")
            worker.end_turn(held_audio=[b"whole reply containing the prefix"])
            # Queue another independent turn to prove failed-turn isolation.
            worker.say("An independent announcement.")
            # Repeat with a multi-sentence streamed turn and an interleaved say.
            worker.feed("Another already voiced sentence. ")
            worker.say("An interleaved announcement.")
            worker.feed("A sentence that fails. A remaining sentence to skip. ")
            worker.end_turn(held_audio=[b"whole reply containing the prefix"])
            worker.say("The next reply succeeds.")
            for _ in range(200):
                if attempted and attempted[-1] == "The next reply succeeds.":
                    break
                await asyncio.sleep(0.01)
            assert attempted == [
                "An already voiced sentence.",
                "An independent announcement.",
                "Another already voiced sentence.",
                "An interleaved announcement.",
                "A sentence that fails.",
                "The next reply succeeds.",
            ]
            assert fallbacks == []
            assert len(notices) == 1
            frames = []
            while not era.audio_in_queue.empty():
                chunk = era.audio_in_queue.get_nowait()
                if isinstance(chunk, SpeechFrame):
                    frames.append(chunk.phrase.text)
            assert "A remaining sentence to skip." not in frames
            assert "The next reply succeeds." in frames
            if raises:
                assert "TimeoutError" in notices[0]
        finally:
            await worker.stop()

    asyncio.run(run())


def test_late_old_turn_failure_preserves_new_partial_text(monkeypatch):
    async def run():
        blocked, release, reported = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def synth(text, lang):
            if text == "The old sentence fails.":
                blocked.set()
                await release.wait()
                return
            yield EnginePCM(b"\x01\x00" * 32, "fish")

        monkeypatch.setattr(tts_router, "stream", synth)
        era = SimpleNamespace(
            audio_in_queue=asyncio.Queue(),
            _turn_done_event=asyncio.Event(),
            _tts_partial_failure=lambda reason: reported.set(),
        )
        worker = SpeechOut(era)
        try:
            worker.feed("The old prefix succeeds. The old sentence fails. ")
            worker.end_turn(held_audio=[b"old reply"])
            await asyncio.wait_for(blocked.wait(), 2)
            worker.feed("An unfinished future reply")
            release.set()
            await asyncio.wait_for(reported.wait(), 2)
            assert worker.pending_text() == "An unfinished future reply"
        finally:
            release.set()
            await worker.stop()

    asyncio.run(run())


def test_incomplete_voice_report_explains_failure_without_speaking(monkeypatch):
    from app.era_live import audio

    events, logs = [], []
    monkeypatch.setattr(audio, "log_event", lambda *args, **kwargs: events.append((args, kwargs)))
    era = SimpleNamespace(ui=SimpleNamespace(write_log=logs.append))
    audio.AudioIOMixin._tts_partial_failure(era, "provider produced no audio")
    assert events[0][0][1] == "voice.reply.incomplete"
    assert events[0][1]["whole_turn_replay_suppressed"] is True
    assert len(logs) == 1 and "stopped before the reply finished" in logs[0]


def test_first_failure_hands_whole_audio_off_once(monkeypatch):
    async def run():
        fallbacks, attempted = [], []

        async def synth(text, lang):
            attempted.append(text)
            return
            yield  # empty provider output

        monkeypatch.setattr(tts_router, "stream", synth)
        era = SimpleNamespace(
            audio_in_queue=asyncio.Queue(),
            _turn_done_event=asyncio.Event(),
            _tts_fallback=lambda *a, **kw: fallbacks.append((a, kw)),
        )
        worker = SpeechOut(era)
        try:
            worker.feed("The first sentence fails. The next sentence must not trigger again. ")
            worker.end_turn(held_audio=[b"whole Gemini reply"])
            await asyncio.wait_for(era._turn_done_event.wait(), 2)
            assert len(fallbacks) == 1
            assert fallbacks[0][1]["held_audio"] == [b"whole Gemini reply"]
            assert attempted == ["The first sentence fails."]
        finally:
            await worker.stop()

    asyncio.run(run())
