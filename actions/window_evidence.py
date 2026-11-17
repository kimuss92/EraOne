"""Turn a window_control result into evidence when the handler READ the result back.

Why: minimize/maximize poll the real window state and move_to_monitor asks
Windows which monitor the window ended up on, but they returned plain text, so
the completion contract saw "no verifier" and Era said "I couldn't confirm it
took effect" about windows she had just watched move. Focus never checked at
all; here it reads the real foreground window.

Only a read-back success becomes a verified ToolResult. Everything else stays the
handler's own string: the dispatcher does `r or "Done."`, and a failed
ToolResult is falsy, so returning one would turn a failure into "Done.".
"""

from __future__ import annotations

import time

from core.tool_result import ToolResult

VERIFIER = "window_native"

# Messages each handler returns ONLY after reading the new state back from Windows.
_READ_BACK = {
    "move_to_monitor": (("moved '",), ("' is already on screen",)),
    "minimize": (("minimized:",), ("is already minimized",)),
    "maximize": (("maximized:",), ("is already maximized",)),
}
_FOCUS_DONE = ("focused", "shown:", "restored:")


def _foreground_is(hwnd: int | None, timeout_s: float = 0.6) -> str | None:
    """The foreground caption when it is EXACTLY the window that was acted on.

    Identity is the HWND itself (or a window it owns, such as a dialog it put up).
    Same process or a caption containing the name never counts: a second Brave
    window, or another app whose title mentions Brave, is a different window.
    """
    if not hwnd:
        return None
    try:
        import win32gui

        ga_rootowner = 3
    except Exception:
        return None
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fg = win32gui.GetForegroundWindow()
            if fg and (fg == hwnd or win32gui.GetAncestor(fg, ga_rootowner) == hwnd):
                return win32gui.GetWindowText(hwnd) or "the window"
        except Exception:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def with_evidence(action: str, params: dict, message, hwnd: int | None = None):
    """A verified ToolResult for a read-back success; otherwise `message` unchanged."""
    if not isinstance(message, str):
        return message
    lower = message.strip().lower()
    proof = ""
    if action in _READ_BACK:
        starts, contains = _READ_BACK[action]
        if lower.startswith(starts) or any(c in lower for c in contains):
            proof = f"Windows read-back after {action}: {message.strip()}"
    elif action in ("focus", "show", "restore") and lower.startswith(_FOCUS_DONE):
        caption = _foreground_is(hwnd)
        if caption:
            proof = f"Foreground window is now '{caption[:60]}'."
    if not proof:
        return message
    result = ToolResult.ok(message, tool_name="window_control", action_name=action)
    result.verified = True
    result.verifier_name = VERIFIER
    result.verification_status = "VERIFIED"
    result.verification_message = proof
    return result
