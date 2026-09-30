"""audio_control / core.audio_endpoints: state truthfulness, name resolution, routing.

All hardware is faked - these never touch the real mixer.
"""

import pytest

import core.audio_endpoints as ae
from actions import audio_control as ac_mod
from actions.audio_control import audio_control


class FakeEV:
    def __init__(self, vol=50, muted=False, quantum=None):
        self.scalar, self.muted, self.quantum = vol / 100.0, muted, quantum

    def GetMasterVolumeLevelScalar(self):
        return self.scalar

    def SetMasterVolumeLevelScalar(self, v, ctx):
        if self.quantum:  # driver that only accepts coarse steps
            v = round(v / self.quantum) * self.quantum
        self.scalar = v

    def GetMute(self):
        return int(self.muted)

    def SetMute(self, m, ctx):
        self.muted = bool(m)


class FakeDev:
    def __init__(self, ev):
        self.EndpointVolume = ev


def _ep(name, flow, kind, vol=50, muted=False, quantum=None, id_=None):
    return ae.Endpoint(
        id=id_ or f"id-{name}",
        name=name,
        flow=flow,
        kind=kind,
        dev=FakeDev(FakeEV(vol, muted, quantum)),
    )


@pytest.fixture
def world(monkeypatch):
    eps = [
        _ep("Haut-parleurs (Realtek(R) Audio)", ae.OUTPUT, "speakers", vol=44),
        _ep("Star glass (CY03-CB36)", ae.OUTPUT, "headphones", vol=100),
        _ep("Casque (CY03-CB36)", ae.INPUT, "headset", vol=80),
        _ep("Microphone Array (Intel)", ae.INPUT, "microphone", vol=70),
    ]
    defaults = {
        ae.OUTPUT: {"console": eps[1].id, "multimedia": eps[1].id, "communications": eps[0].id},
        ae.INPUT: {"console": eps[2].id, "multimedia": eps[2].id, "communications": eps[2].id},
    }
    monkeypatch.setattr(
        ae, "enumerate_endpoints", lambda flow=None: [e for e in eps if flow in (None, e.flow)]
    )
    monkeypatch.setattr(ae, "default_ids", lambda flow: dict(defaults[flow]))
    monkeypatch.setattr("core.audio_devices._identifiers", lambda: ("star glass", "cy03cb36"))
    return eps, defaults


# ── resolution ───────────────────────────────────────────────────────────────


def test_resolve_kind_word_survives_localised_names(world):
    ep, err = ae.resolve("speakers")
    assert err is None and ep.name.startswith("Haut-parleurs")


def test_resolve_partial_and_exact(world):
    assert ae.resolve("realtek")[0].name.startswith("Haut-parleurs")
    assert ae.resolve("Microphone Array (Intel)")[0].kind == "microphone"


def test_glasses_alias_prefers_their_speakers(world):
    ep, err = ae.resolve("glasses")
    assert err is None and ep.flow == ae.OUTPUT and ep.name.startswith("Star glass")
    ep, err = ae.resolve("glasses", flow=ae.INPUT)
    assert ep.name.startswith("Casque")


def test_ambiguous_asks_instead_of_guessing(world):
    ep, err = ae.resolve("cy03")
    assert ep is None and not err.ok and len(err.data["candidates"]) == 2
    assert "Which one" in err.message


def test_mic_word_infers_input_flow(world):
    ep, err = ae.resolve("mic")  # matches "microphone array" only among inputs
    assert err is None and ep.name.startswith("Microphone Array")


def test_unknown_device_lists_what_exists(world):
    ep, err = ae.resolve("bose")
    assert ep is None and "Haut-parleurs" in err.message and len(err.data["available"]) == 4


# ── state truthfulness ───────────────────────────────────────────────────────


def test_relative_step_uses_fresh_read_not_a_remembered_level(world):
    eps, _ = world
    spk = eps[0]
    assert "54%" in ae.step_device_volume("realtek", +10).message
    spk.dev.EndpointVolume.scalar = 0.20  # user moved the slider behind our back
    assert "30%" in ae.step_device_volume("realtek", +10).message


