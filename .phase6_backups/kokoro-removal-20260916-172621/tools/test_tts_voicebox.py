"""Voicebox TTS client: WAV→PCM conversion and framing.

The conversion is the part that fails SILENTLY if it is wrong. A rate mismatch
does not raise — it just plays Era's voice at the wrong pitch and speed; a
channel mismatch turns speech into noise. So these tests assert the numbers,
not merely that a call returned bytes.

No backend is required: everything here is pure conversion, plus a check that a
dead backend degrades rather than raising.
"""

import asyncio
import io
import struct
import wave

import numpy as np

from app.audio_config import RECEIVE_SAMPLE_RATE
from core.tts_voicebox import (
    FRAME_BYTES,
    VoiceboxClient,
    iter_pcm_frames,
    wav_to_pcm24k,
)


def _tone(n_samples: int, freq: float = 220.0, rate: int = RECEIVE_SAMPLE_RATE):
    """A float32 sine in [-1, 1] — a signal whose resampling we can verify."""
    t = np.arange(n_samples, dtype=np.float64) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _int16_wav(samples: np.ndarray, rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _float32_wav(samples: np.ndarray, rate: int, channels: int = 1) -> bytes:
    """Hand-built IEEE-float WAV (format 3) — stdlib `wave` cannot write these."""
    data = samples.astype("<f4").tobytes()
    block_align = 4 * channels
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * block_align, block_align, 32)
    return (
        b"RIFF"
        + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


# ── Rate handling ────────────────────────────────────────────────────────────
def test_native_rate_is_lossless_passthrough():
    """Kokoro is natively 24 kHz, so the common path must not resample at all."""
    samples = _tone(2400)
    pcm = wav_to_pcm24k(_int16_wav(samples, RECEIVE_SAMPLE_RATE))

    assert len(pcm) == 2400 * 2
    back = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    # Quantisation is the only permitted loss.
    assert np.max(np.abs(back - samples)) < 1e-3


def test_downsamples_48k_to_24k():
    """A 48 kHz source must come back at half the sample count, not double speed."""
    pcm = wav_to_pcm24k(_int16_wav(_tone(4800, rate=48000), 48000))
    assert abs(len(pcm) // 2 - 2400) <= 1


def test_upsamples_16k_to_24k():
    pcm = wav_to_pcm24k(_int16_wav(_tone(1600, rate=16000), 16000))
    assert abs(len(pcm) // 2 - 2400) <= 1


def test_resample_preserves_duration_and_pitch():
    """The resampled tone must still be the same tone, not a chipmunk.

    Compares the dominant FFT bin: a rate bug shifts the peak frequency, which
    a length-only assertion would happily pass.
    """
    freq = 300.0
    pcm = wav_to_pcm24k(_int16_wav(_tone(9600, freq=freq, rate=48000), 48000))
    got = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(got * np.hanning(got.size)))
    peak_hz = np.argmax(spec) * RECEIVE_SAMPLE_RATE / got.size
    assert abs(peak_hz - freq) < 10.0


# ── Channel handling ─────────────────────────────────────────────────────────
def test_stereo_is_downmixed_to_mono():
    mono = _tone(1200)
    stereo = np.repeat(mono, 2)  # identical L/R, interleaved
    pcm = wav_to_pcm24k(_int16_wav(stereo, RECEIVE_SAMPLE_RATE, channels=2))

    assert len(pcm) == 1200 * 2
    back = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    assert np.max(np.abs(back - mono)) < 1e-3


# ── Encoding variants ────────────────────────────────────────────────────────
def test_float32_wav_is_parsed():
    """soundfile defaults to PCM_16, but a float-WAV must not silence Era."""
    samples = _tone(1200)
    pcm = wav_to_pcm24k(_float32_wav(samples, RECEIVE_SAMPLE_RATE))

    assert len(pcm) == 1200 * 2
    back = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    assert np.max(np.abs(back - samples)) < 1e-3


def test_eight_bit_wav_is_centred():
    """8-bit WAV is UNSIGNED; treating it as signed yields a DC-offset buzz."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(RECEIVE_SAMPLE_RATE)
        w.writeframes(bytes([128] * 512))  # 128 == silence for unsigned 8-bit
    pcm = wav_to_pcm24k(buf.getvalue())
    assert np.max(np.abs(np.frombuffer(pcm, dtype="<i2"))) <= 1


# ── Degradation ──────────────────────────────────────────────────────────────
def test_garbage_returns_empty_not_raises():
    assert wav_to_pcm24k(b"") == b""
    assert wav_to_pcm24k(b"not a wav at all") == b""
    assert wav_to_pcm24k(b"RIFF____WAVEjunk") == b""


def test_dead_backend_degrades(monkeypatch):
    """A dead backend costs the voice, never the process."""
    import core.tts_voicebox as tv

    # Port 1 has nothing listening, and no running server may be discovered.
    monkeypatch.setattr(
        tv, "get_voicebox_config", lambda: {"host": "127.0.0.1", "port": 1}, raising=False
    )
    monkeypatch.setattr(tv, "discover_running_ports", lambda: [])

    client = VoiceboxClient()

    async def _run():
        try:
            assert await client.health() is False
            assert await client.synthesize("hello") == b""
            assert await client.list_profiles() == []
        finally:
            await client.close()

    asyncio.run(_run())


# ── Port discovery ───────────────────────────────────────────────────────────
# The desktop app picks a RANDOM high port per launch and passes it as --port
# (observed 17493), so a hard-coded 8000 only ever works for a backend Era
# spawned herself.
def test_discovers_port_from_process_cmdline(monkeypatch):
    import core.tts_voicebox as tv

    class _Proc:
        def __init__(self, name, cmdline):
            self.info = {"name": name, "cmdline": cmdline}

    fake = [
        _Proc("voicebox-server.exe", ["voicebox-server.exe", "--port", "17493"]),
        _Proc("chrome.exe", ["chrome.exe", "--port", "9222"]),
        _Proc("voicebox-backend", ["voicebox-backend", "--port=8000"]),
    ]
    monkeypatch.setattr(
        tv, "psutil", type("P", (), {"process_iter": staticmethod(lambda *a: fake)}), raising=False
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", tv.psutil)

    ports = tv.discover_running_ports()
    assert 17493 in ports
    assert 8000 in ports
    assert 9222 not in ports, "picked up a non-Voicebox process"


def test_port_discovery_survives_a_bad_process(monkeypatch):
    """A process that vanishes mid-scan must not break discovery."""
    import core.tts_voicebox as tv

    class _Exploding:
        @property
        def info(self):
            raise RuntimeError("process vanished")

    monkeypatch.setattr(
        tv,
        "psutil",
        type("P", (), {"process_iter": staticmethod(lambda *a: [_Exploding()])}),
        raising=False,
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", tv.psutil)
    assert tv.discover_running_ports() == []


def test_forget_port_clears_cached_profile():
    """A different server has different profiles, so the id must not persist."""
    c = VoiceboxClient()
    c._port = 17493
    c._profile_id = "abc"
    c.forget_port()
    assert c._port is None and c._profile_id is None


# ── Framing ──────────────────────────────────────────────────────────────────
def test_frames_are_sample_aligned():
    """An odd frame boundary would swap an int16's bytes and emit noise."""
    pcm = b"\x01\x02" * 5000
    frames = list(iter_pcm_frames(pcm))

    assert b"".join(frames) == pcm
    assert all(len(f) % 2 == 0 for f in frames)
    assert all(len(f) <= FRAME_BYTES for f in frames)


def test_odd_frame_size_is_rounded_down():
    frames = list(iter_pcm_frames(b"\x00" * 100, frame_bytes=7))
    assert all(len(f) % 2 == 0 for f in frames)
    assert b"".join(frames) == b"\x00" * 100


def test_short_tail_is_kept():
    pcm = b"\x00" * (FRAME_BYTES + 2)
    frames = list(iter_pcm_frames(pcm))
    assert len(frames) == 2
    assert len(frames[-1]) == 2
