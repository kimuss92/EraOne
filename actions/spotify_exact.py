# actions/spotify_exact.py
"""Exact Spotify control that window-automation cannot do, using APIs already in
this repo — no new dependencies, no bridge.

GROUP A — exact per-app volume via pycaw (Spotify.exe's own audio session, never
the system mixer). GROUP B — current track as DATA via Windows GSMTC
(GlobalSystemMediaTransportControls), the same WinRT surface windows/
media_exclusivity.py already drives.

Every function returns a dict and never raises: callers (spotify_control._dispatch)
serialise it to JSON over the tool boundary. Volume in this module is 0.0..1.0 at
the pycaw layer and 0..100 across the function boundary — converted here.

This file is a separate lane from core/spotify_bridge.py (the dormant keyless
bridge) and never imports or references it.
"""

from __future__ import annotations

import logging

log = logging.getLogger("era_one.spotify.exact")

# ── GROUP A: exact per-app volume (pycaw) ─────────────────────────────────────
try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

    _PYCAW = True
    _COMError = comtypes.COMError
except ImportError:
    _PYCAW = False
    _COMError = Exception  # so `except _COMError` is valid even without pycaw

# The whole latency budget lives here: AudioUtilities.GetAllSessions() enumerates
# every audio session on the machine (~20-40 ms). We enumerate ONCE, cache the
# Spotify.exe ISimpleAudioVolume pointer, and reuse it on every later call
# (~1-3 ms). The cache is per-Era-process and never crosses a process boundary.
VOLUME_CACHE_ENABLED = True
_vol_cache: dict | None = None  # {"iface": ISimpleAudioVolume, "pid": int}
_vol_hits = 0
_vol_misses = 0


def _invalidate_volume_cache() -> None:
    """Drop the cached Spotify volume pointer; the next call re-enumerates once.
    Exposed for tests and called on any COMError from a cached pointer."""
    global _vol_cache
    _vol_cache = None


def _enumerate_spotify_volume():
    """Full one-shot enumeration. Returns (ISimpleAudioVolume, pid) or (None, None).
    Prefers the Active Spotify.exe session, else the first one found."""
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return None, None
    chosen = None
    for session in sessions:
        try:
            proc = session.Process
            if not proc or proc.name().lower() != "spotify.exe":
                continue
        except Exception:
            continue
        if getattr(session, "State", None) == 1:  # AudioSessionStateActive
            chosen = session
            break
        if chosen is None:
            chosen = session
    if chosen is None:
        return None, None
    try:
        iface = chosen._ctl.QueryInterface(ISimpleAudioVolume)
        pid = getattr(getattr(chosen, "Process", None), "pid", None)
        return iface, pid
    except Exception:
        return None, None


def _spotify_audio_volume():
    """Cached ISimpleAudioVolume for Spotify.exe, or None if Spotify has no audio
    session. A cache hit skips the machine-wide enumeration entirely."""
    global _vol_cache, _vol_hits, _vol_misses
    if not _PYCAW:
        return None
    if VOLUME_CACHE_ENABLED and _vol_cache is not None:
        _vol_hits += 1
        log.debug("spotify volume cache hit (pid=%s)", _vol_cache.get("pid"))
        return _vol_cache["iface"]
    _vol_misses += 1
    iface, pid = _enumerate_spotify_volume()
    if iface is None:
        return None
    if VOLUME_CACHE_ENABLED:
        _vol_cache = {"iface": iface, "pid": pid}
    log.debug("spotify volume cache miss -> enumerated (pid=%s)", pid)
    return iface


def volume_cache_stats() -> dict:
    """Hit/miss counters for latency reporting and tests."""
    total = _vol_hits + _vol_misses
    rate = (_vol_hits / total * 100.0) if total else 0.0
    return {"hits": _vol_hits, "misses": _vol_misses, "hit_rate_percent": round(rate, 1)}


def _not_running() -> dict:
    return {"ok": False, "error": "spotify not running"}


