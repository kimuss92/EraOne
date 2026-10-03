# actions/spotify_control.py
from __future__ import annotations

import json
import os
import subprocess
import threading  # Lets Era One handle background checks without lag
import time
import urllib.parse

from core.intent_memory import remember_action
from core.win_subprocess import NO_WINDOW

try:
    import ctypes

    import comtypes
    import keyboard  # noqa: F401  (availability probe: presence sets the capability flag)
    import pyautogui
    import win32api
    import win32con
    import win32gui
    import win32process

    # Background volume mixer tools
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
except ImportError as exc:
    raise ImportError(
        "Spotify control dependencies are missing. Install them with: "
        "pip install -r requirements.windows.txt"
    ) from exc

# ─────────────────────────────────────────────────────────────────────────────
# CORE SPOTIFY INTERFACE HARDWARE BYPASS (CRITICAL FIX LAYER)
# ─────────────────────────────────────────────────────────────────────────────


def _enforce_spotify_priority():
    """Pauses background media in Brave (Netflix/YouTube) natively via CDP layer."""
    try:
        from actions.browser_control import get_browser_session

        sess = get_browser_session("brave", connect_only=True)

        script = "document.querySelectorAll('video').forEach(v => v.pause());"
        sess.run(sess.evaluate_background_media("netflix.com", script))
        sess.run(sess.evaluate_background_media("youtube.com", script))
        print("[Spotify] Single Playback Enforcer: Browser media blanket pause completed.")
    except Exception as e:
        print(f"[Spotify] Browser auto-pause bridge exception: {e}")


def _get_spotify_hwnd_and_title() -> tuple[int, str]:
    """
    Locates the TRUE visible running Spotify GUI window by its process ID.
    Accounts for minimized states where Windows reports negative coordinates.
    """
    main_hwnd = 0

    def enum_cb(hwnd_enum, extra):
        nonlocal main_hwnd
        if main_hwnd != 0:
            return True

        try:
            # 1. Must belong to spotify.exe
            _, pid = win32process.GetWindowThreadProcessId(hwnd_enum)
            handle = win32api.OpenProcess(0x1000, False, pid)
            exe_name = win32process.GetModuleFileNameEx(handle, 0).lower()
            if "spotify.exe" not in exe_name:
                return True

            # 2. Must have a valid title (filters out hidden system helper windows)
            title = win32gui.GetWindowText(hwnd_enum).strip()
            if not title:
                return True

            # 3. Check if it's minimized
            placement = win32gui.GetWindowPlacement(hwnd_enum)
            is_minimized = placement[1] == win32con.SW_SHOWMINIMIZED

            rect = win32gui.GetWindowRect(hwnd_enum)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]

            # CRITICAL FIX: Bypass the width check if we explicitly know it's minimized
            if is_minimized or (width > 300 and height > 300):
                main_hwnd = hwnd_enum
                return True
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass

    if main_hwnd != 0:
        return main_hwnd, win32gui.GetWindowText(main_hwnd).strip()
    return 0, ""


WPF_RESTORETOMAXIMIZED = 0x0002


def _focus_spotify_window(main_hwnd) -> bool:
    """Forces Spotify to foreground on Windows 11 and preserves Maximized state."""
    if main_hwnd == 0:
        return False
    try:
        # Check current state to avoid un-maximizing the window. A window that
        # is currently MINIMIZED but was maximized before must be restored back
        # to maximized — plain SW_RESTORE would shrink it to its normal rect,
        # which is what silently resized Spotify on every media command.
        placement = win32gui.GetWindowPlacement(main_hwnd)
        is_maximized = placement[1] == win32con.SW_SHOWMAXIMIZED or (
            placement[1] == win32con.SW_SHOWMINIMIZED
            and bool(placement[0] & WPF_RESTORETOMAXIMIZED)
        )
        show_cmd = win32con.SW_SHOWMAXIMIZED if is_maximized else win32con.SW_RESTORE

        fore_hwnd = win32gui.GetForegroundWindow()
        if fore_hwnd != main_hwnd:
            fore_thread, _ = win32process.GetWindowThreadProcessId(fore_hwnd)
            curr_thread = win32api.GetCurrentThreadId()

            win32process.AttachThreadInput(curr_thread, fore_thread, True)
            win32gui.ShowWindow(main_hwnd, show_cmd)
            win32gui.SetForegroundWindow(main_hwnd)
            win32process.AttachThreadInput(curr_thread, fore_thread, False)
        else:
            win32gui.ShowWindow(main_hwnd, show_cmd)
        time.sleep(0.15)
        return True
    except Exception:
        return False


def _get_spotify_volume_interface():
    """Finds the background Windows audio slider specifically for Spotify, with COM initialized."""
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process and session.Process.name().lower() == "spotify.exe":
                return session._ctl.QueryInterface(ISimpleAudioVolume)
    except Exception:
        pass
    return None


def _get_spotify_audio_session_state() -> int | None:
    """
    Returns Spotify's Windows audio session state when available.
    """
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process and session.Process.name().lower() == "spotify.exe":
                state = getattr(session, "State", None)
                if state is not None:
                    try:
                        return int(state)
                    except Exception:
                        return state
    except Exception:
        pass
    return None


