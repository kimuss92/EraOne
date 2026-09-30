"""core.voice_output / audio_control: where Era's SPOKEN VOICE plays.

All hardware is faked - these never touch the real mixer, the real device list
or the real preferences file. The invariants under test are the ones a user
would notice if they broke:

  * an explicit pin beats every automatic rule, and takes effect immediately;
  * it survives a restart, and survives media routing changing underneath it;
  * it never moves the Windows default device (Spotify/YouTube stay put);
  * a pinned device that vanishes costs the ROUTING, never the voice;
  * an ambiguous name asks instead of guessing.
"""

import asyncio

import pytest

import core.audio_endpoints as ae
import core.user_prefs as up
import core.voice_output as vo
from actions.audio_control import audio_control

# PortAudio's MME host API truncates names to 31 characters. The speakers below
# are the real names from this machine, truncation included, because an
# exact-match lookup silently fails on them and drops Era onto the default.
SD_DEVICES = [
    {"name": "Microsoft Sound Mapper - Output", "max_output_channels": 2, "hostapi": 0},
    {"name": "Star glass (CY03-CB36)", "max_output_channels": 2, "hostapi": 0},
    {"name": "pc - speakers (Realtek(R) Audio", "max_output_channels": 2, "hostapi": 0},
    {"name": "Microphone Array (Intel)", "max_output_channels": 0, "hostapi": 0},
    {"name": "Star glass (CY03-CB36)", "max_output_channels": 2, "hostapi": 2},
    {"name": "pc - speakers (Realtek(R) Audio)", "max_output_channels": 2, "hostapi": 2},
]
GLASS_SD_INDEX = 1
SPEAKERS_SD_INDEX = 2


class _FakeSD:
    """Just enough sounddevice for the resolver."""

    class _Default:
        device = (0, 0)  # default output lives on hostapi 0 (MME)

    default = _Default()

    @staticmethod
    def query_devices(index=None):
        return SD_DEVICES[index] if index is not None else list(SD_DEVICES)


def _ep(name, kind, id_):
    return ae.Endpoint(id=id_, name=name, flow=ae.OUTPUT, kind=kind, dev=None)


