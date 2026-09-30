"""Open (or re-focus) the HUD as a Chromium app window.

No install path is hard-coded: the browser is found through the registry —
the user's default browser if it is Chromium-based, else Edge (always present
on Windows 11), else Chrome/Brave via their App Paths entries. If none can be
found, the system default browser opens a normal tab.

The app window runs on a DEDICATED profile under ~/.era, so it never inherits
the user's everyday extensions (one of which strips page controls) and never
mixes the HUD into their browsing session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from app.hud.instance import era_home

HUD_TITLE = "ERA ONE"
_CHROMIUM = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe")
_NO_WINDOW = 0x08000000


def _reg_default(root, subkey: str) -> str | None:
    try:
        import winreg

        with winreg.OpenKey(root, subkey) as k:
            val, _ = winreg.QueryValueEx(k, "")
            return str(val) if val else None
    except OSError:
        return None


def _app_path(exe: str) -> str | None:
    import winreg

    sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        val = _reg_default(root, sub)
        if val:
            path = val.strip().strip('"')
            if Path(path).exists():
                return path
    return None


def _default_browser_exe() -> str | None:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as k:
            prog_id, _ = winreg.QueryValueEx(k, "ProgId")
        cmd = _reg_default(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command")
    except OSError:
        return None
    if not cmd:
        return None
    exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0]
    return exe if Path(exe).name.lower() in _CHROMIUM and Path(exe).exists() else None


def find_app_browser() -> str | None:
    if sys.platform != "win32":
        return None
    found = _default_browser_exe()
    if found:
        return found
    for exe in _CHROMIUM:
        path = _app_path(exe)
        if path:
            return path
    return None


def profile_dir() -> Path:
    return era_home() / "hud-browser"


def app_window_args(exe: str, url: str) -> list[str]:
    return [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1600,960",
        "--disable-features=Translate,msEdgeTranslate",
        "--autoplay-policy=no-user-gesture-required",
    ]


def launch_app_window(url: str) -> bool:
    """Start the app window DETACHED (via `cmd /c start`), so the browser is not
    a child of the caller — the supervisor's taskkill /T on a restarting backend
    must never take the user's open HUD window down with it."""
    exe = find_app_browser()
    if not exe:
        return False
    try:
        profile_dir().mkdir(parents=True, exist_ok=True)
        args = app_window_args(exe, url)
        subprocess.Popen(
            ["cmd", "/c", "start", "", *args],
            creationflags=_NO_WINDOW,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def focus_existing(title: str = HUD_TITLE) -> bool:
    """Bring an already-open HUD window to the front. True if one was found."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "Chrome_WidgetWin_1" and buf.value.startswith(title):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _is_hud_process(pid: int) -> bool:
    """True if `pid` is a browser running on the HUD's DEDICATED profile — so a
    shutdown never closes the user's own browser windows."""
    try:
        import psutil

        cmd = " ".join(psutil.Process(pid).cmdline()).lower()
    except Exception:
        return False
    return f"--user-data-dir={profile_dir()}".lower() in cmd


def close_hud_windows() -> int:
    """Close every HUD app window (WM_CLOSE — the browser's own clean close).
    Called on an intentional shutdown only; a relaunch keeps the window, which
    reconnects to the new backend by itself. Returns how many were closed."""
    if sys.platform != "win32" or os.environ.get("ERA_NO_BROWSER") == "1":
        return 0
    if "PYTEST_CURRENT_TEST" in os.environ:
        return 0  # a test must never close the user's live HUD
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1" or not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _is_hud_process(pid.value):
            found.append(hwnd)
        return True

    user32.EnumWindows(_enum, 0)
    for hwnd in found:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return len(found)


def open_hud(url: str, *, prefer_app: bool = True, hud_clients: int = 0) -> str:
    """focused | app | browser | suppressed — how the HUD was brought up."""
    if os.environ.get("ERA_NO_BROWSER") == "1":
        return "suppressed"  # tests / headless runs: never pop a window
    if hud_clients > 0 and focus_existing():
        return "focused"
    if prefer_app and launch_app_window(url):
        return "app"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return "browser"