def _vol_state(iface) -> dict:
    """Read-back state so a write caller never has to re-query."""
    percent = int(round(iface.GetMasterVolume() * 100))
    return {"ok": True, "percent": max(0, min(100, percent)), "muted": bool(iface.GetMute())}


def _with_iface(op):
    """Run op(iface) on the cached pointer. On COMError (session died / Spotify
    closed / pid recycled) invalidate, re-enumerate once, and retry. Returns a
    clean dict; never raises."""
    iface = _spotify_audio_volume()
    if iface is None:
        return _not_running()
    try:
        return op(iface)
    except _COMError:
        _invalidate_volume_cache()  # stale pointer — rebuild from a fresh scan
        iface = _spotify_audio_volume()
        if iface is None:
            return _not_running()
        try:
            return op(iface)
        except Exception as exc:
            return {"ok": False, "error": f"volume op failed: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"volume op failed: {exc}"}


def get_volume_exact() -> dict:
    return _with_iface(_vol_state)


def set_volume_exact(percent) -> dict:
    pct = max(0, min(100, int(round(float(percent)))))

    def _op(iface):
        iface.SetMasterVolume(pct / 100.0, None)  # pycaw wants 0.0..1.0 + event ctx
        return _vol_state(iface)

    return _with_iface(_op)


def volume_delta(delta) -> dict:
    step = int(round(float(delta)))

    def _op(iface):
        cur = int(round(iface.GetMasterVolume() * 100))
        iface.SetMasterVolume(max(0, min(100, cur + step)) / 100.0, None)
        return _vol_state(iface)

    return _with_iface(_op)


def _set_mute(value: bool) -> dict:
    def _op(iface):
        iface.SetMute(1 if value else 0, None)
        return _vol_state(iface)

    return _with_iface(_op)


def mute_exact() -> dict:
    return _set_mute(True)


def unmute_exact() -> dict:
    return _set_mute(False)


def toggle_mute_exact() -> dict:
    def _op(iface):
        iface.SetMute(0 if bool(iface.GetMute()) else 1, None)
        return _vol_state(iface)

    return _with_iface(_op)


# ── GROUP B: current track as DATA (WinRT GSMTC via persistent PowerShell) ────
# No CPython 3.14 wheel exists for winrt/winsdk, and the repo has no comtypes-WinRT
# helper to clone, so GSMTC is read through a warm PowerShell host
# (actions/spotify_gsmtc_host.py) that owns the WinRT AsTask/Await idiom. Each
# verb is one JSON round-trip; the host returns exactly the shapes below, so these
# wrappers only clamp inputs and pass results through. Never raise.


def current_track() -> dict:
    from actions import spotify_gsmtc_host

    return spotify_gsmtc_host.request({"op": "current_track"})


def playback_state() -> dict:
    from actions import spotify_gsmtc_host

    return spotify_gsmtc_host.request({"op": "playback_state"})


def progress() -> dict:
    from actions import spotify_gsmtc_host

    return spotify_gsmtc_host.request({"op": "progress"})


def seek_exact(seconds) -> dict:
    from actions import spotify_gsmtc_host

    secs = max(0.0, min(3600.0, float(seconds)))
    return spotify_gsmtc_host.request({"op": "seek_exact", "seconds": secs})


def seek_relative(delta_seconds) -> dict:
    """Move playback by a signed number of seconds. The host reads the current
    position, clamps to [0, track end], and seeks — one atomic round-trip, so it
    is resize-proof and never touches the UI."""
    from actions import spotify_gsmtc_host

    delta = max(-3600.0, min(3600.0, float(delta_seconds)))
    return spotify_gsmtc_host.request({"op": "seek_relative", "delta_seconds": delta})


def forward(seconds=15) -> dict:
    return seek_relative(abs(float(seconds if seconds is not None else 15)))


def rewind(seconds=15) -> dict:
    return seek_relative(-abs(float(seconds if seconds is not None else 15)))
