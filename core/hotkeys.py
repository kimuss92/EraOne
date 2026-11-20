"""One way to read and send keyboard shortcuts, for every tool that sends them.

pyautogui silently drops a key name it does not know: "control+c" pressed a bare
"c", "windows+r" typed "r", an empty hotkey pressed nothing, and each one still
reported success. So names are normalised here, and an unknown name is refused
before any key goes down, because half a chord types into whatever has focus.

An injected key reaches the focused window, and nothing shows the app reacted. So
success is worded "sent" and names that window: the model can tell a shortcut
went to the wrong place, and must look at the screen if the outcome matters.

safety_guard compares chords through canonical_chord, so a spelling that sends
the same keys cannot slip past its dangerous-hotkey list.
"""

from __future__ import annotations

from core.workstation_lock import is_lock_chord, lock_workstation

_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "windows": "win",
    "window": "win",
    "windows key": "win",
    "win key": "win",
    "super": "win",
    "meta": "win",
    "option": "alt",
    "spacebar": "space",
    "space bar": "space",
    "page up": "pageup",
    "page_up": "pageup",
    "page down": "pagedown",
    "page_down": "pagedown",
    "print screen": "printscreen",
    "print_screen": "printscreen",
    "prtscn": "printscreen",
    "prt sc": "printscreen",
    "up arrow": "up",
    "down arrow": "down",
    "left arrow": "left",
    "right arrow": "right",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "caps lock": "capslock",
    "plus": "+",
    "minus": "-",
    # pyautogui's own left/right and duplicate names: the same key to the OS, so the
    # same key to the safety gate.
    "winleft": "win",
    "winright": "win",
    "altleft": "alt",
    "altright": "alt",
    "ctrlleft": "ctrl",
    "ctrlright": "ctrl",
    "shiftleft": "shift",
    "shiftright": "shift",
    "escape": "esc",
    "del": "delete",
    "return": "enter",
    "prtsc": "printscreen",
    "prtscr": "printscreen",
    "prntscrn": "printscreen",
}

# Windows accepts this chord only from the physical keyboard.
_SECURE_CHORD = frozenset({"ctrl", "alt", "delete"})

# What Era may press without asking: reversible, and all named in core/prompt.txt's
# KEYBOARD SHORTCUTS rule. Screen or email text can talk the model into a shortcut, so
# this is an allowlist: an unlisted chord (win+x, ctrl+enter, shift+delete, win+r+a)
# waits for the user's yes rather than relying on a list of known-bad chords.
ROUTINE_CHORDS = frozenset(
    frozenset(chord.split("|"))
    for chord in (
        "win|shift|r",
        "win|shift|s",
        "win|l",
        "win|v",
        "win|.",
        "win|h",
        "win|e",
        "win|i",
        "win|a",
        "win|n",
        "win|tab",
        "alt|tab",
        "win|ctrl|d",
        "win|ctrl|left",
        "win|ctrl|right",
        "win|shift|left",
        "win|shift|right",
        "win|up",
        "win|down",
        "win|left",
        "win|right",
        "ctrl|s",
        "ctrl|z",
        "ctrl|y",
        "ctrl|f",
        "ctrl|a",
        "ctrl|c",
        "ctrl|v",
        "ctrl|x",
        "ctrl|t",
        "ctrl|shift|t",
        "ctrl|l",
        "ctrl|tab",
        "ctrl|shift|tab",
    )
)


def resolve_keys(params: dict):
    """The keys a hotkey or press call means. The gate and every sender read this one
    answer, so a call cannot show the gate one field and send another."""
    for field in ("keys", "key", "value"):
        if params.get(field):
            return params[field]
    return ""


def is_routine_chord(chord: frozenset[str]) -> bool:
    """True when `chord` may run unconfirmed. A lone Win key opens a launcher to type into."""
    if len(chord) <= 1:
        return chord != {"win"}
    return chord in ROUTINE_CHORDS


def _normalise(key: str) -> str:
    key = " ".join(key.lower().split())
    return _ALIASES.get(key, key)


def parse_keys(raw) -> list[str]:
    """Key names from 'Ctrl + Shift + Esc', 'win shift r', 'ctrl++' or a list."""
    if isinstance(raw, (list, tuple)):
        return [key for part in raw for key in parse_keys(part)]
    text = str(raw or "").strip()
    if not text:
        return []
    if text == "+":
        return ["+"]
    trailing_plus = text.endswith("++")
    parts = [part.strip() for part in text.split("+") if part.strip()]
    if len(parts) == 1 and " " in parts[0] and " ".join(parts[0].lower().split()) not in _ALIASES:
        # No alias claims the whole phrase, so spaces are the separator: "win shift r".
        parts = parts[0].split()
    keys = [_normalise(part) for part in parts]
    return [*keys, "+"] if trailing_plus else keys


def canonical_chord(raw) -> frozenset[str]:
    """Order-free form of a chord, for comparing against a list of chords."""
    return frozenset(parse_keys(raw))


def _unknown_keys(keys: list[str]) -> list[str]:
    try:
        import pyautogui

        # KEYBOARD_KEYS also lists names with no key on this OS ("command" maps to
        # None on Windows), and those are dropped just as silently.
        mapping = pyautogui.platformModule.keyboardMapping
    except Exception:  # noqa: BLE001 - pyautogui raises more than ImportError headless
        return []
    return [key for key in keys if mapping.get(key) is None]


def _focused_window_line() -> str:
    try:
        from core.window_awareness import get_active_window

        title = get_active_window().get("title") or ""
    except Exception:  # noqa: BLE001 - the shortcut matters more than this note
        return ""
    return f"\nFocused window when sent: {title}" if title else ""


def _send_failure(exc: Exception) -> str:
    """Plain reason for a failed send. A corner mouse is deliberate, so say how to clear it."""
    if type(exc).__name__ == "FailSafeException":
        return "the mouse is in a screen corner, which is the automation stop. Move it and retry."
    return f"{type(exc).__name__}: {exc}."


def send_hotkey(raw, pyautogui) -> str:
    """Send a chord through `pyautogui`; the first line is the status."""
    from core.utils import is_windows

    keys = parse_keys(raw)
    if not keys:
        return "Could not send a hotkey: no keys were given."
    chord = "+".join(keys)
    unknown = _unknown_keys(keys)
    if unknown:
        return (
            f"Could not send {chord}: unknown key name {', '.join(unknown)}. Nothing was pressed."
        )
    if is_windows():
        if is_lock_chord(keys):
            return lock_workstation()
        if frozenset(keys) == _SECURE_CHORD:
            return f"Could not send {chord}: Windows only accepts it from the physical keyboard."
    target = _focused_window_line()
    try:
        pyautogui.hotkey(*keys)
    except Exception as exc:  # noqa: BLE001 - pyautogui's errors share no usable base
        return f"Could not send {chord}: {_send_failure(exc)}"
    return f"Hotkey sent: {chord}{target}"


def send_key(raw, pyautogui) -> str:
    """Press one key through `pyautogui`; a chord belongs to send_hotkey."""
    keys = parse_keys(raw)
    if len(keys) != 1:
        return f"Could not press '{raw}': that is not one key. Use the hotkey action for a chord."
    unknown = _unknown_keys(keys)
    if unknown:
        return f"Could not press '{raw}': unknown key name. Nothing was pressed."
    try:
        pyautogui.press(keys[0])
    except Exception as exc:  # noqa: BLE001 - see send_hotkey
        return f"Could not press {keys[0]}: {_send_failure(exc)}"
    return f"Pressed: {keys[0]}"
