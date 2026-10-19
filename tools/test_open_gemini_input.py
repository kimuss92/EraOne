"""Unblocked Gemini input and speech-only interruption, without live hardware."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from app.era_live.audio import AudioIOMixin
from core.vad import BargeInDetector, build_barge_in_detector, get_endpointing_config


def pcm(frames=1):
    t = np.arange(480 * frames) / 16000
    return (3000 * np.sin(2 * np.pi * 200 * t)).astype("<i2").tobytes()


def test_media_and_attention_filters_never_consume_live_requests():
    async def scenario():
        sent = []
        requested = [pcm(2), bytes(1920), pcm(2)]
        era = SimpleNamespace(
            _go_away_event=asyncio.Event(),
            out_queue=asyncio.Queue(),
            _mic_muted=False,
            _browser_mic_active=True,
            session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
            _speaker_gate=lambda: SimpleNamespace(feed=lambda frame: [frame]),
            _attention_gate=lambda: pytest.fail("attention must not gate live requests"),
            _media_voice_frames=lambda frame: pytest.fail("media must not gate live requests"),
            _note_sent_voice=lambda frame: None,
        )

        async def send(audio):
            sent.append(audio.data)
            if len(sent) == len(requested):
                era._go_away_event.set()

        era.session = SimpleNamespace(send_realtime_input=send)
        for frame in requested:
            era.out_queue.put_nowait({"data": frame, "mime_type": "audio/pcm;rate=16000"})
        await asyncio.wait_for(AudioIOMixin._send_realtime(era), 1)
        assert sent == requested  # Silence and complete speech preserved in order.

    asyncio.run(scenario())


def detector():
    result = BargeInDetector(
        require_speech_vad=True,
        onset_grace_ms=0,
        min_speech_ms=300,
        amplitude_threshold=0.01,
        echo_factor=0,
    )
    result.note_speaking(True)
    return result


def test_missing_vad_does_not_fall_back_to_noise_interruptions():
    det = detector()
    det._vad = None
    assert not det.feed(pcm(30))


def test_sustained_non_speech_never_interrupts_even_if_loud():
    det = detector()
    det._vad = SimpleNamespace(is_speech=lambda *a: False)
    assert not any(det.feed(pcm(2)) for _ in range(100))


def test_broadband_noise_is_rejected_even_when_vad_claims_speech():
    det = detector()
    det._vad = SimpleNamespace(is_speech=lambda *a: True)
    rng = np.random.default_rng(42)
    for _ in range(60):
        noise = np.clip(rng.normal(0, 0.04, 1024) * 32768, -32768, 32767).astype("<i2").tobytes()
        assert not det.feed(noise)


def test_dc_signal_is_not_speech_even_if_vad_claims_it_is():
    det = detector()
    det._vad = SimpleNamespace(is_speech=lambda *a: True)
    assert not det.feed(np.full(480 * 30, 3000, dtype="<i2").tobytes())


def test_clicks_do_not_accumulate_into_a_speech_interruption():
    det = detector()
    spoken = [True]
    det._vad = SimpleNamespace(is_speech=lambda *a: spoken[0])
    for _ in range(30):
        assert not det.feed(pcm(2))
        spoken[0] = False
        assert not det.feed(pcm(2))
        spoken[0] = True


def test_sustained_quiet_wearable_speech_interrupts_after_confirmation():
    det = detector()
    det.configure_input("Casque (CY03-CB36)")
    det._vad = SimpleNamespace(is_speech=lambda *a: True)
    outcomes = [det.feed(pcm(2)) for _ in range(6)]
    assert outcomes == [False, False, False, False, True, False]


def test_interruption_prefix_contains_onset_and_confirmation(monkeypatch):
    # Capture the real callback, fake the device, then issue twelve input blocks.
    import threading

    from app.era_live import audio

    async def scenario():
        callbacks, count = [], [0]
        era = SimpleNamespace(
            _speaking_lock=threading.Lock(),
            _is_speaking=True,
            _standby_active=False,
            _browser_mic_active=True,
            _mic_muted=False,
            ui=SimpleNamespace(muted=False),
            out_queue=asyncio.Queue(),
            _go_away_event=asyncio.Event(),
            _shutdown_requested=False,
            _listening_state=lambda: SimpleNamespace(echo_hold_active=lambda: False),
        )

        def feed(data):
            count[0] += 1
            return count[0] == 12

        era._barge_in = SimpleNamespace(enabled=True, configure_input=lambda name: False, feed=feed)
        era.trigger_barge_in = lambda: setattr(era, "_is_speaking", False)

        class Stream:
            active = True

            def __init__(self, **kwargs):
                callbacks.append(kwargs["callback"])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(audio.sd, "InputStream", Stream)
        monkeypatch.setattr(audio.sd, "query_devices", lambda *a: {"name": "test microphone"})
        monkeypatch.setattr(audio.voice_input, "resolve_input_index", lambda *a: 1)
        monkeypatch.setattr(audio.voice_input, "status", lambda *a: "test")
        listener = asyncio.create_task(AudioIOMixin._listen_audio(era))
        try:
            await asyncio.sleep(0)
            samples = [np.full((1024, 1), n, dtype="<i2") for n in range(1, 13)]
            for sample in samples:
                callbacks[0](sample, 1024, None, None)
            await asyncio.sleep(0)
            assert [era.out_queue.get_nowait()["data"] for _ in range(12)] == [
                s.tobytes() for s in samples
            ]
        finally:
            era._go_away_event.set()
            await listener

    asyncio.run(scenario())


def test_voice_settings_override_legacy_amplitude_only_config(monkeypatch):
    monkeypatch.setattr("core.utils.load_config", lambda: {"barge_in": {"min_speech_ms": 150}})
    monkeypatch.setattr(
        "core.vad.voice_settings",
        lambda: {
            "barge_in": {"min_speech_ms": 300, "require_speech_vad": True},
            "endpointing": {"silence_ms": 600, "prefix_ms": 80},
        },
    )
    det = build_barge_in_detector()
    assert det.min_speech_ms == 300 and det.require_speech_vad
    assert get_endpointing_config()["silence_ms"] == 600
    assert get_endpointing_config()["prefix_ms"] == 80


def test_default_grace_also_covers_new_slow_live_tools():
    from app.era_live.tool_dispatch import ToolDispatchMixin

    era = SimpleNamespace(
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        ui=SimpleNamespace(muted=False),
        _standby_active=False,
    )
    assert ToolDispatchMixin._background_grace(era, "new_slow_tool", {}) == 1.5
    assert ToolDispatchMixin._background_grace(era, "enter_standby", {}) is None