def _spotify_is_playing() -> bool:
    """
    Best-effort playback detector.
    """
    state = _get_spotify_audio_session_state()
    if state is not None:
        return state == 1

    try:
        hwnd, title = _get_spotify_hwnd_and_title()
        return hwnd != 0 and bool(title) and " - " in title.lower()
    except Exception:
        return False


def _spotify_window_exists() -> bool:
    hwnd, _ = _get_spotify_hwnd_and_title()
    return hwnd != 0


def _send_spotify_command(app_command: int) -> bool:
    """
    Sends a targeted WM_APPCOMMAND directly into Spotify's process.
    """
    hwnd, _ = _get_spotify_hwnd_and_title()
    if hwnd != 0:
        win32gui.SendMessage(hwnd, 0x0319, 0, app_command << 16)
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# PLAYBACK METHODS (Direct Injection — Immune to Browser Media Locks)
# ─────────────────────────────────────────────────────────────────────────────

APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
APPCOMMAND_MEDIA_PLAY_PAUSE = 14
APPCOMMAND_MEDIA_PLAY = 46
APPCOMMAND_MEDIA_PAUSE = 47
VK_MEDIA_PLAY_PAUSE = 0xB3


def _launch_spotify_desktop() -> bool:
    """Launches Spotify through the URI handler, with EXE fallbacks for Windows installs."""
    launch_attempted = False
    try:
        if hasattr(os, "startfile"):
            os.startfile("spotify:")
            launch_attempted = True
        else:
            os.system('start "" "spotify:"')
            launch_attempted = True
    except Exception:
        pass

    possible_paths = [
        os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe"),
    ]

    for exe_path in possible_paths:
        try:
            if exe_path and os.path.exists(exe_path):
                subprocess.Popen(
                    [exe_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                )
                launch_attempted = True
                break
        except Exception:
            pass

    return launch_attempted


def _open_search(query: str) -> bool:
    """Enter a search cleanly. Preferred: UIA SetValue on Spotify's search box —
    focus-independent AND shows clean text ("chill music", not "chill+music"), and
    Chromium fires the input event so the live search reacts. Fallback (only if the
    box can't be reached): the `spotify:search:` URI, which is reliable but makes
    Spotify render the query with '+' for spaces. Requires the window to exist."""
    hwnd, _ = _get_spotify_hwnd_and_title()
    if hwnd != 0:
        from actions.spotify_ui import set_search_query

        if set_search_query(hwnd, query):
            return True
    return _open_spotify_uri(f"spotify:search:{urllib.parse.quote(query)}")


def _open_spotify_uri(uri: str) -> bool:
    """Opens a Spotify URI safely without going through a shell when possible."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(uri)
            return True
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        return True
    except Exception:
        pass
    try:
        os.system(f'start "" "{uri}"')
        return True
    except Exception:
        return False


def _press_global_media_play_pause() -> None:
    """Last-resort global media key fallback."""
    win32api.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    win32api.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, win32con.KEYEVENTF_KEYUP, 0)


def _wait_for_spotify_window(wait_s: float = 12.0, focus: bool = True) -> tuple[int, str]:
    """Waits until Spotify exposes a usable visible window."""
    deadline = time.time() + max(wait_s, 3.0)
    last_launch_attempt = 0.0

    while time.time() < deadline:
        hwnd, title = _get_spotify_hwnd_and_title()
        if hwnd != 0:
            if focus:
                _focus_spotify_window(hwnd)
            return hwnd, title

        if time.time() - last_launch_attempt > 2.0:
            _launch_spotify_desktop()
            last_launch_attempt = time.time()

        time.sleep(0.35)

    return 0, ""


def _wait_until(predicate, timeout_s: float, interval_s: float = 0.25) -> bool:
    """Polls predicate until it returns True or the timeout elapses."""
    deadline = time.time() + max(timeout_s, 0.1)
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def _verify_playback_started(timeout_s: float = 4.0) -> bool:
    """Polls for actual audio/title evidence that playback began, instead of
    assuming a click worked. This is what prevents 'pressed play too early'."""
    return _wait_until(_spotify_is_playing, timeout_s, interval_s=0.35)


def _content_region(hwnd: int) -> tuple[int, int, int, int] | None:
    """Screen rect of Spotify's main content pane — the band every click
    candidate targets (top result card + track list)."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    w, h = right - left, bottom - top
    if w < 300 or h < 300:
        return None
    return (
        left + int(w * 0.22),
        top + int(h * 0.12),
        max(1, int(w * 0.66)),
        max(1, int(h * 0.42)),
    )


def _region_fingerprint(region: tuple[int, int, int, int]) -> list[int] | None:
    """Downsampled grayscale fingerprint of a screen region.

    Two consecutive equal fingerprints mean the area stopped repainting; a
    fingerprint with almost no spread means it is still a blank/skeleton
    surface with nothing on it yet.
    """
    try:
        img = pyautogui.screenshot(region=region).convert("L").resize((28, 14))
        return list(img.getdata())
    except Exception:
        return None


def _fp_spread(fp: list[int]) -> float:
    """Population std-dev of a fingerprint — how much CONTENT is on screen."""
    n = len(fp)
    if not n:
        return 0.0
    mean = sum(fp) / n
    return (sum((v - mean) ** 2 for v in fp) / n) ** 0.5


def _fp_delta(a: list[int], b: list[int]) -> float:
    """Mean absolute difference between two fingerprints — how much MOVED."""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(p - q) for p, q in zip(a, b, strict=True)) / len(a)


