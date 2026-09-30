# core/media_state.py
"""
Central synchronous media state manager for Era One (Option 1: standalone).

- Cached dict = intended media state.
- Lightweight window-title substring verification (no DOM injection).
- Single double-playback sweep called by all three controllers.
- Never launches an app; only pauses sources already running.
"""

from __future__ import annotations

import threading

from core.utils import is_windows, log

_LOCK = threading.RLock()

MEDIA_STATE: dict[str, dict] = {
    "spotify": {"status": "paused", "current_track": None},
    "youtube": {"status": "paused", "current_url": None},
    "netflix": {"status": "paused", "current_show": None},
}

_VALID = ("spotify", "youtube", "netflix")


def _is_spotify_process_running() -> bool:
    """Check if Spotify.exe process is actually running."""
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and proc.info["name"].lower() == "spotify.exe":
                return True
    except Exception:
        pass
    return False


_WINDOW_HINTS = {
    "spotify": ("spotify",),
    "youtube": ("youtube",),
    "netflix": ("netflix",),
}


# ── window-title verification ────────────────────────────────────────────────
def _visible_window_titles() -> list[str]:
    titles: list[str] = []
    if not is_windows():
        return titles
    try:
        import win32gui

        def _enum(hwnd, acc):
            try:
                if win32gui.IsWindowVisible(hwnd):
                    t = (win32gui.GetWindowText(hwnd) or "").strip()
                    if t:
                        acc.append(t.lower())
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum, titles)
    except Exception:
        pass
    return titles


def source_window_present(source: str) -> bool:
    """True if any visible window title matches the source's hints."""
    hints = _WINDOW_HINTS.get(source.lower().strip(), ())
    if not hints:
        return False
    titles = _visible_window_titles()
    return any(any(h in t for h in hints) for t in titles)


# ── state read / write ───────────────────────────────────────────────────────
def get_state() -> dict[str, dict]:
    with _LOCK:
        return {k: dict(v) for k, v in MEDIA_STATE.items()}


def set_status(source: str, status: str, **details) -> None:
    source = source.lower().strip()
    status = status.lower().strip()
    if source not in _VALID or status not in ("playing", "paused"):
        return
    with _LOCK:
        MEDIA_STATE[source]["status"] = status
        for key, val in details.items():
            if key in MEDIA_STATE[source]:
                MEDIA_STATE[source][key] = val


def is_playing(source: str) -> bool:
    """Query live tool before confirming; NEVER return cached state alone."""
    # Source-of-truth: live status query supersedes cache per /goal directive.
    # Caller MUST have verified via spotify_control(action='status') or equivalent.
    with _LOCK:
        return MEDIA_STATE.get(source.lower().strip(), {}).get("status") == "playing"


def mark_paused(source: str, player=None) -> None:
    set_status(source, "paused")


# ── state reconciliation bridge (GSMTC → MEDIA_STATE) ────────────────────────
def get_live_spotify_status() -> dict:
    """Real-time query via spotify_control(status). Source of truth over MEDIA_STATE."""
    try:
        from app.era_live.tool_dispatch import dispatch

        # Direct tool query supersedes assumed/visual/cache per goal step 3.
        return dispatch({"name": "spotify_control", "arguments": {"action": "status"}})
    except Exception:
        return {}


def reconcile_from_os_sessions(sessions: list[dict]) -> None:
    """
    Called by media_exclusivity.py to sync actual OS playback status
    back into the central MEDIA_STATE cache.

    Each session dict must have: source (str), status ('playing'|'paused').
    """
    with _LOCK:
        for s in sessions:
            src = s.get("source", "").lower().strip()
            st = s.get("status", "").lower().strip()
            if src in _VALID and st in ("playing", "paused"):
                MEDIA_STATE[src]["status"] = st


# ── pause a single source (never launches it) ────────────────────────────────
def _cdp_pause_video(domain: str) -> bool:
    """Try pausing video via CDP on a specific domain. Returns True if successful."""
    try:
        from actions.browser_control import get_browser_session

        sess = get_browser_session("brave", connect_only=True)
        script = (
            "const v=document.querySelector('video'); if(v){v.pause(); return true;} return false;"
        )
        result = sess.run(sess.evaluate_background_media(domain, script))
        return "true" in str(result).lower()
    except Exception:
        return False


def _pause_source(source: str, player=None) -> None:
    try:
        if source == "spotify":
            # Strict process check: only pause if Spotify.exe is actually running
            if not _is_spotify_process_running():
                return  # Spotify not running, nothing to pause
            # State check: only pause if currently playing
            if not is_playing("spotify"):
                return  # Spotify not playing, nothing to pause
            # Both checks passed - safely pause Spotify
            from actions.spotify_control import spotify_control

            spotify_control({"action": "pause_if_running_only"}, player=player)
        elif source == "youtube":
            if source_window_present("youtube"):
                from actions.youtube_video import youtube_video

                youtube_video({"action": "youtube_pause"}, player=player)
            else:
                _cdp_pause_video("youtube.com")
        elif source == "netflix":
            if source_window_present("netflix"):
                from actions.netflix_control import netflix_control

                netflix_control({"action": "pause"}, player=player)
            else:
                _cdp_pause_video("netflix.com")
    except Exception as exc:
        log("MediaState", f"pause {source} skipped: {exc}", player)


# ── single-playback enforcement ──────────────────────────────────────────────
def enforce_single_playback(active: str, player=None) -> None:
    """Mark `active` playing; pause every OTHER source. Call AFTER playback starts."""
    active = active.lower().strip()
    if active not in _VALID:
        return
    set_status(active, "playing")
    for source in _VALID:
        if source != active:
            _pause_source(source, player=player)
            set_status(source, "paused")
    log("MediaState", f"single-playback → {active}", player)


def post_action_sweep(player=None) -> str | None:
    """Safety sweep: if cache shows >1 playing, keep the first verified, pause rest."""
    with _LOCK:
        playing = [s for s in _VALID if MEDIA_STATE[s]["status"] == "playing"]
    if len(playing) <= 1:
        return None
    verified = [s for s in playing if s == "spotify" or source_window_present(s)] or playing
    keep = verified[0]
    for source in _VALID:
        if source != keep:
            _pause_source(source, player=player)
            set_status(source, "paused")
    return f"Double-playback resolved → kept {keep}."
