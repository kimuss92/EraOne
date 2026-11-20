"""Where media REALLY plays, and moving it — per app, any app.

2026-10-16: "send the music to my glasses" got "the glasses are already the
default output, the music should be playing there" five times while Spotify kept
playing on the PC speakers. The glasses WERE the default; Spotify (and Edge) were
held on the speakers by Windows' per-app "Output device" setting (Settings ->
Volume mixer), which no set_default can move.

Two truths this module works from, never from the default device alone:

- WHERE: each output endpoint's own audio-session list (IAudioSessionManager2)
  says which process is producing sound on it right now.
- MOVE: Windows' per-app endpoint API, IAudioPolicyConfigFactory::
  SetPersistedDefaultAudioEndpoint — the same call the Volume mixer's per-app
  "Output device" dropdown makes (undocumented; used by EarTrumpet). An empty
  device clears the pin, so the app follows the system default again.

Era's OWN process is never routed here: her voice has its own pin
(core/voice_output.py). Degrade-never-raise: every public function answers.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
import uuid
from ctypes import POINTER, byref, c_void_p
from dataclasses import dataclass

from core import audio_endpoints as ae

_RENDER_IFACE = "{e6327cad-dcec-4949-ae8a-991e976a79d2}"  # KSCATEGORY_AUDIO render
# Windows 10 21H2+ first, then the older layout; both share the vtable below.
_FACTORY_IIDS = ("ab3d4648-e242-459f-b02f-541c70306324", "2a59116d-6c4f-45e0-a74f-707e3fef9258")
# IUnknown(3) + IInspectable(3) + 19 unexposed methods -> Set=25, Get=26.
_VT_SET_PERSISTED = 25
_ROLES = (0, 1)  # eConsole, eMultimedia — what the Volume mixer sets
_ACTIVE = 1  # AudioSessionStateActive

_BROWSERS = frozenset({"msedge", "chrome", "firefox", "brave", "opera", "vivaldi", "arc"})
# What the user calls it -> process stems. Web media lives in the browser.
_APP_WORDS: dict[str, frozenset[str]] = {
    "spotify": frozenset({"spotify"}),
    "youtube": _BROWSERS,
    "netflix": _BROWSERS,
    "browser": _BROWSERS,
    "edge": frozenset({"msedge"}),
    "chrome": frozenset({"chrome"}),
    "firefox": frozenset({"firefox"}),
    "vlc": frozenset({"vlc"}),
}
# "the music", "media", "everything": every app that is playing right now.
_ALL_WORDS = frozenset({"", "all", "media", "music", "everything", "audio", "sound", "playback"})
# A device word meaning "stop pinning: follow the system default".
_FOLLOW_WORDS = frozenset({"default", "system", "auto", "automatic", "follow", "normal"})
_PRETTY = {
    "spotify": "Spotify",
    "msedge": "Microsoft Edge",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "vlc": "VLC",
    "brave": "Brave",
    "opera": "Opera",
}


@dataclass(frozen=True)
class MediaSession:
    pid: int
    stem: str  # process name without .exe, lower-case
    endpoint_id: str
    endpoint_name: str
    active: bool

    @property
    def app(self) -> str:
        return _PRETTY.get(self.stem, self.stem.replace("_", " ").title())


# ── WHERE: the sessions on every output endpoint ─────────────────────────────


def _process_stem(pid: int) -> str:
    try:
        import psutil

        return os.path.splitext(psutil.Process(pid).name())[0].lower()
    except Exception:
        return ""


def media_sessions() -> list[MediaSession]:
    """Every app audio session on every active output, Era's own excluded."""
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2

    own = os.getpid()
    out: list[MediaSession] = []
    for ep in ae.enumerate_endpoints(ae.OUTPUT):
        try:
            mgr = ep.dev._dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
            sessions = mgr.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
            count = sessions.GetCount()
        except Exception:
            continue
        for i in range(count):
            try:
                ctl = sessions.GetSession(i).QueryInterface(IAudioSessionControl2)
                pid, state = int(ctl.GetProcessId()), int(ctl.GetState())
            except Exception:
                continue
            if not pid or pid == own:
                continue
            stem = _process_stem(pid)
            if stem:
                out.append(MediaSession(pid, stem, ep.id, ep.name, state == _ACTIVE))
    return out


def _select(sessions: list[MediaSession], app: str) -> tuple[list[MediaSession], str]:
    """(sessions to act on, how the user named them)."""
    a = (app or "").strip().lower()
    if a in _ALL_WORDS:
        return [s for s in sessions if s.active], "the media"
    stems = _APP_WORDS.get(a) or frozenset({a.removesuffix(".exe").replace(" ", "")})
    return [s for s in sessions if s.stem in stems], app.strip()


def _apps(sessions: list[MediaSession]) -> str:
    names = list(dict.fromkeys(s.app for s in sessions))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _is(sessions: list[MediaSession]) -> str:
    return "is" if len({s.app for s in sessions}) == 1 else "are"