# A rendered results page has real content (album art, text rows); a blank or
# skeleton pane is nearly uniform. Tuned to be forgiving — these are floors,
# not fingerprints of a specific layout.
_CONTENT_SPREAD_MIN = 8.0
_SETTLE_DELTA_MAX = 2.0


def _wait_for_page_settled(
    hwnd: int,
    timeout_s: float = 8.0,
    min_wait_s: float = 0.5,
    poll_s: float = 0.22,
) -> bool:
    """Block until Spotify's content pane has PAINTED and gone quiet.

    Spotify renders its pages in an embedded web view asynchronously, so the
    window can be focused, titled and idle-looking while the results list is
    still empty. Clicking then lands on blank canvas (or the page that was
    there before) and nothing plays — the "pressed play too early" bug. There
    is no native readiness signal, so watch the pane itself: wait until it
    carries real content AND two consecutive samples are identical, meaning the
    render finished rather than merely started.

    Returns True if the page settled, False on timeout (caller still proceeds —
    this is a gate that grants time, not one that blocks the action).
    """
    region = _content_region(hwnd)
    if region is None:
        time.sleep(min_wait_s)
        return False

    deadline = time.time() + max(timeout_s, min_wait_s)
    floor = time.time() + max(0.0, min_wait_s)
    prev: list[int] | None = None

    while time.time() < deadline:
        fp = _region_fingerprint(region)
        if fp is None:
            time.sleep(poll_s)
            continue

        if (
            time.time() >= floor
            and prev is not None
            and _fp_spread(fp) >= _CONTENT_SPREAD_MIN
            and _fp_delta(fp, prev) <= _SETTLE_DELTA_MAX
        ):
            return True

        prev = fp
        time.sleep(poll_s)

    print("[Spotify] Page did not settle before the deadline — clicking anyway.")
    return False


def _wait_for_spotify_ui_ready(cold_start: bool) -> tuple[int, str]:
    """Waits until Spotify's window exists AND its UI is rendered enough to
    accept navigation hotkeys/clicks. On a cold start the window appears
    seconds before the web view finishes loading, so an extra render grace
    period is required."""
    hwnd, title = _wait_for_spotify_window(wait_s=15.0 if cold_start else 6.0, focus=True)
    if hwnd == 0:
        return 0, ""

    # The title bar reads "Spotify"/"Spotify Premium" once the shell is up;
    # an empty title means the window is still initializing.
    _wait_until(lambda: bool(_get_spotify_hwnd_and_title()[1]), 5.0, 0.3)

    # Cold start: give the embedded web view time to render the home page.
    # There is no window-level signal for this, so use a generous grace that
    # the click-verify-retry loop below can absorb if it's still not enough.
    time.sleep(2.8 if cold_start else 0.6)
    return _get_spotify_hwnd_and_title()


def _resume_spotify_playback(max_attempts: int = 6) -> bool:
    for attempt in range(max_attempts):
        if _spotify_is_playing_by_title():
            return True

        hwnd, _ = _get_spotify_hwnd_and_title()
        if hwnd == 0:
            _launch_spotify_desktop()
            time.sleep(0.8)
            continue

        _focus_spotify_window(hwnd)

        _send_spotify_command(APPCOMMAND_MEDIA_PLAY)
        time.sleep(0.8)
        if _spotify_is_playing_by_title():
            return True

        if attempt >= 2:
            _send_spotify_command(APPCOMMAND_MEDIA_PLAY_PAUSE)
            time.sleep(0.8)
            if _spotify_is_playing_by_title():
                return True

        if attempt >= 4:
            _press_global_media_play_pause()
            time.sleep(0.8)
            if _spotify_is_playing_by_title():
                return True

    return _spotify_is_playing_by_title()


def _ensure_spotify_started_and_focused(wait_s: float = 12.0) -> tuple[int, str]:
    _enforce_spotify_priority()
    _launch_spotify_desktop()

    hwnd, title = _wait_for_spotify_window(wait_s=wait_s, focus=True)
    if hwnd != 0:
        time.sleep(0.8)
        _resume_spotify_playback(max_attempts=3)
        hwnd, title = _get_spotify_hwnd_and_title()

    return hwnd, title


def _background_open_and_resume(wait_s: float = 18.0) -> None:
    try:
        _enforce_spotify_priority()
        _launch_spotify_desktop()
        hwnd, _ = _wait_for_spotify_window(wait_s=wait_s, focus=True)
        if hwnd != 0:
            time.sleep(0.8)
            _resume_spotify_playback(max_attempts=5)
            try:
                from core.media_state import set_status

                set_status("spotify", "playing")
            except Exception:
                pass
    except Exception as e:
        print(f"[Spotify] Background open/resume failed: {e}")


