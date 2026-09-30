"""Per-endpoint audio control for Era (Windows Core Audio via pycaw).

Two rules shape everything here:

* NEVER GUESS, NEVER GO STALE. Every operation re-enumerates the endpoints and
  re-reads volume/mute from the hardware; nothing is cached between calls. A
  write is verified by reading the value back and the reply reports what the
  device says, not what we asked for. Muting is an explicit set (mute / unmute),
  never a blind toggle, unless the caller literally asked to toggle - and then
  the toggle is computed from the state we just read.
* NEVER PICK A DEVICE THE USER DIDN'T NAME. A name that matches several
  endpoints returns the candidates and asks; it does not choose one.

"system" means the current default render endpoint (the one the Windows volume
keys drive) - resolved fresh every call, because the default changes when a
device connects. Routing policy (a bare "volume up" is SYSTEM, only a named
device redirects) lives in actions/audio_control.py and computer_settings.

Degrade, never raise: every public function returns an ``AudioResult``.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field

OUTPUT = "output"
INPUT = "input"
_FLOW_VALUE = {OUTPUT: 0, INPUT: 1}  # EDataFlow: eRender=0, eCapture=1
_FLOW_LABEL = {OUTPUT: "output", INPUT: "input"}

# PKEY_AudioEndpoint_FormFactor ({1DA5D803-...} pid 0). Locale-proof, unlike names
# ("Haut-parleurs", "Casque"), so "the speakers" resolves on a French install.
_FORM_FACTOR_KEY = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"
_KIND_BY_FORM_FACTOR = {
    1: "speakers",
    2: "line-level",
    3: "headphones",
    4: "microphone",
    5: "headset",
    6: "handset",
    8: "s/pdif",
    9: "display audio",
}

# Words that name a whole CLASS of device rather than one device.
_KIND_WORDS = {
    "speaker": {"speakers"},
    "speakers": {"speakers"},
    "headphone": {"headphones", "headset"},
    "headphones": {"headphones", "headset"},
    "headset": {"headset", "headphones"},
    "earbuds": {"headphones", "headset"},
    "earphones": {"headphones", "headset"},
}
_INPUT_WORDS = {"mic", "microphone", "input", "recording", "capture"}
_OUTPUT_WORDS = {"output", "playback"}
_GLASS_WORDS = {"glasses", "glass", "star glass", "star glasses", "smart glasses", "my glasses"}
# Names that mean "the system mixer" - handled by callers, never looked up as a device.
SYSTEM_WORDS = frozenset(
    {"system", "default", "master", "computer", "pc", "global", "main", "current", "mixer"}
)


@dataclass
class AudioResult:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)


@dataclass
class Endpoint:
    id: str
    name: str
    flow: str
    kind: str
    dev: object = field(repr=False, compare=False, default=None)


# ── COM plumbing ─────────────────────────────────────────────────────────────

_tls = threading.local()


def _ensure_com() -> None:
    """Tool calls run on executor threads; COM must be initialised on each one."""
    if getattr(_tls, "ready", False):
        return
    try:
        import comtypes

        comtypes.CoInitialize()
    except OSError:
        pass  # thread already initialised with a different apartment model - usable
    except Exception:
        pass
    _tls.ready = True


def _pycaw():
    _ensure_com()
    from pycaw.constants import DEVICE_STATE, ERole
    from pycaw.utils import AudioUtilities

    return AudioUtilities, DEVICE_STATE, ERole


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def _make(d, flow: str) -> Endpoint:
    """Wrap a pycaw AudioDevice. Kind comes from the form factor, not the name."""
    ff = (getattr(d, "properties", None) or {}).get(_FORM_FACTOR_KEY)
    kind = _KIND_BY_FORM_FACTOR.get(ff, "microphone" if flow == INPUT else "output")
    return Endpoint(id=str(d.id), name=str(d.FriendlyName or d.id), flow=flow, kind=kind, dev=d)


def enumerate_endpoints(flow: str | None = None) -> list[Endpoint]:
    """Fresh list of ACTIVE endpoints (a disabled/unplugged device is not listed)."""
    AudioUtilities, DEVICE_STATE, _ = _pycaw()
    flows = [flow] if flow in _FLOW_VALUE else [OUTPUT, INPUT]
    out: list[Endpoint] = []
    for fl in flows:
        for d in AudioUtilities.GetAllDevices(_FLOW_VALUE[fl], DEVICE_STATE.ACTIVE.value):
            try:
                out.append(_make(d, fl))
            except Exception:
                continue
    return out


_seen_notices: set = set()


def _log_once(key, message: str) -> None:
    """A masked default is a steady state, not an event - say it once per change."""
    if key in _seen_notices:
        return
    _seen_notices.add(key)
    if len(_seen_notices) > 64:  # a flapping device must not grow this forever
        _seen_notices.clear()
    print(message)


def _api_default_ids(flow: str) -> dict[str, str | None]:
    """What the endpoint API says the defaults are - app override and all."""
    AudioUtilities, _, ERole = _pycaw()
    enum = AudioUtilities.GetDeviceEnumerator()
    ids: dict[str, str | None] = {}
    for role_name, role in (
        ("console", ERole.eConsole),
        ("multimedia", ERole.eMultimedia),
        ("communications", ERole.eCommunications),
    ):
        try:
            ids[role_name] = str(
                enum.GetDefaultAudioEndpoint(_FLOW_VALUE[flow], role.value).GetId()
            )
        except Exception:
            ids[role_name] = None  # no default device for that role
    return ids


def _undo_app_override(flow: str, ids: dict[str, str | None]) -> dict[str, str | None]:
    """Replace app-overridden answers with the real system defaults.

    Windows rewrites GetDefaultAudioEndpoint for a process that has a per-app
    device preference, so from inside Era the pinned device LOOKS like the
    system default - which is why "make the speakers default" read back as a
    refusal and why a bare "volume up" drove the glasses. Only a pin on a
    CONNECTED endpoint is actually in force; without one this returns ``ids``
    untouched, so a machine with no preference behaves exactly as before.
    """
    try:
        from core import audio_policy as ap

        pins = ap.pinned_endpoint_guids(flow)
        if not pins:
            return ids
        live = [e.id for e in enumerate_endpoints(flow)]
        by_guid = {ap.guid_tail(i): i for i in live}
        if not any(p in by_guid for p in pins):
            return ids  # pinned device isn't connected - the override is dormant
        fixed = dict(ids)
        for role in fixed:
            guid = ap.ranked_default_guid(flow, role, live)
            if guid and guid in by_guid and by_guid[guid] != fixed[role]:
                _log_once(
                    (flow, role, fixed[role], by_guid[guid]),
                    f"[AudioEndpoints] {flow} {role}: app preference masked the system "
                    f"default; using {by_guid[guid]} instead of {fixed[role]}",
                )
                fixed[role] = by_guid[guid]
        return fixed
    except Exception as e:  # degrade, never raise: keep the API's answer
        print(f"[AudioEndpoints] app-override check failed: {type(e).__name__}: {e}")
        return ids


def default_ids(flow: str) -> dict[str, str | None]:
    """{'console': id, 'multimedia': id, 'communications': id} for a flow.

    These are the SYSTEM defaults - not this process's app-pinned view of them.
    """
    return _undo_app_override(flow, _api_default_ids(flow))


def _norm_id(i: str | None) -> str:
    return (i or "").lower()


def read_state(ep: Endpoint) -> tuple[int, bool]:
    """(volume 0-100, muted) read straight from the endpoint, right now."""
    ev = ep.dev.EndpointVolume
    return round(ev.GetMasterVolumeLevelScalar() * 100), bool(ev.GetMute())


# ── Resolution: name/id -> exactly one endpoint, or a question ───────────────


def resolve(query: str, flow: str | None = None) -> tuple[Endpoint | None, AudioResult | None]:
    """Resolve a user's device reference. Returns (endpoint, None) on a unique
    match, else (None, AudioResult explaining why - not found / ambiguous)."""
    q_raw = (query or "").strip()
    if not q_raw:
        return None, AudioResult(False, "Which audio device, sir?")
    q = _norm(q_raw)
    words = set(q.split())

    if flow not in _FLOW_VALUE:
        flow = None
    if flow is None:
        if words & _INPUT_WORDS and not words & _OUTPUT_WORDS:
            flow = INPUT
        elif words & _OUTPUT_WORDS and not words & _INPUT_WORDS:
            flow = OUTPUT

    eps = enumerate_endpoints()
    pool = [e for e in eps if flow is None or e.flow == flow]

    def done(matches: list[Endpoint]):
        if len(matches) == 1:
            return matches[0], None
        return None, _ambiguous(q_raw, matches)

    # 1. exact id
    hit = [e for e in eps if _norm_id(e.id) == _norm_id(q_raw)]
    if hit:
        return done(hit)
    # 2. exact name
    hit = [e for e in pool if _norm(e.name) == q]
    if hit:
        return done(hit)
    # 3. glasses alias (identifiers are configurable; punctuation-insensitive)
    if q in _GLASS_WORDS:
        try:
            from core.audio_devices import _identifiers

            ids = [_norm(i).replace(" ", "") for i in _identifiers()]
        except Exception:
            ids = ["starglass", "cy03cb36"]
        g = [e for e in pool if any(i in _norm(e.name).replace(" ", "") for i in ids)]
        if flow is None and len({e.flow for e in g}) > 1:
            g = [e for e in g if e.flow == OUTPUT]  # "the glasses" = their speakers
        if g:
            return done(g)
        return None, AudioResult(
            False, "The Star Glasses aren't connected, sir.", {"available": _names(eps)}
        )
    # 4. every query word appears in the name (handles "realtek", "array", "cy03")
    tokens = q.split()
    hit = [e for e in pool if all(t in _norm(e.name) for t in tokens)]
    if hit:
        return done(hit)
    # 5. a class of device: "speakers", "headset", "microphone"
    if q in _KIND_WORDS:
        kinds = _KIND_WORDS[q]
        hit = [e for e in pool if e.kind in kinds]
        if hit:
            return done(hit)
    if words & _INPUT_WORDS and len(words) == 1:
        hit = [e for e in eps if e.flow == INPUT]
        if hit:
            return done(hit)

    return None, AudioResult(
        False,
        f"I don't see an audio device called '{q_raw}', sir. Available: {_names(eps)}.",
        {"available": [_public(e) for e in eps]},
    )


def _names(eps: list[Endpoint]) -> str:
    return "; ".join(f"{e.name} ({_FLOW_LABEL[e.flow]})" for e in eps) or "none"


def _public(e: Endpoint) -> dict:
    return {"id": e.id, "name": e.name, "flow": e.flow, "kind": e.kind}


def _ambiguous(q: str, matches: list[Endpoint]) -> AudioResult:
    return AudioResult(
        False,
        f"'{q}' matches more than one device, sir: {_names(matches)}. Which one?",
        {"candidates": [_public(e) for e in matches]},
    )


def _tag(ep: Endpoint) -> str:
    """Short spoken label. Names the flow only when it isn't obvious."""
    return f"{ep.name}" if ep.flow == OUTPUT else f"{ep.name} (input)"


