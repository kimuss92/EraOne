"""Direct-speech ownership and real playback wiring, without network or hardware."""

import asyncio
from types import SimpleNamespace

import pytest

import main
from app.era_live.speech_out import SpeechOut
from core.input_envelope import InputEnvelope, current_input
from core.speech_frames import SpeechEnd, SpeechFrame, SpeechPhrase
from core.telegram_reply import TelegramReply
from core.telegram_speech import TelegramSpeech
from era_telegram import ReplyDeliveryResult


def make_bridge(calls):
    async def sender(method, payload):
        calls.append((method, payload["text"]))
        return ReplyDeliveryResult("sent")

    return TelegramSpeech(lambda owner: TelegramReply(owner, "42", sender=sender, interval=0))


def test_handler_context_survives_thread_and_child_task(monkeypatch):
    async def scenario():
        owner = InputEnvelope.create("remote command", "telegram")
        seen = []
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def child():
            seen.append(current_input.get())
            finished.set()

        def handler(self, text):
            assert current_input.get() is owner
            asyncio.run_coroutine_threadsafe(child(), loop)

        monkeypatch.setattr(main.EraLive, "_handle_text_command", handler)
        await asyncio.to_thread(main.EraLive._handle_owned_text, SimpleNamespace(), owner)
        await asyncio.wait_for(finished.wait(), 1)
        assert seen == [owner]
        assert current_input.get() is None

    asyncio.run(scenario())


def test_owner_survives_synthesis_and_end_marker_follows_audio(monkeypatch):
    async def scenario():
        from core import tts_router

        async def stream(text, lang):
            yield b"\x01\x00" * 32

        monkeypatch.setattr(tts_router, "stream", stream)
        owner = InputEnvelope.create("hello", "telegram")
        era = SimpleNamespace(audio_in_queue=asyncio.Queue(), _turn_done_event=asyncio.Event())
        worker = SpeechOut(era)
        worker.say("A complete sentence.", owner=owner)
        await asyncio.wait_for(era._turn_done_event.wait(), 1)
        first = era.audio_in_queue.get_nowait()
        last = era.audio_in_queue.get_nowait()
        assert isinstance(first, SpeechFrame) and first.phrase.owner is owner
        assert isinstance(last, SpeechEnd) and last.owner is owner
        assert bytes(last) == b"" and era.audio_in_queue.empty()
        await worker.stop()

    asyncio.run(scenario())


def test_reply_waits_for_playback_and_end_marker():
    async def scenario():
        calls = []
        owner = InputEnvelope.create("hello", "telegram")
        bridge = make_bridge(calls)
        bridge.select(owner)
        assert bridge.begin(owner)
        assert calls == []
        phrase = SpeechPhrase("p", "Spoken reply.", "fish", owner)
        bridge.event(phrase, "started")
        bridge.event(phrase, "started")  # duplicate events do not repeat text
        await asyncio.sleep(0)
        assert calls == [("sendMessageDraft", "Spoken reply.")]
        bridge.end(owner)
        await asyncio.gather(*bridge._tasks)
        assert calls[-1] == ("sendMessage", "Spoken reply.")
        assert len(calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["typed", "voice", "telegram"])
def test_new_input_invalidates_old_frames_before_async_cancellation(source):
    async def scenario():
        calls = []
        old = InputEnvelope.create("old", "telegram")
        fresh = InputEnvelope.create("new", source)
        bridge = make_bridge(calls)
        bridge.select(old)
        bridge.begin(old)
        bridge.select(fresh)
        bridge.event(SpeechPhrase("old-p", "Private stale response", "fish", old), "started")
        bridge.end(old)
        await asyncio.gather(*bridge._tasks)
        assert calls == []
        assert bridge.begin(old) is False
        if source == "telegram":
            assert bridge.begin(fresh)
            bridge.event(SpeechPhrase("new-p", "Fresh response", "fish", fresh), "started")
            bridge.end(fresh)
            await asyncio.gather(*bridge._tasks)
            assert calls[-1] == ("sendMessage", "Fresh response")

    asyncio.run(scenario())


def test_failed_playback_cancels_final_with_explicit_reason():
    async def scenario():
        calls = []
        owner = InputEnvelope.create("hello", "telegram")
        bridge = make_bridge(calls)
        bridge.select(owner)
        bridge.begin(owner)
        reply = bridge._reply
        bridge.event(SpeechPhrase("p", "Not played", "fish", owner), "failed")
        bridge.end(owner)
        await asyncio.gather(*bridge._tasks)
        assert calls == []
        assert reply.events[-1]["reason"] == "playback_failed"

    asyncio.run(scenario())


