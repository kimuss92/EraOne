# actions/media_coordinator.py
import time

import psutil

from actions.computer_settings import computer_settings
from actions.netflix_control import netflix_control
from actions.window_control import focus_window
from actions.youtube_video import youtube_video


def _hijack_existing_tab(target: str, url: str) -> bool:
    """Replaced by platform_tab.get_or_open_platform_tab. Kept for legacy compatibility."""
    try:
        from actions.browser_control import get_browser_session
        from actions.platform_tab import get_or_open_platform_tab

        sess = get_browser_session()
        tab_result = get_or_open_platform_tab(platform=target)
        page = tab_result.page
        if page is None:
            return False
        if url:
            sess.run(page.goto(url, wait_until="domcontentloaded"))
        sess.run(page.bring_to_front())
        return True
    except Exception:
        return False


def _focus_existing_tab(target: str) -> bool:
    """Replaced by platform_tab.get_or_open_platform_tab. Kept for legacy compatibility."""
    try:
        from actions.platform_tab import get_or_open_platform_tab

        tab_result = get_or_open_platform_tab(platform=target)
        page = tab_result.page
        if page is None:
            return False
        return True
    except Exception:
        return False


def _pause_spotify_safely(player=None) -> str:
    """Pause Spotify only if it's running.

    Checks for spotify.exe process first, then delegates to
    spotify_control.pause_if_running_only which checks the real
    playback state via window title (more reliable than cached state).

    Returns:
        Status message string
    """
    try:
        # Check if Spotify process is running
        spotify_running = False
        try:
            for proc in psutil.process_iter(["name"]):
                if proc.info["name"] and "spotify" in proc.info["name"].lower():
                    spotify_running = True
                    break
        except Exception:
            spotify_running = False

        if not spotify_running:
            return "Spotify not running, skipping pause."

        # Delegate to spotify_control which now checks real state via window title
        from actions.spotify_control import spotify_control

        return spotify_control({"action": "pause_if_running_only"}, player=player)
    except Exception as e:
        return f"Error pausing Spotify safely: {e}"


def media_coordinator(parameters: dict = None, player=None) -> str:
    import win32api
    import win32con

    from core.media_state import enforce_single_playback, mark_paused

    params = parameters or {}
    target = str(params.get("target", "spotify")).lower().strip()
    action = str(params.get("action", "play")).lower().strip()

    # ── PAUSE ACTIVE (detect which service is playing and pause it) ──────────
    if target == "all" and action in ("pause", "stop"):
        from core.media_state import is_playing

        def _really_playing(candidate: str) -> bool:
            # Spotify: its REAL state (Windows audio session / window title).
            # The cached media_state alone is empty in a fresh process and stale
            # when Karim starts music himself -- 25/09/2026 "Pause" answered
            # "Nothing is currently playing" while Spotify played on.
            if candidate == "spotify":
                try:
                    from actions.spotify_control import _spotify_is_playing

                    return _spotify_is_playing()
                except Exception:
                    return is_playing(candidate)
            return is_playing(candidate)

        for candidate in ("spotify", "youtube", "netflix"):
            if _really_playing(candidate):
                if candidate == "youtube":
                    return youtube_video({"action": "youtube_pause"}, player=player)
                elif candidate == "netflix":
                    return netflix_control({"action": "pause"}, player=player)
                else:
                    try:
                        from actions.spotify_control import spotify_control

                        return spotify_control({"action": "pause_if_running_only"}, player=player)
                    except Exception:
                        win32api.keybd_event(0xB3, 0, 0, 0)
                        win32api.keybd_event(0xB3, 0, win32con.KEYEVENTF_KEYUP, 0)
                        return "Spotify paused via system key."
        return "Nothing is currently playing, sir."

    # ── PAUSE / STOP MANAGEMENT ───────────────────────────────────────────────
    if action in ("pause", "stop"):
        # Attempt the actual pause regardless of cached state.
        # The individual controllers (youtube, netflix, spotify) have their own
        # guards that check the real playback state via CDP JS or window title.
        # This prevents stale cache from falsely reporting "already paused".
        if target == "youtube":
            res = youtube_video({"action": "youtube_pause"}, player=player)
        elif target == "netflix":
            res = netflix_control({"action": "pause"}, player=player)
        else:
            try:
                from actions.spotify_control import spotify_control

                res = spotify_control({"action": "pause_if_running_only"}, player=player)
            except Exception:
                win32api.keybd_event(0xB3, 0, 0, 0)
                win32api.keybd_event(0xB3, 0, win32con.KEYEVENTF_KEYUP, 0)
                res = "Spotify paused via global Virtual hardware key fallback."

        mark_paused(target)
        return res

    # ── PLAY / RESUME MANAGEMENT ───────────────────────────────────────────────
    if action in ("play", "resume"):
        # Pause Spotify safely before activating a video service.
        # Only pauses if Spotify is actually running and playing.
        _pause_spotify_safely(player)
        # Short delay to let the audio channel release before starting video
        time.sleep(0.3)

        if target == "youtube":
            query = params.get("query", "").strip() or params.get("title", "").strip()
            if query:
                res = youtube_video({"action": "play", "query": query}, player=player)
            else:
                _focus_existing_tab("youtube")
                res = youtube_video({"action": "youtube_resume"}, player=player)
        elif target == "netflix":
            query = params.get("query", "").strip() or params.get("title", "").strip()
            if query:
                # Search with auto-play — netflix_control handles autoplay internally.
                # Do NOT call fullscreen here; search results page has no player.
                res = netflix_control(
                    {"action": "search", "value": query, "_auto_play": True},
                    player=player,
                )
            else:
                # No query: delegate to netflix_control play which handles
                # search_results, player, browse correctly.
                # Do NOT shortcut with grid_play here — let netflix_control
                # validate grid freshness against current page context.
                if not _focus_existing_tab("netflix"):
                    focus_window("netflix")
                    time.sleep(0.5)
                res = netflix_control({"action": "play"}, player=player)
        else:
            res = computer_settings({"action": "spotify_play"}, player=player)

        enforce_single_playback(target, player=player)
        return res

    # ── TRACK NAVIGATION (NEXT / PREVIOUS) ──────────────────────────────────────
    if action in ("next", "previous"):
        if target == "spotify":
            # Map navigation to bulletproof Windows Virtual Media Key Registers
            key_code = (
                0xB0 if action == "next" else 0xB1
            )  # VK_MEDIA_NEXT_TRACK / VK_MEDIA_PREV_TRACK
            win32api.keybd_event(key_code, 0, 0, 0)
            win32api.keybd_event(key_code, 0, win32con.KEYEVENTF_KEYUP, 0)
            return f"Spotify track navigation '{action}' triggered via OS virtual key pipeline."

    return f"Unknown media_coordinator target/action: {target} / {action}"
