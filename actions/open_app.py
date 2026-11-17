# actions/open_app.py
"""
High-performance Windows Application Launcher for Era One.
Ranks and inspects Start Menu shortcuts, resolves registry/PATH/Store targets,
and checks process-owned windows. Never selects an uninspected shell search result.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from functools import lru_cache
from pathlib import Path

from core.app_launch_safety import (
    inspect_shortcut,
    normalized_name,
    safe_executable,
    shortcut_score,
    unsafe_launch,
)
from core.cancel_registry import CancelledTask, raise_if_cancelled, sleep_cancellable
from core.win_subprocess import NO_WINDOW

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
                    if safe_executable(clean):
                        return clean
            except Exception:
                continue
    return None


@lru_cache(maxsize=4)
def _shortcut_inventory(roots: tuple[str, ...], epoch: int) -> tuple[str, ...]:
    paths = []
    for folder in roots:
        root = Path(folder)
        if root.is_dir():
            try:
                paths.extend(str(path) for path in root.rglob("*.lnk"))
            except OSError:
                continue
    return tuple(paths)


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

    ranked = sorted(
        (
            (shortcut_score(Path(path).stem, target_phrase), path)
            for path in _shortcut_inventory(
                tuple(str(root) for root in shortcut_roots), int(time.monotonic() // 60)
            )
        ),
        key=lambda item: (-item[0], item[1].casefold()),
    )
    candidates = []
    best_score = 0
    for score, path in ranked:
        raise_if_cancelled()
        if not score or (best_score and score < best_score):
            break
        inspected = inspect_shortcut(path)
        if inspected:
            best_score = score
            candidates.append((path, inspected))
    # Equal-ranked shortcuts for different binaries are ambiguous: do not guess.
    if len({target.casefold() for _, (target, _) in candidates}) > 1:
        return None
    return candidates[0][0] if candidates else None


def _find_launch_window(app_name: str, executable: str | None = None) -> int:
    """Read back a visible app window by owning executable, never a browser tab."""
    if sys.platform != "win32":
        return 0
    import psutil
    import win32process

    spec = MASTER_APP_REGISTRY.get(_normalize_app_token(app_name))
    if not executable and Path(app_name).suffix.casefold() == ".exe" and Path(app_name).is_file():
        executable = app_name
    expected = (
        Path(executable).name.casefold()
        if executable
        else (spec["exe"].casefold() if spec else app_name.casefold().removesuffix(".exe") + ".exe")
    )
    # Electron shortcuts may launch Update.exe with --processStart. The visible
    # app belongs to the app process, not that short-lived bootstrap executable.
    if spec and expected in {"update.exe", "updater.exe"}:
        expected = spec["exe"].casefold()
        executable = None
    expected_names = {expected}
    if _normalize_app_token(app_name) == "calculator" and not executable:
        expected_names.update({"calculatorapp.exe", "calculator.exe"})
    found = []

    def collect(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            proc = psutil.Process(pid)
            process_name = proc.name().casefold()
            if process_name not in expected_names:
                return True
            if executable and os.path.normcase(proc.exe()) != os.path.normcase(executable):
                return True
            # Shared Store host processes cannot identify an app on their own.
            if process_name == "applicationframehost.exe":
                names = {normalized_name(app_name)}
                if _normalize_app_token(app_name) == "calculator":
                    names.add("calculatrice")
                if not any(name in normalized_name(title) for name in names):
                    return True
            found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return found[0] if found else 0


def _focus_launch_window(hwnd: int) -> bool:
    from actions.window_control import force_absolute_focus

    raise_if_cancelled()
    force_absolute_focus(hwnd)
    return win32gui.GetForegroundWindow() == hwnd


def _launch_result(app_name: str, executable: str | None = None, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        raise_if_cancelled()
        hwnd = _find_launch_window(app_name, executable)
        if hwnd:
            focused = _focus_launch_window(hwnd)
            return (
                f"Opened {app_name}, sir."
                if focused
                else f"{app_name} is open, but Windows did not allow foreground focus."
            )
        if time.monotonic() >= deadline:
            return f"Launch sent for {app_name}, but its application window has not been confirmed yet."
        sleep_cancellable(0.15)


def _normalize_app_token(app_name: str) -> str:
    value = (app_name or "").strip().lower()
    return {
        "telegram desktop": "telegram",
        "brave browser": "brave",
        "google chrome": "chrome",
        "microsoft edge": "edge",
        "vs code": "vscode",
    }.get(value, value)


@lru_cache(maxsize=2)
def _store_catalog(epoch: int) -> tuple[dict, ...]:
    """Read Windows' registered app catalog; never interpolate user input in code."""
    if sys.platform != "win32":
        return ()
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=5,
            creationflags=NO_WINDOW,
        )
        entries = json.loads(result.stdout) if result.returncode == 0 else []
        return tuple(entries if isinstance(entries, list) else [entries])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ()


