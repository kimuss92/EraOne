"""Transport completion must precede local phrase playback."""

import asyncio
from types import SimpleNamespace

import pytest

from app.era_live.speech_out import SpeechOut, split_sentences
from core import tts_fish, tts_router
from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG


@pytest.fixture(autouse=True)
def _offline_fallback(monkeypatch):
    from core import tts_kokoro

    monkeypatch.setattr(tts_kokoro.KOKORO, "_ready", False)


def test_long_token_waits_for_a_real_boundary():
    token = "x" * 700
    assert split_sentences(token) == ([], token)
    assert split_sentences(token, final=True) == ([token], "")
    assert split_sentences(token + " next") == ([token], "next")


class _Response:
    status = 200

    def __init__(self, fail):
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def content(self):
        return self

    async def iter_chunked(self, size):
        yield b"\x01\x00" * 30
        if self.fail:
            raise TimeoutError("disconnected after prefix")
        yield b"\x02\x00" * 30


class _Session:
    closed = False

    def __init__(self, fail):
        self.fail = fail

    def post(self, *args, **kwargs):
        return _Response(self.fail)


@pytest.mark.parametrize("fail", [True, False])
def test_buffered_fish_discards_incomplete_response(monkeypatch, fail):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-only")
    client = tts_fish.FishAudioClient()
    client._session = _Session(fail)
    pcm = asyncio.run(client.synthesize("A complete phrase."))
    assert pcm == (b"" if fail else b"\x01\x00" * 30 + b"\x02\x00" * 30)
    assert client.in_cooldown() is fail


def test_router_never_releases_a_failed_fish_prefix(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-only")
    client = tts_fish.FishAudioClient()
    client._session = _Session(True)
    monkeypatch.setattr(tts_router, "FISH", client)
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO)

    async def collect():
        return [chunk async for chunk in tts_router.stream("A complete phrase.")]

    assert asyncio.run(collect()) == []


def test_stopped_worker_cannot_replay_old_session_text(monkeypatch):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO)

    async def run():
        started = asyncio.Event()
        never_finish = asyncio.Event()
        calls = []

        async def stream(text, lang=""):
            calls.append(text)
            if text.startswith("Old"):
                started.set()
                await never_finish.wait()
            yield b"\x01\x00" * 32

        monkeypatch.setattr(tts_router, "stream", stream)
        era = SimpleNamespace(audio_in_queue=asyncio.Queue(), _turn_done_event=asyncio.Event())
        worker = SpeechOut(era)
        worker.say("Old first sentence. Old second sentence.")
        await asyncio.wait_for(started.wait(), 1)
        await worker.stop()
        worker.say("Fresh session sentence.")
        await asyncio.wait_for(era._turn_done_event.wait(), 1)
        await worker.stop()
        assert calls == ["Old first sentence.", "Fresh session sentence."]
        frame = era.audio_in_queue.get_nowait()
        assert frame.phrase.text == "Fresh session sentence."
        assert frame.first and frame.last
        assert era.audio_in_queue.empty()

    asyncio.run(run())


def test_router_holds_prefix_until_completion_and_propagates_cancel(monkeypatch):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO)

    async def run():
        prefix_produced = asyncio.Event()
        finish = asyncio.Event()
        released = []

        async def stream(text, lang=""):
            yield b"\x01\x00"
            prefix_produced.set()
            await finish.wait()
            yield b"\x02\x00"

        monkeypatch.setattr(tts_router.FISH, "stream", stream)

        async def collect():
            async for chunk in tts_router.stream("A complete phrase."):
                released.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.wait_for(prefix_produced.wait(), 1)
        assert released == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released == []

    asyncio.run(run())


def test_failed_fish_phrase_uses_ready_kokoro_without_changing_preference(monkeypatch):
    from core import tts_kokoro

    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-only")
    client = tts_fish.FishAudioClient()
    client._session = _Session(True)
    monkeypatch.setattr(tts_router, "FISH", client)
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO)

    class Ready:
        ready = True

        async def stream(self, text, lang=""):
            assert text == "A complete phrase."
            yield b"\x03\x00" * 30

    monkeypatch.setattr(tts_kokoro, "KOKORO", Ready())

    async def collect():
        return [chunk async for chunk in tts_router.stream("A complete phrase.", "en")]

    assert asyncio.run(collect()) == [b"\x03\x00" * 30]
    assert VOICE_CFG.mode == MODE_FISH_AUDIO


def test_switch_to_gemini_during_synthesis_discards_local_audio(monkeypatch):
    from core.voice_mode import MODE_GEMINI_AUDIO

    monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO)

    async def stream(text, lang=""):
        yield b"\x01\x00"
        VOICE_CFG.mode = MODE_GEMINI_AUDIO
        yield b"\x02\x00"

    monkeypatch.setattr(tts_router.FISH, "stream", stream)

    async def collect():
        return [chunk async for chunk in tts_router.stream("A complete phrase.")]

    assert asyncio.run(collect()) == []