# ── Public operations ────────────────────────────────────────────────────────


def _guard(fn):
    """Degrade-never-raise wrapper with a spoken failure that names the cause."""

    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except ImportError:
            return AudioResult(
                False, "Audio control needs pycaw and comtypes, sir - not installed."
            )
        except Exception as e:  # COMError on a device that vanished mid-call, etc.
            print(f"[AudioEndpoints] {fn.__name__} failed: {type(e).__name__}: {e}")
            return AudioResult(
                False, "I couldn't reach that audio device, sir - it may have just disconnected."
            )

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_guard
def list_devices(flow: str | None = None) -> AudioResult:
    """Every active endpoint with LIVE volume, mute, and default-role flags."""
    if flow not in _FLOW_VALUE:
        flow = None
    eps = enumerate_endpoints(flow)
    if not eps:
        return AudioResult(False, "I don't see any active audio devices, sir.")
    rows: list[dict] = []
    defaults = {fl: default_ids(fl) for fl in ([flow] if flow else [OUTPUT, INPUT])}
    for ep in eps:
        try:
            vol, muted = read_state(ep)
        except Exception:
            vol, muted = None, None  # keep listing the device; say the state is unreadable
        roles = [r for r, i in defaults[ep.flow].items() if _norm_id(i) == _norm_id(ep.id)]
        rows.append({**_public(ep), "volume": vol, "muted": muted, "default_roles": roles})

    def line(r: dict) -> str:
        state = (
            "level unreadable"
            if r["volume"] is None
            else (f"{r['volume']}%{' muted' if r['muted'] else ''}")
        )
        default = " [DEFAULT]" if {"console", "multimedia"} & set(r["default_roles"]) else ""
        return f"{r['name']} ({r['flow']}, {state}){default}"

    outs = [line(r) for r in rows if r["flow"] == OUTPUT]
    ins = [line(r) for r in rows if r["flow"] == INPUT]
    parts = []
    if outs:
        parts.append("Outputs: " + "; ".join(outs))
    if ins:
        parts.append("Inputs: " + "; ".join(ins))
    return AudioResult(True, " | ".join(parts) + ".", {"devices": rows})


