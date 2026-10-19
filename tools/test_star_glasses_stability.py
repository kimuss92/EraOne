"""Star Glasses routing stability tests with no audio hardware or network access."""

from __future__ import annotations

import sys
import types

import core.audio_devices as audio_devices
from features import star_glasses_stability


def _device(name="Star Glass CY03CB36"):
    return {"name": name, "max_output_channels": 2, "max_input_channels": 1}


def _install_fake_sounddevice(monkeypatch, enumerations):
    pending = iter(enumerations)
    fake = types.SimpleNamespace(query_devices=lambda: next(pending))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)


def _clock(monkeypatch, initial=0.0):
    value = {"now": initial}
    monkeypatch.setattr(audio_devices.time, "time", lambda: value["now"])
    return value


def setup_function():
    audio_devices._reset_probe_state()


def teardown_function():
    audio_devices._reset_probe_state()


def test_one_enumeration_miss_does_not_disconnect(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [[_device()], []])
    clock = _clock(monkeypatch)

    connected = audio_devices.probe_glass(force=True)
    clock["now"] = 1.0
    transient = audio_devices.probe_glass(force=True)

    assert connected["connected"] is True
    assert transient == connected
    assert transient["output_index"] == 0


def test_sustained_absence_is_eventually_published(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [[_device()], [], [], []])
    clock = _clock(monkeypatch)

    assert audio_devices.probe_glass(force=True)["connected"] is True
    clock["now"] = 1.0
    assert audio_devices.probe_glass(force=True)["connected"] is True
    clock["now"] = 5.0
    assert audio_devices.probe_glass(force=True)["connected"] is True
    clock["now"] = 9.0
    state = audio_devices.probe_glass(force=True)

    assert state == {
        "connected": False,
        "output_index": None,
        "input_index": None,
        "name": "",
    }


def test_reconnect_is_accepted_immediately(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [[_device()], [], [], [_device("Star Glass restored")]])
    clock = _clock(monkeypatch)

    audio_devices.probe_glass(force=True)
    clock["now"] = 1.0
    audio_devices.probe_glass(force=True)
    clock["now"] = 9.0
    assert audio_devices.probe_glass(force=True)["connected"] is False
    clock["now"] = 9.1
    restored = audio_devices.probe_glass(force=True)

    assert restored["connected"] is True
    assert restored["name"] == "Star Glass restored"


def test_invalidate_preserves_hysteresis(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [[_device()], []])
    clock = _clock(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "core.voice_output",
        types.SimpleNamespace(reassert_after_media_change=lambda: None),
    )

    assert audio_devices.probe_glass(force=True)["connected"] is True
    audio_devices.invalidate()
    clock["now"] = 1.0

    assert audio_devices.probe_glass()["connected"] is True


def test_diagnostic_feature_detects_rapid_flapping():
    logs = "\n".join(
        [
            "2026-09-26 03:12:00.000 | INFO | audio | Star Glasses CONNECTED",
            "2026-09-26 03:12:10.000 | WARN | audio | Star Glasses disconnected",
            "2026-09-26 03:12:20.000 | INFO | audio | Star Glasses reconnected",
            "2026-09-26 03:12:30.000 | WARN | audio | Star Glasses NOT connected",
        ]
    )

    result = star_glasses_stability.run({"log_text": logs})

    assert star_glasses_stability.FEATURE["id"] == "star_glasses_stability"
    assert result["ok"] is True
    assert result["observations"] == 4
    assert result["transitions"] == 3
    assert result["flapping"] is True
    assert result["span_seconds"] == 30.0


def test_diagnostic_feature_ignores_duplicates_and_unrelated_connections():
    logs = "\n".join(
        [
            "2026-09-26 03:12:00 | INFO | wifi | connected",
            "2026-09-26 03:12:01 | INFO | audio | Star Glasses connected",
            "2026-09-26 03:12:02 | INFO | audio | Star Glasses connected",
            "2026-09-26 03:12:03 | WARN | audio | Star Glasses disconnected",
        ]
    )

    result = star_glasses_stability.run({"log_text": logs})

    assert result["observations"] == 3
    assert result["transitions"] == 1
    assert result["flapping"] is False