def test_output_volume_raise_unmutes_but_input_never_does(world):
    eps, _ = world
    eps[0].dev.EndpointVolume.muted = True
    ae.set_device_volume("realtek", 60)
    assert eps[0].dev.EndpointVolume.muted is False
    eps[3].dev.EndpointVolume.muted = True
    res = ae.set_device_volume("Microphone Array", 60)
    assert eps[3].dev.EndpointVolume.muted is True  # opening a mic is never a side effect
    assert "still muted" in res.message


def test_quantising_driver_reports_the_truth(world):
    eps, _ = world
    eps[0].dev.EndpointVolume.quantum = 0.25
    res = ae.set_device_volume("realtek", 60)
    assert res.data["volume"] == 50 and "wouldn't go to 60%" in res.message


def test_volume_clamped(world):
    assert ae.set_device_volume("realtek", 250).data["volume"] == 100
    assert ae.step_device_volume("realtek", -500).data["volume"] == 0


# ── mute: explicit, idempotent, toggle only from a read ─────────────────────


def test_unmute_when_not_muted_does_not_mute(world):
    eps, _ = world
    res = ae.set_device_mute("realtek", False)
    assert eps[0].dev.EndpointVolume.muted is False and "already unmuted" in res.message


def test_mute_is_idempotent(world):
    eps, _ = world
    ae.set_device_mute("realtek", True)
    res = ae.set_device_mute("realtek", True)
    assert eps[0].dev.EndpointVolume.muted is True and "already muted" in res.message


def test_toggle_flips_the_state_it_just_read(world):
    eps, _ = world
    ae.set_device_mute("realtek", None)
    assert eps[0].dev.EndpointVolume.muted is True
    ae.set_device_mute("realtek", None)
    assert eps[0].dev.EndpointVolume.muted is False


def test_mute_that_does_not_stick_is_reported(world):
    eps, _ = world
    ev = eps[0].dev.EndpointVolume
    ev.SetMute = lambda m, ctx: None  # driver ignores the write
    res = ae.set_device_mute("realtek", True)
    assert res.ok is False and "reports unmuted" in res.message


# ── default device ───────────────────────────────────────────────────────────


class _Role:
    def __init__(self, v):
        self.value = v


class _ERole:
    eConsole, eMultimedia, eCommunications = _Role(0), _Role(1), _Role(2)


def _fake_pycaw(monkeypatch, defaults, applied, stick=True):
    class AU:
        @staticmethod
        def SetDefaultDevice(dev_id, roles):
            applied.append((dev_id, [r.value for r in roles]))
            if stick:
                for r in roles:
                    defaults[ae.OUTPUT][
                        {0: "console", 1: "multimedia", 2: "communications"}[r.value]
                    ] = dev_id

    monkeypatch.setattr(ae, "_pycaw", lambda: (AU, None, _ERole))


def test_set_default_leaves_communications_role_alone(world, monkeypatch):
    eps, defaults = world
    applied = []
    _fake_pycaw(monkeypatch, defaults, applied)
    res = ae.set_default_device("realtek", role="default")
    assert res.ok and applied == [(eps[0].id, [0, 1])]
    assert defaults[ae.OUTPUT]["communications"] == eps[0].id  # untouched (was already realtek)


def test_set_default_communications_only(world, monkeypatch):
    eps, defaults = world
    applied = []
    _fake_pycaw(monkeypatch, defaults, applied)
    ae.set_default_device("glass", role="communications")
    assert applied[-1][1] == [2] and defaults[ae.OUTPUT]["console"] == eps[1].id


def test_set_default_already_default_is_a_noop(world, monkeypatch):
    applied = []
    _fake_pycaw(monkeypatch, world[1], applied)
    res = ae.set_default_device("glasses")
    assert res.ok and applied == [] and "already" in res.message


def test_set_default_verifies_against_the_os(world, monkeypatch):
    applied = []
    _fake_pycaw(monkeypatch, world[1], applied, stick=False)  # Windows ignores it
    res = ae.set_default_device("realtek")
    assert res.ok is False and "didn't switch" in res.message


def test_bad_role_rejected(world):
    assert ae.set_default_device("realtek", role="bogus").ok is False


# ── degrade, never raise ─────────────────────────────────────────────────────


