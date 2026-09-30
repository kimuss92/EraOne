"""Speaker faults must never escape _play_audio.

Every `_play_audio` engine.reconnect_error in the logs was PortAudio's
"There is no driver installed on your system" [MME error 6] — the output
endpoint vanished/switched (glasses, BT, connect_device). It propagated through
the TaskGroup and tore down the whole Live session. These tests drive a fake
device through drops, flapping and total loss with no audio hardware.
"""

import asyncio
import random
import types

import pytest
import sounddevice as sd

import app.era_live.audio_output as ao
from app.era_live.audio import AudioIOMixin
from app.era_live.audio_output import ResilientOutputStream
from core.live_turn_state import LiveTurnState

MME6 = "Unanticipated host error [PaErrorCode -9999]: 'There is no driver installed' [MME error 6]"


class FakeDevice:
    """Scriptable output device: `fail_opens` / `fail_writes` count down."""

    def __init__(self, fail_opens=0, fail_writes=0, flaky=0.0, seed=0):
        self.fail_opens = fail_opens
        self.fail_writes = fail_writes
        self.flaky = flaky
        self.rng = random.Random(seed)
        self.played: list[bytes] = []
        self.opened_on: list[object] = []
        self.missing: set = set()

    def opener(self, device):
        dev = self

        class _Stream:
            def start(self):
                if device in dev.missing:
                    raise sd.PortAudioError(MME6)
                if dev.fail_opens > 0:
                    dev.fail_opens -= 1
                    raise sd.PortAudioError(MME6)
                dev.opened_on.append(device)

            def write(self, chunk):
                if dev.fail_writes > 0:
                    dev.fail_writes -= 1
                    raise sd.PortAudioError(MME6)
                if dev.flaky and dev.rng.random() < dev.flaky:
                    raise sd.PortAudioError(MME6)
                dev.played.append(chunk)

            def stop(self):
                raise sd.PortAudioError("stop on dead device")  # must be swallowed

            def close(self):
                pass

        return _Stream()


async def _nosleep(_s):
    return None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _run(coro):
    return asyncio.run(coro)


def test_healthy_write():
    dev = FakeDevice()
    out = ResilientOutputStream(dev.opener, sleep=_nosleep)
    assert out.open()
    assert _run(out.write(b"a"))
    assert dev.played == [b"a"]


def test_mid_stream_drop_reopens_and_replays_same_chunk():
    dev = FakeDevice(fail_writes=1)
    out = ResilientOutputStream(dev.opener, sleep=_nosleep)
    out.open()
    assert _run(out.write(b"a")) is True  # no raise, chunk not lost
    assert dev.played == [b"a"]
    assert len(dev.opened_on) == 2
    assert not out.degraded


def test_endpoint_settling_is_ridden_out_inline():
    # Windows needs a moment after a BT drop: first two reopens fail too.
    dev = FakeDevice(fail_writes=1)
    out = ResilientOutputStream(dev.opener, sleep=_nosleep)
    out.open()
    dev.fail_opens = 2
    assert _run(out.write(b"a")) is True
    assert dev.played == [b"a"]


def test_missing_glasses_fall_back_to_default():
    dev = FakeDevice()
    dev.missing.add(7)
    out = ResilientOutputStream(dev.opener, preferred_device=7, sleep=_nosleep)
    assert out.open()
    assert dev.opened_on == [None]


def test_total_loss_degrades_then_recovers_with_backoff():
    dev = FakeDevice(fail_opens=10**6)
    clock = Clock()
    out = ResilientOutputStream(dev.opener, sleep=_nosleep, clock=clock)
    assert out.open() is False
    assert out.degraded

    async def drive():
        # While inside the backoff window, writes are dropped without probing.
        opens_before = dev.fail_opens
        for _ in range(50):
            assert await out.write(b"x") is False
        assert dev.fail_opens == opens_before
        # Backoff grows 1s → 2s → 4s → 8s (capped).
        gaps = []
        for _ in range(5):
            prev = out._next_retry_at
            clock.t = out._next_retry_at
            assert await out.write(b"x") is False
            gaps.append(round(out._next_retry_at - prev, 3))
        assert gaps == [2.0, 4.0, 8.0, 8.0, 8.0]
        # Speaker comes back.
        dev.fail_opens = 0
        clock.t = out._next_retry_at
        assert await out.write(b"back") is True

    _run(drive())
    assert not out.degraded
    assert dev.played == [b"back"]