GLASS = _ep("Star glass (CY03-CB36)", "headphones", "{0.0.0}.{glass-guid}")
SPEAKERS = _ep("pc - speakers (Realtek(R) Audio)", "speakers", "{0.0.0}.{realtek-guid}")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A machine with glasses + speakers, isolated prefs, no real hardware."""
    connected = [GLASS, SPEAKERS]

    monkeypatch.setattr(up, "PREFS_PATH", tmp_path / "user_prefs.json")
    monkeypatch.setattr(
        ae,
        "enumerate_endpoints",
        lambda flow=None: [e for e in connected if flow in (None, e.flow)],
    )
    monkeypatch.setattr("core.audio_devices._identifiers", lambda: ("star glass", "cy03cb36"))
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", _FakeSD)
    # Unpinned, Era auto-routes to the glasses — the historical behaviour.
    monkeypatch.setattr("core.audio_devices.glass_output_index", lambda *a, **k: GLASS_SD_INDEX)
    monkeypatch.setattr(
        "core.audio_devices.probe_glass",
        lambda *a, **k: {"connected": True, "output_index": GLASS_SD_INDEX, "name": GLASS.name},
    )
    return connected


# ── the decision ─────────────────────────────────────────────────────────────


def test_unpinned_keeps_the_automatic_glass_route(world):
    assert vo.get_pin() is None
    assert vo.resolve_output_index() == GLASS_SD_INDEX


def test_pin_overrides_the_automatic_route(world):
    # The glasses are connected, so the auto rule would pick them. An explicit
    # pin on the speakers must win — this is the whole feature.
    assert vo.set_pin("pc - speakers").ok
    assert vo.resolve_output_index() == SPEAKERS_SD_INDEX


def test_pin_matches_through_mme_name_truncation(world):
    # The endpoint is "...(Realtek(R) Audio)"; MME enumerates it without the
    # closing paren. An exact-match lookup would find nothing and fall back.
    assert vo.sd_index_for_name("pc - speakers (Realtek(R) Audio)") == SPEAKERS_SD_INDEX


def test_pin_survives_a_restart(world):
    vo.set_pin("glasses")
    # A "restart" is a fresh read of the store — nothing cached in the process.
    assert vo.get_pin()["name"] == GLASS.name
    assert up.load_prefs()["voice_output_device"]["id"] == GLASS.id


def test_disconnected_pin_falls_back_to_the_system_default_and_is_kept(world):
    vo.set_pin("glasses")
    world.remove(GLASS)  # the glasses walk away mid-session
    assert vo.resolve_output_index() is None  # None = the system default
    assert vo.get_pin()["name"] == GLASS.name  # the choice is NOT forgotten
    world.append(GLASS)  # ...and is reclaimed when they come back
    assert vo.resolve_output_index() == GLASS_SD_INDEX


def test_clear_returns_to_the_automatic_route(world):
    vo.set_pin("pc - speakers")
    assert vo.clear_pin().ok
    assert vo.get_pin() is None
    assert vo.resolve_output_index() == GLASS_SD_INDEX


# ── re-enumeration signal (device connected AFTER startup) ────────────────────


def test_needs_reenumeration_when_pinned_device_invisible_to_portaudio(world, monkeypatch):
    """Glasses live in Core Audio but missing from PortAudio's frozen list — the
    'connected after startup' case the in-session reclaim cannot fix. The playback
    loop uses this to schedule a reconnect-to-reenumerate."""
    vo.set_pin("glasses")
    assert vo.resolve_output_index() == GLASS_SD_INDEX  # visible right now
    assert vo.needs_reenumeration() is False

    # PortAudio can no longer see the glasses; Core Audio (enumerate_endpoints)
    # still can — exactly the frozen-list miss.
    no_glass = [d for d in SD_DEVICES if "star glass" not in d["name"].lower()]

    class _SDNoGlass(_FakeSD):
        @staticmethod
        def query_devices(index=None):
            return no_glass[index] if index is not None else list(no_glass)

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", _SDNoGlass)
    assert vo.resolve_output_index() is None  # can't route to them
    assert vo.needs_reenumeration() is True  # ...so a reconnect is warranted


def test_no_reenumeration_when_unpinned_or_disconnected(world):
    assert vo.needs_reenumeration() is False  # nothing pinned
    vo.set_pin("glasses")
    world.remove(GLASS)  # genuinely gone, not merely invisible
    assert vo.needs_reenumeration() is False  # nothing to reclaim


# ── code-enforced glasses-volume shortcut ─────────────────────────────────────


def test_strip_glass_scope_parses_action_names():
    from actions.audio_control import _strip_glass_scope

    assert _strip_glass_scope("glass_volume_up") == (True, "volume_up")
    assert _strip_glass_scope("glasses_set_volume") == (True, "set_volume")
    assert _strip_glass_scope("glass-mute") == (True, "mute")
    assert _strip_glass_scope("glass_volume") == (True, "volume")
    assert _strip_glass_scope("glasses") == (True, "status")
    assert _strip_glass_scope("cy03cb36_volume_down") == (True, "volume_down")
    # Non-glasses actions are left entirely alone.
    assert _strip_glass_scope("volume_up") == (False, "volume_up")
    assert _strip_glass_scope("set_default") == (False, "set_default")


def test_glass_volume_up_targets_the_glasses_without_a_device(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ae,
        "step_device_volume",
        lambda device, delta, flow=None: (
            calls.append((device, delta)) or ae.AudioResult(True, "ok")
        ),
    )
    audio_control({"action": "glass_volume_up"})  # no device given
    assert calls == [("glasses", 10)]


def test_glass_volume_set_targets_the_glasses(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ae,
        "set_device_volume",
        lambda device, value, flow=None: (
            calls.append((device, value)) or ae.AudioResult(True, "ok")
        ),
    )
    audio_control({"action": "glass_volume_set", "value": 40})
    assert calls == [("glasses", 40)]


def test_glass_mute_targets_the_glasses(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ae,
        "set_device_mute",
        lambda device, muted, flow=None: (
            calls.append((device, muted)) or ae.AudioResult(True, "ok")
        ),
    )
    audio_control({"action": "glass_mute"})
    assert calls == [("glasses", True)]


def test_glass_volume_status_targets_the_glasses(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ae,
        "device_status",
        lambda device, flow=None: calls.append(device) or ae.AudioResult(True, "ok"),
    )
    audio_control({"action": "glass_volume"})
    assert calls == ["glasses"]


def test_bare_volume_up_still_hits_the_system_not_the_glasses(world, monkeypatch):
    """The Baseline Routing Rule is untouched: a device-less command is SYSTEM."""
    from actions import computer_settings as cs

    sys_calls, dev_calls = [], []
    monkeypatch.setattr(cs, "volume_up", lambda step=10: sys_calls.append(step) or "system ok")
    monkeypatch.setattr(
        ae, "step_device_volume", lambda *a, **k: dev_calls.append(a) or ae.AudioResult(True, "d")
    )
    audio_control({"action": "volume_up"})
    assert sys_calls == [10] and dev_calls == []


# ── immediacy ────────────────────────────────────────────────────────────────


def test_pin_and_clear_bump_the_reroute_signal(world):
    """_play_audio re-opens its stream when this counter moves; if a pin did not
    bump it, the change would only land after a restart."""
    start = vo.generation()
    vo.set_pin("glasses")
    after_pin = vo.generation()
    assert after_pin > start
    vo.clear_pin()
    assert vo.generation() > after_pin


def test_a_failed_pin_does_not_bump_the_signal(world):
    start = vo.generation()
    assert not vo.set_pin("toaster").ok
    assert vo.generation() == start


# ── independence from media routing ──────────────────────────────────────────


def test_pinning_the_voice_never_touches_the_windows_default(world, monkeypatch):
    """Routing Era's voice must leave Spotify/YouTube exactly where they were."""

    def _boom(*a, **kw):
        raise AssertionError("set_default_device must not be called by a voice pin")

    monkeypatch.setattr(ae, "set_default_device", _boom)
    assert "Star glass" in audio_control({"action": "set_voice_output", "device": "glasses"})


