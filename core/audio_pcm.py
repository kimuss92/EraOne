"""PCM/WAV helpers for Era's audio path.

Extracted from the old Voicebox client so the format conversion outlives any one
TTS backend: every synthesis backend hands Era raw **int16 mono PCM at
RECEIVE_SAMPLE_RATE (24 kHz)**, and this is where arbitrary WAV payloads become
that. No network, no backend, no engine state — pure bytes in, bytes out.

Degrade, never raise: a payload that cannot be decoded returns b"" so a bad
response costs the utterance, never the process.
"""

from __future__ import annotations

import io
import struct
import wave

import numpy as np

from app.audio_config import CHUNK_SIZE, RECEIVE_SAMPLE_RATE

# Bytes per frame handed to the player. CHUNK_SIZE samples of int16 mono is
# ~43 ms at 24 kHz — comfortably finer than _play_audio's ~33 Hz amplitude
# throttle, so the sphere still gets a fresh spectrum on every emit.
FRAME_BYTES = CHUNK_SIZE * 2


def _log(msg: str) -> None:
    print(f"[PCM] {msg}")


def _parse_wav(wav_bytes: bytes) -> tuple[np.ndarray, int, int] | None:
    """Return (samples, channels, sample_rate) with samples as float32 in [-1, 1].

    Tries the stdlib `wave` module first, which handles the integer-PCM WAVs
    soundfile writes by default. Falls back to a minimal RIFF walk for
    WAVE_FORMAT_IEEE_FLOAT (format 3), which `wave` rejects outright with
    "unknown format: 3". Handling both means an upstream encoder change cannot
    silently mute Era.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
        if width == 1:
            # 8-bit WAV is unsigned, centred on 128.
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 2:
            arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 4:
            arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            _log(f"unsupported WAV sample width {width}")
            return None
        return arr, channels, rate
    except wave.Error:
        pass  # fall through to the float-WAV path
    except Exception as e:
        _log(f"WAV parse failed: {e}")
        return None

    return _parse_wav_float(wav_bytes)


def _parse_wav_float(wav_bytes: bytes) -> tuple[np.ndarray, int, int] | None:
    """Minimal RIFF walk for IEEE-float WAV, which stdlib `wave` cannot read."""
    try:
        if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
            _log("not a RIFF/WAVE payload")
            return None
        pos = 12
        fmt: tuple[int, int, int, int] | None = None
        end = len(wav_bytes)
        while pos + 8 <= end:
            cid = wav_bytes[pos : pos + 4]
            (csize,) = struct.unpack("<I", wav_bytes[pos + 4 : pos + 8])
            body = pos + 8
            if cid == b"fmt " and csize >= 16:
                audio_fmt, channels, rate, _br, _ba, bits = struct.unpack(
                    "<HHIIHH", wav_bytes[body : body + 16]
                )
                fmt = (audio_fmt, channels, rate, bits)
            elif cid == b"data" and fmt is not None:
                audio_fmt, channels, rate, bits = fmt
                chunk = wav_bytes[body : body + csize]
                if audio_fmt == 3 and bits == 32:
                    arr = np.frombuffer(chunk, dtype="<f4").astype(np.float32)
                elif audio_fmt == 3 and bits == 64:
                    arr = np.frombuffer(chunk, dtype="<f8").astype(np.float32)
                elif audio_fmt == 1 and bits == 16:
                    arr = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
                else:
                    _log(f"unsupported WAV format={audio_fmt} bits={bits}")
                    return None
                return arr, channels, rate
            # Chunks are word-aligned: an odd size carries a pad byte.
            pos = body + csize + (csize & 1)
        _log("no data chunk found in WAV")
        return None
    except Exception as e:
        _log(f"float-WAV parse failed: {e}")
        return None


def wav_to_pcm24k(wav_bytes: bytes) -> bytes:
    """Convert a WAV payload to raw int16 mono PCM at RECEIVE_SAMPLE_RATE.

    Backends are asked for 24 kHz mono, so in the normal case the resample is a
    no-op — but the conversion is done from the header rather than assumed,
    because a rate mismatch would not error, it would just play Era's voice at
    the wrong pitch and speed.

    Returns b"" on any failure (degrade, never raise).
    """
    if not wav_bytes:
        return b""
    parsed = _parse_wav(wav_bytes)
    if parsed is None:
        return b""
    samples, channels, rate = parsed
    if samples.size == 0:
        return b""

    # Downmix to mono by averaging the interleaved frames.
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)

    # Linear-interpolation resample. Adequate for speech and dependency-free —
    # scipy/librosa are deliberately not added, and audioop.ratecv is gone
    # (removed in Python 3.13; this machine runs 3.14).
    if rate != RECEIVE_SAMPLE_RATE and samples.size > 1:
        n_out = int(round(samples.size * RECEIVE_SAMPLE_RATE / float(rate)))
        if n_out <= 0:
            return b""
        samples = np.interp(
            np.linspace(0.0, samples.size - 1, n_out, dtype=np.float64),
            np.arange(samples.size, dtype=np.float64),
            samples.astype(np.float64),
        ).astype(np.float32)

    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def iter_pcm_frames(pcm: bytes, frame_bytes: int = FRAME_BYTES):
    """Yield frame-sized slices of PCM, keeping any short tail intact."""
    if frame_bytes <= 0:
        yield pcm
        return
    # Never split a sample across two frames — an odd offset would swap the
    # bytes of an int16 and turn the tail into noise.
    frame_bytes -= frame_bytes % 2
    for i in range(0, len(pcm), frame_bytes):
        chunk = pcm[i : i + frame_bytes]
        if chunk:
            yield chunk