@ae._guard
def where_media_plays() -> ae.AudioResult:
    playing = [s for s in media_sessions() if s.active]
    default = ae._system_endpoint()
    tail = f" The default output is {default.name}." if default else ""
    if not playing:
        return ae.AudioResult(True, f"Nothing is playing right now, sir.{tail}", {"playing": []})
    by_device: dict[str, list[MediaSession]] = {}
    for s in playing:
        by_device.setdefault(s.endpoint_name, []).append(s)
    parts = [f"{_apps(group)} on {name}" for name, group in by_device.items()]
    data = {"playing": [{"app": s.app, "device": s.endpoint_name} for s in playing]}
    return ae.AudioResult(True, "Playing: " + "; ".join(parts) + f", sir.{tail}", data)


def held_elsewhere_note(target: ae.Endpoint) -> str:
    """For set_default: name media still playing on ANOTHER device (never raises)."""
    try:
        stuck = [s for s in media_sessions() if s.active and s.endpoint_id != target.id]
    except Exception:
        return ""
    if not stuck:
        return ""
    where = stuck[0].endpoint_name
    return (
        f" But {_apps(stuck)} {_is(stuck)} still playing on {where} — an app setting holds it there. "
        f"Say 'send the music to {target.name}' and I'll move it."
    )


# ── MOVE: Windows' per-app endpoint (IAudioPolicyConfigFactory) ─────────────


