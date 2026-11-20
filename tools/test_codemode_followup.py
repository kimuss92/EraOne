"""CodeMode's external speech and local-source continuation regression cases."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.era_live.speech_out import SpeechOut
from core import codemode as cm
from core import codemode_context as context
from core import tts_gemini, tts_router
from core.input_envelope import InputEnvelope
from core.speech_frames import SpeechFrame
from core.voice_mode import MODE_GEMINI_AUDIO, VOICE_CFG
from crew import source
from tools.test_codemode import Advisory, empty_context, repository  # noqa: F401 — fixture


@pytest.fixture(autouse=True)
def codemode_switched_on(monkeypatch):
    """PF-050: CodeMode is off by default; these cases test how it behaves when ON."""
    monkeypatch.setenv("ERA_CODEMODE", "on")


@pytest.mark.asyncio
async def test_external_reply_produces_speaker_frames_when_gemini_selected(monkeypatch):
    from app.codemode_runtime import emit

    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO)
    original_preference = VOICE_CFG.preferred
    synth = AsyncMock(return_value=b"\x01\x00" * 1200)
    monkeypatch.setattr(tts_gemini, "synthesize", synth)
    logs, fallback = [], []
    era = SimpleNamespace(
        audio_in_queue=asyncio.Queue(),
        _turn_done_event=asyncio.Event(),
        ui=SimpleNamespace(write_log=logs.append),
        _tts_fallback=lambda *a, **kw: fallback.append(True),
    )
    speaker = SpeechOut(era)
    owner = InputEnvelope.create("Explain this", "typed")
    era.speech_out, era._input_envelope = speaker, owner
    await emit(era, "text", "CodeMode can speak through Gemini now.", owner)
    await emit(era, "end", "CodeMode can speak through Gemini now.", owner)
    await asyncio.wait_for(era._turn_done_event.wait(), 2)
    frames = []
    while not era.audio_in_queue.empty():
        frame = era.audio_in_queue.get_nowait()
        if isinstance(frame, SpeechFrame):
            frames.append(frame)
    await speaker.stop()
    assert frames and sum(len(frame) for frame in frames) == 2400
    assert all(frame.phrase.engine == "gemini" and frame.phrase.owner == owner for frame in frames)
    synth.assert_awaited_once()
    assert not fallback and not any("WARN:" in line for line in logs)
    assert not speaker._external_turns
    assert VOICE_CFG.preferred == original_preference and VOICE_CFG.mode == MODE_GEMINI_AUDIO


@pytest.mark.asyncio
async def test_failed_external_speech_is_visible_without_replaying_live_audio(monkeypatch):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO)
    monkeypatch.setattr(tts_gemini, "synthesize", AsyncMock(return_value=b""))
    logs, replay = [], []
    era = SimpleNamespace(
        audio_in_queue=asyncio.Queue(),
        _turn_done_event=asyncio.Event(),
        ui=SimpleNamespace(write_log=logs.append),
        _tts_fallback=lambda *a, **kw: replay.append(True),
    )
    speaker = SpeechOut(era)
    speaker.say("An answer that cannot be synthesized.", external=True)
    await asyncio.wait_for(era._turn_done_event.wait(), 2)
    await speaker.stop()
    assert not replay and not era.audio_in_queue.qsize()
    assert len(logs) == 1 and "speech unavailable" in logs[0]


@pytest.mark.asyncio
async def test_normal_live_gemini_route_still_uses_native_audio(monkeypatch):
    monkeypatch.setattr(VOICE_CFG, "mode", MODE_GEMINI_AUDIO)
    synth = AsyncMock()
    monkeypatch.setattr(tts_gemini, "synthesize", synth)
    assert [frame async for frame in tts_router.stream("Ordinary Live response")] == []
    synth.assert_not_awaited()


def test_complete_source_larger_than_old_excerpt_is_not_cut_off(repository, monkeypatch):
    monkeypatch.setattr(source, "ROOT", repository)
    path = repository / "core/voice_mode.py"
    path.write_text("".join(f"value_{i} = {i}\n" for i in range(406)), encoding="utf-8")
    data = context.read_file("core/voice_mode.py")
    assert not data["truncated"] and data["total_lines"] == 406
    assert "406: value_405 = 405" in data["content"]
    expanded = context.expand(empty_context(""), [{"path": "core/voice_mode.py"}])
    assert expanded["bytes"] <= 100000 and len(expanded["files"]) == 1


@pytest.mark.parametrize("path", ["config/api_keys.json", "../outside.py", "runtime/private.py"])
def test_automatic_read_requests_keep_source_privacy_limits(repository, monkeypatch, path):
    monkeypatch.setattr(source, "ROOT", repository)
    with pytest.raises(ValueError):
        context.expand(empty_context(""), [{"path": path}])


@pytest.mark.asyncio
async def test_missing_local_source_is_retrieved_without_user_upload(repository, monkeypatch):
    monkeypatch.setattr(source, "ROOT", repository)

    class NeedsSource(Advisory):
        async def generate(self, prompt, emit):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                return {
                    "reply": "",
                    "candidate": None,
                    "read_requests": [{"path": "core/example.py"}],
                }
            assert "VALUE = 1" in prompt
            await emit("I have retrieved the local source.")
            return {"reply": "I have retrieved the local source.", "candidate": None}

    manager = cm.CodeMode(cli_factory=NeedsSource, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()
    events = []

    async def emit(kind, text, owner):
        events.append((kind, text))

    manager.start(InputEnvelope.create("Investigate the source", "typed"), emit)
    await asyncio.wait_for(manager._task, 2)
    assert manager.phase == "ready" and len(manager._cli.calls) == 2
    assert manager.context["files"][0]["path"] == "core/example.py"
    assert [value for kind, value in events if kind == "end"] == [
        "I have retrieved the local source."
    ]


@pytest.mark.asyncio
async def test_automatic_source_continuation_has_a_fixed_round_limit(repository, monkeypatch):
    monkeypatch.setattr(source, "ROOT", repository)

    class Loops(Advisory):
        async def generate(self, prompt, emit):
            self.calls.append(prompt)
            return {"reply": "", "candidate": None, "read_requests": [{"path": "core/example.py"}]}

    manager = cm.CodeMode(cli_factory=Loops, context_builder=empty_context)
    monkeypatch.setattr(cm, "_instance", manager)
    manager.enter()

    async def emit(*args):
        pass

    manager.start(InputEnvelope.create("Investigate", "typed"), emit)
    await asyncio.wait_for(manager._task, 2)
    assert manager.phase == "failed" and "retrieval budget" in manager.error
    assert len(manager._cli.calls) == 3