def test_media_default_changing_does_not_move_the_pinned_voice(world, monkeypatch):
    """The user moves Spotify from the glasses to the speakers. Era's voice stays."""
    vo.set_pin("glasses")
    monkeypatch.setattr(
        ae,
        "default_ids",
        lambda flow: {"console": SPEAKERS.id, "multimedia": SPEAKERS.id, "communications": None},
    )
    assert vo.resolve_output_index() == GLASS_SD_INDEX
    assert vo.get_pin()["id"] == GLASS.id


# ── the spoken vocabulary ────────────────────────────────────────────────────


def test_switch_your_voice_to_the_computer_pins_the_speakers(world):
    """'the computer' names a DEVICE. Treating it as 'follow the default' would
    drop the pin and bounce Era straight back to the connected glasses."""
    vo.set_pin("glasses")
    assert "pc - speakers" in audio_control({"action": "set_voice_output", "device": "computer"})
    assert vo.resolve_output_index() == SPEAKERS_SD_INDEX


def test_follow_the_system_default_clears_the_pin(world):
    vo.set_pin("pc - speakers")
    audio_control({"action": "set_voice_output", "device": "default"})
    assert vo.get_pin() is None


def test_unnamed_device_asks_rather_than_guessing(world):
    assert "Which device" in audio_control({"action": "set_voice_output"})
    assert vo.get_pin() is None


def test_unknown_device_lists_the_real_ones_and_pins_nothing(world):
    reply = audio_control({"action": "set_voice_output", "device": "toaster"})
    assert "toaster" in reply and "Star glass" in reply
    assert vo.get_pin() is None


def test_ambiguous_name_asks_which_one(world):
    world.append(_ep("Star glass (CY03-CB36) 2", "headphones", "{0.0.0}.{glass-2}"))
    reply = audio_control({"action": "set_voice_output", "device": "star glass"})
    assert "more than one" in reply.lower() or "which" in reply.lower()
    assert vo.get_pin() is None


def test_status_reports_the_live_answer(world):
    assert "isn't pinned" in audio_control({"action": "voice_output"})
    vo.set_pin("glasses")
    assert "Star glass" in audio_control({"action": "voice_output"})


