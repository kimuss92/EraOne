"""Quiet wearable interruptions preserve command onset and respect mic safety."""

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.era_live import audio
from core.vad import BargeInDetector


def test_quiet_input_gain_is_bounded_and_preserves_pcm_shape():
    from core.audio_pcm import amplify_input_pcm

    quiet = np.full(1024, 50, dtype="<i2").tobytes()
    assert np.frombuffer(amplify_input_pcm(quiet), dtype="<i2")[0] == 400
    loud = np.array([32767, -32768], dtype="<i2").tobytes()
    limited = np.frombuffer(amplify_input_pcm(loud), dtype="<i2").astype(np.int32)
    assert np.max(np.abs(limited)) <= int(0.89 * 32767)
    assert len(amplify_input_pcm(quiet)) == len(quiet)
    assert amplify_input_pcm(bytes(2048)) == bytes(2048)


def test_wearable_floor_accepts_quiet_speech_and_restores_pc_floor():
    det = BargeInDetector(amplitude_threshold=0.04, onset_grace_ms=0, min_speech_ms=150)
    det._vad = None
    det.configure_input("Casque (CY03-CB36)")
    det.note_speaking(True)
    pcm = np.full(480 * 5, 600, dtype="<i2").tobytes()
    assert det.feed(pcm)
    det.configure_input("Microphone Array (Realtek)")
    det.note_speaking(True)
    assert not det.feed(pcm)
    assert det.amplitude_threshold == 0.04


def test_wearable_floor_keeps_adaptive_echo_protection():
    det = BargeInDetector(amplitude_threshold=0.04, onset_grace_ms=0, min_speech_ms=150)
    det._vad = None
    det.configure_input("Star Glass")
    det.note_speaking(True)
    det._echo.extend([0.02] * 32)
    assert not det.feed(np.full(480 * 5, 600, dtype="<i2").tobytes())


@pytest.mark.parametrize("late_change", ["none", "mute", "session", "finished"])
@pytest.mark.parametrize("gain_enabled", [False, True])
def test_microphone_preserves_interruption_prefix_once(monkeypatch, late_change, gain_enabled):
    async def run():
        callbacks, stopped = [], []
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
            _barge_in=SimpleNamespace(
                enabled=True,
                feed=lambda data: True,
                configure_input=lambda name: gain_enabled and "CY03" in name,
            ),
        )

        def interrupt():
            stopped.append(True)
            era._is_speaking = False

        era.trigger_barge_in = interrupt

        class Stream:
            active = True

            def __init__(self, **kw):
                callbacks.append(kw["callback"])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(audio.sd, "InputStream", Stream)
        monkeypatch.setattr(audio.sd, "query_devices", lambda *args: {"name": "Casque (CY03-CB36)"})
        monkeypatch.setattr(audio.voice_input, "resolve_input_index", lambda sd: 1)
        monkeypatch.setattr(audio.voice_input, "status", lambda sd: "fake glasses input")
        monkeypatch.setattr(audio, "log_event", lambda *args, **kw: None)
        listener = asyncio.create_task(audio.AudioIOMixin._listen_audio(era))
        try:
            await asyncio.sleep(0)
            raw = np.full((1024, 1), 600, dtype="<i2")
            callbacks[0](raw, 1024, None, None)
            if late_change == "mute":
                era._mic_muted = True
            elif late_change == "session":
                era.out_queue = asyncio.Queue()
            elif late_change == "finished":
                era._is_speaking = False
            await asyncio.sleep(0)
            assert len(stopped) == (1 if late_change == "none" else 0)
            if late_change == "none":
                assert era.out_queue.qsize() == 1
                from core.audio_pcm import amplify_input_pcm

                expected = amplify_input_pcm(raw.tobytes()) if gain_enabled else raw.tobytes()
                assert era.out_queue.get_nowait()["data"] == expected
            else:
                assert era.out_queue.empty()
        finally:
            era._go_away_event.set()
            await listener

    asyncio.run(run())


@pytest.mark.parametrize("gain_enabled", [False, True])
@pytest.mark.parametrize("mic_open", [False, True])
def test_standby_wake_receives_conditioned_glasses_pcm_and_respects_mic_off(
    monkeypatch, gain_enabled, mic_open
):
    async def run():
        callbacks = []
        era = SimpleNamespace(
            _speaking_lock=threading.Lock(),
            _is_speaking=False,
            _standby_active=True,
            _browser_mic_active=mic_open,
            _mic_muted=False,
            ui=SimpleNamespace(muted=True),
            out_queue=asyncio.Queue(),
            _wake_queue=asyncio.Queue(),
            _go_away_event=asyncio.Event(),
            _shutdown_requested=False,
            _listening_state=lambda: SimpleNamespace(echo_hold_active=lambda: False),
            _barge_in=SimpleNamespace(configure_input=lambda name: gain_enabled and "CY03" in name),
        )

        class Stream:
            active = True

            def __init__(self, **kw):
                callbacks.append(kw["callback"])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(audio.sd, "InputStream", Stream)
        monkeypatch.setattr(audio.sd, "query_devices", lambda *a: {"name": "Casque (CY03-CB36)"})
        monkeypatch.setattr(audio.voice_input, "resolve_input_index", lambda sd: 1)
        monkeypatch.setattr(audio.voice_input, "status", lambda sd: "test input")
        monkeypatch.setattr(audio, "log_event", lambda *a, **k: None)
        listener = asyncio.create_task(audio.AudioIOMixin._listen_audio(era))
        try:
            await asyncio.sleep(0)
            raw = np.full((1024, 1), 50, dtype="<i2")
            callbacks[0](raw, 1024, None, None)
            await asyncio.sleep(0)
            assert era.out_queue.empty()  # Standby audio remains local.
            if mic_open:
                from core.audio_pcm import amplify_input_pcm

                expected = amplify_input_pcm(raw.tobytes()) if gain_enabled else raw.tobytes()
                assert era._wake_queue.get_nowait() == expected
            else:
                assert era._wake_queue.empty()
        finally:
            era._go_away_event.set()
            await listener

    asyncio.run(run())
