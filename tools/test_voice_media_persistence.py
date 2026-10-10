"""The voice stays on the Star Glasses while media routing moves around.

Switching the media default between the glasses and the PC speakers must not
strand Era's voice on the fallback device. These drive the re-assert with a
fake route and a fake resolver: no audio hardware, no registry, no PortAudio.
"""

import pytest

from core import audio_devices
from core import voice_output as vo

GLASSES = 7


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(vo, "_active", None)
    monkeypatch.setattr(vo, "_generation", 0)
    monkeypatch.setattr(vo, "resolve_output_index", lambda: GLASSES)


def _opened(index, fallback=False):
    vo.note_active_route(index, fallback=fallback, name="stream")


def test_no_stream_yet_means_nothing_to_move():
    assert vo.reassert_after_media_change() is False
    assert vo.generation() == 0


def test_voice_already_on_the_glasses_is_left_alone():
    _opened(GLASSES)
    assert vo.reassert_after_media_change() is False
    assert vo.generation() == 0


def test_voice_stranded_on_the_fallback_returns_to_the_glasses():
    _opened(None, fallback=True)
    assert vo.reassert_after_media_change() is True
    assert vo.generation() == 1


def test_voice_on_a_stale_index_is_moved_to_the_current_one():
    _opened(3)
    assert vo.reassert_after_media_change() is True
    assert vo.generation() == 1


def test_glasses_gone_does_not_tear_down_a_working_stream(monkeypatch):
    monkeypatch.setattr(vo, "resolve_output_index", lambda: None)
    _opened(None, fallback=True)
    assert vo.reassert_after_media_change() is False
    assert vo.generation() == 0


def test_a_pin_on_the_speakers_is_respected(monkeypatch):
    # resolve_output_index is the decision: with the voice pinned to the PC
    # speakers it answers their index, and a stream already there is not moved.
    monkeypatch.setattr(vo, "resolve_output_index", lambda: 2)
    _opened(2)
    assert vo.reassert_after_media_change() is False
    assert vo.generation() == 0


def test_repeated_media_switches_do_not_keep_reopening():
    _opened(None, fallback=True)
    assert vo.reassert_after_media_change() is True
    _opened(GLASSES)  # the playback layer reopened where it was told
    assert vo.reassert_after_media_change() is False
    assert vo.generation() == 1


def test_resolver_failure_degrades_instead_of_raising(monkeypatch):
    def boom():
        raise RuntimeError("probe died")

    monkeypatch.setattr(vo, "resolve_output_index", boom)
    _opened(None, fallback=True)
    assert vo.reassert_after_media_change() is False
    assert vo.generation() == 0


def test_invalidate_reasserts_the_voice_route(monkeypatch):
    calls = []
    monkeypatch.setattr(vo, "reassert_after_media_change", lambda: calls.append(1) or False)
    audio_devices.invalidate()
    assert calls == [1]
    assert audio_devices._cache["state"] is None


def test_invalidate_survives_a_failing_reassert(monkeypatch):
    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(vo, "reassert_after_media_change", boom)
    audio_devices.invalidate()  # must not raise
    assert audio_devices._cache["state"] is None


def test_invalidate_end_to_end_bumps_the_signal_for_a_stranded_voice():
    _opened(None, fallback=True)
    audio_devices.invalidate()
    assert vo.generation() == 1