@_guard
def device_status(device: str, flow: str | None = None) -> AudioResult:
    ep, err = resolve(device, flow)
    if err:
        return err
    vol, muted = read_state(ep)
    return AudioResult(
        True,
        f"{_tag(ep)} is at {vol}%{' and muted' if muted else ''}, sir.",
        {"device": _public(ep), "volume": vol, "muted": muted},
    )


def _apply_volume(ep: Endpoint, target: int) -> AudioResult:
    """Write a level, then report what the hardware says (not what we asked)."""
    target = max(0, min(100, int(target)))
    ev = ep.dev.EndpointVolume
    was_muted = bool(ev.GetMute())
    # Output devices unmute when the level is raised, like the Windows volume keys.
    # An INPUT is never unmuted as a side effect: opening a microphone must be an
    # explicit request, never a by-product of "turn the mic down".
    if ep.flow == OUTPUT and was_muted and target > 0:
        ev.SetMute(0, None)
    ev.SetMasterVolumeLevelScalar(target / 100.0, None)
    vol, muted = read_state(ep)
    data = {"device": _public(ep), "volume": vol, "muted": muted, "requested": target}
    note = " (still muted)" if muted else ""
    if abs(vol - target) > 2:
        # Some drivers quantise. Report the truth rather than claiming success.
        return AudioResult(
            True, f"{_tag(ep)} is at {vol}% - it wouldn't go to {target}%, sir.{note}", data
        )
    return AudioResult(True, f"{_tag(ep)} set to {vol}%{note}, sir.", data)