def _start_background_open_and_resume() -> None:
    threading.Thread(target=_background_open_and_resume, daemon=True).start()


def _mark_spotify_playing_state() -> None:
    try:
        from core.media_state import set_status

        set_status("spotify", "playing")
    except Exception:
        pass


def _spotify_open_and_resume() -> str:
    return _spotify_play()


def _spotify_play():
    threading.Thread(target=_enforce_spotify_priority, daemon=True).start()

    if _spotify_is_playing():
        # The detector lags: right after a pause the Windows audio session still
        # reads active, so a resume said "already playing" and did nothing (ZORO
        # full test, 25/09/2026). MEDIA_PLAY is explicit, not a toggle -- sending
        # it to a playing Spotify is a no-op -- so send it anyway, unfocused.
        _send_spotify_command(APPCOMMAND_MEDIA_PLAY)
        _mark_spotify_playing_state()
        return "Spotify is playing, sir."

    hwnd, _ = _get_spotify_hwnd_and_title()

    if hwnd == 0:
        _launch_spotify_desktop()
        hwnd, _ = _wait_for_spotify_window(wait_s=10.0, focus=True)
        if hwnd == 0:
            return "Opening Spotify, sir."

        # Wait for the client to be ready before commanding playback, then
        # verify it actually started and retry through the fallback chain.
        time.sleep(2.5)
        if not _spotify_is_playing():
            _send_spotify_command(APPCOMMAND_MEDIA_PLAY)
            if not _verify_playback_started(timeout_s=3.0):
                _resume_spotify_playback(max_attempts=3)

        _mark_spotify_playing_state()
        return "Spotify opened and music resumed, sir."

    _focus_spotify_window(hwnd)
    _send_spotify_command(APPCOMMAND_MEDIA_PLAY)
    _mark_spotify_playing_state()
    return "Spotify music resumed, sir."


def _spotify_is_playing_by_title() -> bool:
    try:
        hwnd, title = _get_spotify_hwnd_and_title()
        if hwnd != 0 and title:
            if " - " in title:
                return True
            return False
    except Exception:
        pass
    return False


def _spotify_pause():
    """
    CRITICAL FIX: Uses the explicit APPCOMMAND_MEDIA_PAUSE (47).
    Also checks if it's already paused to prevent accidental un-pausing.
    """
    # Anti-Toggle Protection: if it looks paused, never send a TOGGLE. The
    # explicit MEDIA_PAUSE is still sent: it is a no-op on a paused Spotify, and
    # the detector can lag behind a resume (25/09/2026, see _spotify_play).
    if not _spotify_is_playing():
        _send_spotify_command(APPCOMMAND_MEDIA_PAUSE)
        try:
            from core.media_state import set_status

            set_status("spotify", "paused")
        except Exception:
            pass
        return "Spotify is paused, sir."

    # Try the explicit hard pause command first
    if not _send_spotify_command(APPCOMMAND_MEDIA_PAUSE):
        # Fallback to play/pause toggle if the hard pause fails
        if not _send_spotify_command(APPCOMMAND_MEDIA_PLAY_PAUSE):
            _press_global_media_play_pause()

    try:
        from core.media_state import set_status

        set_status("spotify", "paused")
    except Exception:
        pass

    return "Spotify paused securely, sir."


def _spotify_next():
    # 1. Silently pause YouTube/Netflix in the background first
    threading.Thread(target=_enforce_spotify_priority, daemon=True).start()

    if not _send_spotify_command(APPCOMMAND_MEDIA_NEXTTRACK):
        win32api.keybd_event(0xB0, 0, 0, 0)
        win32api.keybd_event(0xB0, 0, win32con.KEYEVENTF_KEYUP, 0)

    # 2. Tell Era One's brain that music is officially playing
    _mark_spotify_playing_state()
    return "Skipping track."


def _spotify_previous():
    # 1. Silently pause YouTube/Netflix in the background first
    threading.Thread(target=_enforce_spotify_priority, daemon=True).start()

    if _send_spotify_command(APPCOMMAND_MEDIA_PREVIOUSTRACK):
        time.sleep(0.3)
        _send_spotify_command(APPCOMMAND_MEDIA_PREVIOUSTRACK)
    else:
        win32api.keybd_event(0xB1, 0, 0, 0)
        win32api.keybd_event(0xB1, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.3)
        win32api.keybd_event(0xB1, 0, 0, 0)
        win32api.keybd_event(0xB1, 0, win32con.KEYEVENTF_KEYUP, 0)

    # 2. Tell Era One's brain that music is officially playing
    _mark_spotify_playing_state()
    return "Previous track."


# ─────────────────────────────────────────────────────────────────────────────
# INDEPENDENT VOLUME CONTROLS (Hybrid Background + Hardware Fallback)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# INDEPENDENT VOLUME CONTROLS (Strict Internal UI Macro)
# ─────────────────────────────────────────────────────────────────────────────