def test_every_new_alias_reaches_its_handler(world):
    from actions.audio_control import _norm_action

    for alias in ("speak_on", "pin_voice", "set_voice_device", "voice_output_set"):
        assert _norm_action(alias) == "set_voice_output"
    for alias in ("unpin_voice", "reset_voice_output", "voice_output_auto"):
        assert _norm_action(alias) == "clear_voice_output"
    for alias in ("battery", "battery_level", "device_battery"):
        assert _norm_action(alias) == "battery"


# ── voice recognition ────────────────────────────────────────────────────────


def test_pin_records_the_voice_lock_state_it_was_set_under(world, monkeypatch):
    """The owner gate (core/voice_gate.py) blocks a stranger's audio at the
    mic->Gemini choke point, so with voice_lock ON nothing is ever generated for
    a voice that isn't the owner's — and therefore nothing plays on the pinned
    device either. The pin records which authority it was set under so status()
    can say so instead of the model guessing."""
    monkeypatch.setattr(up, "get_voice_lock_enabled", lambda: True)
    monkeypatch.setattr("core.voice_output._voice_lock_active", lambda: True)
    vo.set_pin("glasses")
    assert vo.get_pin()["voice_lock"] is True
    assert "Voice recognition was on" in audio_control({"action": "voice_output"})


def test_pin_set_without_voice_lock_does_not_claim_it(world, monkeypatch):
    monkeypatch.setattr("core.voice_output._voice_lock_active", lambda: False)
    vo.set_pin("glasses")
    assert vo.get_pin()["voice_lock"] is False
    assert "Voice recognition" not in audio_control({"action": "voice_output"})


# ── the prompt must not contradict the pin ───────────────────────────────────


def test_routing_summary_states_the_pin(world):
    from core.audio_devices import routing_summary

    assert "Star Glasses CONNECTED" in routing_summary(force=True)
    vo.set_pin("pc - speakers")
    summary = routing_summary(force=True)
    assert "VOICE OUTPUT PINNED" in summary and "pc - speakers" in summary


# ── the safety layer ─────────────────────────────────────────────────────────


def test_voice_routing_is_reversible_so_it_is_not_gated():
    """Unlike clear_app_preference (which deletes a Windows setting), these are
    undone by one spoken command and must not stop to ask."""
    from core.safety_guard import classify_action

    for action in ("set_voice_output", "clear_voice_output", "voice_output", "battery"):
        verdict = classify_action("audio_control", {"action": action})
        assert verdict["requires_confirmation"] is False, action


# ── live re-route of an open stream ──────────────────────────────────────────


async def _nosleep(_s):
    return None


class _FakeStream:
    def __init__(self, device, log):
        self.device, self.log = device, log

    def start(self):
        self.log.append(self.device)

    def write(self, chunk):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def test_open_stream_reroutes_without_a_restart():
    from app.era_live.audio_output import ResilientOutputStream

    opened = []
    out = ResilientOutputStream(lambda d: _FakeStream(d, opened), preferred_device=GLASS_SD_INDEX)
    assert out.open()
    assert opened == [GLASS_SD_INDEX]

    out.set_preferred_device(SPEAKERS_SD_INDEX)
    assert asyncio.run(out.write(b"\x00" * 8)) is True
    assert opened == [GLASS_SD_INDEX, SPEAKERS_SD_INDEX]


def test_fallback_keeps_the_preference_and_reclaims_the_device():
    """A pinned device that comes back must be reclaimed. Erasing the preference
    on fallback meant the pin looked like it had been silently ignored."""
    from app.era_live.audio_output import ResilientOutputStream

    opened, missing = [], {GLASS_SD_INDEX}

    def opener(device):
        if device in missing:
            raise OSError("device gone")
        return _FakeStream(device, opened)

    out = ResilientOutputStream(
        opener, preferred_device=GLASS_SD_INDEX, sleep=lambda _s: asyncio.sleep(0)
    )
    assert out.open()
    assert opened == [None] and out.on_fallback  # degraded to the default...
    assert out._preferred == GLASS_SD_INDEX  # ...but the choice is intact

    missing.clear()
    out._drop_stream()
    assert asyncio.run(out.write(b"\x00" * 8)) is True
    assert opened[-1] == GLASS_SD_INDEX and not out.on_fallback


