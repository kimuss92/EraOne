"""The Windows per-app device preference, and Era seeing past it.

Windows rewrites GetDefaultAudioEndpoint for a process that has an "App volume
and device preferences" entry: the pinned device is returned AS the default. On
this machine python.exe is pinned to the Star Glasses, which made "make the
speakers default" read back as a refusal and sent a bare "volume up" to the
glasses. These tests pin down the correction - and, just as importantly, that a
machine WITHOUT a preference keeps the endpoint API's answer untouched.

All hardware and registry access is faked; nothing here touches the real mixer.
"""

import os
import threading

import pytest

import core.audio_endpoints as ae
import core.audio_policy as ap
from actions.audio_control import audio_control

SPEAKERS = "{0.0.0.00000000}.{0b460ad8-e455-4b3c-ad4a-d8cc0c2fedc2}"
GLASSES = "{0.0.0.00000000}.{d8283f54-1674-4e8c-ba8b-403a03e8ecd0}"
ABSENT = "{0.0.0.00000000}.{deadbeef-1111-2222-3333-444444444444}"


class FakeEV:
    def __init__(self, vol=50):
        self.scalar, self.muted = vol / 100.0, False

    def GetMasterVolumeLevelScalar(self):
        return self.scalar

    def SetMasterVolumeLevelScalar(self, v, ctx):
        self.scalar = v

    def GetMute(self):
        return int(self.muted)

    def SetMute(self, m, ctx):
        self.muted = bool(m)


class FakeDev:
    def __init__(self, ev):
        self.EndpointVolume = ev


def _ep(id_, name, vol):
    return ae.Endpoint(id=id_, name=name, flow=ae.OUTPUT, kind="speakers", dev=FakeDev(FakeEV(vol)))


@pytest.fixture
def pinned(monkeypatch):
    """Speakers are the system default; this process is pinned to the glasses."""
    eps = [
        _ep(SPEAKERS, "pc - speakers (Realtek(R) Audio)", 28),
        _ep(GLASSES, "Star glass (CY03-CB36)", 87),
    ]
    monkeypatch.setattr(
        ae, "enumerate_endpoints", lambda flow=None: [e for e in eps if flow in (None, ae.OUTPUT)]
    )
    # What Windows tells THIS process: the pin masquerading as the default.
    monkeypatch.setattr(
        ae,
        "_api_default_ids",
        lambda flow: {"console": GLASSES, "multimedia": GLASSES, "communications": SPEAKERS},
    )
    # What the system really thinks, from the Level ranking.
    monkeypatch.setattr(ap, "ranked_default_guid", lambda flow, role, ids: ap.guid_tail(SPEAKERS))
    monkeypatch.setattr(ap, "pinned_endpoint_guids", lambda flow: [ap.guid_tail(GLASSES)])
    monkeypatch.setattr(ap, "pinned_endpoint_guid", lambda flow: ap.guid_tail(GLASSES))
    ae._seen_notices.clear()
    return eps


# ── seeing past the pin ──────────────────────────────────────────────────────


def test_default_ids_report_the_system_default_not_the_pin(pinned):
    ids = ae.default_ids(ae.OUTPUT)
    assert ids["console"] == ids["multimedia"] == SPEAKERS


def test_list_marks_the_real_default(pinned):
    assert "pc - speakers (Realtek(R) Audio) (output, 28%) [DEFAULT]" in ae.list_devices().message


def test_generic_volume_drives_the_system_output_not_the_pinned_device(pinned):
    eps = pinned
    assert "pc - speakers" in ae.system_step_volume(+10).message
    assert eps[0].dev.EndpointVolume.scalar == pytest.approx(0.38)
    assert eps[1].dev.EndpointVolume.scalar == pytest.approx(0.87)  # glasses untouched


def test_system_status_names_the_system_output(pinned):
    assert "pc - speakers" in ae.system_status().message


def test_system_mute_targets_the_system_output(pinned):
    eps = pinned
    ae.system_set_mute(True)
    assert eps[0].dev.EndpointVolume.muted is True
    assert eps[1].dev.EndpointVolume.muted is False


# ── no pin: the endpoint API keeps the last word ─────────────────────────────


def test_without_a_preference_nothing_is_second_guessed(pinned, monkeypatch):
    monkeypatch.setattr(ap, "pinned_endpoint_guids", lambda flow: [])
    monkeypatch.setattr(ap, "pinned_endpoint_guid", lambda flow: None)
    # The ranking would say SPEAKERS; with no pin the API's GLASSES must stand.
    assert ae.default_ids(ae.OUTPUT)["console"] == GLASSES
    assert "Star glass" in ae.system_status().message