def _spotify_volume_up():
    """Increases Spotify's internal slider directly via UI macro."""
    original_hwnd = win32gui.GetForegroundWindow()
    hwnd, _ = _get_spotify_hwnd_and_title()

    if hwnd != 0:
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            is_minimized = placement[1] == win32con.SW_SHOWMINIMIZED

            _focus_spotify_window(hwnd)
            time.sleep(0.15)

            win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            time.sleep(0.05)
            pyautogui.press("up")
            time.sleep(0.1)
            pyautogui.press("up")
            time.sleep(0.05)
            win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

            time.sleep(0.15)

            if is_minimized:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)

            if original_hwnd and original_hwnd != hwnd:
                try:
                    win32gui.SetForegroundWindow(original_hwnd)
                except Exception:
                    pass

            return "Spotify volume increased, sir."
        except Exception as e:
            return f"Volume adjustment failed: {e}"

    return "Spotify is not running or visible, sir."


def _spotify_volume_down():
    """Decreases Spotify's internal slider directly via UI macro."""
    original_hwnd = win32gui.GetForegroundWindow()
    hwnd, _ = _get_spotify_hwnd_and_title()

    if hwnd != 0:
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            is_minimized = placement[1] == win32con.SW_SHOWMINIMIZED

            _focus_spotify_window(hwnd)
            time.sleep(0.15)

            win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            time.sleep(0.05)
            pyautogui.press("down")
            time.sleep(0.1)
            pyautogui.press("down")
            time.sleep(0.05)
            win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

            time.sleep(0.15)

            if is_minimized:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)

            if original_hwnd and original_hwnd != hwnd:
                try:
                    win32gui.SetForegroundWindow(original_hwnd)
                except Exception:
                    pass

            return "Spotify volume decreased, sir."
        except Exception as e:
            return f"Volume adjustment failed: {e}"

    return "Spotify is not running or visible, sir."


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH, LAYOUT RATIOS AND MACROS
# ─────────────────────────────────────────────────────────────────────────────


def _spotify_search_and_play(query: str):
    """Search for a track and play the best-matching result via the accessibility
    tree: each result exposes a "Play <title>" button, so the query is matched
    against those names and the winner is invoke()d. No pixel-ratio clicks, so a
    layout change or window resize can never send the click to the wrong result
    (the old failure mode)."""
    query = str(query or "").strip()
    if not query:
        return "Spotify search failed: no song name was provided, sir."

    threading.Thread(target=_enforce_spotify_priority, daemon=True).start()

    spotify_was_closed = not _spotify_window_exists()

    # The window must exist before UIA can drive its search box (launch if cold).
    hwnd, _ = _wait_for_spotify_ui_ready(spotify_was_closed)
    if hwnd == 0:
        return "Spotify window verification failed."

    # Snapshot what is playing NOW, so success means "the track changed", not
    # "a button accepted invoke()". 2026-09-28: "Snoop Dogg" reported "Playing
    # 'Snoop Dogg'" while Sunroof by Nicky Youre kept playing — invoke() on a
    # hover-revealed row button returned cleanly and did nothing, and the old
    # check (is the audio session active?) passed on the song already playing.
    before = _now_playing()

    _open_search(query)  # UIA SetValue (clean box) -> URI fallback

    from actions.spotify_ui import invoke_search_result

    timeout = 10.0 if spotify_was_closed else 8.0
    ok, _how = invoke_search_result(hwnd, query, timeout_s=timeout)
    if not ok:
        return f"I searched for '{query}' but could not find a playable result, sir."

    now = _wait_for_new_track(before, query)
    if now is None and before is not None:
        # First attempt did not change the track: retry with a real click.
        hwnd, _ = _get_spotify_hwnd_and_title()
        if hwnd:
            _focus_spotify_window(hwnd)
            ok, _how = invoke_search_result(hwnd, query, timeout_s=4.0, prefer_click=True)
            if ok:
                now = _wait_for_new_track(before, query)
        if now is None:
            title, artist = before
            return (
                f"I found '{query}' in Spotify but could not start it — "
                f"'{title}' by {artist} is still playing, sir."
            )

    if now is None:
        # No GSMTC reading at all (media session unavailable): fall back to the
        # audio-session check rather than claim a track we cannot see.
        if not _verify_playback_started(timeout_s=3.0):
            return f"I clicked '{query}' in Spotify but playback did not start, sir."
        _mark_spotify_playing_state()
        return f"Playing '{query}', sir."

    _mark_spotify_playing_state()
    title, artist = now
    return f"Playing '{title}' by {artist} (search: '{query}'), sir."


def _now_playing() -> tuple[str, str] | None:
    """(title, artist) of Spotify's current media session, or None when there is
    no session / the reader is unavailable. Never raises."""
    try:
        from actions import spotify_gsmtc_host

        data = spotify_gsmtc_host.request({"op": "current_track"}, timeout=3.0)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("ok") or not data.get("title"):
        return None
    return str(data.get("title", "")).strip(), str(data.get("artist", "")).strip()


