# actions/open_app.py
"""
High-performance Windows Application Launcher for Era One.
Features:
- Ultra-fast Start Menu Shortcut (.lnk) indexing (instant alternative to deep disk walking)
- Native Windows Search Bar GUI injection fallback
- Registry App Paths scanning
- Already-running window detection & absolute focus injection
"""

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

if sys.platform == "win32":
    import winreg

    import win32con
    import win32gui

    try:
        import keyboard
        import pyautogui  # noqa: F401  (availability probe: presence sets the capability flag)

        _GUI_AUTOMATION = True
    except ImportError:
        _GUI_AUTOMATION = False

from core.intent_memory import remember_action
from core.win_subprocess import NO_WINDOW

# Harmonized Master Registry for precise process cross-referencing
MASTER_APP_REGISTRY = {
    "spotify": {"exe": "Spotify.exe", "class": "SpotifyMainWindow"},
    "brave": {"exe": "brave.exe", "class": "Chrome_WidgetWin_1"},
    "chrome": {"exe": "chrome.exe", "class": "Chrome_WidgetWin_1"},
    "edge": {"exe": "msedge.exe", "class": "Chrome_WidgetWin_1"},
    "firefox": {"exe": "firefox.exe", "class": "MozillaWindowClass"},
    "discord": {"exe": "Discord.exe", "class": "Chrome_WidgetWin_1"},
    "calculator": {"exe": "ApplicationFrameHost.exe", "class": "ApplicationFrameWindow"},
    "vscode": {"exe": "Code.exe", "class": "Chrome_WidgetWin_1"},
    "visual studio code": {"exe": "Code.exe", "class": "Chrome_WidgetWin_1"},
    "code": {"exe": "Code.exe", "class": "Chrome_WidgetWin_1"},
    "photoshop": {"exe": "Photoshop.exe", "class": "photoshop"},
    "illustrator": {"exe": "Illustrator.exe", "class": "illustrator_app"},
    "premiere": {"exe": "Premiere.exe", "class": "Premiere Pro"},
    "premiere pro": {"exe": "Premiere.exe", "class": "Premiere Pro"},
    "whatsapp": {"exe": "WhatsApp.exe", "class": "ApplicationFrameWindow"},
    "telegram": {"exe": "Telegram.exe", "class": "TelegramWidget"},
    "neat download manager": {"exe": "NeatDM.exe", "class": "NeatDownloadManagerClass"},
}


def get_executable_from_registry(app_name: str) -> str | None:
    """Return absolute path from Windows Registry (App Paths)."""
    if sys.platform != "win32":
        return None

    target_key = app_name.lower().strip()
    spec = MASTER_APP_REGISTRY.get(target_key)
    target_exe = spec["exe"] if spec else f"{app_name}.exe"

    registry_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\App Paths",
    ]

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for reg_sub in registry_paths:
            try:
                key_path = f"{reg_sub}\\{target_exe}"
                key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ)
                exe_path, _ = winreg.QueryValueEx(key, "")
                winreg.CloseKey(key)
                if exe_path:
                    clean = exe_path.strip('"')
                    if os.path.exists(clean):
                        return clean
            except Exception:
                continue
    return None


def scan_start_menu_shortcuts(app_name: str) -> str | None:
    """
    Instantly searches Windows Start Menu directories for .lnk shortcuts matching the app name.
    This eliminates slow disk-walking completely.
    """
    if sys.platform != "win32":
        return None

    target_phrase = app_name.lower().strip()

    # Common system locations where shortcuts are saved upon installation
    shortcut_roots = [
        Path(os.environ.get("ALLUSERSPROFILE", "C:\\ProgramData"))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]

    for root in shortcut_roots:
        if not root.exists():
            continue
        try:
            # Search for shortcut files matching the name patterns cleanly and fast
            for path in root.rglob("*.lnk"):
                if target_phrase in path.name.lower():
                    return str(path)
        except Exception:
            continue
    return None


def _normalize_app_token(app_name: str) -> str:
    return (app_name or "").strip().lower()


def _get_matching_window_handles(app_query: str) -> list[int]:
    """Best-effort: return window HWNDs whose title contains the query."""
    if sys.platform != "win32":
        return []
    if not app_query:
        return []

    query = app_query.lower().strip()
    handles: list[int] = []

    def _enum(hwnd, acc):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip().lower()
            if query in title:
                acc.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_enum, handles)
    except Exception:
        pass

    return handles


def _try_close_window_handles(handles: list[int]) -> bool:
    """Try graceful close via WM_CLOSE; return True if any handle close signal was sent."""
    if sys.platform != "win32":
        return False
    if not handles:
        return False

    sent = False
    for hwnd in handles:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            sent = True
        except Exception:
            pass
    return sent