def test_a_pin_on_a_disconnected_device_is_dormant(pinned, monkeypatch):
    monkeypatch.setattr(ap, "pinned_endpoint_guids", lambda flow: [ap.guid_tail(ABSENT)])
    assert ae.default_ids(ae.OUTPUT)["console"] == GLASSES


def test_policy_failure_falls_back_to_the_api(pinned, monkeypatch):
    def boom(flow):
        raise OSError("registry unavailable")

    monkeypatch.setattr(ap, "pinned_endpoint_guids", boom)
    assert ae.default_ids(ae.OUTPUT)["console"] == GLASSES  # degraded, not raised


def test_unrankable_roles_keep_the_api_answer(pinned, monkeypatch):
    monkeypatch.setattr(ap, "ranked_default_guid", lambda flow, role, ids: None)
    assert ae.default_ids(ae.OUTPUT)["console"] == GLASSES


# ── set_default tells the truth about the pin ────────────────────────────────


class _Role:
    def __init__(self, v):
        self.value = v


class _ERole:
    eConsole, eMultimedia, eCommunications = _Role(0), _Role(1), _Role(2)


@pytest.fixture
def switchable(pinned, monkeypatch):
    state = {"console": SPEAKERS, "multimedia": SPEAKERS, "communications": SPEAKERS}

    class AU:
        @staticmethod
        def SetDefaultDevice(dev_id, roles):
            for r in roles:
                state[{0: "console", 1: "multimedia", 2: "communications"}[r.value]] = dev_id

    monkeypatch.setattr(ae, "_pycaw", lambda: (AU, None, _ERole))
    monkeypatch.setattr(ae, "default_ids", lambda flow: dict(state))
    return state


def test_switching_away_from_the_pin_says_era_stays_put(switchable):
    switchable.update({"console": GLASSES, "multimedia": GLASSES})
    res = ae.set_default_device("realtek")
    assert res.ok and "per-app preference" in res.message and "Star glass" in res.message


def test_switching_onto_the_pinned_device_promises_the_voice_follows(switchable):
    res = ae.set_default_device("glass")
    assert res.ok and "voice follows" in res.message


# ── reporting and clearing the preference ────────────────────────────────────


def test_status_names_the_pinned_device(pinned):
    assert "Star glass" in ae.app_preference_status().message


def test_status_when_free(pinned, monkeypatch):
    monkeypatch.setattr(ap, "pinned_endpoint_guids", lambda flow: [])
    monkeypatch.setattr(ap, "pinned_endpoint_guid", lambda flow: None)
    assert "Nothing is overriding" in ae.app_preference_status().message


def test_clear_reports_what_it_removed_and_that_the_exe_is_shared(pinned, monkeypatch):
    monkeypatch.setattr(ap, "clear_app_override", lambda flow=None: (2, []))
    msg = ae.clear_app_preference().message
    assert "Cleared 2 app preferences" in msg
    # The entry is the interpreter's, not Era's alone - the reply must not imply
    # that only Era's routing changed.
    assert "Python interpreter" in msg


def test_clear_with_nothing_to_clear_is_not_a_failure(pinned, monkeypatch):
    monkeypatch.setattr(ap, "clear_app_override", lambda flow=None: (0, []))
    res = ae.clear_app_preference()
    assert res.ok and "no audio app preference" in res.message


def test_clear_failure_is_reported(pinned, monkeypatch):
    monkeypatch.setattr(ap, "clear_app_override", lambda flow=None: (0, ["access denied"]))
    assert ae.clear_app_preference().ok is False


def test_audio_control_routes_the_new_actions(pinned, monkeypatch):
    monkeypatch.setattr(ap, "clear_app_override", lambda flow=None: (1, []))
    assert "Star glass" in audio_control({"action": "app_preference"})
    assert "Cleared 1 app preference" in audio_control({"action": "clear_app_default"})
    assert "Cleared 1 app preference" in audio_control({"action": "reset_app_preference"})


def test_clearing_is_never_what_a_bare_volume_command_means():
    # 'clear'/'reset' must not be reachable by accident from the volume vocabulary.
    from actions import audio_control as ac_mod

    assert ac_mod._norm_action("volume_up") == "up"
    assert ac_mod._norm_action("app_preference") == "app_preference"
    assert ac_mod._norm_action("clear_app_default") == "clear_app_preference"


