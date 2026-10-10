"""Glasses display-name mapping in core.voice_output.

'Star glass' / 'glasses' must pin the CY03-CB36 endpoint, never the PC speakers.
All hardware, storage and PortAudio lookups are faked.
"""

from types import SimpleNamespace

import pytest

from core import audio_endpoints as ae
from core import voice_output as vo

GLASSES = SimpleNamespace(id="{glass-id}", name="Star glass (CY03-CB36)")
SPEAKERS = SimpleNamespace(id="{pc-id}", name="PC - Speakers (Realtek(R) Audio)")


@pytest.fixture
def world(monkeypatch):
    stored = []
    state = {"eps": [SPEAKERS, GLASSES]}
    monkeypatch.setattr(ae, "enumerate_endpoints", lambda flow=None: list(state["eps"]))
    monkeypatch.setattr(vo, "_store", lambda pin: stored.append(pin) or True)
    monkeypatch.setattr(vo, "sd_index_for_name", lambda name, refresh_on_miss=False: 7)
    monkeypatch.setattr(vo, "_voice_lock_active", lambda: False)
    return SimpleNamespace(stored=stored, state=state)


@pytest.mark.parametrize(
    "alias", ["Star glass", "glasses", "the glasses", "CY03-CB36", "star glasses", "glass"]
)
def test_glasses_alias_resolves_to_the_glasses_endpoint(world, alias):
    assert vo._glass_endpoint(alias) is GLASSES


def test_set_pin_glasses_pins_cy03_not_the_pc_speakers(world):
    res = vo.set_pin("glasses")
    assert res.ok
    assert world.stored[-1]["id"] == GLASSES.id
    assert "CY03-CB36" in world.stored[-1]["name"]
    assert "Speakers" not in world.stored[-1]["name"]


def test_set_pin_bumps_the_reroute_signal(world):
    before = vo.generation()
    vo.set_pin("Star glass")
    assert vo.generation() == before + 1


def test_non_glasses_names_are_left_to_the_generic_resolver(world, monkeypatch):
    assert vo._glass_endpoint("speakers") is None
    assert vo._glass_endpoint("computer") is None
    monkeypatch.setattr(ae, "resolve", lambda device, flow=None: (SPEAKERS, None))
    res = vo.set_pin("speakers")
    assert res.ok
    assert world.stored[-1]["id"] == SPEAKERS.id


def test_disconnected_glasses_defer_to_the_resolver_which_reports_it(world, monkeypatch):
    world.state["eps"] = [SPEAKERS]
    assert vo._glass_endpoint("glasses") is None
    err = ae.AudioResult(False, "I can't find that device, sir.")
    monkeypatch.setattr(ae, "resolve", lambda device, flow=None: (None, err))
    res = vo.set_pin("glasses")
    assert not res.ok
    assert world.stored == []


def test_two_glasses_endpoints_are_ambiguous_so_not_guessed(world):
    other = SimpleNamespace(id="{glass-2}", name="Star glass Hands-Free (CY03-CB36)")
    world.state["eps"] = [SPEAKERS, GLASSES, other]
    assert vo._glass_endpoint("glasses") is None


def test_endpoint_probe_failure_degrades_to_none(monkeypatch):
    def boom(flow=None):
        raise RuntimeError("no com")

    monkeypatch.setattr(ae, "enumerate_endpoints", boom)
    assert vo._glass_endpoint("glasses") is None