def test_intermittent_flapping_never_raises():
    # 20% of writes fail at random — far worse than any real endpoint churn.
    dev = FakeDevice(flaky=0.2, seed=42)
    clock = Clock()

    async def advancing_sleep(s):
        clock.t += s

    out = ResilientOutputStream(dev.opener, sleep=advancing_sleep, clock=clock)
    out.open()

    async def drive():
        results = []
        for i in range(2000):
            clock.t += 0.043  # Live audio cadence: ~43ms per chunk
            results.append(await out.write(bytes([i % 256])))
        return results

    results = _run(drive())  # the real assertion: nothing raised
    assert len(dev.played) == sum(results)
    # Rare 4-in-a-row failures cost at most a ~1s backoff window each.
    assert sum(results) >= 0.95 * len(results)
    assert results[-1] is True  # and it is playing again at the end


def test_non_device_errors_are_not_swallowed():
    class Boom:
        def start(self):
            pass

        def write(self, _c):
            raise ValueError("bug")

        def stop(self):
            pass

        def close(self):
            pass

    out = ResilientOutputStream(lambda _d: Boom(), sleep=_nosleep)
    out.open()
    with pytest.raises(ValueError):
        _run(out.write(b"a"))


# ── Integration: the real _play_audio loop ────────────────────────────────


def _fake_era(chunks):
    era = types.SimpleNamespace()
    era._go_away_event = asyncio.Event()
    era._barge_in_requested = False
    era._barge_in_fade_ms = 0
    era.audio_in_queue = asyncio.Queue()
    for c in chunks:
        era.audio_in_queue.put_nowait(c)
    era._turn_done_event = asyncio.Event()
    era.speaking = []
    era.set_speaking = era.speaking.append
    era.ui = types.SimpleNamespace(set_amplitude=lambda *a, **k: None)
    # The real turn-state object, not a stub: _play_audio counts the bytes it
    # hands the speaker, and that count is what decide_recovery reads to tell
    # "finish playing what we hold" from "ask the model for the rest".
    era.live_turn = LiveTurnState()
    return era


def _run_play_audio(monkeypatch, dev, chunks, preferred=None):
    import core.audio_devices as ad
    import core.voice_output as vo

    # _play_audio asks core.voice_output where to play, not audio_devices: a
    # user-pinned device now outranks the automatic glasses route. Patch BOTH
    # seams - the old one so the auto path still answers, and the new one so
    # this never reads (or re-routes on) the real user's saved pin.
    monkeypatch.setattr(ad, "glass_output_index", lambda: preferred)
    monkeypatch.setattr(vo, "get_pin", lambda: None)
    monkeypatch.setattr(vo, "resolve_output_index", lambda: preferred)
    monkeypatch.setattr(vo, "pinned_device_connected", lambda: False)
    monkeypatch.setattr(sd, "RawOutputStream", lambda **kw: dev.opener(kw["device"]))

    async def main():
        era = _fake_era(chunks)
        task = asyncio.create_task(AudioIOMixin._play_audio(era))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if era.audio_in_queue.empty():
                break
        await asyncio.sleep(0.3)  # let the last write (and any inline retry) land
        era._go_away_event.set()
        await asyncio.wait_for(task, timeout=2)  # must END cleanly, not raise
        return era

    return asyncio.run(main())


def test_play_audio_survives_device_drop_mid_turn(monkeypatch):
    dev = FakeDevice(fail_writes=2)
    chunks = [bytes([i]) * 64 for i in range(20)]
    _run_play_audio(monkeypatch, dev, chunks)
    assert dev.played == chunks  # every chunk, in order, nothing lost


def test_play_audio_survives_glasses_vanishing_at_session_start(monkeypatch):
    dev = FakeDevice()
    dev.missing.add(5)
    chunks = [b"\x01" * 64, b"\x02" * 64]
    _run_play_audio(monkeypatch, dev, chunks, preferred=5)
    assert dev.opened_on[0] is None
    assert dev.played == chunks


def test_play_audio_with_no_speaker_at_all_keeps_session_alive(monkeypatch):
    dev = FakeDevice(fail_opens=10**6)
    chunks = [b"\x01" * 64] * 10
    era = _run_play_audio(monkeypatch, dev, chunks)
    assert dev.played == []
    assert era.audio_in_queue.empty()  # drained, so turn-done logic still works