def _track_matches(query: str, track: tuple[str, str]) -> bool:
    """True when every query word appears in the track's title or artist."""
    from actions.spotify_ui import _norm

    qtokens = set(_norm(query).split())
    have = set(_norm(f"{track[0]} {track[1]}").split())
    return bool(qtokens) and qtokens.issubset(have)


def _wait_for_new_track(
    before: tuple[str, str] | None, query: str, timeout_s: float = 5.0
) -> tuple[str, str] | None:
    """Poll until the playing track differs from `before` (or already matches the
    query, e.g. re-searching the current song) AND audio is actually playing.
    Returns the new (title, artist), or None if nothing changed in time."""
    found: list[tuple[str, str]] = []

    def _changed() -> bool:
        now = _now_playing()
        if now is None:
            return False
        if now != before or _track_matches(query, now):
            if _spotify_is_playing():
                found.append(now)
                return True
        return False

    _wait_until(_changed, timeout_s, interval_s=0.4)
    return found[-1] if found else None


def _uia_play_page(label, success_msg, *, uri=None, hotkey_letter=None):
    """Navigate to a Spotify page and start it via the accessibility tree's
    primary Play control — no pixel-ratio clicks, so a layout change or resize
    can never send the click to the wrong place (the old failure mode).

    Navigation is by stable URI when one exists (Liked Songs), else by Spotify's
    own keyboard shortcut. Playback is triggered with UIA `invoke()` on the
    page's "Play <context>" button, then verified.
    """
    # Silently pause YouTube/Netflix in the background first
    threading.Thread(target=_enforce_spotify_priority, daemon=True).start()

    cold_start = not _spotify_window_exists()

    if uri:
        _open_spotify_uri(uri)
        hwnd, _ = _wait_for_spotify_ui_ready(cold_start)
    else:
        hwnd, _ = _wait_for_spotify_ui_ready(cold_start)
        if hwnd:
            _focus_spotify_window(hwnd)
            pyautogui.hotkey("alt", "shift", hotkey_letter)  # Spotify's own shortcut, not a click
    if hwnd == 0:
        return f"I could not open Spotify to play {label}, sir."

    _wait_for_page_settled(hwnd, timeout_s=10.0 if cold_start else 7.0, min_wait_s=1.0)
    hwnd, _ = _get_spotify_hwnd_and_title()
    if hwnd == 0:
        return f"I could not open Spotify to play {label}, sir."

    from actions.spotify_ui import invoke_primary_play

    ok, _how = invoke_primary_play(hwnd, timeout_s=10.0 if cold_start else 8.0)
    if ok:
        _verify_playback_started(timeout_s=3.0)
        _mark_spotify_playing_state()
        return success_msg
    # The accessibility tree did not expose a Play control; report honestly rather
    # than fall back to a blind coordinate click.
    if _spotify_is_playing():
        _mark_spotify_playing_state()
        return success_msg
    return f"I opened {label} in Spotify but could not start playback from the controls, sir."


def _spotify_play_liked_songs():
    # Liked Songs has a stable URI, so navigation is exact and resize-proof.
    return _uia_play_page(
        "Liked Songs", "Playing your Liked Songs, sir.", uri="spotify:collection:tracks"
    )


def _spotify_play_new_releases():
    return _uia_play_page("New Releases", "Playing New Releases, sir.", hotkey_letter="n")


def _spotify_play_made_for_you():
    return _uia_play_page(
        "Made For You mixes", "Playing Made For You mixes, sir.", hotkey_letter="m"
    )


def _spotify_like():
    VK_MENU = 0x12
    VK_SHIFT = 0x10
    VK_B = 0x42
    KEYEVENTF_KEYUP = 0x0002

    original_hwnd = win32gui.GetForegroundWindow()
    main_hwnd, _ = _get_spotify_hwnd_and_title()

    if main_hwnd != 0:
        try:
            placement = win32gui.GetWindowPlacement(main_hwnd)
            is_minimized = placement[1] == win32con.SW_SHOWMINIMIZED

            _focus_spotify_window(main_hwnd)
            time.sleep(0.5)

            ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_SHIFT, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_B, 0, 0, 0)
            time.sleep(0.05)

            ctypes.windll.user32.keybd_event(VK_B, 0, KEYEVENTF_KEYUP, 0)
            ctypes.windll.user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
            ctypes.windll.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

            time.sleep(0.2)

            if is_minimized:
                win32gui.ShowWindow(main_hwnd, win32con.SW_MINIMIZE)

            if original_hwnd and original_hwnd != main_hwnd:
                try:
                    win32gui.SetForegroundWindow(original_hwnd)
                except Exception:
                    pass

            return "Track liked successfully, sir."
        except Exception as e:
            return f"Keystroke delivery failed: {e}"
    return "Spotify is not running, sir."