# ── the policy layer's own logic ─────────────────────────────────────────────


# ── the safety gate ──────────────────────────────────────────────────────────


def test_reading_the_preference_is_free_but_clearing_it_is_gated():
    from core.safety_guard import classify_action

    for free in ("list_devices", "set_default", "volume_up", "app_preference"):
        r = classify_action("audio_control", {"action": free})
        assert r["requires_confirmation"] is False, free
    # Deleting a Windows setting is not "instantly reversible" - every spelling
    # of it must be confirmed, or an alias becomes a way round the gate.
    for gated in ("clear_app_preference", "clear_app_default", "clear_app_override"):
        r = classify_action("audio_control", {"action": gated})
        assert (r["risk_level"], r["requires_confirmation"]) == ("MEDIUM", True), gated


def test_every_clearing_alias_is_gated():
    """A new alias in audio_control must not slip past the safety guard."""
    from actions import audio_control as ac_mod
    from core.safety_guard import classify_action

    aliases = [a for a, c in ac_mod._ALIASES.items() if c == "clear_app_preference"]
    assert aliases
    for alias in aliases:
        assert classify_action("audio_control", {"action": alias})["requires_confirmation"] is True


def test_guid_tail_extracts_the_endpoint_guid():
    assert ap.guid_tail(SPEAKERS) == "{0b460ad8-e455-4b3c-ad4a-d8cc0c2fedc2}"
    assert ap.guid_tail("{0B460AD8-E455-4B3C-AD4A-D8CC0C2FEDC2}") == ap.guid_tail(SPEAKERS)
    assert ap.guid_tail("") == ""


def test_interface_signature_matches_windows_two_spellings():
    stored = (
        r"{2}.\\?\bthenum#{0000110b-0000-1000-8000-00805f9b34fb}_vid&000105d6_pid&000a"
        r"#7&15dfbade&1&c6010100cb36_c00000000#{6994ad04-93ef-11d0-a3cc-00a0c9223196}\src"
    )
    preference = (
        r"{2}.\\?\bthenum#{0000110b-0000-1000-8000-00805f9b34fb}_vid&000105d6_pid&000a"
        r"#{6994ad04-93ef-11d0-a3cc-00a0c9223196}\src/00010001"
    )
    assert ap._iface_signature(stored) == ap._iface_signature(preference)
    assert ap._iface_signature("") is None


def test_ranked_default_picks_the_highest_live_level(monkeypatch):
    world = {
        "{a}": {"DeviceState": 1, "Level:0": 0x105},
        "{b}": {"DeviceState": 1, "Level:0": 0xE1},
        "{c}": {"DeviceState": 8, "Level:0": 0xFFFF},  # unplugged: ranks highest, never chosen
        "{d}": {"DeviceState": 1},  # never made default: no ranking at all
    }

    class Key:
        def __init__(self, guid):
            self.guid = guid

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ap, "_endpoint_key", lambda flow, guid, sub="": Key(guid))
    monkeypatch.setattr(ap, "_value", lambda key, name: world[key.guid].get(name))
    monkeypatch.setattr(ap, "winreg", object())
    assert ap.ranked_default_guid("output", "console", ["{a}", "{b}", "{c}", "{d}"]) == "{a}"
    assert ap.ranked_default_guid("output", "console", ["{c}", "{d}"]) is None
    assert ap.ranked_default_guid("output", "bogus", ["{a}"]) is None
    assert ap.ranked_default_guid("sideways", "console", ["{a}"]) is None


def test_clear_only_ever_deletes_entries_naming_this_executable(monkeypatch):
    deleted = []
    monkeypatch.setattr(ap, "winreg", object())
    monkeypatch.setattr(ap, "_app_pref_entries", lambda: [("k1", "iface-one"), ("k2", "iface-two")])
    monkeypatch.setattr(ap, "_endpoint_signatures", lambda flow: {"iface-one": ["{a}"]})
    monkeypatch.setattr(ap, "_iface_signature", lambda p: p)
    monkeypatch.setattr(ap, "_delete_pref", lambda name: deleted.append(name))
    removed, errors = ap.clear_app_override("output")
    assert (removed, errors, deleted) == (1, [], ["k1"])  # the input-flow entry survives


# ── security-review regressions (2026-09-20) ─────────────────────────────────


