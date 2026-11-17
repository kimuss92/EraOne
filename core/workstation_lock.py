"""Lock the Windows session for real, and report only what Windows confirms.

2026-11-04: "lock my screen" answered "Done: lock_screen." (trace: VERIFIED) and
nothing locked. The action injected Win+L with pyautogui; the injection raised
nothing, the desktop stayed unlocked, and no verifier ever looked at the session.
Telling Era to "press Windows+L" took the same dead path through the hotkey action.

So: LockWorkStation is the only lock call, and success means the session's own
lock flag flipped. LockWorkStation returning nonzero only means the request was
accepted (the lock is asynchronous), so it is not evidence on its own.

Failure messages lead with "Could not" / "failed" so core.tool_result's
looks_like_failure classifies them as failures.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_WTS_CURRENT_SESSION = 0xFFFFFFFF
_WTS_SESSION_INFO_EX = 25
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1
# WTSINFOEXW: Level (DWORD) at 0; the union aligns to 8, then SessionId,
# SessionState, SessionFlags as 4-byte fields.
_SESSION_FLAGS_OFFSET = 16

_WIN_KEYS = frozenset({"win", "windows", "winleft", "winright", "super"})

LOCK_TIMEOUT_S = 5.0
_POLL_S = 0.1


def is_lock_chord(keys) -> bool:
    """True for Win+L in any spelling: the chord Windows ignores when injected."""
    parts = [str(key).strip().lower() for key in keys]
    return len(parts) == 2 and "l" in parts and bool(_WIN_KEYS.intersection(parts))


def session_is_locked() -> bool | None:
    """True/False from the session's lock flag; None when it cannot be read."""
    try:
        wts = ctypes.WinDLL("wtsapi32")
        wts.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD()
        if not wts.WTSQuerySessionInformationW(
            None,
            _WTS_CURRENT_SESSION,
            _WTS_SESSION_INFO_EX,
            ctypes.byref(buffer),
            ctypes.byref(size),
        ):
            return None
        try:
            flags = ctypes.c_long.from_address(buffer.value + _SESSION_FLAGS_OFFSET).value
        finally:
            wts.WTSFreeMemory(buffer)
    except (OSError, AttributeError, ValueError):
        return None
    if flags == _WTS_SESSIONSTATE_LOCK:
        return True
    if flags == _WTS_SESSIONSTATE_UNLOCK:
        return False
    return None


def _call_lock_workstation() -> tuple[bool, int]:
    """Ask Windows to lock. Returns (accepted, win32 error code)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    accepted = bool(user32.LockWorkStation())
    return accepted, 0 if accepted else ctypes.get_last_error()


def lock_workstation(timeout: float = LOCK_TIMEOUT_S) -> str:
    """Lock the session and return a status line that says what really happened."""
    if session_is_locked() is True:
        return "Screen is already locked."

    try:
        accepted, code = _call_lock_workstation()
    except (OSError, AttributeError) as exc:
        return f"Could not lock the screen: {exc}"
    if not accepted:
        return f"Could not lock the screen: Windows refused the request (code {code})."

    deadline = time.monotonic() + timeout
    state = session_is_locked()
    while state is not True and time.monotonic() < deadline:
        time.sleep(_POLL_S)
        state = session_is_locked()

    if state is True:
        return "Screen locked. Windows confirms the session is locked."
    if state is None:
        return (
            "Could not confirm the lock: Windows accepted the request, "
            "but the session state is unreadable. Check the screen."
        )
    return (
        "Lock failed: Windows accepted the request, but the session is still "
        f"unlocked after {timeout:g} seconds. The screen is NOT locked."
    )