def test_enumeration_failure_becomes_a_message(monkeypatch):
    def boom(flow=None):
        raise OSError("audio service down")

    monkeypatch.setattr(ae, "enumerate_endpoints", boom)
    res = ae.list_devices()
    assert res.ok is False and "couldn't reach" in res.message


def test_list_marks_defaults_and_state(world):
    res = ae.list_devices()
    assert res.ok and "[DEFAULT]" in res.message and "44%" in res.message


# ── Baseline Routing Rule ────────────────────────────────────────────────────


@pytest.fixture
def cs_spy(monkeypatch):
    import actions.computer_settings as cs

    calls = []
    monkeypatch.setattr(cs, "volume_up", lambda step=10: calls.append(("up", step)) or "sys up")
    monkeypatch.setattr(
        cs, "volume_down", lambda step=10: calls.append(("down", step)) or "sys down"
    )
    monkeypatch.setattr(
        cs, "volume_mute", lambda mode="toggle": calls.append(("mute", mode)) or "sys mute"
    )
    monkeypatch.setattr(
        cs, "volume_set", lambda v, device="system": calls.append(("set", v, device)) or "sys set"
    )
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    return calls


@pytest.mark.parametrize("device", [None, "", "system", "default", "master", "computer"])
def test_no_device_or_system_word_targets_system_mixer(world, cs_spy, device):
    p = {"device": device} if device is not None else {}
    assert audio_control({"action": "device_volume_up", **p}) == "sys up"
    assert audio_control({"action": "device_mute", **p}) == "sys mute"
    assert audio_control({"action": "device_volume_set", "value": "50", **p}) == "sys set"
    assert ("up", 10) in cs_spy and ("mute", "mute") in cs_spy and ("set", 50, "system") in cs_spy


def test_named_device_never_touches_system_mixer(world, cs_spy):
    eps, _ = world
    out = audio_control(
        {"action": "device_volume_set", "device": "Microphone Array", "value": "20"}
    )
    assert "20%" in out and cs_spy == []
    assert eps[3].dev.EndpointVolume.scalar == pytest.approx(0.20)


def test_computer_settings_routes_only_explicit_device(world, cs_spy):
    from actions.computer_settings import computer_settings

    eps, _ = world
    out = computer_settings({"action": "volume_set", "value": "30", "device": "realtek"})
    assert "30%" in out and eps[0].dev.EndpointVolume.scalar == pytest.approx(0.30)
    assert cs_spy == []  # the system mixer was not touched
    computer_settings({"action": "volume_up"})  # no device -> system path
    assert ("up", 10) in cs_spy


def test_computer_settings_unmute_is_not_a_blind_toggle(world, cs_spy):
    from actions.computer_settings import computer_settings

    computer_settings({"action": "unmute"})
    computer_settings({"action": "mute"})
    computer_settings({"action": "toggle_mute"})
    assert [c[1] for c in cs_spy if c[0] == "mute"] == ["unmute", "mute", "toggle"]


def test_unmute_in_a_description_is_not_read_as_mute(world, cs_spy):
    from actions.computer_settings import computer_settings

    computer_settings({"description": "unmute the system volume"})
    assert [c[1] for c in cs_spy if c[0] == "mute"] == ["unmute"]


def test_unknown_action_is_explained(world):
    assert "don't know" in audio_control({"action": "explode"})


def test_action_aliases_are_tolerated():
    assert ac_mod._norm_action("device_volume_up") == "up"
    assert ac_mod._norm_action("list") == "list_devices"
    assert ac_mod._norm_action("Switch Device") == "set_default"


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_keeps_audio_control_and_mute_variants_distinct():
    from core.action_registry import canonicalize_tool_call

    tool, p = canonicalize_tool_call("audio_control", {"action": "list_devices"})
    assert tool == "audio_control" and p["action"] == "list_devices"
    for a in ("mute", "unmute", "toggle_mute"):
        t, p = canonicalize_tool_call("computer_settings", {"action": a})
        assert (t, p["action"]) == ("computer_settings", a)
    # a bare set_default/list from audio_control must never be hijacked
    assert canonicalize_tool_call("audio_control", {"action": "list"})[0] == "audio_control"
