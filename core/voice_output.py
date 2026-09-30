"""Where Era's SPOKEN VOICE plays - the single source of truth.

This is the universal, user-selectable replacement for the old two-case rule
("glasses if connected, else the laptop"). Any output endpoint on the machine
can be pinned by name, the pin survives a restart, and it is independent of the
Windows default device.

The decision, in order:

1. **An explicit pin** (``set_pin``) - the user named a device. It wins over
   everything, including the system default, and it is the ONLY thing that can
   move Era's voice. It takes effect on the live stream immediately: a pin bumps
   ``generation()``, which ``_play_audio`` watches and re-opens on.
2. **A pin whose device is not connected** -> the system default. A pinned
   device that vanished must cost the utterance's ROUTING, never the utterance.
   The pin is NOT erased: it reclaims the device when it comes back.
3. **No pin** -> the Star Glasses when connected, else the system default. The
   historical behaviour, kept as the default so nothing changes for a user who
   never picks a device.

What this module deliberately does NOT do: touch the Windows default device.
Era's voice and media playback are separate concerns - pinning her voice to the
glasses must leave Spotify exactly where the user put it, and conversely, the
user switching Spotify to the speakers must not drag Era's voice along. That
independence is the whole point of routing at the STREAM (PortAudio device
index) rather than by changing a system-wide setting.

Degrade, never raise: every public function answers, and any probe failure
resolves to "the system default" rather than to silence.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from datetime import datetime

from core.audio_endpoints import OUTPUT, AudioResult

PREF_KEY = "voice_output_device"

_lock = threading.Lock()
_generation = 0


# ── live re-route signal ─────────────────────────────────────────────────────


def generation() -> int:
    """Bumped whenever the pin changes. ``_play_audio`` polls this to re-route
    an OPEN stream, which is what makes 'speak on my glasses' take effect on the
    current sentence instead of after a restart."""
    with _lock:
        return _generation


def _bump() -> int:
    global _generation
    with _lock:
        _generation += 1
        return _generation


# ── where the voice ACTUALLY opened ─────────────────────────────────────────
# The pin is an intention; this is the outcome. They diverge for one real
# reason: PortAudio freezes its device list at Pa_Initialize, so a device that
# connected after Era started is invisible to the audio engine even though
# Windows lists it live - the pin then resolves to None and the stream opens on
# the default device. Reporting only the pin made that look like success while
# the sound came out of the laptop, which is the whole complaint this records.

_active: dict | None = None


def note_active_route(device_index, *, fallback: bool = False, name: str = "") -> None:
    """Called by the playback layer each time it opens an output stream."""
    global _active
    with _lock:
        _active = {
            "index": device_index,
            "fallback": bool(fallback),
            "name": name,
            "at": datetime.now().isoformat(timespec="seconds"),
        }


def active_route() -> dict | None:
    """The last stream the playback layer actually opened, or None if it never has."""
    with _lock:
        return dict(_active) if _active else None


def _route_disagrees_with_pin(pin: dict | None) -> str:
    """A sentence naming the divergence, or '' when the route matches intent."""
    route = active_route()
    if pin is None or route is None:
        return ""
    if not route.get("fallback") and route.get("index") is not None:
        return ""
    where = route.get("name") or "the system default output"
    return (
        f" I have to be straight with you though: my voice is actually coming out of "
        f"{where} right now, not {pin.get('name')} - my audio engine opened the "
        "fallback. It should reclaim the device on my next reconnect."
    )


# ── persistence ──────────────────────────────────────────────────────────────


def get_pin() -> dict | None:
    """The stored pin ({'id', 'name', 'set_at', 'voice_lock'}), or None."""
    try:
        from core.user_prefs import load_prefs

        pin = load_prefs().get(PREF_KEY)
    except Exception as e:
        print(f"[VoiceOutput] pin read failed: {type(e).__name__}: {e}")
        return None
    if isinstance(pin, dict) and (pin.get("id") or pin.get("name")):
        return pin
    return None


def _store(pin: dict | None) -> bool:
    """Read-modify-write the pin under the prefs lock.

    Held across the whole cycle, like every other writer in core.user_prefs:
    load-then-save with the lock released in between loses a concurrent write,
    and the other writers here are the mic-mute and voice-lock toggles - both
    reachable from a different thread in the same breath as a routing command.
    """
    try:
        from core import user_prefs

        with user_prefs._LOCK:
            data = user_prefs._load_unlocked()
            if pin is None:
                data.pop(PREF_KEY, None)
            else:
                data[PREF_KEY] = pin
            user_prefs._save(data)
        return True
    except Exception as e:
        print(f"[VoiceOutput] pin write failed: {type(e).__name__}: {e}")
        return False


# ── endpoint  ->  PortAudio device index ─────────────────────────────────────


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def _name_score(endpoint_name: str, sd_name: str) -> int:
    """How well a PortAudio device name matches a Core Audio endpoint name.

    3 = identical, 2 = one is a prefix of the other, 1 = substring, 0 = no match.

    The prefix rule is not a nicety: PortAudio's MME host API truncates device
    names to 31 characters, so the endpoint Windows calls
    ``pc - speakers (Realtek(R) Audio)`` is enumerated as
    ``pc - speakers (Realtek(R) Audio`` - an exact-match lookup finds nothing
    and Era's voice silently falls back to the default device.
    """
    a, b = _norm(endpoint_name), _norm(sd_name)
    if not a or not b:
        return 0
    if a == b:
        return 3
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 6:  # too short to identify anything; "casque" would match all
        return 0
    if long.startswith(short):
        return 2
    return 1 if short in long else 0


def _preferred_hostapi(sd) -> int | None:
    """The host API PortAudio itself uses for the default output.

    Picking the SAME one keeps a pinned stream behaving like the fallback
    stream, instead of quietly switching Era between MME and WASAPI depending
    on which device she was pinned to.
    """
    try:
        out = sd.default.device[1]
        if out is None or out < 0:
            return None
        return int(sd.query_devices(out)["hostapi"])
    except Exception:
        return None


# PortAudio builds its device list ONCE, during Pa_Initialize, and never
# refreshes it. sounddevice adds no cache of its own, so a device that connects
# after Era starts - every Bluetooth reconnect - is invisible to the audio
# engine while Windows and pycaw both show it live. That is the whole "the
# glasses are connected but I can't stream to them" failure. Pa_Terminate +
# Pa_Initialize re-enumerates, measured at ~70 ms on this machine.
_REFRESH_MIN_INTERVAL_S = 2.0
_last_refresh = 0.0


def _under_pytest() -> bool:
    import os
    import sys

    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def refresh_devices() -> bool:
    """Re-enumerate PortAudio's device list. True if the refresh ran.

    DANGEROUS WITH ANY OPEN STREAM. ``Pa_Terminate`` is process-wide: it turns
    every stream this process holds into an invalid pointer (PaErrorCode -9988,
    measured). Era's microphone ``InputStream`` is open for the whole session
    and ``_listen_audio`` re-raises, which tears down the Live session - so
    calling this mid-session trades a silent speaker for a dead session.

    The ONE safe caller is ``refresh_between_sessions``, on the reconnect path
    where the previous TaskGroup has unwound and no stream exists yet.
    """
    global _last_refresh
    if _under_pytest():
        # Pa_Terminate is PROCESS-wide. In a test run it would tear the audio
        # engine out from under every other test in the session, including ones
        # that never mention audio. Never real here.
        return False
    with _lock:
        now = time.monotonic()
        if now - _last_refresh < _REFRESH_MIN_INTERVAL_S:
            return False
        _last_refresh = now
    try:
        import sounddevice as sd

        sd._terminate()
        sd._initialize()
        print("[VoiceOutput] re-enumerated PortAudio devices")
        return True
    except Exception as e:  # degrade, never raise: keep the stale list
        print(f"[VoiceOutput] device refresh failed: {type(e).__name__}: {e}")
        return False


def refresh_between_sessions() -> bool:
    """Pick up a device the audio engine has never seen. Returns True if it did.

    Call ONLY from the reconnect loop, after the old TaskGroup has unwound and
    before the new audio tasks start - that is the only moment in Era's life
    with no stream open (see refresh_devices for what happens otherwise).

    Does nothing unless it is actually needed: there must be a pin, the device
    must be connected according to Core Audio, and PortAudio must be unable to
    see it. A device that is merely unplugged is not a reason to re-enumerate.
    """
    try:
        pin = get_pin()
        if pin is None:
            return False
        ep = _live_endpoint(pin)
        if ep is None:
            return False  # genuinely disconnected - nothing to find
        if _lookup(ep.name) is not None:
            return False  # the engine can already see it
        print(
            f"[VoiceOutput] {ep.name} is connected but missing from the audio "
            "engine's device list; re-enumerating."
        )
        return refresh_devices()
    except Exception as e:  # degrade, never raise: a session must still start
        print(f"[VoiceOutput] between-session refresh skipped: {type(e).__name__}: {e}")
        return False


def sd_index_for_name(device_name: str, refresh_on_miss: bool = False):
    """PortAudio output index for an endpoint name, or None when not found.

    The same hardware appears once per host API (MME, DirectSound, WASAPI ...).
    That is not ambiguity - it is one device seen three ways - so the best-named
    match wins and the preferred host API breaks the tie.

    ``refresh_on_miss`` re-enumerates and looks again when nothing matched. Only
    pass it where no output stream is open (see ``refresh_devices``).
    """
    index = _lookup(device_name)
    if index is None and refresh_on_miss and refresh_devices():
        index = _lookup(device_name)
    return index


def _lookup(device_name: str):
    if not (device_name or "").strip():
        return None
    try:
        import sounddevice as sd

        devices = list(sd.query_devices())
    except Exception as e:
        print(f"[VoiceOutput] device enumeration failed: {type(e).__name__}: {e}")
        return None

    want_api = _preferred_hostapi(sd)
    best = None  # (score, api_rank, -index)
    best_index = None
    for idx, dev in enumerate(devices):
        try:
            if int(dev.get("max_output_channels", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        score = _name_score(device_name, str(dev.get("name", "")))
        if not score:
            continue
        api_rank = 1 if (want_api is not None and dev.get("hostapi") == want_api) else 0
        key = (score, api_rank, -idx)
        if best is None or key > best:
            best, best_index = key, idx
    return best_index


def _live_endpoint(pin: dict):
    """The pinned endpoint if it is currently connected, else None.

    Matched by endpoint id first (stable across reconnects) and by name second,
    so a device that Windows re-enumerates under a new id is still recognised.
    """
    try:
        from core.audio_endpoints import enumerate_endpoints

        eps = enumerate_endpoints(OUTPUT)
    except Exception as e:
        print(f"[VoiceOutput] endpoint probe failed: {type(e).__name__}: {e}")
        return None
    pin_id = str(pin.get("id") or "").lower()
    if pin_id:
        for ep in eps:
            if ep.id.lower() == pin_id:
                return ep
    pin_name = _norm(pin.get("name") or "")
    if pin_name:
        for ep in eps:
            if _norm(ep.name) == pin_name:
                return ep
    return None


# ── the decision ─────────────────────────────────────────────────────────────


def resolve_output_index():
    """The PortAudio output index Era's voice should use, or None for "default".

    None is a real answer, not a failure: it hands the stream to the system
    default output, which is both the no-pin fallback and the answer when a
    pinned device is unplugged.
    """
    pin = get_pin()
    if pin:
        ep = _live_endpoint(pin)
        if ep is None:
            print(
                f"[VoiceOutput] pinned device {pin.get('name')!r} is not connected; "
                "using the system default until it returns."
            )
            return None
        index = sd_index_for_name(ep.name)
        if index is None:
            print(f"[VoiceOutput] no PortAudio device for {ep.name!r}; using the system default.")
        return index

    # No pin: the historical auto-route (glasses when present).
    try:
        from core import audio_devices

        return audio_devices.glass_output_index()
    except Exception as e:
        print(f"[VoiceOutput] glass probe failed: {type(e).__name__}: {e}")
        return None


# ── the tool surface ─────────────────────────────────────────────────────────


def pinned_device_connected() -> bool:
    """Is the pinned device physically present RIGHT NOW?

    Answered from Core Audio, which is live, never from PortAudio, whose device
    list is a snapshot from startup. This is the "truly disconnected" test: the
    playback loop uses it while running on the fallback device to decide whether
    the chosen device has come back and should be reclaimed.
    """
    pin = get_pin()
    return pin is not None and _live_endpoint(pin) is not None


def needs_reenumeration() -> bool:
    """True when the pinned device is physically connected but the audio engine
    can't route to it — the signature of PortAudio's frozen device list missing a
    device that connected AFTER Era started.

    This is the ONE reconnect case the in-session reclaim loop cannot fix: with
    no PortAudio index there is nothing to reopen onto, and re-enumeration is
    process-wide and unsafe while the microphone stream is open (see
    ``refresh_devices``). The playback loop uses this to schedule a
    between-session refresh (a reconnect) at the next idle moment, where
    ``refresh_between_sessions`` re-enumerates and the fresh stream binds the
    device.

    False for a device that was present at startup (it keeps its index across a
    disconnect/reconnect, so the normal reclaim handles it) and for one that is
    genuinely disconnected (nothing to reclaim).
    """
    pin = get_pin()
    if pin is None or _live_endpoint(pin) is None:
        return False
    return resolve_output_index() is None


def _voice_lock_active() -> bool:
    """Is the owner-only voice gate on? Recorded WITH the pin so the routing
    decision carries the authority it was made under (see status())."""
    try:
        from core.user_prefs import get_voice_lock_enabled

        return bool(get_voice_lock_enabled())
    except Exception:
        return False


def set_pin(device: str) -> AudioResult:
    """Pin Era's voice to one named output device. Takes effect immediately.

    Resolution goes through ``audio_endpoints.resolve``, so this inherits the
    rule that matters most: a name matching several devices ASKS rather than
    picking one. Nothing about the system default is touched - media playback
    stays exactly where the user put it.
    """
    from core.audio_endpoints import resolve

    ep, err = resolve(device, OUTPUT)
    if err:
        return err

    # Core Audio has already confirmed this endpoint is live. PortAudio may
    # still be blind to it - its device list is frozen at Pa_Initialize, so a
    # device that connected after Era started is missing until we re-enumerate.
    # That is a transient view, NOT a reason to refuse: this used to answer "it
    # isn't exposed to my audio engine" and throw the user's choice away over a
    # stale snapshot. Pin regardless of what the snapshot says.
    #
    # NOT refresh_on_miss: this runs on a tool-call thread while the microphone
    # stream is open, and Pa_Terminate would invalidate it. The re-enumeration
    # happens between sessions instead (refresh_between_sessions).
    index = sd_index_for_name(ep.name)

    pin = {
        "id": ep.id,
        "name": ep.name,
        "set_at": datetime.now().isoformat(timespec="seconds"),
        "voice_lock": _voice_lock_active(),
    }
    if not _store(pin):
        return AudioResult(False, "I couldn't save that voice-output choice, sir.")
    _bump()
    if index is None:
        # Pinned, but the audio engine still cannot see the device. Say so
        # plainly rather than promising sound that will not arrive.
        return AudioResult(
            True,
            f"My voice is pinned to {ep.name}, sir. My audio engine can't see it yet "
            "even though Windows can - I'll take it the moment it appears, and use "
            "the laptop until then.",
            {"device": {"id": ep.id, "name": ep.name}, "sd_index": None, "changed": True},
        )
    return AudioResult(
        True,
        f"My voice now plays on {ep.name}, sir - from this sentence on, and after a "
        "restart. Your media routing is unchanged.",
        {"device": {"id": ep.id, "name": ep.name}, "sd_index": index, "changed": True},
    )


def clear_pin() -> AudioResult:
    """Drop the pin: back to the glasses-when-connected default."""
    pin = get_pin()
    if pin is None:
        return AudioResult(
            True,
            "My voice already follows the default routing, sir - the glasses when "
            "they're connected, the laptop otherwise.",
            {"changed": False},
        )
    if not _store(None):
        return AudioResult(False, "I couldn't clear that voice-output choice, sir.")
    _bump()
    return AudioResult(
        True,
        f"My voice is no longer pinned to {pin.get('name') or 'that device'}, sir - "
        "back to the glasses when they're connected, the laptop otherwise.",
        {"changed": True, "was": pin},
    )


def status() -> AudioResult:
    """What Era's voice is doing right now, and why."""
    pin = get_pin()
    if pin is None:
        try:
            from core import audio_devices

            st = audio_devices.probe_glass()
        except Exception:
            st = {"connected": False, "name": ""}
        where = (
            f"the {st.get('name') or 'Star Glasses'} (auto-routed while they're connected)"
            if st.get("connected")
            else "the system default output"
        )
        return AudioResult(
            True,
            f"My voice isn't pinned, sir - it's on {where}. Name a device and I'll pin it.",
            {"pinned": None, "auto": True},
        )

    ep = _live_endpoint(pin)
    lock = " Voice recognition was on when you set it." if pin.get("voice_lock") else ""
    if ep is None:
        return AudioResult(
            True,
            f"My voice is pinned to {pin.get('name')}, sir, but it isn't connected - "
            f"I'm on the system default until it's back.{lock}",
            {"pinned": pin, "connected": False},
        )
    # The pin says where the voice SHOULD go; active_route() is where the stream
    # actually opened. Reporting the pin alone is what made a silent fallback to
    # the laptop read as "pinned to your glasses, sir".
    mismatch = _route_disagrees_with_pin(pin)
    return AudioResult(
        True,
        f"My voice plays on {ep.name}, sir - pinned, so media routing "
        f"doesn't move it.{lock}{mismatch}",
        {
            "pinned": pin,
            "connected": True,
            "device": {"id": ep.id, "name": ep.name},
            "active_route": active_route(),
            "routed_as_pinned": not mismatch,
        },
    )


def summary_line() -> str:
    """One line for the system prompt (see audio_devices.routing_summary)."""
    pin = get_pin()
    if pin is None:
        return ""
    name = pin.get("name") or "a chosen device"
    connected = _live_endpoint(pin) is not None
    where = name if connected else f"{name} (not connected - on the system default meanwhile)"
    return (
        f"VOICE OUTPUT PINNED - your spoken voice plays on {where}, chosen by the user "
        "and independent of the system default. It does NOT move when media routing "
        "changes. Only an explicit request ('speak on X', 'stop pinning your voice') "
        "changes it: audio_control(action='set_voice_output', device='X') or "
        "action='clear_voice_output'."
    )