def _spotify_status() -> str:
    """Cheap, vision-free playback status.

    This exists so Era never needs screen_process (a 12s+ vision round-trip)
    just to answer "did the playlist actually start". Everything here is read
    straight from the Windows audio session and the window title.
    """
    hwnd, _title = _get_spotify_hwnd_and_title()
    if hwnd == 0:
        return "Spotify is not running, sir."

    playing = _spotify_is_playing()
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            window_state = "minimized"
        elif placement[1] == win32con.SW_SHOWMAXIMIZED:
            window_state = "maximized"
        else:
            window_state = "windowed"
    except Exception:
        window_state = "unknown"

    if playing:
        return f"Spotify is playing ({_spotify_current_track()}). Window is {window_state}."
    return f"Spotify is running but not playing. Window is {window_state}."


def _spotify_minimize() -> str:
    """Minimize the Spotify window without stealing focus or raising others."""
    hwnd, _ = _get_spotify_hwnd_and_title()
    if hwnd == 0:
        return "Spotify is not running, sir."
    try:
        if win32gui.IsIconic(hwnd):
            return "Spotify is already minimized, sir."
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        if _wait_until(lambda: win32gui.IsIconic(hwnd), 1.0, 0.05):
            return "Spotify minimized, sir."
        win32gui.PostMessage(hwnd, 0x0112, 0xF020, 0)  # WM_SYSCOMMAND / SC_MINIMIZE
        if _wait_until(lambda: win32gui.IsIconic(hwnd), 1.0, 0.05):
            return "Spotify minimized, sir."
        return "I could not minimize Spotify, sir."
    except Exception as e:
        return f"Minimize failed: {e}"


def _spotify_current_track() -> str:
    try:
        hwnd, title = _get_spotify_hwnd_and_title()
        if hwnd == 0 or not title:
            return "Spotify is not running, sir."

        title_lower = title.lower()

        # If it's just sitting on the menu, it's not playing a track yet.
        # The window title may lag behind a fresh play command, so poll
        # briefly (up to 3 s) for the title to update.
        if title_lower in ("spotify", "spotify premium", "spotify free"):
            for _ in range(6):
                time.sleep(0.5)
                hwnd, title = _get_spotify_hwnd_and_title()
                if not hwnd or not title:
                    return "Spotify is not running, sir."
                title_lower = title.lower()
                if title_lower not in ("spotify", "spotify premium", "spotify free"):
                    break
            else:
                return "No track is currently playing on Spotify, sir."

        if " - " in title:
            # Clean up the string if Spotify occasionally appends its name
            track_part = title
            if track_part.lower().endswith(" - spotify"):
                track_part = track_part[:-10].strip()

            # Spotify's format is usually "Artist - Song"
            parts = track_part.split(" - ", 1)
            if len(parts) == 2:
                artist, song = parts[0].strip(), parts[1].strip()
                return f"Now playing: '{song}' by {artist}, sir."
            else:
                return f"Now playing on Spotify: {track_part}, sir."

        return f"Now playing on Spotify: {title}, sir."
    except Exception as e:
        return f"Could not retrieve current track: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ROUTER ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────