class _GUID(ctypes.Structure):
    _fields_ = [
        ("a", ctypes.c_uint32),
        ("b", ctypes.c_uint16),
        ("c", ctypes.c_uint16),
        ("d", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    u = uuid.UUID(text)
    g = _GUID(u.fields[0], u.fields[1], u.fields[2])
    for i, b in enumerate(u.bytes[8:]):
        g.d[i] = b
    return g


def _persisted_id(endpoint_id: str) -> str:
    return rf"\\?\SWD#MMDEVAPI#{endpoint_id}#{_RENDER_IFACE}"


def _set_persisted(pids: list[int], endpoint_id: str | None) -> dict[int, int]:
    """{pid: HRESULT} — run on a fresh MTA thread (WinRT activation needs one)."""
    out: dict[int, int] = {}

    def work() -> None:
        combase = ctypes.WinDLL("combase")
        combase.RoInitialize(1)  # RO_INIT_MULTITHREADED
        handles: list[c_void_p] = []

        def hstring(text: str) -> c_void_p:
            h = c_void_p()
            if text:
                combase.WindowsCreateString(ctypes.c_wchar_p(text), len(text), byref(h))
                handles.append(h)
            return h

        factory = c_void_p()
        try:
            cls = hstring("Windows.Media.Internal.AudioPolicyConfig")
            for iid in _FACTORY_IIDS:
                if combase.RoGetActivationFactory(cls, byref(_guid(iid)), byref(factory)) == 0:
                    break
            else:
                for pid in pids:
                    out[pid] = -1
                return
            vtable = ctypes.cast(factory, POINTER(POINTER(c_void_p)))[0]
            setter = ctypes.WINFUNCTYPE(
                ctypes.c_long, c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, c_void_p
            )(vtable[_VT_SET_PERSISTED])
            device = hstring(_persisted_id(endpoint_id)) if endpoint_id else c_void_p()
            for pid in pids:
                hr = 0
                for role in _ROLES:
                    hr = hr or setter(factory, pid, 0, role, device)  # 0 = eRender
                out[pid] = hr
        finally:
            if factory:
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(
                    ctypes.cast(factory, POINTER(POINTER(c_void_p)))[0][2]
                )
                release(factory)
            for h in handles:
                combase.WindowsDeleteString(h)
            combase.RoUninitialize()

    t = threading.Thread(target=work, name="AudioPolicyConfig", daemon=True)
    t.start()
    t.join(5.0)
    return out


def _settled(pids: set[int], endpoint_id: str | None, timeout_s: float = 2.0) -> set[int]:
    """The pids whose sessions are now on `endpoint_id` (or the default)."""
    deadline = time.monotonic() + timeout_s
    while True:
        if endpoint_id is None:
            default = ae._system_endpoint()
            want_id = default.id if default else ""
        else:
            want_id = endpoint_id
        on = {s.pid for s in media_sessions() if s.active and s.endpoint_id == want_id}
        moved = pids & on
        if moved == pids or time.monotonic() >= deadline:
            return moved
        time.sleep(0.2)


# ── The REMEMBERED media device ──────────────────────────────────────────────
# The owner's setup (2026-10-16): Era's voice on the glasses, MUSIC on the PC
# speakers — always, unless they ask for another device, which then sticks until
# they ask again. Stored beside the voice pin in core.user_prefs; unset means the
# PC speakers (resolved by form factor, so a renamed endpoint still matches).

MEDIA_PREF_KEY = "media_output"
_DEFAULT_MEDIA_QUERY = "speakers"


def _media_pref() -> dict | None:
    try:
        from core.user_prefs import load_prefs

        pref = load_prefs().get(MEDIA_PREF_KEY)
    except Exception:
        return None
    return pref if isinstance(pref, dict) else None


def _store_media_pref(pref: dict | None) -> None:
    try:
        from core import user_prefs

        with user_prefs._LOCK:
            data = user_prefs._load_unlocked()
            if pref is None:
                data.pop(MEDIA_PREF_KEY, None)
            else:
                data[MEDIA_PREF_KEY] = pref
            user_prefs._save(data)
    except Exception as e:
        print(f"[MediaRouting] media pref write failed: {type(e).__name__}: {e}")


def media_target() -> tuple[ae.Endpoint | None, str]:
    """(endpoint media should play on, why). None endpoint = follow the default."""
    pref = _media_pref()
    if pref and pref.get("follow"):
        return None, "follows the default"
    if pref and (pref.get("id") or pref.get("name")):
        for ep in ae.enumerate_endpoints(ae.OUTPUT):
            if ep.id == pref.get("id") or ep.name == pref.get("name"):
                return ep, "your choice"
        return None, f"{pref.get('name')} is not connected"
    ep, err = ae.resolve(_DEFAULT_MEDIA_QUERY, ae.OUTPUT)
    return (ep, "default for media") if err is None else (None, "no speakers found")


def enforce_media_target(app: str, *, wait_s: float = 4.0) -> str:
    """Keep `app`'s new sound on the remembered media device. Never raises.

    Called after Era starts playback: the app's session may take a moment to
    appear, so it is awaited briefly. Silent when it already plays there."""
    try:
        target, _why = media_target()
        if target is None:
            return ""
        deadline = time.monotonic() + wait_s
        while True:
            chosen, _ = _select(media_sessions(), app)
            playing = [s for s in chosen if s.active]
            if playing or time.monotonic() >= deadline:
                break
            time.sleep(0.3)
        stray = sorted({s.pid for s in playing if s.endpoint_id != target.id})
        if not stray:
            return ""
        _set_persisted(stray, target.id)
        return f"moved to {target.name}"
    except Exception as e:
        print(f"[MediaRouting] enforce skipped: {type(e).__name__}: {e}")
        return ""


def enforce_media_target_async(app: str) -> None:
    """Fire-and-forget `enforce_media_target`: never delays the spoken reply."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return  # a test run must never move the owner's real audio

    def run() -> None:
        try:
            ae._ensure_com()
        except Exception:
            pass
        note = enforce_media_target(app)
        if note:
            print(f"[MediaRouting] {app}: {note}")

    threading.Thread(target=run, name="MediaTarget", daemon=True).start()


def hud_snapshot() -> dict:
    """What the HUD's 'Media out' row shows, from the live sessions (never raises)."""
    try:
        target, why = media_target()
        playing = [s for s in media_sessions() if s.active]
        return {
            "target": target.name if target else None,
            "why": why,
            "playing": [{"app": s.app, "device": s.endpoint_name} for s in playing],
        }
    except Exception:
        return {"target": None, "why": "unavailable", "playing": []}


@ae._guard
def route_media(device: str, app: str = "") -> ae.AudioResult:
    """Send one app's sound (or everything playing) to `device`.

    The choice is REMEMBERED: media Era starts later goes there too, until the
    owner names another device."""
    d = (device or "").strip()
    if not d:
        return ae.AudioResult(False, "Which device should the media play on, sir?")
    follow = d.lower() in _FOLLOW_WORDS
    target = None
    if not follow:
        target, err = ae.resolve(d, ae.OUTPUT)
        if err:
            return err
    _store_media_pref(
        {"follow": True, "set_at": time.time()}
        if target is None
        else {"id": target.id, "name": target.name, "set_at": time.time()}
    )
    chosen, label = _select(media_sessions(), app)
    where = target.name if target else "the default output"
    if not chosen:
        # Remembered above, so this is still a done deal for what plays next.
        return ae.AudioResult(
            True, f"Done, sir — music will play on {where} from now on.", {"moved": []}
        )
    if target and all(s.endpoint_id == target.id for s in chosen):
        return ae.AudioResult(
            True, f"{_apps(chosen)} {_is(chosen)} already on {where}, sir.", {"changed": False}
        )
    pids = sorted({s.pid for s in chosen})
    results = _set_persisted(pids, target.id if target else None)
    refused = [p for p in pids if results.get(p, -1) != 0]
    if len(refused) == len(pids):
        return ae.AudioResult(
            False, f"Windows refused to move {_apps(chosen)} to {where}, sir.", {"hr": results}
        )
    moved = _settled(set(pids) - set(refused), target.id if target else None)
    late = [s for s in chosen if s.pid not in moved and s.pid not in refused]
    if not moved:
        msg = (
            f"{_apps(chosen)} {_is(chosen)} set to {where}, sir, but the sound hasn't "
            "switched yet — pause and play once if it stays put."
        )
    else:
        done = [s for s in chosen if s.pid in moved]
        msg = f"{_apps(done)} {_is(done)} now on {where}, sir."
        if late:
            msg += f" {_apps(late)} will follow when its next sound starts."
    return ae.AudioResult(True, msg, {"moved": sorted(moved), "refused": refused, "device": where})