def test_play_audio_reroutes_mid_turn_without_a_restart(monkeypatch):
    """End to end through the REAL _play_audio loop: the user says 'speak on my
    glasses' while Era is mid-answer, and the remaining audio comes out of the
    glasses. Nothing is dropped in the hand-off."""
    import sounddevice as sd
    from test_audio_output import FakeDevice, _fake_era  # sibling test module

    from app.era_live.audio import AudioIOMixin

    dev = FakeDevice()
    monkeypatch.setattr(sd, "RawOutputStream", lambda **kw: dev.opener(kw["device"]))

    # The pin flips on the 5th read of the counter, i.e. a few chunks into the
    # answer. Driving it off the loop's OWN polling rather than off wall-clock
    # sleeps is what keeps this deterministic: the queue drains in milliseconds,
    # so a timed flip races the end of the turn and proves nothing.
    state = {"gen": 0, "route": None, "ticks": 0}

    def _generation():
        state["ticks"] += 1
        if state["ticks"] == 5:
            state["gen"], state["route"] = 1, GLASS_SD_INDEX
        return state["gen"]

    monkeypatch.setattr(vo, "generation", _generation)
    monkeypatch.setattr(vo, "resolve_output_index", lambda: state["route"])

    chunks = [bytes([i]) * 64 for i in range(12)]

    async def main():
        era = _fake_era(chunks)
        task = asyncio.create_task(AudioIOMixin._play_audio(era))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if era.audio_in_queue.empty():
                break
        await asyncio.sleep(0.3)
        era._go_away_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
    assert dev.played == chunks  # every chunk, in order — nothing lost
    assert dev.opened_on[0] is None  # started on the system default...
    assert GLASS_SD_INDEX in dev.opened_on  # ...and moved to the glasses mid-turn


# ── the stale audio engine ───────────────────────────────────────────────────
#
# PortAudio builds its device list during Pa_Initialize and never refreshes it.
# A Bluetooth device that reconnects after Era starts is therefore invisible to
# the audio engine while Windows and pycaw both show it connected. That is the
# "Star glass is connected but I can't stream to it" failure.


def test_pin_is_kept_when_the_audio_engine_cannot_see_the_device_yet(world, monkeypatch):
    """Core Audio says the glasses are here; PortAudio has not noticed. Refusing
    the pin threw the user's choice away over a stale snapshot."""
    monkeypatch.setattr(vo, "_lookup", lambda name: None)  # engine is blind
    monkeypatch.setattr(vo, "refresh_devices", lambda: False)  # and stays blind

    result = vo.set_pin("glasses")
    assert result.ok
    assert vo.get_pin()["name"] == GLASS.name  # the choice SURVIVED
    assert result.data["sd_index"] is None
    assert "can't see it yet" in result.message  # and she says so honestly


def test_a_miss_triggers_one_refresh_then_finds_the_device(world, monkeypatch):
    calls = {"refresh": 0}
    seen = {"ok": False}

    def _lookup(name):
        return GLASS_SD_INDEX if seen["ok"] else None

    def _refresh():
        calls["refresh"] += 1
        seen["ok"] = True  # the re-enumeration reveals the device
        return True

    monkeypatch.setattr(vo, "_lookup", _lookup)
    monkeypatch.setattr(vo, "refresh_devices", _refresh)

    assert vo.sd_index_for_name(GLASS.name, refresh_on_miss=True) == GLASS_SD_INDEX
    assert calls["refresh"] == 1


def test_refresh_never_tears_down_portaudio_during_a_test_run():
    """Pa_Terminate is process-wide: in pytest it would pull the audio engine
    out from under unrelated tests. The guard is the reason every test above can
    call the real code path safely."""
    assert vo.refresh_devices() is False