def test_owned_local_speak_suppresses_legacy_final_notification(monkeypatch):
    async def scenario():
        monkeypatch.setattr(main.VOICE_CFG, "uses_speech_out", lambda: True)
        monkeypatch.setattr("era_telegram.enabled", lambda: True)
        owner = InputEnvelope.create("hello", "telegram")
        queued, legacy = [], []
        era = SimpleNamespace(
            session=object(),
            _loop=asyncio.get_running_loop(),
            _input_envelope=owner,
            _tg_reply_pending=True,
            _tg_ack=legacy.append,
            _note_era_question=lambda t: None,
            _begin_owned_speech=lambda o, t: queued.append((o, t)),
        )
        token = current_input.set(owner)
        try:
            main.EraLive.speak(era, "Spoken reply.")
        finally:
            current_input.reset(token)
        await asyncio.sleep(0)
        assert queued == [(owner, "Spoken reply.")]
        assert legacy == [] and era._tg_reply_pending is False

    asyncio.run(scenario())


def test_later_direct_utterance_keeps_playing_without_duplicate_reply(monkeypatch):
    async def scenario():
        monkeypatch.setattr("era_telegram.enabled", lambda: True)
        calls, spoken = [], []
        owner = InputEnvelope.create("hello", "telegram")
        bridge = make_bridge(calls)
        bridge.select(owner)
        era = SimpleNamespace(
            _input_envelope=owner,
            _telegram_speech=bridge,
            speech_out=SimpleNamespace(say=lambda text, **kw: spoken.append((text, kw))),
        )
        era._prepare_owned_speech = lambda owner: main.EraLive._prepare_owned_speech(era, owner)
        main.EraLive._begin_owned_speech(era, owner, "First")
        main.EraLive._begin_owned_speech(era, owner, "Second")
        assert spoken == [("First", {"owner": owner}), ("Second", {})]
        bridge.select(None)
        await asyncio.gather(*bridge._tasks)

    asyncio.run(scenario())


def test_voice_input_disarms_legacy_flag_and_reports_owned_interruption(monkeypatch):
    async def scenario():
        calls, notices = [], []
        monkeypatch.setattr(main, "_tg_notify", notices.append)
        old = InputEnvelope.create("remote", "telegram")
        bridge = make_bridge(calls)
        bridge.select(old)
        bridge.begin(old)
        era = SimpleNamespace(_input_envelope=old, _tg_reply_pending=False, _telegram_speech=bridge)
        fresh = InputEnvelope.create("spoken", "voice")
        main.EraLive._accept_input(era, fresh)
        assert era._input_envelope is fresh and era._tg_reply_pending is False
        assert len(notices) == 1 and "interrupted" in notices[0]
        bridge.end(old)
        await asyncio.gather(*bridge._tasks)
        assert calls == []

    asyncio.run(scenario())


def test_flush_invalidates_reply_before_another_frame_can_reveal(monkeypatch):
    async def scenario():
        calls = []
        owner = InputEnvelope.create("remote", "telegram")
        bridge = make_bridge(calls)
        bridge.select(owner)
        bridge.begin(owner)
        era = SimpleNamespace(
            _loop=asyncio.get_running_loop(),
            _telegram_speech=bridge,
            speech_out=SimpleNamespace(interrupt=lambda: 0),
            audio_in_queue=asyncio.Queue(),
        )
        main.EraLive._flush_audio_in_queue(era)
        bridge.event(SpeechPhrase("p", "stale", "fish", owner), "started")
        await asyncio.gather(*bridge._tasks)
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("available", [True, False])
def test_real_player_drives_owned_delivery_from_successful_writes(monkeypatch, available):
    from tools import test_audio_output as playback

    calls = []
    owner = InputEnvelope.create("hello", "telegram")
    phrase = SpeechPhrase("p", "Actual playback reply.", "fish", owner)
    frame = SpeechFrame(b"\x01\x00" * 32, phrase, first=True, last=True)
    original = playback._fake_era

    def fake(chunks):
        era = original(chunks)
        bridge = make_bridge(calls)
        bridge.select(owner)
        bridge.begin(owner)
        era._telegram_speech = bridge
        return era

    monkeypatch.setattr(playback, "_fake_era", fake)
    device = playback.FakeDevice() if available else playback.FakeDevice(fail_opens=999)
    playback._run_play_audio(monkeypatch, device, [frame, SpeechEnd(owner)])
    if available:
        assert ("sendMessage", phrase.text) in calls
    else:
        assert calls == []