@_guard
def set_device_volume(device: str, value: int, flow: str | None = None) -> AudioResult:
    ep, err = resolve(device, flow)
    return err or _apply_volume(ep, value)


@_guard
def step_device_volume(device: str, delta: int, flow: str | None = None) -> AudioResult:
    ep, err = resolve(device, flow)
    if err:
        return err
    vol, _ = read_state(ep)  # relative change is computed from a FRESH read
    return _apply_volume(ep, vol + int(delta))


def _apply_mute(ep: Endpoint, muted: bool | None) -> AudioResult:
    """muted=True/False = explicit; None = toggle computed from a fresh read."""
    ev = ep.dev.EndpointVolume
    current = bool(ev.GetMute())
    want = (not current) if muted is None else bool(muted)
    if want != current:
        ev.SetMute(1 if want else 0, None)
    vol, now = read_state(ep)
    data = {"device": _public(ep), "volume": vol, "muted": now}
    if now != want:
        return AudioResult(
            False,
            f"I asked {_tag(ep)} to {'mute' if want else 'unmute'} but it reports "
            f"{'muted' if now else 'unmuted'}, sir.",
            data,
        )
    if muted is not None and want == current:
        return AudioResult(
            True, f"{_tag(ep)} was already {'muted' if now else 'unmuted'}, sir.", data
        )
    return AudioResult(
        True, f"{_tag(ep)} is now {'muted' if now else 'unmuted'} ({vol}%), sir.", data
    )