def _force_alt_f4_close_window(handles: list[int]) -> bool:
    """Fallback close by Alt+F4 on foreground window."""
    if sys.platform != "win32":
        return False
    if not _GUI_AUTOMATION:
        return False
    if not handles:
        return False

    try:
        hwnd = handles[0]
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        keyboard.send("alt+f4")
        return True
    except Exception:
        return False


def _launch_via_windows_search_bar_verified(app_name: str) -> bool:
    """
    Fallback injection that interacts with the Windows desktop shell to force
    an application launch using the OS Search layout indexing mechanism.
    """
    if not _GUI_AUTOMATION:
        return False

    try:
        print(f"[open_app] Engaging hardware automation layer to launch: '{app_name}'")
        # Tap the Windows key to call up the Start Menu / Search UI layout cleanly
        keyboard.send("windows")
        time.sleep(0.3)  # Allow UI thread frame to change state safely

        # Type target application identifier match sequence directly into shell focus line
        keyboard.write(app_name, delay=0.02)
        time.sleep(0.4)

        # Execute target line selection handle activation sequence
        keyboard.send("enter")
        return True
    except Exception as e:
        print(f"[open_app] Search Bar GUI mapping error: {e}")
        return False


def _find_spotify_hwnd() -> int:
    """Spotify's main window, or 0. Current Spotify builds are CEF apps whose
    window class is Chrome_WidgetWin_1 (checked 25/09/2026), so the old
    "SpotifyMainWindow" class never matched: "open spotify" relaunched an
    already-running Spotify and waited out a 6s timeout. Match by PROCESS."""
    if sys.platform != "win32":
        return 0
    hwnd = win32gui.FindWindow("SpotifyMainWindow", None)
    if hwnd:
        return hwnd
    found: list[int] = []

    def enum_spotify(h, acc):
        if not win32gui.IsWindowVisible(h) or not win32gui.GetWindowText(h):
            return True
        try:
            import psutil
            import win32process

            pid = win32process.GetWindowThreadProcessId(h)[1]
            if psutil.Process(pid).name().lower() == "spotify.exe":
                acc.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(enum_spotify, found)
    except Exception:
        pass
    return found[0] if found else 0


def _wait_for_spotify_window(timeout: float = 4.0, poll_interval: float = 0.4) -> int:
    """Wait until the Spotify desktop window appears and return its HWND."""
    if sys.platform != "win32":
        return 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_spotify_hwnd()
        if hwnd:
            return hwnd
        time.sleep(poll_interval)
    return 0


def _resume_spotify(delay: float = 0.25, attempts: int = 3, interval: float = 0.25) -> None:
    """Force Spotify to start playing after launch or focus."""
    time.sleep(delay)
    try:
        from actions.computer_settings import spotify_play

        for _ in range(attempts):
            result = spotify_play()
            lowered = result.lower()
            if any(token in lowered for token in ("sent", "fired", "resumed", "playing")):
                return
            time.sleep(interval)
    except Exception as e:
        print(f"[open_app] Could not resume Spotify: {e}")


def _ensure_spotify_launched(spotify_exe: str | None = None) -> None:
    """Launch Spotify using the executable path or protocol handler."""
    if spotify_exe:
        try:
            subprocess.Popen([spotify_exe], shell=False, creationflags=NO_WINDOW)
            return
        except Exception:
            pass

    try:
        os.startfile("spotify:")
    except Exception:
        subprocess.Popen(["cmd", "/c", "start", "spotify:"], shell=False, creationflags=NO_WINDOW)


def _play_spotify_after_open() -> None:
    """Attempt to restore Spotify playback after the app has been launched."""
    try:
        from actions.media_coordinator import media_coordinator

        result = media_coordinator({"target": "spotify", "action": "play"})
        if any(x in result.lower() for x in ("failed", "unknown", "could not", "not running")):
            raise RuntimeError(result)
    except Exception:
        _resume_spotify(delay=0.35, attempts=4, interval=0.25)


