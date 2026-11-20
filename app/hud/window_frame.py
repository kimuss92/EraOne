"""Borderless HUD window for the Chromium app window (Windows).

Edge/Chrome ``--app`` windows paint their own title bar INSIDE the window, so
removing WS_CAPTION changes nothing (verified 2026-10-15). Instead the HUD page
measures the exact title-bar height and invisible resize border from inside
(outer vs inner size) and asks for a window region that clips them away. The
HUD header then supplies minimize / maximize / close, drag-to-move and
double-click-to-maximize.

Scope is deliberately narrow: an allowlisted action, applied only to the
FOREGROUND window, and only if that window is an ERA ONE app window of a
known Chromium browser (the request comes from a click or resize inside it).
Anything else is refused, never guessed. Window regions are transient: closing
the window, or ``frame`` with ``enabled: false``, restores the normal frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.hud.browser import _CHROMIUM, HUD_TITLE

ACTIONS = frozenset({"frame", "minimize", "maximize", "close", "drag"})
_MAX_INSET = 200  # physical px; a real caption/border is far smaller


class WindowError(ValueError):
    """The request is invalid or no HUD window is in front."""


def _process_name(user32, kernel32, hwnd) -> str:
    from ctypes import byref, create_unicode_buffer, wintypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = create_unicode_buffer(1024), wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def _hud_window():
    import ctypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise WindowError("no window in front")
    n = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, title, n + 1)
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if (
        title.value != HUD_TITLE
        or cls.value != "Chrome_WidgetWin_1"
        or _process_name(user32, kernel32, hwnd) not in _CHROMIUM
    ):
        raise WindowError("the ERA ONE window is not in front")
    return user32, hwnd


def _inset(value) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INSET:
        raise WindowError("inset must be an integer 0-200")
    return value


def apply(body: dict) -> dict:
    if sys.platform != "win32":
        raise WindowError("borderless mode needs Windows")
    action = body.get("action")
    if action not in ACTIONS:
        raise WindowError("unknown action")
    import ctypes
    from ctypes import wintypes

    user32, hwnd = _hud_window()
    if action == "frame":
        if not body.get("enabled", True):
            user32.SetWindowRgn(hwnd, None, True)
            return {"ok": True, "borderless": False}
        top, side = _inset(body.get("top")), _inset(body.get("side"))
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w - 2 * side < 200 or h - top - side < 150:
            raise WindowError("window too small")
        rgn = ctypes.windll.gdi32.CreateRectRgn(side, top, w - side, h - side)
        if not user32.SetWindowRgn(hwnd, rgn, True):  # the system owns rgn on success
            ctypes.windll.gdi32.DeleteObject(rgn)
            raise WindowError("region refused")
        return {"ok": True, "borderless": True}
    if action == "minimize":
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    elif action == "maximize":
        user32.ShowWindow(hwnd, 9 if user32.IsZoomed(hwnd) else 3)  # SW_RESTORE / SW_MAXIMIZE
    elif action == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    elif action == "drag":
        # Hand the held mouse button to the system move loop, as a click on
        # the (now hidden) title bar would.
        user32.ReleaseCapture()
        user32.PostMessageW(hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
    return {"ok": True, "maximized": bool(user32.IsZoomed(hwnd))}