@_guard
def set_device_mute(device: str, muted: bool | None, flow: str | None = None) -> AudioResult:
    ep, err = resolve(device, flow)
    return err or _apply_mute(ep, muted)


_ROLE_SETS = {
    # What Windows' own "Set as Default Device" does. The communications default
    # is deliberately left alone: people split it on purpose (media on one device,
    # calls on another) and a silent overwrite would undo that.
    "default": ("console", "multimedia"),
    "communications": ("communications",),
    "all": ("console", "multimedia", "communications"),
}


@_guard
def set_default_device(device: str, flow: str | None = None, role: str = "default") -> AudioResult:
    """Make an endpoint the default, then verify each role from the OS."""
    role = (role or "default").lower().strip()
    if role in ("call", "calls", "comms", "communication"):
        role = "communications"
    if role not in _ROLE_SETS:
        return AudioResult(False, "The role must be default, communications or all, sir.")
    ep, err = resolve(device, flow)
    if err:
        return err
    AudioUtilities, _, ERole = _pycaw()
    roles = _ROLE_SETS[role]
    what = (
        "communications device" if role == "communications" else f"default {_FLOW_LABEL[ep.flow]}"
    )
    before = default_ids(ep.flow)
    if all(_norm_id(before[r]) == _norm_id(ep.id) for r in roles):
        return AudioResult(
            True,
            f"{_tag(ep)} is already the {what}, sir.",
            {"device": _public(ep), "changed": False},
        )
    role_enum = {"console": ERole.eConsole, "multimedia": ERole.eMultimedia}
    role_enum["communications"] = ERole.eCommunications
    AudioUtilities.SetDefaultDevice(ep.id, [role_enum[r] for r in roles])
    after = default_ids(ep.flow)  # verify from the OS, don't trust the call
    missed = [r for r in roles if _norm_id(after[r]) != _norm_id(ep.id)]
    try:
        from core.audio_devices import invalidate

        invalidate()  # glasses probe must re-read the world
    except Exception:
        pass
    if missed:
        return AudioResult(
            False,
            f"Windows didn't switch {_tag(ep)} for: {', '.join(missed)}, sir.",
            {"device": _public(ep), "not_switched": missed},
        )
    note = ""
    if ep.flow == OUTPUT and role != "communications":
        pinned = app_preference(ep.flow)
        note = (
            f" My own audio still goes to {pinned.name} though - Windows has a per-app "
            "preference pinning me there. Say 'clear my audio app preference' to drop it."
            if pinned is not None and _norm_id(pinned.id) != _norm_id(ep.id)
            else " My own voice follows on my next reconnect."
        )
    return AudioResult(
        True, f"{_tag(ep)} is now the {what}, sir.{note}", {"device": _public(ep), "changed": True}
    )


# -- the per-app device preference Windows applies to THIS process ------------


def app_preference(flow: str = OUTPUT) -> Endpoint | None:
    """The endpoint a Windows app preference pins this process to, or None.

    Only a pin on a connected endpoint is reported, and only when the
    preference names one device unambiguously: a preference naming a device
    that isn't plugged in has no effect, and one that matches two same-model
    devices must not be spoken as if we knew which.
    """
    try:
        from core import audio_policy as ap

        pin = ap.pinned_endpoint_guid(flow)
        if not pin:
            return None
        return next((e for e in enumerate_endpoints(flow) if ap.guid_tail(e.id) == pin), None)
    except Exception as e:
        print(f"[AudioEndpoints] app preference lookup failed: {type(e).__name__}: {e}")
        return None