def open_app(parameters=None, response=None, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    app_name = str(parameters.get("app_name", "")).strip()
    mode = str(parameters.get("mode", "open") or "open").strip().lower()

    # Heuristic: if user says close/quit/exit in the app_name text, treat as toggle-close.
    # (LLM sometimes passes the whole phrase into app_name.)
    if mode in {"open", ""} and any(
        k in app_name.lower()
        for k in ["close ", "close it", "exit", "quit", "shutdown app", "terminate"]
    ):
        mode = "close"

    do_toggle = mode in {"toggle", "toggle_open_close", "open_or_close"}
    if do_toggle:
        mode = "toggle"

    if not app_name:
        return "No application name provided."

    try:
        media_app = "spotify" if "spotify" in app_name.lower() else None
        remember_action(tool="open_app", action="open_app", app=app_name, media_app=media_app)
    except Exception:
        pass

    # ----- 1. URL Handling Mode -----
    if app_name.startswith(("http://", "https://")) or ".com" in app_name or ".org" in app_name:
        webbrowser.open(app_name)
        return "Opening link in your default browser, sir."

    print(f"[open_app] Launch request: '{app_name}' (mode={mode})")

    # ----- 2. Spotify Custom Path Override Sequence -----
    # 25/09/2026: OPENING Spotify no longer starts playback. "open spotify"
    # used to launch/focus AND run media_coordinator play (which first pauses
    # every browser tab over CDP): 12s, and music Karim hadn't asked for. Play
    # only when the call asks for it -- mode "play" or play=true. "Play music"
    # itself goes to spotify_control / media_coordinator, never here.
    want_play = mode == "play" or str(parameters.get("play", "")).lower() in {"1", "true", "yes"}
    if "spotify" in app_name.lower():
        spotify_hwnd = _find_spotify_hwnd()
        if spotify_hwnd:
            try:
                from actions.window_control import force_absolute_focus

                force_absolute_focus(spotify_hwnd)
            except Exception:
                if win32gui.IsIconic(spotify_hwnd):
                    win32gui.ShowWindow(spotify_hwnd, win32con.SW_RESTORE)
                else:
                    win32gui.ShowWindow(spotify_hwnd, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(spotify_hwnd)

            if not want_play:
                return "Spotify is in front, sir."
            try:
                from actions.media_coordinator import media_coordinator

                media_coordinator({"target": "spotify", "action": "play"})
            except Exception:
                _resume_spotify(delay=0.15, attempts=3, interval=0.2)
            return "Spotify is in front and playing, sir."

        spotify_exe = get_executable_from_registry("spotify") or scan_start_menu_shortcuts(
            "spotify"
        )
        _ensure_spotify_launched(spotify_exe)
        hwnd = _wait_for_spotify_window(timeout=6.0)
        if not want_play:
            return "Spotify is open, sir." if hwnd else "Spotify is starting, sir."
        _play_spotify_after_open()
        return "Spotify opened and now playing, sir."

    # ----- 3. Live Context Synchronization Framework -----
    if sys.platform == "win32":
        try:
            from actions.window_control import find_window_by_master_spec, force_absolute_focus

            target = find_window_by_master_spec(app_name)
            if target:
                print(
                    f"[open_app] '{app_name}' already running → shifting workspace window target focal state."
                )
                force_absolute_focus(target.getHandle())
                return f"{app_name} was already open, sir. I have brought it to the foreground."
        except Exception as e:
            print(f"[open_app] Window focus hook warning: {e}")

    # ----- 4. System Registry Pipeline Launch -----
    reg_path = get_executable_from_registry(app_name)
    if reg_path:
        if any(b in app_name.lower() for b in ("brave", "chrome", "edge", "firefox")):
            webbrowser.open("about:blank")
            return f"{app_name.capitalize()} context opened, sir."
        subprocess.Popen([reg_path], shell=False, creationflags=NO_WINDOW)
        return f"{app_name} launched successfully from system registry, sir."

    # ----- 5. Fast Start Menu Shortcuts Mapping Verification -----
    shortcut_path = scan_start_menu_shortcuts(app_name)
    if shortcut_path:
        try:
            os.startfile(shortcut_path)
            return (
                f"Located and launched {app_name} from system shortcuts directory hierarchy, sir."
            )
        except Exception as e:
            print(f"[open_app] Shortcut startfile failed, moving to fallback layer: {e}")

    # ----- 6. Global Command PATH Allocation Lookup -----
    binary = shutil.which(app_name) or shutil.which(f"{app_name}.exe")
    if binary:
        subprocess.Popen([binary], shell=False, creationflags=NO_WINDOW)
        return f"Opened {app_name} via standard path routing."

    # ----- 7. Toggle-close / open-or-close support (generic) -----
    # Generic close path: if mode is close or toggle, try to close any visible window whose title matches.
    if mode in {"close", "toggle"}:
        app_query = _normalize_app_token(app_name)
        handles = _get_matching_window_handles(app_query)
        if handles:
            _try_close_window_handles(handles)
            # Give it a moment to disappear
            time.sleep(0.3)
            return f"Closed {app_name}, sir."

    # ----- 8. Absolute Final Smart Fallback: Windows Search Automation Injection -----
    if _launch_via_windows_search_bar_verified(app_name):
        # Verify it opened by checking any window title contains the query.
        query = _normalize_app_token(app_name)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if _get_matching_window_handles(query):
                return f"Invoked Windows desktop search module and opened '{app_name}', sir."
            time.sleep(0.3)
        return f"Invoked Windows desktop search module to locate and initialize '{app_name}', sir."

    return f"Sir, I could not locate '{app_name}' anywhere on your system storage layout or via shell indexing."