def test_the_stream_layer_never_re_enumerates_portaudio(monkeypatch):
    """THE safety invariant. Pa_Terminate is process-wide and invalidates every
    open stream, including the microphone InputStream that _listen_audio holds
    for the whole session - and _listen_audio RE-RAISES, which kills the Live
    session. A flapping Bluetooth device hits this path constantly, so a refresh
    here would turn a blinking headset into a dead session: strictly worse than
    the silent speaker it was meant to fix."""
    from app.era_live import audio_output as ao

    def _boom():
        raise AssertionError("the stream layer must never re-enumerate PortAudio")

    monkeypatch.setattr(vo, "refresh_devices", _boom)
    monkeypatch.setattr(vo, "refresh_between_sessions", _boom)

    opened = []

    def opener(device):
        if device == GLASS_SD_INDEX:
            raise OSError("MME error 6: there is no driver installed on your system")
        return _FakeStream(device, opened)

    out = ao.ResilientOutputStream(opener, preferred_device=GLASS_SD_INDEX)
    assert out.open()
    assert opened == [None] and out.on_fallback  # fell back, quietly, alive


def test_flapping_device_recovers_by_retrying_its_own_index():
    """A device present at Pa_Initialize keeps its index across a
    disconnect/reconnect, so the reopen needs no re-enumeration at all - just
    the preferred-first candidate order. This is the real-world Bluetooth flap."""
    from app.era_live.audio_output import ResilientOutputStream

    opened, missing = [], set()

    def opener(device):
        if device in missing:
            raise OSError("MME error 6")
        return _FakeStream(device, opened)

    out = ResilientOutputStream(opener, preferred_device=GLASS_SD_INDEX, sleep=_nosleep)
    assert out.open()
    assert opened == [GLASS_SD_INDEX]

    missing.add(GLASS_SD_INDEX)  # glasses drop mid-sentence
    out._drop_stream()
    assert asyncio.run(out.write(b"\x00" * 8)) is True
    assert opened[-1] is None and out.on_fallback  # laptop carries the voice

    missing.clear()  # glasses come back on their own
    out.reclaim_preferred()
    assert asyncio.run(out.write(b"\x00" * 8)) is True
    assert opened[-1] == GLASS_SD_INDEX and not out.on_fallback


def test_a_flapping_device_is_reported_once_not_once_per_chunk(capsys):
    """MME error 6 arrives on every buffer while the device is gone. Logging it
    each time made one blinking headset read like a cascade of failures."""
    from app.era_live.audio_output import ResilientOutputStream

    out = ResilientOutputStream(lambda d: _FakeStream(d, []), sleep=_nosleep)
    out.open()
    capsys.readouterr()

    class _Dead:
        def write(self, chunk):
            raise OSError("MME error 6")

        def stop(self):
            pass

        def close(self):
            pass

    for _ in range(5):
        out._stream = _Dead()
        asyncio.run(out.write(b"\x00" * 8))
    assert capsys.readouterr().out.count("output device lost") == 1


def test_a_recovered_write_re_arms_the_outage_report():
    """A SECOND, later outage must still be reported - the once-only guard is
    per episode, not per process."""
    from app.era_live.audio_output import ResilientOutputStream

    out = ResilientOutputStream(lambda d: _FakeStream(d, []), sleep=_nosleep)
    out.open()
    out._lost_logged = True
    assert asyncio.run(out.write(b"\x00" * 8)) is True
    assert out._lost_logged is False


def test_still_falls_back_when_the_device_is_genuinely_gone():
    """A real unplug still costs the routing, never the voice."""
    from app.era_live.audio_output import ResilientOutputStream

    opened = []

    def opener(device):
        if device == GLASS_SD_INDEX:
            raise OSError("really gone")
        return _FakeStream(device, opened)

    out = ResilientOutputStream(opener, preferred_device=GLASS_SD_INDEX)
    assert out.open()
    assert opened == [None] and out.on_fallback


# ── the between-sessions refresh (the one safe re-enumeration) ───────────────


def test_between_sessions_refresh_only_fires_when_it_would_help(world, monkeypatch):
    calls = []
    monkeypatch.setattr(vo, "refresh_devices", lambda: calls.append(1) or True)

    # No pin: nothing to look for.
    assert vo.refresh_between_sessions() is False

    vo.set_pin("glasses")
    # Pinned, connected, and the engine can already see it: no need.
    monkeypatch.setattr(vo, "_lookup", lambda name: GLASS_SD_INDEX)
    assert vo.refresh_between_sessions() is False

    # Pinned and connected, but invisible to the engine: THIS is the case.
    monkeypatch.setattr(vo, "_lookup", lambda name: None)
    assert vo.refresh_between_sessions() is True
    assert len(calls) == 1

    # Pinned but genuinely unplugged: re-enumerating cannot conjure it back.
    world.remove(GLASS)
    assert vo.refresh_between_sessions() is False
    assert len(calls) == 1


