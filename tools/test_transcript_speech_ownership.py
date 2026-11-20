"""Model generation, delayed local synthesis, real playback and owned delivery."""

import asyncio
from types import MethodType, SimpleNamespace

import pytest

import main
from app.era_live import audio
from app.era_live.speech_out import SpeechOut
from core.input_envelope import InputEnvelope
from core.model_reply import ModelReplyOwnership, send_model_text
from core.speech_frames import EnginePCM
from core.telegram_reply import TelegramReply
from core.telegram_speech import TelegramSpeech
from era_telegram import ReplyDeliveryResult
from tools import test_audio_output as playback


@pytest.mark.parametrize(
    "backend,outcome",
    [
        ("local", "normal"),
        ("local", "synthesis_failure"),
        ("local", "new_local_input"),
        ("local", "internal_overlap"),
        ("native", "normal"),
        ("native", "progressive"),
        ("native", "playback_failure"),
        ("native", "partial_playback_failure"),
        ("native", "missing_transcript"),
        ("native", "new_local_input"),
        ("native", "internal_overlap"),
    ],
)
def test_generation_completion_preserves_playback_owner_and_never_sends_duplicate(
    monkeypatch, backend, outcome
):
    async def run():
        from core import audio_devices, tts_router, voice_output

        monkeypatch.setattr(audio.VOICE_CFG, "uses_speech_out", lambda: backend == "local")
        monkeypatch.setattr(
            audio.VOICE_CFG, "synthesizes_from_transcript", lambda: backend == "local"
        )
        monkeypatch.setattr("era_telegram.enabled", lambda: True)
        monkeypatch.setattr(audio_devices, "glass_output_index", lambda: None)
        monkeypatch.setattr(voice_output, "get_pin", lambda: None)
        monkeypatch.setattr(voice_output, "resolve_output_index", lambda: None)
        monkeypatch.setattr(voice_output, "pinned_device_connected", lambda: False)
        device = playback.FakeDevice()
        monkeypatch.setattr(audio.sd, "RawOutputStream", lambda **kw: device.opener(kw["device"]))
        generated, release, stop, marker_seen, final_sent = (asyncio.Event() for _ in range(5))
        preview_sent, continue_generation = asyncio.Event(), asyncio.Event()
        if backend == "native":
            from app.era_live.audio_output import ResilientOutputStream

            original_write = ResilientOutputStream.write
            native_writes = 0

            async def delayed_write(output, chunk):
                nonlocal native_writes
                await release.wait()
                native_writes += 1
                if outcome == "playback_failure" or (
                    outcome == "partial_playback_failure" and native_writes == 2
                ):
                    return False
                return await original_write(output, chunk)

            monkeypatch.setattr(ResilientOutputStream, "write", delayed_write)
        calls, legacy, replies = [], [], []
        monkeypatch.setattr(audio, "_tg_notify", legacy.append)
        monkeypatch.setattr(main, "_tg_notify", legacy.append)

        async def sender(method, payload):
            calls.append((method, payload["text"]))
            if method == "sendMessage":
                final_sent.set()
            else:
                preview_sent.set()
            return ReplyDeliveryResult("sent")

        async def synth(text, lang):
            await release.wait()
            if outcome != "synthesis_failure":
                yield EnginePCM(b"\x01\x00" * 2048, "fish")

        monkeypatch.setattr(tts_router, "stream", synth)
        owner = InputEnvelope.create("remote question", "telegram")

        def factory(owner):
            reply = TelegramReply(owner, "42", sender=sender, interval=0)
            replies.append(reply)
            return reply

        bridge = TelegramSpeech(factory)
        bridge.select(owner)
        original_end = bridge.end

        def end(owner):
            original_end(owner)
            marker_seen.set()

        bridge.end = end
        original_event = bridge.event

        def event(phrase, status):
            original_event(phrase, status)
            if status == "failed":
                marker_seen.set()

        bridge.event = event
        era = playback._fake_era([])
        era._input_envelope = owner
        era._telegram_speech = bridge
        era._loop = asyncio.get_running_loop()
        era._model_reply_ownership = ModelReplyOwnership()
        era._model_reply_ownership.register(owner)
        era._prepare_owned_speech = MethodType(main.EraLive._prepare_owned_speech, era)
        era._owned_speech_event = MethodType(audio.AudioIOMixin._owned_speech_event, era)
        era._cm_handoff_active = lambda: False
        era._take_cloud_audio_buffer = lambda: []
        era._clear_cloud_audio_buffer = lambda: None
        era._maybe_restore_primary_tts = lambda: None
        era._note_era_question = lambda _: None
        era._tg_reply_pending = True
        era._resume_offer_enabled = False
        era._log_turn_latency = lambda: None
        era._renewal_deadline = None
        era._is_expected_rotation = lambda _: False
        era._is_network_blip = lambda _: True
        era.ui.write_log = lambda _: None
        era.session_manager = SimpleNamespace(
            handle_go_away=lambda _: False, capture_token=lambda _: False
        )
        era.speech_out = SpeechOut(era)

        def response(text, complete=False):
            return SimpleNamespace(
                data=b"\x01\x00" * 2048 if backend == "native" else None,
                tool_call=None,
                server_content=SimpleNamespace(
                    output_transcription=SimpleNamespace(
                        text="" if outcome == "missing_transcript" else text
                    ),
                    input_transcription=None,
                    turn_complete=complete,
                    generation_complete=complete,
                ),
            )

        async def receive():
            yield response("First complete sentence. ")
            if outcome == "progressive":
                await continue_generation.wait()
            if outcome == "internal_overlap":
                await send_model_text(era, "Internal context note", internal=True)
            yield response("Second complete sentence.", True)
            generated.set()
            await stop.wait()

        async def sdk_send(**kwargs):
            pass

        era.session = SimpleNamespace(receive=receive, send_realtime_input=sdk_send)
        receiver = asyncio.create_task(audio.AudioIOMixin._receive_audio(era))
        player = asyncio.create_task(audio.AudioIOMixin._play_audio(era))
        try:
            if outcome == "progressive":
                # The provider is still mid-turn and PCM is blocked at output.
                await asyncio.sleep(0.05)
                assert calls == []
                release.set()
                await asyncio.wait_for(preview_sent.wait(), 2)
                assert calls == [("sendMessageDraft", "First complete sentence.")]
                assert device.played and not generated.is_set()
                assert not final_sent.is_set()
                continue_generation.set()
            await asyncio.wait_for(generated.wait(), 2)
            assert not era._model_reply_ownership.active
            if outcome != "progressive":
                assert calls == [] and legacy == []  # generation is not playback
            assert len(replies) == 1
            reply = replies[0]
            assert reply.owner is owner
            if outcome == "new_local_input":
                main.EraLive._accept_input(era, InputEnvelope.create("local question", "typed"))
            release.set()
            await asyncio.wait_for(marker_seen.wait(), 2)
            if outcome in ("normal", "progressive"):
                await asyncio.wait_for(final_sent.wait(), 2)
                assert [t for m, t in calls if m == "sendMessage"] == [
                    "First complete sentence. Second complete sentence."
                ]
                assert device.played
                assert legacy == []
            else:
                await asyncio.gather(*bridge._tasks)
                assert not any(m == "sendMessage" for m, _ in calls)
                assert reply.outcome == "cancelled"
                if outcome in ("synthesis_failure", "playback_failure"):
                    assert device.played == [] and legacy == []
                    assert reply.events[-1]["reason"] == "playback_failed"
                if outcome == "internal_overlap":
                    assert reply.events[-1]["reason"] == "model_attribution_uncertain"
                if outcome == "partial_playback_failure":
                    assert device.played
                    assert reply.events[-1]["reason"] == "playback_failed"
                if outcome == "missing_transcript":
                    assert device.played
                    assert reply.events[-1]["reason"] == "model_attribution_uncertain"
        finally:
            continue_generation.set()
            release.set()
            era._go_away_event.set()
            stop.set()
            await asyncio.gather(receiver, player)
            await era.speech_out.stop()

    asyncio.run(run())


