# core/media_state.py
"""
Central synchronous media state manager for Era One (Option 1: standalone).

- Cached dict = intended media state.
- Fresh native/browser playback evidence supersedes intention.
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
    """Affirmative playback evidence; callers still verify task outcomes live.

    Fresh native and browser observations supersede intention. After observation
    starts, unknown state returns False, which does not prove paused playback.
    Unobserved browser sources retain the legacy intention fallback.
    """
    # Source-of-truth: live status query supersedes cache per /goal directive.
    # Caller MUST have verified via spotify_control(action='status') or equivalent.
    from core.system_awareness import get_media_awareness, observed_source_status

    observed = observed_source_status(source.lower().strip())
    if observed is not None:
        return observed == "playing"
    if source.lower().strip() == "spotify" and get_media_awareness().has_observed():
        # Unknown/removed telemetry must not resurrect an old requested status.
        # False means no affirmative evidence, not verified paused playback.
        return False
    if source.lower().strip() in ("youtube", "netflix"):
        from core.browser_media import get_browser_media_cache

        if get_browser_media_cache().has_observed(source.lower().strip()):
            return False
    with _LOCK:
        return MEDIA_STATE.get(source.lower().strip(), {}).get("status") == "playing"


def mark_paused(source: str, player=None) -> None:
    set_status(source, "paused")


# ── state reconciliation bridge (GSMTC → MEDIA_STATE) ────────────────────────
def get_live_spotify_status() -> dict:
    """Read the OS directly; never fall back to requested/cached playback."""
    from core.live_app_state import collect_live_state

    return collect_live_state("spotify")


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
        script = "const v=document.querySelector('video'); if(v){v.pause(); return v.paused === true;} return false;"
        result = sess.run(sess.evaluate_background_media(domain, script))
        return result is True or str(result).strip().lower() == "true"
    except Exception:
        return False


def _pause_source(source: str, player=None) -> bool:
    try:
        if source == "spotify":
            # Strict process check: only pause if Spotify.exe is actually running
            if not _is_spotify_process_running():
                return False  # absence of a process is not a paused read-back
            # State check: only pause if currently playing
            if not is_playing("spotify"):
                from core.system_awareness import observed_source_status

                return observed_source_status("spotify") == "paused"
            # Both checks passed - safely pause Spotify
            from actions.spotify_control import APPCOMMAND_MEDIA_PAUSE, _send_spotify_command

            # The public legacy pause helper may send a global toggle and write
            # intent before verification. Exclusivity must stay targeted.
            if not _send_spotify_command(APPCOMMAND_MEDIA_PAUSE):
                return False
            from core.system_awareness import observed_source_status

            return observed_source_status("spotify") == "paused"
        elif source == "youtube":
            return _cdp_pause_video("youtube.com")
        elif source == "netflix":
            return _cdp_pause_video("netflix.com")
    except Exception as exc:
        log("MediaState", f"pause {source} skipped: {exc}", player)
    return False


# ── single-playback enforcement ──────────────────────────────────────────────
def enforce_single_playback(active: str, player=None) -> None:
    """Mark `active` playing; pause every OTHER source. Call AFTER playback starts."""
    active = active.lower().strip()
    if active not in _VALID:
        return
    set_status(active, "playing")
    for source in _VALID:
        if source != active:
            if _pause_source(source, player=player):
                set_status(source, "paused")
            else:
                log("MediaState", f"pause {source} unverified; intended state retained", player)
    log(
        "MediaState",
        f"single-playback requested → {active}; only confirmed pauses recorded",
        player,
    )


def post_action_sweep(player=None) -> str | None:
    """Safety sweep: if cache shows >1 playing, keep the first verified, pause rest."""
    with _LOCK:
        playing = [s for s in _VALID if MEDIA_STATE[s]["status"] == "playing"]
    if len(playing) <= 1:
        return None
    verified = [s for s in playing if s == "spotify" or source_window_present(s)] or playing
    keep = verified[0]
    unresolved = []
    for source in playing:
        if source != keep:
            if _pause_source(source, player=player):
                set_status(source, "paused")
            else:
                unresolved.append(source)
    if unresolved:
        return f"Double-playback unresolved → kept {keep}; pause unverified for {', '.join(unresolved)}."
    return f"Double-playback resolved → kept {keep}."