def test_reclaims_the_device_when_it_comes_back_mid_session():
    """Nothing FAILS while the laptop is playing fine, so without an explicit
    poll a reconnected device was never taken back and the pin looked dead."""
    from app.era_live.audio_output import ResilientOutputStream

    opened, missing = [], {GLASS_SD_INDEX}

    def opener(device):
        if device in missing:
            raise OSError("gone")
        return _FakeStream(device, opened)

    out = ResilientOutputStream(opener, preferred_device=GLASS_SD_INDEX)
    assert out.open()
    assert opened == [None] and out.on_fallback

    missing.clear()  # the glasses reconnect
    out.reclaim_preferred()
    assert asyncio.run(out.write(b"\x00" * 8)) is True
    assert opened[-1] == GLASS_SD_INDEX and not out.on_fallback


def test_reclaim_is_a_no_op_when_not_on_fallback():
    """It must not churn a healthy stream every poll."""
    from app.era_live.audio_output import ResilientOutputStream

    opened = []
    out = ResilientOutputStream(lambda d: _FakeStream(d, opened), preferred_device=GLASS_SD_INDEX)
    assert out.open()
    out.reclaim_preferred()
    out.reclaim_preferred()
    assert opened == [GLASS_SD_INDEX]  # opened once, never re-opened


def test_connected_check_asks_core_audio_not_the_stale_engine(world):
    """'Truly disconnected' is a Core Audio question. Asking PortAudio would
    answer 'gone' for a device that is sitting there connected."""
    vo.set_pin("glasses")
    assert vo.pinned_device_connected() is True
    world.remove(GLASS)
    assert vo.pinned_device_connected() is False


def test_slow_reclaim_probe_never_stalls_playback(monkeypatch):
    """The device probe is a pycaw COM enumeration (~117 ms measured). Awaiting
    it on the play loop would stutter the very audio it exists to route, so it
    runs off-loop. Here it takes a full second: playback must not notice."""
    import sounddevice as sd
    from test_audio_output import _fake_era

    from app.era_live.audio import AudioIOMixin

    played = []

    class _Stream:
        def __init__(self, dev):
            self.dev = dev

        def start(self):
            if self.dev == GLASS_SD_INDEX:
                raise OSError("MME error 6")  # forces the fallback

        def write(self, chunk):
            played.append(chunk)

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sd, "RawOutputStream", lambda **kw: _Stream(kw["device"]))
    monkeypatch.setattr(vo, "get_pin", lambda: {"id": "x", "name": GLASS.name})
    monkeypatch.setattr(vo, "resolve_output_index", lambda: GLASS_SD_INDEX)
    monkeypatch.setattr(vo, "generation", lambda: 0)

    import time as _time

    def _slow_probe():
        _time.sleep(1.0)  # far longer than any acceptable audio gap
        return False

    monkeypatch.setattr(vo, "pinned_device_connected", _slow_probe)

    chunks = [bytes([i % 251]) * 64 for i in range(40)]

    async def main():
        era = _fake_era([])
        task = asyncio.create_task(AudioIOMixin._play_audio(era))
        started = _time.monotonic()
        for c in chunks:
            await era.audio_in_queue.put(c)
            await asyncio.sleep(0.01)
        elapsed = _time.monotonic() - started
        for _ in range(300):
            await asyncio.sleep(0.01)
            if era.audio_in_queue.empty():
                break
        await asyncio.sleep(0.2)
        era._go_away_event.set()
        await asyncio.wait_for(task, timeout=5)
        return elapsed

    asyncio.run(main())
    # Every chunk reached the speaker despite a 1s probe running alongside.
    assert len(played) == len(chunks)


# ── battery telemetry ────────────────────────────────────────────────────────


