"""Era listens on the microphone she was told to, and never goes deaf trying.

The regression this guards: connecting the Star Glass glasses moved Windows'
default INPUT to their 8 kHz Hands-Free mic. Era's sd.InputStream had no
`device=`, so she followed it; the owner voiceprint -- enrolled on the 48 kHz
Intel array -- stopped matching (0.16-0.47 against a 0.488 threshold), and the
fail-closed speaker gate turned every utterance into silence. The HUD showed
"<no speech detected>" with nothing anywhere saying the microphone had moved.

Every test uses a fake sounddevice and a tmp_path config, so none of them
touches the real config/voice.json or opens an audio device.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import voice_input  # noqa: E402


class FakeSD:
    """Just enough sounddevice for resolve_input_index."""

    def __init__(self, devices):
        self._devices = devices

    def query_devices(self):
        return self._devices


def _dev(name, inputs=2, rate=48000):
    return {"name": name, "max_input_channels": inputs, "default_samplerate": rate}


# The machine that produced the bug, in the order PortAudio reported it.
REAL_LAYOUT = [
    _dev("Microsoft Sound Mapper - Input", 2, 44100),
    _dev("Casque (CY03-CB36)", 1, 44100),  # the glasses: Windows' default
    _dev("Microphone Array (Technologie I", 4, 44100),  # MME truncates at 31
    _dev("Speakers (Realtek(R) Audio)", 0, 48000),  # output only: never a candidate
    _dev("Casque (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-", 1, 8000),
]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    cfg = tmp_path / "voice.json"
    cfg.write_text(json.dumps({"mode": "gemini_audio", "gemini_voice": "Algenib"}), "utf-8")
    monkeypatch.setattr(voice_input, "VOICE_CONFIG_PATH", cfg)
    return cfg


# ── fail open: deafness is the worse failure ─────────────────────────────


def test_no_pin_means_the_system_default():
    """Unset must behave exactly as Era did before this module existed."""
    assert voice_input.resolve_input_index(FakeSD(REAL_LAYOUT)) is None


def test_a_pinned_microphone_that_is_not_connected_falls_back():
    voice_input.set_pin("Blue Yeti")
    assert voice_input.resolve_input_index(FakeSD(REAL_LAYOUT)) is None


def test_a_broken_device_list_does_not_deafen_era():
    class Exploding:
        def query_devices(self):
            raise OSError("PortAudio is having a day")

    voice_input.set_pin("Microphone Array")
    assert voice_input.resolve_input_index(Exploding()) is None


# ── the actual bug ───────────────────────────────────────────────────────


def test_the_pin_beats_windows_default_bluetooth_mic():
    """THE regression test.

    Windows made the glasses (index 1) the default input. Era must open the
    Intel array instead, because that is where the voiceprint was enrolled.
    """
    voice_input.set_pin("Microphone Array")
    assert voice_input.resolve_input_index(FakeSD(REAL_LAYOUT)) == 2


def test_output_only_devices_are_never_chosen():
    voice_input.set_pin("Speakers")
    assert voice_input.resolve_input_index(FakeSD(REAL_LAYOUT)) is None


def test_hfp_endpoints_are_recognisable():
    assert voice_input.is_hfp("Casque (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-") is True
    assert voice_input.is_hfp("Headset Microphone (Bose)") is True
    assert voice_input.is_hfp("Microphone Array (Technologie I") is False


# ── name matching ────────────────────────────────────────────────────────


def test_exact_match_beats_a_mere_substring():
    devices = [_dev("USB Microphone Array Pro"), _dev("microphone array")]
    voice_input.set_pin("Microphone Array")
    assert voice_input.resolve_input_index(FakeSD(devices)) == 1


def test_a_prefix_beats_a_substring():
    devices = [_dev("Rear Microphone Array Jack"), _dev("Microphone Array (Intel)")]
    voice_input.set_pin("microphone array")
    assert voice_input.resolve_input_index(FakeSD(devices)) == 1


def test_matching_ignores_case_and_extra_whitespace():
    voice_input.set_pin("  MICROPHONE   array  ")
    assert voice_input.resolve_input_index(FakeSD(REAL_LAYOUT)) == 2


# ── the config file ──────────────────────────────────────────────────────


def test_setting_the_pin_keeps_every_other_key(isolated_config):
    voice_input.set_pin("Microphone Array")
    data = json.loads(isolated_config.read_text("utf-8"))
    assert data["voice_input"] == "Microphone Array"
    assert data["mode"] == "gemini_audio", "the voice mode must survive"
    assert data["gemini_voice"] == "Algenib"


def test_clearing_the_pin_removes_only_that_key(isolated_config):
    voice_input.set_pin("Microphone Array")
    voice_input.clear_pin()
    data = json.loads(isolated_config.read_text("utf-8"))
    assert "voice_input" not in data
    assert data["mode"] == "gemini_audio"
    assert voice_input.get_pin() == ""


def test_an_empty_pin_is_refused_rather_than_silently_stored():
    with pytest.raises(ValueError):
        voice_input.set_pin("   ")


def test_an_unreadable_config_reads_as_unpinned(isolated_config):
    isolated_config.write_text("{ not json", "utf-8")
    assert voice_input.get_pin() == ""
    assert voice_input.resolve_input_index(FakeSD(REAL_LAYOUT)) is None


# ── the status line ──────────────────────────────────────────────────────


def test_status_says_which_microphone_and_whether_it_is_there():
    sd = FakeSD(REAL_LAYOUT)
    assert "not pinned" in voice_input.status(sd)
    voice_input.set_pin("Microphone Array")
    assert "device 2" in voice_input.status(sd)
    voice_input.set_pin("Blue Yeti")
    assert "not connected" in voice_input.status(sd)
