"""core.audio_pcm: WAV -> int16 mono 24 kHz PCM, the format every TTS backend
must hand the player.

This layer sits between an arbitrary synthesis backend and Era's speaker, so a
silent regression here costs the utterance rather than raising: the contract is
"degrade, never raise", and these tests pin that b"" - not an exception - is
what a bad payload produces. The conversion is pure bytes in, bytes out, so
nothing here touches sound hardware, a backend or an engine session.

The cases that matter are the ones that would not error, only sound wrong: a
sample-rate mismatch (right bytes, wrong pitch), a stereo payload played as mono
(double speed), and an odd frame boundary splitting an int16 into noise.
"""

import io
import struct
import wave

import numpy as np
import pytest

import core.audio_pcm as pcm
from app.audio_config import CHUNK_SIZE, RECEIVE_SAMPLE_RATE


# ── helpers: build real WAV payloads in memory ───────────────────────────────
def _wav(samples, rate=RECEIVE_SAMPLE_RATE, channels=1, width=2):
    """Encode float samples in [-1, 1] as an integer-PCM WAV via stdlib `wave`."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        clipped = np.clip(samples, -1, 1)
        if width == 1:
            raw = (clipped * 127.0 + 128.0).astype(np.uint8).tobytes()
        elif width == 2:
            raw = (clipped * 32767.0).astype("<i2").tobytes()
        else:
            raw = (clipped * 2147483647.0).astype("<i4").tobytes()
        w.writeframes(raw)
    return buf.getvalue()


def _wav_float(samples, rate=RECEIVE_SAMPLE_RATE, channels=1, bits=32):
    """Hand-build a WAVE_FORMAT_IEEE_FLOAT payload - stdlib `wave` refuses to
    write (and read) these, which is why the module carries a RIFF fallback."""
    dtype = "<f4" if bits == 32 else "<f8"
    data = np.asarray(samples).astype(dtype).tobytes()
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 3, channels, rate, byte_rate, block_align, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def _as_float(pcm_bytes):
    return np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32767.0


# ── degrade-never-raise: bad input returns empty, never an exception ─────────
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not a wav at all",
        b"RIFF",
        b"RIFF\x00\x00\x00\x00WAVE",
        bytes(range(256)),
    ],
    ids=["empty", "garbage", "truncated", "no-chunks", "binary"],
)
def test_undecodable_payload_returns_empty_not_raise(payload):
    assert pcm.wav_to_pcm24k(payload) == b""


def test_wav_with_zero_frames_returns_empty():
    assert pcm.wav_to_pcm24k(_wav(np.zeros(0, dtype=np.float32))) == b""


def test_unsupported_sample_width_returns_empty():
    """A 24-bit WAV is valid but unhandled: it must degrade, not raise."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(RECEIVE_SAMPLE_RATE)
        w.writeframes(b"\x00\x00\x00" * 64)
    assert pcm.wav_to_pcm24k(buf.getvalue()) == b""


# ── the normal path: 16-bit mono at the target rate is a pass-through ────────
def test_16bit_mono_at_target_rate_preserves_sample_count():
    sig = np.sin(np.linspace(0, 8 * np.pi, 480, dtype=np.float32)) * 0.5
    out = pcm.wav_to_pcm24k(_wav(sig))
    assert len(out) == 480 * 2, "int16 mono: two bytes per sample, no resample"
    np.testing.assert_allclose(_as_float(out), sig, atol=1e-3)


def test_output_is_little_endian_int16():
    """Byte order is part of the contract: the player reads '<i2', so a
    big-endian buffer would be full-scale noise rather than a loud tone."""
    out = pcm.wav_to_pcm24k(_wav(np.array([1.0, -1.0], dtype=np.float32)))
    assert len(out) == 4
    little = np.frombuffer(out, dtype="<i2")
    assert little[0] > 32000 and little[1] < -32000, "full-scale +/- as little-endian"
    big = np.frombuffer(out, dtype=">i2")
    assert not (big[0] > 32000), "reading the same bytes big-endian must NOT look valid"


# ── width handling: 8-bit is unsigned and centred on 128 ─────────────────────
def test_8bit_unsigned_is_recentred_not_offset():
    """An 8-bit WAV read as signed decodes to a loud DC offset, not silence."""
    silence = _wav(np.zeros(128, dtype=np.float32), width=1)
    out = _as_float(pcm.wav_to_pcm24k(silence))
    assert np.abs(out).max() < 0.02, "8-bit midpoint must decode as near-silence"


def test_32bit_int_width_supported():
    sig = np.linspace(-0.8, 0.8, 200, dtype=np.float32)
    out = _as_float(pcm.wav_to_pcm24k(_wav(sig, width=4)))
    np.testing.assert_allclose(out, sig, atol=1e-3)


# ── channels: stereo must be downmixed, or it plays at double speed ──────────
def test_stereo_is_downmixed_to_mono():
    frames = 300
    interleaved = np.zeros(frames * 2, dtype=np.float32)
    interleaved[0::2] = 0.6  # left
    interleaved[1::2] = 0.2  # right
    out = pcm.wav_to_pcm24k(_wav(interleaved, channels=2))
    assert len(out) == frames * 2, "one mono sample per stereo FRAME, not per sample"
    np.testing.assert_allclose(_as_float(out), 0.4, atol=1e-3)


def test_odd_stereo_tail_does_not_crash():
    """A payload that is not a whole number of frames is truncated, never a
    reshape error."""
    odd = np.full(301 * 2, 0.5, dtype=np.float32)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RECEIVE_SAMPLE_RATE)
        w.writeframes((odd * 32767).astype("<i2").tobytes())
    assert pcm.wav_to_pcm24k(buf.getvalue()) != b""