def test_battery_matches_the_sibling_node_that_carries_the_level(monkeypatch):
    """The endpoint is 'Star glass (CY03-CB36)'; the battery lives on a device
    called 'CY03-CB36 Hands-Free AG'. Only the shared model token links them."""
    import core.audio_battery as ab

    monkeypatch.setattr(
        ab,
        "battery_devices",
        lambda: [
            {"name": "iPhone", "percent": 94},
            {"name": "CY03-CB36 Hands-Free AG", "percent": 80},
        ],
    )
    assert ab.battery_for("Star glass (CY03-CB36)")["percent"] == 80
    assert ab.battery_for("pc - speakers (Realtek(R) Audio)") is None


def test_battery_refuses_to_guess_between_two_equal_matches(monkeypatch):
    import core.audio_battery as ab

    monkeypatch.setattr(
        ab,
        "battery_devices",
        lambda: [
            {"name": "Buds Left", "percent": 40},
            {"name": "Buds Right", "percent": 90},
        ],
    )
    assert ab.battery_for("Buds") is None


def test_battery_role_words_are_not_hardware_identity(monkeypatch):
    """'Headset' / 'Hands-Free' describe a ROLE. If they counted as identity,
    every headset on the machine would match every other one's battery."""
    import core.audio_battery as ab

    assert ab.hardware_tokens("Casque (CY03-CB36 Hands-Free)") == {"cy03", "cb36"}


def test_battery_reports_the_number_or_says_it_has_none(world, monkeypatch):
    import core.audio_battery as ab

    monkeypatch.setattr(ab, "battery_devices", lambda: [{"name": "CY03-CB36 AG", "percent": 80}])
    assert "80%" in audio_control({"action": "battery", "device": "glasses"})
    assert "doesn't report" in audio_control({"action": "battery", "device": "pc - speakers"})


# ── the route that actually opened, vs the pin (2026-09-25) ──────────────────
# "Pinned to your glasses, sir" while the sound came out of the laptop: the pin
# is an intention, the open stream is the outcome, and status() used to report
# only the first. PortAudio's frozen device list is what makes them diverge.


@pytest.fixture(autouse=True)
def _forget_route(monkeypatch):
    """Each test starts with no recorded route - the state is module-global."""
    monkeypatch.setattr(vo, "_active", None)


def test_active_route_starts_unknown_and_records_what_opened():
    assert vo.active_route() is None
    vo.note_active_route(4, fallback=False, name="Star glass (CY03-CB36)")
    route = vo.active_route()
    assert route["index"] == 4 and route["fallback"] is False
    assert route["name"] == "Star glass (CY03-CB36)"


def test_active_route_is_a_copy_not_the_live_dict():
    vo.note_active_route(4)
    vo.active_route()["index"] = 999
    assert vo.active_route()["index"] == 4


def test_status_admits_when_the_stream_is_on_the_fallback(world):
    """The complaint this exists for: pinned, connected, and still audible on
    the laptop because the audio engine opened the default device."""
    vo.set_pin("glasses")
    vo.note_active_route(None, fallback=True, name="pc - speakers (Realtek(R) Audio)")
    res = vo.status()
    assert res.ok
    assert "actually coming out of" in res.message
    assert "pc - speakers (Realtek(R) Audio)" in res.message
    assert res.data["routed_as_pinned"] is False


def test_status_stays_quiet_when_the_route_matches_the_pin(world):
    vo.set_pin("glasses")
    vo.note_active_route(GLASS_SD_INDEX, fallback=False, name=GLASS.name)
    res = vo.status()
    assert "actually coming out of" not in res.message
    assert res.data["routed_as_pinned"] is True


def test_no_route_recorded_yet_makes_no_claim_either_way(world):
    """Before any playback there is nothing to contradict - say nothing."""
    vo.set_pin("glasses")
    assert "actually coming out of" not in vo.status().message


def test_an_unpinned_voice_never_reports_a_mismatch(world):
    """With no pin there is no intention to violate, however the stream opened."""
    vo.note_active_route(None, fallback=True, name="pc - speakers")
    assert vo._route_disagrees_with_pin(None) == ""


def test_a_resolved_index_of_none_is_itself_a_mismatch(world):
    """The real failure mode: PortAudio cannot see the pinned device, so the
    stream opens on the default WITHOUT any error - fallback flag unset."""
    vo.set_pin("glasses")
    vo.note_active_route(None, fallback=False, name="")
    assert "actually coming out of" in vo.status().message