def spotify_control(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()
    print(f"[Spotify] Executing control: {action!r}")

    if player:
        player.write_log(f"[Spotify] {action}")

    try:
        remember_action(tool="spotify_control", action=action, app="spotify")
    except Exception:
        pass

    raw_action = action
    value_text = (
        " ".join(
            str(params.get(key, ""))
            for key in ("value", "query", "text", "utterance", "command", "user_input")
            if params.get(key) is not None
        )
        .lower()
        .strip()
    )
    intent_text = f"{raw_action} {value_text}".lower().strip()

    play_phrases = (
        "play music",
        "play some music",
        "some music",
        "start music",
        "resume music",
        "continue music",
        "put music",
        "put some music",
        "music please",
        "spotify music",
    )
    open_phrases = (
        "open spotify",
        "launch spotify",
        "start spotify",
        "run spotify",
    )

    # Phrase sniffing is for vague verbs only. An explicit verb such as `search`
    # must never be rewritten by words in its own query ("chill music please"
    # contains "music please" and used to turn the search into a plain resume).
    explicit = action in _EXPLICIT_ACTIONS or action in _EXACT_ACTIONS

    if action in (
        "play",
        "play_pause",
        "toggle",
        "resume",
        "music",
        "play_music",
        "some_music",
        "start_music",
        "resume_music",
        "continue_music",
        "play_some_music",
        "nothing_playing",
    ) or (not explicit and any(phrase in intent_text for phrase in play_phrases)):
        action = "play"
    elif action in (
        "open",
        "launch",
        "start",
        "open_spotify",
        "launch_spotify",
        "start_spotify",
    ) or (not explicit and any(phrase in intent_text for phrase in open_phrases)):
        action = "open"
    elif action in ("skip", "forward"):
        action = "next"
    elif action in ("back", "rewind"):
        action = "previous"

    try:
        result = _dispatch(action, params)
    except Exception as e:
        return f"Spotify routing failed: {e}"

    # STANDING RULE: "minimize Spotify after use". Enforced HERE rather than
    # left to the model — honoring it from the prompt would require Era to chain
    # a second tool call after this one already returned a success message, and
    # she does not do that reliably, so the rule was saved and then silently
    # ignored. Only actions that actually surface the window trigger it, and
    # only once the action succeeded.
    if action in _FOREGROUNDING_ACTIONS:
        try:
            from core.user_prefs import is_post_action_minimize

            if is_post_action_minimize("spotify"):
                _minimize_after_use()
        except Exception as e:
            print(f"[Spotify] Post-action minimize skipped: {e}")

    return result


# Verbs that already say exactly what to do; the phrase heuristics above skip them.
_EXPLICIT_ACTIONS = {
    "pause",
    "pause_if_running_only",
    "search",
    "liked_songs",
    "new_releases",
    "made_for_you",
    "volume_up",
    "volume_down",
    "like_track",
    "like",
    "dislike_track",
    "dislike",
    "current_track",
    "status",
    "is_playing",
    "verify",
    "check",
    "minimize",
    "next",
    "previous",
}


# Actions that bring the Spotify window forward (focus/click/keystroke). These
# are the ones "after use" refers to; status/current_track never raise it.
_FOREGROUNDING_ACTIONS = {
    "play",
    "open",
    "next",
    "previous",
    "search",
    "liked_songs",
    "new_releases",
    "made_for_you",
    "like_track",
    "like",
    "dislike_track",
    "dislike",
    "volume_up",
    "volume_down",
}


def _minimize_after_use() -> None:
    """Minimize Spotify once playback has actually settled.

    Minimizing the instant the click returns can race the client's own
    post-click focus work, so wait briefly for playback to be real first. The
    minimize is best-effort: it must never turn a successful play into a
    reported failure.
    """
    _verify_playback_started(timeout_s=3.0)
    time.sleep(0.4)
    msg = _spotify_minimize()
    print(f"[Spotify] Post-action minimize (standing user rule): {msg}")


def _dispatch(action: str, params: dict) -> str:
    if action == "play":
        return _spotify_play()
    elif action == "open":
        return _spotify_open_and_resume()
    elif action == "pause":
        return _spotify_pause()
    elif action == "pause_if_running_only":
        hwnd, _ = _get_spotify_hwnd_and_title()
        if hwnd == 0:
            return "Spotify is not running, sir."
        return _spotify_pause()
    elif action == "next":
        return _spotify_next()
    elif action == "previous":
        return _spotify_previous()
    elif action == "search":
        search_query = (
            params.get("value")
            or params.get("query")
            or params.get("text")
            or params.get("utterance")
            or params.get("command")
            or ""
        )
        return _spotify_search_and_play(search_query)
    elif action == "liked_songs":
        return _spotify_play_liked_songs()
    elif action == "new_releases":
        return _spotify_play_new_releases()
    elif action == "made_for_you":
        return _spotify_play_made_for_you()
    elif action == "volume_up":
        return _spotify_volume_up()
    elif action == "volume_down":
        return _spotify_volume_down()
    elif action in ("like_track", "like", "dislike_track", "dislike"):
        return _spotify_like()
    elif action == "current_track":
        return _spotify_current_track()
    elif action in ("status", "is_playing", "verify", "check"):
        return _spotify_status()
    elif action == "minimize":
        return _spotify_minimize()

    # ── Exact volume (pycaw) + track-as-data (WinRT GSMTC) ──────────────────
    # Delegated to actions/spotify_exact.py so this already-large file does not
    # grow further. Data/state verbs return JSON so the model gets structured
    # fields, not a spoken sentence. These are window-automation lane, not bridge.
    elif action in _EXACT_ACTIONS:
        return json.dumps(_EXACT_ACTIONS[action](params))

    return f"Unknown Spotify action: '{action}'."


# Exact-control verbs delegated to actions/spotify_exact.py. Each maps the
# _dispatch token to a callable taking the params dict and returning a dict.
def _exact(name, *, arg=None):
    """Build a params->dict caller for a spotify_exact function, pulling `arg`
    from params when the function needs one (percent / delta / seconds)."""

    def _call(params):
        from actions import spotify_exact

        fn = getattr(spotify_exact, name)
        if arg is None:
            return fn()
        value = params.get(arg)
        if value is None and arg == "percent":
            value = params.get("value")
        return fn(value)

    return _call


_EXACT_ACTIONS = {
    "get_volume_exact": _exact("get_volume_exact"),
    "set_volume_exact": _exact("set_volume_exact", arg="percent"),
    "volume_delta": _exact("volume_delta", arg="delta"),
    "mute_exact": _exact("mute_exact"),
    "unmute_exact": _exact("unmute_exact"),
    "toggle_mute_exact": _exact("toggle_mute_exact"),
    "current_track_data": _exact("current_track"),
    "playback_state": _exact("playback_state"),
    "progress": _exact("progress"),
    "seek_exact": _exact("seek_exact", arg="seconds"),
    # Tokens are seek_forward/seek_rewind, NOT forward/rewind: the router above
    # normalizes "forward"->next and "rewind"->previous (skip-track), which would
    # hijack a seek into a track change.
    "seek_forward": _exact("forward", arg="seconds"),
    "seek_rewind": _exact("rewind", arg="seconds"),
}