def test_gate_holds_when_the_alias_table_cannot_be_imported(monkeypatch):
    """The guard's fallback must FAIL CLOSED.

    classify_action sees the RAW action, then normalises through the tool's
    alias table. That import can fail on a synced path - and the one alias the
    old substring backstop missed was `reset_app_preference`, which would then
    have deleted registry keys with no confirmation.
    """
    import builtins

    from core.safety_guard import classify_action

    real_import = builtins.__import__

    def no_audio_control(name, *a, **kw):
        if name == "actions.audio_control":
            raise OSError("file temporarily unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_audio_control)
    for alias in (
        "clear_app_preference",
        "clear_app_default",
        "clear_app_override",
        "reset_app_preference",
    ):
        r = classify_action("audio_control", {"action": alias})
        assert r["requires_confirmation"] is True, alias
    # ...without over-gating the read-only neighbours.
    for free in ("app_preference", "app_default", "volume_up", "set_default"):
        assert classify_action("audio_control", {"action": free})["requires_confirmation"] is False


def test_an_unscopable_flow_refuses_instead_of_clearing_everything(monkeypatch):
    """An empty signature map must not widen 'clear the input pin' into 'clear all'."""
    deleted = []
    monkeypatch.setattr(ap, "winreg", object())
    monkeypatch.setattr(ap, "_endpoint_signatures", lambda flow: {})  # registry unreadable
    monkeypatch.setattr(ap, "_app_pref_entries", lambda: [("k1", "iface-one")])
    monkeypatch.setattr(ap, "_delete_pref", lambda name: deleted.append(name))
    removed, errors = ap.clear_app_override("input")
    assert (removed, deleted) == (0, []) and errors and "refusing" in errors[0]


def test_an_unknown_flow_is_refused_not_treated_as_all(pinned, monkeypatch):
    called = []
    monkeypatch.setattr(ap, "clear_app_override", lambda flow=None: called.append(flow) or (9, []))
    res = ae.clear_app_preference("render")  # not a flow this module knows
    assert res.ok is False and called == [] and "won't guess" in res.message


def test_a_pin_matching_two_same_model_devices_is_never_named(monkeypatch):
    """Detection still fires; the spoken reply must not pick one of the twins."""
    monkeypatch.setattr(ap, "winreg", object())
    monkeypatch.setattr(ap, "_prefs_stamp", lambda: (1, 1))
    monkeypatch.setattr(ap, "_read_pins", lambda: {"output": ["{twin-a}", "{twin-b}"]})
    ap._cache.clear()
    assert ap.pinned_endpoint_guids("output") == ["{twin-a}", "{twin-b}"]
    assert ap.has_app_override("output") is True  # the correction still applies
    assert ap.pinned_endpoint_guid("output") is None  # but we refuse to name one
    ap._cache.clear()


def test_a_binary_registry_value_does_not_escape_as_a_typeerror():
    assert ap._iface_signature(b"\x00\x01binary") is None
    assert ap._iface_signature("") is None


def test_an_empty_sys_executable_matches_nothing(monkeypatch):
    """abspath('') is the CWD, so the guard has to test sys.executable itself."""
    monkeypatch.setattr(ap.sys, "executable", "")
    assert ap._this_exe_paths() == ()
    monkeypatch.setattr(ap.sys, "executable", "   ")
    assert ap._this_exe_paths() == ()


# ── writing the preference (set_app_override) ────────────────────────────────


class _FakeHKey:
    """A winreg handle that is happy to be used as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeWinreg:
    """Just enough winreg to let the write path run without touching HKCU."""

    REG_SZ = 1
    KEY_ALL_ACCESS = 0xF003F
    HKEY_CURRENT_USER = object()
    HKEY_LOCAL_MACHINE = object()

    def __init__(self, create_error=None):
        self.written: list[str] = []
        self.deleted: list[str] = []
        self.created: list[str] = []
        self._create_error = create_error

    def OpenKey(self, root, path, reserved=0, access=0):
        return _FakeHKey()

    def CreateKey(self, root, name):
        if self._create_error:
            raise self._create_error
        self.created.append(name)
        return _FakeHKey()

    def SetValueEx(self, key, name, reserved, typ, value):
        self.written.append(value)


@pytest.fixture
def writable_prefs(monkeypatch):
    """A fake preference store already holding two entries for this exe.

    ``_this_exe_paths`` deliberately returns ONLY the NT spelling: the value
    written to the registry must still come out as a drive-letter path.
    """
    fake = _FakeWinreg()
    monkeypatch.setattr(ap, "winreg", fake)
    monkeypatch.setattr(ap.sys, "executable", r"C:\Fake\Python\python.exe")
    monkeypatch.setattr(
        ap, "_this_exe_paths", lambda: (r"\device\harddiskvolume3\fake\python.exe",)
    )
    monkeypatch.setattr(ap, "_get_interface_path_for_guid", lambda flow, guid: "iface-target")
    monkeypatch.setattr(ap, "_app_pref_entries", lambda: [("k1", "iface-old"), ("k2", "iface-old")])
    monkeypatch.setattr(
        ap, "_endpoint_signatures", lambda flow: {"iface-old": ["{a}"], "iface-target": ["{b}"]}
    )
    monkeypatch.setattr(ap, "_iface_signature", lambda p: p)
    monkeypatch.setattr(ap, "_delete_pref", lambda name: fake.deleted.append(name))
    ap._cache.clear()
    yield fake
    ap._cache.clear()


def _call_with_deadline(fn, *args, seconds=10):
    """Run ``fn`` on its own thread and refuse to wait forever for it.

    A plain assert cannot catch a deadlock - the test would hang with the code
    under test rather than fail. This is the only way to state "it answers".
    """
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(result=fn(*args)), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"{fn.__name__} never returned - the module lock deadlocked"
    return box["result"]


def test_setting_an_override_does_not_deadlock_on_its_own_clear(writable_prefs):
    """set_app_override -> clear_app_override -> _lock is a NESTED acquire.

    With a plain Lock the second acquire blocks forever, and @_never_raises
    means it does not even surface as an exception: on an Era tool call that is
    an executor thread that simply never answers.
    """
    changed, errors = _call_with_deadline(ap.set_app_override, "output", "{b}")
    assert (changed, errors) == (1, [])


def test_the_lock_is_reentrant_so_a_held_lock_never_blocks_its_own_thread():
    assert isinstance(ap._lock, type(threading.RLock()))
    with ap._lock:
        with ap._lock:  # a plain Lock hangs here
            pass


def test_a_replacement_counts_the_preference_it_made_not_the_ones_it_removed(writable_prefs):
    """Two stale entries cleared + one written is changed=1, not changed=3."""
    changed, errors = _call_with_deadline(ap.set_app_override, "output", "{b}")
    assert writable_prefs.deleted == ["k1", "k2"]  # both really were removed
    assert changed == 1 and errors == []


def test_the_written_value_names_the_drive_letter_exe_deterministically(writable_prefs):
    _call_with_deadline(ap.set_app_override, "output", "{b}")
    exe = os.path.normcase(r"C:\Fake\Python\python.exe")
    assert writable_prefs.written == [f"iface-target|{exe}"]
    assert len(writable_prefs.created) == 1


def test_the_cache_is_invalidated_after_a_write(writable_prefs):
    ap._cache.update(stamp=(1, 1), pins={"output": ["{stale}"]})
    _call_with_deadline(ap.set_app_override, "output", "{b}")
    assert ap._cache == {}


def test_a_failed_write_still_invalidates_the_cache(writable_prefs, monkeypatch):
    """The subkey can exist even when the value write failed - never trust the cache."""
    monkeypatch.setattr(ap, "winreg", _FakeWinreg(create_error=OSError("access denied")))
    ap._cache.update(stamp=(1, 1), pins={"output": ["{stale}"]})
    changed, errors = _call_with_deadline(ap.set_app_override, "output", "{b}")
    assert changed == 0 and errors and "access denied" in errors[0]
    assert ap._cache == {}


def test_an_unknown_flow_is_refused_before_anything_is_written(writable_prefs):
    changed, errors = _call_with_deadline(ap.set_app_override, "sideways", "{b}")
    assert changed == 0 and "unknown audio direction" in errors[0]
    assert writable_prefs.written == [] and writable_prefs.deleted == []


def test_an_unresolvable_target_writes_nothing(writable_prefs, monkeypatch):
    monkeypatch.setattr(ap, "_get_interface_path_for_guid", lambda flow, guid: None)
    changed, errors = _call_with_deadline(ap.set_app_override, "output", "{nope}")
    assert changed == 0 and "could not find interface path" in errors[0]
    assert writable_prefs.written == [] and writable_prefs.deleted == []


def test_an_unidentifiable_executable_writes_nothing(writable_prefs, monkeypatch):
    monkeypatch.setattr(ap, "_this_exe_paths", lambda: ())
    changed, errors = _call_with_deadline(ap.set_app_override, "output", "{b}")
    assert changed == 0 and errors == ["could not determine executable path"]
    assert writable_prefs.written == [] and writable_prefs.deleted == []