@_guard
def app_preference_status(flow: str | None = None) -> AudioResult:
    flow = flow if flow in _FLOW_VALUE else OUTPUT
    ep = app_preference(flow)
    if ep is None:
        return AudioResult(
            True,
            f"Nothing is overriding my {_FLOW_LABEL[flow]} - I follow the system default, sir.",
            {"pinned": None},
        )
    return AudioResult(
        True,
        f"Windows pins my {_FLOW_LABEL[flow]} to {ep.name}, whatever the system default is, sir.",
        {"pinned": _public(ep)},
    )


@_guard
def clear_app_preference(flow: str | None = None) -> AudioResult:
    """Drop the app preference, so Era follows the system default again.

    Scoped to the EXECUTABLE, which is the Python interpreter Era shares with
    every other Python program on this machine - so the reply says so rather
    than implying only Era's own routing changed.
    """
    from core import audio_policy as ap

    if flow is not None and flow not in _FLOW_VALUE:
        # Widening a scoped request into "clear everything" would destroy a
        # preference the user never mentioned.
        return AudioResult(False, "Tell me output or input, sir - I won't guess and clear both.")
    removed, errors = ap.clear_app_override(flow)
    if errors and not removed:
        return AudioResult(
            False, "I couldn't clear the audio app preference, sir.", {"errors": errors}
        )
    if not removed:
        return AudioResult(True, "There was no audio app preference to clear, sir.", {"removed": 0})
    where = "" if flow is None else f" {_FLOW_LABEL[flow]}"
    plural = "" if removed == 1 else "s"
    return AudioResult(
        True,
        f"Cleared {removed}{where} app preference{plural} - I follow the system default "
        "from my next stream on, sir. That entry belongs to the Python interpreter, so "
        "any other Python program you'd pinned to a device follows the default too now.",
        {"removed": removed, "errors": errors},
    )


# ── The system mixer = the default render endpoint, resolved fresh ───────────


def _system_endpoint() -> Endpoint | None:
    """The real system output - the endpoint the Windows volume keys drive.

    Resolved through ``default_ids``, so a per-app device preference cannot
    quietly redirect a bare "volume up" onto the pinned device.
    """
    ids = default_ids(OUTPUT)
    want = ids.get("multimedia") or ids.get("console")
    if want:
        for ep in enumerate_endpoints(OUTPUT):
            if _norm_id(ep.id) == _norm_id(want):
                return ep
    AudioUtilities, _, ERole = _pycaw()  # last resort: whatever the API hands us
    enum = AudioUtilities.GetDeviceEnumerator()
    try:
        dev = enum.GetDefaultAudioEndpoint(0, ERole.eMultimedia.value)
    except Exception:
        return None
    return _make(AudioUtilities.CreateDevice(dev), OUTPUT) if dev is not None else None


@_guard
def system_status() -> AudioResult:
    ep = _system_endpoint()
    if ep is None:
        return AudioResult(False, "There's no default audio output right now, sir.")
    vol, muted = read_state(ep)
    return AudioResult(
        True,
        f"Volume is at {vol}%{' (muted)' if muted else ''} on {ep.name}.",
        {"device": _public(ep), "volume": vol, "muted": muted},
    )


@_guard
def system_set_volume(value: int) -> AudioResult:
    ep = _system_endpoint()
    if ep is None:
        return AudioResult(False, "There's no default audio output right now, sir.")
    return _apply_volume(ep, value)


@_guard
def system_step_volume(delta: int) -> AudioResult:
    ep = _system_endpoint()
    if ep is None:
        return AudioResult(False, "There's no default audio output right now, sir.")
    vol, _ = read_state(ep)
    return _apply_volume(ep, vol + int(delta))


@_guard
def system_set_mute(muted: bool | None) -> AudioResult:
    ep = _system_endpoint()
    if ep is None:
        return AudioResult(False, "There's no default audio output right now, sir.")
    return _apply_mute(ep, muted)
