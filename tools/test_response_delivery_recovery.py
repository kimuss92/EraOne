"""A live socket or generated turn is not evidence of an audible reply."""

import asyncio
from types import SimpleNamespace

import pytest

from app.era_live.claude_voice import ClaudeVoiceMixin
from core.live_health import LiveHealth


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_repeated_transcripts_and_empty_turns_cannot_hide_undelivered_reply():
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_heard()
    for _ in range(6):
        clock.advance(2)
        health.note_heard()
        health.note_reply()  # tool / empty boundary / generated but dropped audio
    clock.advance(1)
    verdict = health.verdict()
    assert verdict is not None and verdict.kind == "undelivered"


def test_only_actual_delivery_releases_the_delivery_deadline():
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_heard()
    health.note_reply()
    health.note_delivered()
    clock.advance(60)
    assert health.verdict() is None
    health.note_heard()
    health.note_reply()
    clock.advance(11)
    assert health.verdict().kind == "undelivered"


def test_typed_reply_stays_pending_through_voice_noise_and_empty_turns():
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_text()
    clock.advance(2)
    health.note_heard()
    health.note_reply()
    assert health.pending_text
    clock.advance(10)
    assert health.verdict().kind == "undelivered"
    health.note_delivered()
    assert not health.pending_text


def test_delivery_timeout_waits_for_the_user_to_finish_speaking():
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_heard()
    for _ in range(150):
        clock.advance(0.1)
        health.note_voice(0.2, 0.1)
        health.note_heard()
        health.note_reply()
        assert health.verdict() is None
    clock.advance(1)
    assert health.verdict().kind == "undelivered"


def test_active_work_gets_a_reply_window_after_it_finishes():
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_heard()
    for _ in range(30):
        clock.advance(1)
        health.note_working()
        assert health.verdict() is None
    clock.advance(11)
    assert health.verdict().kind == "undelivered"


def test_new_connection_clears_stale_delivery_deadline():
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_text()
    health.reset()
    clock.advance(60)
    assert health.verdict() is None and not health.pending_text


@pytest.mark.parametrize("muted", [False, True])
def test_real_watchdog_renews_stalled_delivery_without_resuming_or_replaying(monkeypatch, muted):
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_text()
    health.note_reply()  # upstream output was never delivered
    clock.advance(11)
    actions = []
    era = SimpleNamespace(
        live_health=health,
        _shutdown_requested=False,
        _standby_active=False,
        _mic_muted=muted,
        _tools_in_flight=0,
        _go_away_event=asyncio.Event(),
        session_manager=SimpleNamespace(
            clear_token=lambda: actions.append("clear"),
            save_to_disk=lambda: actions.append("save"),
        ),
        live_turn=SimpleNamespace(note_interrupted=lambda: actions.append("no_replay")),
        _flush_audio_in_queue=lambda: actions.append("flush"),
        ui=SimpleNamespace(write_log=lambda _: actions.append("notice")),
    )

    async def tick(_):
        clock.advance(1)
        # Bound this test even if the watchdog incorrectly forgets the request.
        if clock.now > 115:
            era._shutdown_requested = True

    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", tick)
    monkeypatch.setattr("core.vad.voice_settings", lambda: {"prefer_gemini_reconnect": True})
    monkeypatch.setattr("app.codemode_runtime.owns_reply", lambda: False)
    asyncio.run(ClaudeVoiceMixin._live_health_watch(era))
    assert era._go_away_event.is_set()
    assert era._fresh_session_requested
    assert actions == ["notice", "no_replay", "flush", "clear", "save"]


@pytest.mark.parametrize("pause", ["tools", "codemode", "muted", "standby"])
def test_watchdog_preserves_deliberate_silence_and_active_work(monkeypatch, pause):
    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_heard()
    health.note_reply()
    clock.advance(11)
    era = SimpleNamespace(
        live_health=health,
        _shutdown_requested=False,
        _standby_active=pause == "standby",
        _mic_muted=pause == "muted",
        _tools_in_flight=int(pause == "tools"),
        _go_away_event=asyncio.Event(),
    )

    async def tick(_):
        clock.advance(1)
        if clock.now >= 114:
            era._shutdown_requested = True

    monkeypatch.setattr("app.era_live.claude_voice.asyncio.sleep", tick)
    monkeypatch.setattr("app.codemode_runtime.owns_reply", lambda: pause == "codemode")
    asyncio.run(ClaudeVoiceMixin._live_health_watch(era))
    assert not era._go_away_event.is_set()


@pytest.mark.parametrize("output_available", [False, True])
def test_real_player_clears_deadline_only_after_successful_pcm_write(monkeypatch, output_available):
    from tools import test_audio_output as playback

    clock = Clock()
    health = LiveHealth(clock, reply_timeout_s=10)
    health.note_heard()
    health.note_reply()
    original = playback._fake_era

    def with_health(chunks):
        era = original(chunks)
        era.live_health = health
        return era

    monkeypatch.setattr(playback, "_fake_era", with_health)
    device = playback.FakeDevice(fail_opens=0 if output_available else 10**6)
    playback._run_play_audio(monkeypatch, device, [b"\x01\x00" * 128])
    clock.advance(11)
    verdict = health.verdict()
    if output_available:
        assert device.played and verdict is None
    else:
        assert not device.played and verdict.kind == "undelivered"


def test_server_interruption_reopens_capture_even_without_a_turn_complete():
    from app.era_live.audio import AudioIOMixin
    from core.live_turn_state import LiveTurnState

    async def run():
        era = AudioIOMixin()
        era._go_away_event = asyncio.Event()
        era._renewal_deadline = None
        era._barge_in_requested = False
        era._is_speaking = True
        era.set_speaking = lambda value: setattr(era, "_is_speaking", value)
        era.live_turn = LiveTurnState()
        era._flush_audio_in_queue = lambda: 0
        era.session_manager = SimpleNamespace(
            handle_go_away=lambda _: False, capture_token=lambda _: False
        )

        async def receive():
            yield SimpleNamespace(
                data=None,
                tool_call=None,
                server_content=SimpleNamespace(
                    interrupted=True,
                    turn_complete=False,
                    output_transcription=None,
                    input_transcription=None,
                ),
            )
            era._go_away_event.set()

        era.session = SimpleNamespace(receive=receive)
        await era._receive_audio()
        assert not era._is_speaking
        assert era._barge_in_requested  # stale output is still suppressed

    asyncio.run(run())
