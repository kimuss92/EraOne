"""Standalone recitation recovers only the failed sentence, never a voiced prefix."""

import asyncio
import base64
import io
import wave
from types import SimpleNamespace

import pytest

from app.era_live.speech_out import SpeechOut
from core import tts_gemini, tts_router
from core.input_envelope import InputEnvelope
from core.speech_frames import EnginePCM, SpeechFrame
from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG

_REAL_POST = tts_gemini._post


@pytest.mark.parametrize("status,oversize", [(200, False), (429, False), (200, True)])
def test_transport_checks_status_size_and_closes_session(monkeypatch, status, oversize):
    from core import tts_gemini as module

    closed, sent = [], []

    class Content:
        async def iter_chunked(self, size):
            yield b'{"status":"completed"}'

    class Response:
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("response")

    class Session:
        def __init__(self, **kwargs):
            self.timeout = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("session")

        def post(self, url, **kwargs):
            sent.append((url, kwargs))
            reply = Response()
            reply.status = status
            return reply

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    if oversize:
        monkeypatch.setattr(module, "_MAX_RESPONSE", 10)
    if status == 200 and not oversize:
        assert asyncio.run(_REAL_POST({}, "fake-key")) == {"status": "completed"}
    else:
        with pytest.raises(RuntimeError if status != 200 else ValueError):
            asyncio.run(_REAL_POST({}, "fake-key"))
    assert closed == ["response", "session"]
    assert sent[0][1]["allow_redirects"] is False
    assert sent[0][1]["headers"] == {"x-goog-api-key": "fake-key"}


def response(pcm=b"\x01\x00" * 32, rate=24000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "audio",
                        "mime_type": "audio/wav",
                        "data": base64.b64encode(output.getvalue()).decode(),
                    }
                ],
            }
        ],
    }


def test_gemini_recites_only_given_sentence_with_existing_voice(monkeypatch):
    requests = []

    async def post(payload, key):
        requests.append((payload, key))
        return response()

    monkeypatch.setattr(tts_gemini, "_post", post)
    monkeypatch.setattr(tts_gemini, "get_api_key", lambda: "fake-key")
    monkeypatch.setattr(tts_gemini, "gemini_voice_name", lambda: "Algenib")
    assert asyncio.run(tts_gemini.synthesize("The unsaid sentence.")) == b"\x01\x00" * 32
    payload, key = requests[0]
    assert key == "fake-key"
    assert payload["input"][0]["content"] == [{"type": "text", "text": "The unsaid sentence."}]
    assert payload["store"] is False and payload["stream"] is False
    assert payload["generation_config"]["speech_config"] == [{"voice": "Algenib"}]


@pytest.mark.parametrize(
    "fault", ["incomplete", "empty", "wrong_rate", "bad_base64", "truncated", "multiple"]
)
def test_unverified_audio_never_reaches_playback(fault):
    value = response()
    block = value["steps"][0]["content"][0]
    if fault == "incomplete":
        value["status"] = "incomplete"
    elif fault == "empty":
        value = response(b"")
    elif fault == "wrong_rate":
        value = response(rate=16000)
    elif fault == "bad_base64":
        block["data"] = "!bad!"
    elif fault == "truncated":
        data = base64.b64decode(block["data"])[:-4]
        block["data"] = base64.b64encode(data).decode()
    else:
        value["steps"][0]["content"].append(dict(block))
    with pytest.raises(ValueError):
        tts_gemini.decode_audio(value)


def test_recovery_failure_is_bounded_and_cools_down(monkeypatch):
    calls = []

    async def post(payload, key):
        calls.append(payload)
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(tts_gemini, "_post", post)
    monkeypatch.setattr(tts_gemini, "get_api_key", lambda: "fake-key")

    async def run():
        assert await tts_gemini.synthesize("Unsaid text.") == b""
        assert await tts_gemini.synthesize("Another unsaid sentence.") == b""
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", [False, True])
def test_sentence_recovery_preserves_owner_and_never_replays_prefix(monkeypatch, interrupt):
    async def run():
        requested, notices = [], []
        entered, release = asyncio.Event(), asyncio.Event()
        owner = InputEnvelope.create("remote question", "telegram")

        async def local(text, lang):
            if text == "The unsaid second sentence.":
                return
            yield EnginePCM(b"\x01\x00" * 32, "fish")

        async def recovery(text):
            requested.append(text)
            entered.set()
            await release.wait()
            return b"\x02\x00" * 32

        monkeypatch.setattr(VOICE_CFG, "mode", MODE_FISH_AUDIO)
        monkeypatch.setattr(tts_router, "stream", local)
        monkeypatch.setattr(tts_gemini, "synthesize", recovery)
        era = SimpleNamespace(
            audio_in_queue=asyncio.Queue(),
            _turn_done_event=asyncio.Event(),
            _tts_partial_failure=notices.append,
        )
        worker = SpeechOut(era)
        try:
            worker.feed("The first sentence succeeds. The unsaid second sentence. ", owner=owner)
            worker.end_turn(held_audio=[b"whole turn must not replay"])
            await asyncio.wait_for(entered.wait(), 2)
            if interrupt:
                worker.interrupt()
            release.set()
            if not interrupt:
                await asyncio.wait_for(era._turn_done_event.wait(), 2)
            else:
                await asyncio.sleep(0.05)
            frames = []
            while not era.audio_in_queue.empty():
                chunk = era.audio_in_queue.get_nowait()
                if isinstance(chunk, SpeechFrame):
                    frames.append(chunk.phrase)
            assert requested == ["The unsaid second sentence."]
            assert [(p.text, p.engine) for p in frames] == (
                [("The first sentence succeeds.", "fish")]
                if interrupt
                else [
                    ("The first sentence succeeds.", "fish"),
                    ("The unsaid second sentence.", "gemini"),
                ]
            )
            assert all(p.owner == owner for p in frames)
            assert notices == []
        finally:
            release.set()
            await worker.stop()

    asyncio.run(run())
