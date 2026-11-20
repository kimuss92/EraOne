"""Microphone handles survive renewal without retaining old-session input."""

import asyncio
from types import SimpleNamespace

import pytest

from app.era_live.audio_capture import CaptureCache


class Timer:
    def __init__(self, seconds, callback, args):
        self.seconds, self.callback, self.args = seconds, callback, args
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback(*self.args)


@pytest.fixture
def world():
    streams, timers = [], []

    class Stream:
        active = True

        def __init__(self, callback, **settings):
            self.callback, self.settings = callback, settings
            self.closed = False
            streams.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    def schedule(*args, **kwargs):
        timer = Timer(*args, **kwargs)
        timers.append(timer)
        return timer

    cache = CaptureCache(Stream, timer_factory=schedule)
    return cache, streams, timers, SimpleNamespace(_shutdown_requested=False)


def test_reconnect_reuses_handle_and_discards_input_between_sessions(world):
    cache, streams, timers, owner = world
    old, new = [], []
    with cache.lease(old.append, owner=owner, device=1):
        streams[0].callback(b"first session")
    streams[0].callback(b"during disconnection")
    with cache.lease(new.append, owner=owner, device=1):
        assert cache.reused
        streams[0].callback(b"new session")
        timers[0].fire()  # obsolete timer racing with acquisition
        assert not streams[0].closed
    assert old == [b"first session"] and new == [b"new session"]
    assert len(streams) == 1
    timers[-1].fire()
    assert streams[0].closed


@pytest.mark.parametrize("change", ["device", "format", "inactive"])
def test_changed_or_dead_input_is_closed_instead_of_reused(world, change):
    cache, streams, timers, owner = world
    settings = {"device": 1, "samplerate": 16000}
    with cache.lease(lambda *a: None, owner=owner, **settings):
        pass
    if change == "device":
        settings["device"] = 2
    elif change == "format":
        settings["samplerate"] = 24000
    else:
        streams[0].active = False
    with cache.lease(lambda *a: None, owner=owner, **settings):
        assert not cache.reused
        assert streams[0].closed and not streams[1].closed
    cache.close_idle()


def test_device_fault_closes_immediately_but_cancelled_session_can_reuse(world):
    cache, streams, timers, owner = world
    with pytest.raises(OSError):
        with cache.lease(lambda *a: None, owner=owner, device=1):
            raise OSError("device disconnected")
    assert streams[0].closed and not timers
    with pytest.raises(asyncio.CancelledError):
        with cache.lease(lambda *a: None, owner=owner, device=1):
            raise asyncio.CancelledError
    assert not streams[1].closed
    cache.close_idle()


def test_shutdown_does_not_retain_capture_or_timer(world):
    cache, streams, timers, owner = world
    with cache.lease(lambda *a: None, owner=owner, device=1):
        owner._shutdown_requested = True
    assert streams[0].closed and not timers


def test_refresh_cannot_close_an_active_capture_lease(world):
    cache, streams, timers, owner = world
    with cache.lease(lambda *a: None, owner=owner, device=1):
        assert not cache.close_idle()
        assert not streams[0].closed
        with pytest.raises(RuntimeError, match="owner"):
            with cache.lease(lambda *a: None, owner=owner, device=2):
                pass
    assert cache.close_idle()
    assert streams[0].closed and timers[-1].cancelled


def test_old_capture_expiry_cannot_erase_new_input_observation(monkeypatch):
    from core import voice_input

    monkeypatch.setattr(voice_input, "get_pin", lambda: "glasses")
    monkeypatch.setattr(voice_input, "_active", None)
    monkeypatch.setattr(voice_input, "_active_owner", None)
    old, new = object(), object()
    voice_input.note_active_input(1, "glasses", owner=old)
    voice_input.note_active_input(2, "new microphone", owner=new)
    voice_input.clear_active_input(owner=old)
    assert voice_input.active_input()["index"] == 2
    voice_input.clear_active_input(owner=new)
    assert voice_input.active_input() is None


def test_follow_default_reopens_to_pick_up_the_new_windows_microphone():
    default = ["first microphone"]
    owner = SimpleNamespace(_shutdown_requested=False)

    class DefaultStream:
        active = True

        def __init__(self, **settings):
            self.physical_device = default[0]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    cache = CaptureCache(DefaultStream, timer_factory=Timer)
    try:
        with cache.lease(lambda *a: None, owner=owner, device=None) as stream:
            assert stream.physical_device == "first microphone"
        default[0] = "new Windows default"
        with cache.lease(lambda *a: None, owner=owner, device=None) as stream:
            assert stream.physical_device == "new Windows default"
    finally:
        cache.close_idle()