def test_native_transcript_suffixes_are_not_repeated_or_rewritten():
    from core.speech_frames import NativeSpeechTurn

    turn = NativeSpeechTurn(InputEnvelope.create("remote", "telegram"))
    assert turn.text_delta("First sentence.") == "First sentence."
    assert turn.text_delta("First sentence.") == ""
    assert turn.text_delta("First sentence. Second sentence.") == "Second sentence."
    assert turn.text_delta("A conflicting replacement.") is None
    assert turn.failed


def test_speech_owner_cannot_be_bound_late_or_reassigned(monkeypatch):
    async def run():
        owner = InputEnvelope.create("remote", "telegram")
        fresh = InputEnvelope.create("different", "telegram")
        era = SimpleNamespace(audio_in_queue=asyncio.Queue(), _turn_done_event=asyncio.Event())
        worker = SpeechOut(era)
        worker.feed("Partial unowned text")
        with pytest.raises(ValueError, match="before its first text"):
            worker.feed(" more", owner=owner)
        worker.interrupt()
        worker.feed("Partial owned text", owner=owner)
        with pytest.raises(ValueError, match="cannot change its owner"):
            worker.feed(" more", owner=fresh)
        await worker.stop()

    asyncio.run(run())


def test_unowned_direct_announcement_between_transcript_chunks_stays_local(monkeypatch):
    async def run():
        from core import tts_router
        from core.speech_frames import SpeechFrame

        async def synth(text, lang):
            yield EnginePCM(b"\x01\x00" * 32, "fish")

        monkeypatch.setattr(tts_router, "stream", synth)
        owner = InputEnvelope.create("remote", "telegram")
        era = SimpleNamespace(audio_in_queue=asyncio.Queue(), _turn_done_event=asyncio.Event())
        worker = SpeechOut(era)
        try:
            worker.feed("Part of the remote", owner=owner)
            worker.say("Private local announcement.")
            worker.feed(" answer finishes.", owner=owner)
            worker.end_turn()
            await asyncio.wait_for(era._turn_done_event.wait(), 2)
            # Wait for the streamed turn's marker, not the announcement's event.
            frames = []
            while True:
                frame = await asyncio.wait_for(era.audio_in_queue.get(), 2)
                if isinstance(frame, SpeechFrame):
                    frames.append(frame)
                elif getattr(frame, "owner", None) == owner:
                    break
            assert [(f.phrase.text, f.phrase.owner) for f in frames] == [
                ("Private local announcement.", None),
                ("Part of the remote answer finishes.", owner),
            ]
        finally:
            await worker.stop()

    asyncio.run(run())


@pytest.mark.parametrize("superseded", [False, True])
def test_production_failure_report_reaches_only_its_current_owner(monkeypatch, superseded):
    async def run():
        from core.speech_frames import SpeechPhrase

        notices, logs = [], []
        monkeypatch.setattr("era_telegram.enabled", lambda: True)
        monkeypatch.setattr("era_telegram._chat_id", lambda: "42")
        monkeypatch.setattr(main, "_tg_notify", notices.append)
        owner = InputEnvelope.create("remote", "telegram")
        era = SimpleNamespace(_input_envelope=owner, ui=SimpleNamespace(write_log=logs.append))
        assert main.EraLive._prepare_owned_speech(era, owner, reuse=True)
        bridge = era._telegram_speech
        bridge.event(SpeechPhrase("failed", "Never played", "fish", owner), "failed")
        if superseded:
            era._input_envelope = InputEnvelope.create("new local", "typed")
        await asyncio.gather(*bridge._tasks)
        assert any("playback_failed" in line for line in logs)
        assert len(notices) == (0 if superseded else 1)
        # No phrase was revealed: no bot HTTP send was attempted.

    asyncio.run(run())