# ── resampling: the failure that sounds wrong instead of erroring ────────────
@pytest.mark.parametrize("rate,ratio", [(48000, 0.5), (12000, 2.0), (16000, 1.5), (44100, None)])
def test_resampled_to_target_rate(rate, ratio):
    n = 1000
    sig = np.sin(np.linspace(0, 20 * np.pi, n, dtype=np.float32)) * 0.5
    out = pcm.wav_to_pcm24k(_wav(sig, rate=rate))
    expected = round(n * RECEIVE_SAMPLE_RATE / rate) if ratio is None else int(n * ratio)
    assert len(out) // 2 == expected, f"{rate} Hz must resample to {RECEIVE_SAMPLE_RATE} Hz"


def test_resample_preserves_signal_shape():
    """Downsampling a slow ramp must keep the ramp, not scramble it."""
    sig = np.linspace(-0.9, 0.9, 960, dtype=np.float32)
    out = _as_float(pcm.wav_to_pcm24k(_wav(sig, rate=48000)))
    assert len(out) == 480
    np.testing.assert_allclose(out, np.linspace(-0.9, 0.9, 480), atol=0.02)


def test_single_sample_at_wrong_rate_is_not_resampled():
    """`samples.size > 1` guards the interpolation; one sample must still emit."""
    out = pcm.wav_to_pcm24k(_wav(np.array([0.5], dtype=np.float32), rate=48000))
    assert len(out) == 2


# ── IEEE-float WAV: the format stdlib `wave` rejects outright ────────────────
def test_float32_wav_parsed_via_riff_fallback():
    sig = np.sin(np.linspace(0, 6 * np.pi, 256, dtype=np.float32)) * 0.7
    out = _as_float(pcm.wav_to_pcm24k(_wav_float(sig)))
    assert len(out) == 256
    np.testing.assert_allclose(out, sig, atol=1e-3)


def test_float64_wav_parsed_via_riff_fallback():
    sig = np.linspace(-0.5, 0.5, 128)
    out = _as_float(pcm.wav_to_pcm24k(_wav_float(sig, bits=64)))
    assert len(out) == 128
    np.testing.assert_allclose(out, sig, atol=1e-3)


def test_float_wav_unsupported_bit_depth_returns_empty():
    bogus = _wav_float(np.zeros(16, dtype=np.float32))
    # Rewrite the fmt chunk bit depth to something nobody encodes.
    bogus = bogus.replace(struct.pack("<H", 32), struct.pack("<H", 24), 1)
    assert pcm.wav_to_pcm24k(bogus) == b""


def test_riff_walk_skips_unknown_chunks_and_honours_pad_byte():
    """A LIST chunk of odd size sits before `data` in real encoder output;
    missing the RIFF word-alignment pad would desynchronise the walk."""
    sig = np.full(64, 0.25, dtype=np.float32)
    body = _wav_float(sig)[12:]
    odd = b"LIST" + struct.pack("<I", 5) + b"INFO!" + b"\x00"
    spliced = b"RIFF" + struct.pack("<I", 4 + len(odd) + len(body)) + b"WAVE" + odd + body
    np.testing.assert_allclose(_as_float(pcm.wav_to_pcm24k(spliced)), sig, atol=1e-3)


# ── clipping ─────────────────────────────────────────────────────────────────
def test_out_of_range_samples_are_clipped_not_wrapped():
    """Without the clip, +2.0 overflows int16 and wraps to a negative spike."""
    hot = _wav_float(np.array([2.0, -2.0, 0.0], dtype=np.float32))
    vals = np.frombuffer(pcm.wav_to_pcm24k(hot), dtype="<i2")
    assert vals.tolist() == [32767, -32767, 0]


# ── frame iteration ──────────────────────────────────────────────────────────
def test_frame_bytes_matches_chunk_size_of_int16():
    assert pcm.FRAME_BYTES == CHUNK_SIZE * 2


def test_iter_frames_exact_division():
    data = bytes(800)
    frames = list(pcm.iter_pcm_frames(data, frame_bytes=200))
    assert len(frames) == 4
    assert all(len(f) == 200 for f in frames)
    assert b"".join(frames) == data


def test_iter_frames_keeps_short_tail():
    data = bytes(850)
    frames = list(pcm.iter_pcm_frames(data, frame_bytes=200))
    assert [len(f) for f in frames] == [200, 200, 200, 200, 50]
    assert b"".join(frames) == data


def test_iter_frames_never_splits_an_int16():
    """An odd frame size would put half a sample in each frame and turn the tail
    into noise; the boundary must round down to an even offset."""
    data = bytes(range(100))
    frames = list(pcm.iter_pcm_frames(data, frame_bytes=7))
    assert all(len(f) % 2 == 0 for f in frames[:-1])
    assert b"".join(frames) == data


def test_iter_frames_non_positive_size_yields_whole_payload():
    data = b"abcd"
    assert list(pcm.iter_pcm_frames(data, frame_bytes=0)) == [data]
    assert list(pcm.iter_pcm_frames(data, frame_bytes=-8)) == [data]


def test_iter_frames_on_empty_pcm_yields_nothing():
    assert list(pcm.iter_pcm_frames(b"", frame_bytes=64)) == []


def test_iter_frames_round_trips_a_real_conversion():
    sig = np.sin(np.linspace(0, 40 * np.pi, 5000, dtype=np.float32)) * 0.6
    converted = pcm.wav_to_pcm24k(_wav(sig))
    assert b"".join(pcm.iter_pcm_frames(converted)) == converted
