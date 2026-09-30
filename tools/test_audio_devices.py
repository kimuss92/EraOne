"""Tests for dynamic Star-Glass detection + volume routing policy."""

import sys
import types

import pytest

import core.audio_devices as ad


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # Isolate from the real config and clear the detection cache each test.
    monkeypatch.setattr("core.utils.load_config", lambda: {}, raising=False)
    ad.invalidate()
    yield
    ad.invalidate()


def _fake_sd(devices):
    """Install a fake `sounddevice` module whose query_devices returns `devices`."""
    mod = types.ModuleType("sounddevice")
    mod.query_devices = lambda: devices
    return mod


def test_matches_glass_by_name_and_model():
    assert ad.matches_glass("Star Glass (CY03CB36)") is True
    assert ad.matches_glass("CY03CB36 Hands-Free") is True
    assert ad.matches_glass("Realtek HD Audio") is False
    assert ad.matches_glass("") is False


def test_probe_detects_output_and_input(monkeypatch):
    devices = [
        {"name": "Realtek Speakers", "max_output_channels": 2, "max_input_channels": 0},
        {"name": "Star Glass (CY03CB36)", "max_output_channels": 2, "max_input_channels": 0},
        {"name": "Star Glass (CY03CB36)", "max_output_channels": 0, "max_input_channels": 1},
    ]
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(devices))
    st = ad.probe_glass(force=True)
    assert st["connected"] is True
    assert st["output_index"] == 1
    assert st["input_index"] == 2
    assert "Star Glass" in st["name"]


def test_probe_not_connected(monkeypatch):
    devices = [{"name": "Realtek Speakers", "max_output_channels": 2, "max_input_channels": 0}]
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(devices))
    assert ad.glasses_connected(force=True) is False
    assert ad.glass_output_index(force=True) is None


def test_probe_degrades_on_error(monkeypatch):
    broken = types.ModuleType("sounddevice")

    def _boom():
        raise RuntimeError("no audio subsystem")

    broken.query_devices = _boom
    monkeypatch.setitem(sys.modules, "sounddevice", broken)
    st = ad.probe_glass(force=True)
    assert st["connected"] is False  # never raises


def test_configurable_identifiers(monkeypatch):
    monkeypatch.setattr(
        "core.utils.load_config",
        lambda: {"audio": {"glass_identifiers": ["nreal", "xreal"]}},
        raising=False,
    )
    assert ad.matches_glass("XREAL Air 2") is True
    assert ad.matches_glass("Star Glass (CY03CB36)") is False  # default no longer applies


def test_routing_summary_reflects_state(monkeypatch):
    # This test is about the AUTOMATIC route, so it must state that nothing is
    # pinned. routing_summary now reports a user-pinned voice device instead
    # (deliberately - the prompt must not tell the model "your voice follows the
    # glasses" when the user has chosen otherwise), and without this the test
    # read whatever pin the real user_prefs.json happened to hold.
    monkeypatch.setattr("core.voice_output.get_pin", lambda: None)
    connected = [
        {"name": "Star Glass (CY03CB36)", "max_output_channels": 2, "max_input_channels": 0}
    ]
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(connected))
    summary = ad.routing_summary(force=True)
    assert "CONNECTED" in summary
    assert "system" in summary.lower()

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        _fake_sd([{"name": "Realtek", "max_output_channels": 2, "max_input_channels": 0}]),
    )
    summary = ad.routing_summary(force=True)
    assert "NOT connected" in summary


# ── Registry wiring: glass tokens exist and never collide with system volume ────
def test_glass_volume_tokens_registered_without_collision():
    from core.action_registry import ACTION_SPECS

    for token in ("glass_volume_up", "glass_volume_down", "glass_volume_set"):
        assert token in ACTION_SPECS
        assert ACTION_SPECS[token].tool == "computer_settings"

    # Every alias across the registry must be unique — a duplicate would let a
    # glass command silently resolve to the system mixer (the collision class the
    # memories warn about).
    seen: dict[str, str] = {}
    for name, spec in ACTION_SPECS.items():
        for alias in (name, *spec.aliases):
            assert alias not in seen, f"alias {alias!r} on {name} collides with {seen.get(alias)}"
            seen[alias] = name


# ── Glass volume degrades cleanly when the glasses are absent ───────────────────
def test_glass_volume_refuses_when_not_connected(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        _fake_sd([{"name": "Realtek", "max_output_channels": 2, "max_input_channels": 0}]),
    )
    ad.invalidate()
    from actions.computer_settings import glass_volume, is_windows

    msg = glass_volume("up")
    if is_windows():
        assert "aren't connected" in msg.lower()
    else:
        assert "windows-only" in msg.lower()