def _store_app_id(app_name: str) -> str | None:
    import re

    wanted = normalized_name(app_name)
    wanted_names = {wanted, "calculatrice"} if wanted == "calculator" else {wanted}
    candidates = {
        entry["AppID"]
        for entry in _store_catalog(int(time.monotonic() // 60))
        if isinstance(entry, dict)
        and normalized_name(str(entry.get("Name", ""))) in wanted_names
        and isinstance(entry.get("AppID"), str)
        and re.fullmatch(r"[A-Za-z0-9._-]+![A-Za-z0-9._-]+", entry["AppID"])
        and not unsafe_launch(entry["AppID"])
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


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
    """Legacy entry point: blind shell selection cannot verify target identity."""
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
        raise_if_cancelled()
        hwnd = _find_spotify_hwnd()
        if hwnd:
            return hwnd
        sleep_cancellable(poll_interval)
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
        raise_if_cancelled()
        try:
            if Path(spotify_exe).suffix.casefold() == ".lnk":
                if not inspect_shortcut(spotify_exe):
                    raise OSError("Unsafe Spotify shortcut")
                os.startfile(spotify_exe)
            elif safe_executable(spotify_exe):
                subprocess.Popen([spotify_exe], shell=False, creationflags=NO_WINDOW)
            else:
                raise OSError("Unsafe Spotify executable")
            return
        except CancelledTask:
            raise
        except Exception:
            pass

    raise_if_cancelled()
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
    raise_if_cancelled()
    parameters = parameters or {}
    app_name = str(parameters.get("app_name", "")).strip()
    mode = str(parameters.get("mode", "open") or "open").strip().lower()

    # Heuristic: if user says close/quit/exit in the app_name text, treat as toggle-close.
    # (LLM sometimes passes the whole phrase into app_name.)
    for prefix in ("close ", "quit ", "exit "):
        if mode in {"open", ""} and app_name.casefold().startswith(prefix):
            mode = "close"
            app_name = app_name[len(prefix) :].strip()
            break

    do_toggle = mode in {"toggle", "toggle_open_close", "open_or_close"}
    if do_toggle:
        mode = "toggle"

    if not app_name:
        return "No application name provided."

    app_name = _normalize_app_token(app_name)
    if unsafe_launch(app_name):
        return "Could not open that target: app launching does not run uninstallers, installers or maintenance actions."

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
    if mode in {"open", "play"}:
        try:
            from core.user_prefs.minimize_rule import note_opened_on_request

            note_opened_on_request(app_name)  # a minimize-after-use rule must not hide it
        except Exception:
            pass

    if mode in {"close", "toggle"}:
        hwnd = _find_launch_window(app_name)
        if hwnd:
            raise_if_cancelled()
            _try_close_window_handles([hwnd])
            sleep_cancellable(0.3)
            raise_if_cancelled()
            return (
                f"Closed {app_name}, sir."
                if not _find_launch_window(app_name)
                else f"Close requested for {app_name}; its window is still open."
            )
        if mode == "close":
            return f"Could not find a running {app_name} window to close."

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

    # Existing process identity is enough to focus an app even if a relocated
    # installation left an inaccessible shortcut target behind.
    hwnd = _find_launch_window(app_name)
    if hwnd:
        focused = _focus_launch_window(hwnd)
        return (
            f"{app_name} was already open and is in front, sir."
            if focused
            else f"{app_name} is already open, but Windows did not allow foreground focus."
        )

    # App identity must be resolved before any launch. A shortcut is
    # inspected again immediately before launch so stale cache entries are not trusted.
    reg_path = get_executable_from_registry(app_name)
    shortcut_path = None if reg_path else scan_start_menu_shortcuts(app_name)
    inspected = inspect_shortcut(shortcut_path) if shortcut_path else None
    binary = reg_path or (inspected[0] if inspected else None)
    if not binary:
        binary = shutil.which(app_name) or shutil.which(f"{app_name}.exe")
    if binary and not safe_executable(binary, inspected[1] if inspected else ""):
        return f"Could not safely launch {app_name}: the resolved target is not an application executable."

    hwnd = _find_launch_window(app_name, binary)
    if hwnd:
        focused = _focus_launch_window(hwnd)
        return (
            f"{app_name} was already open and is in front, sir."
            if focused
            else f"{app_name} is already open, but Windows did not allow foreground focus."
        )
    if not binary:
        package_id = _store_app_id(app_name)
        raise_if_cancelled()
        if package_id:
            try:
                os.startfile("shell:AppsFolder\\" + package_id)
            except OSError as exc:
                return f"Could not launch {app_name}: {exc}"
            return _launch_result(app_name)
        return f"Could not find a safe, unambiguous launch target for {app_name}. Please provide its exact app name or executable path."
    raise_if_cancelled()
    try:
        if shortcut_path and inspected:
            os.startfile(shortcut_path)
        else:
            subprocess.Popen([binary], shell=False, creationflags=NO_WINDOW)
    except OSError as exc:
        return f"Could not launch {app_name}: {exc}"
    return _launch_result(app_name, binary)
