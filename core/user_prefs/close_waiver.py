"""Standing 'close without confirmation' exceptions, per app."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import core.user_prefs as _store

# Accessed as _store.X at call time (never `from core.user_prefs import X`):
# tests monkeypatch core.user_prefs.PREFS_PATH / _load_unlocked / _save /
# _LOCK directly, and a `from ... import X` would bind a private copy that
# such a patch never reaches.

# Windows-critical processes that must never be closeable without
# confirmation, regardless of what any feedback text asked for.
_PROTECTED_APPS = {
    "explorer",
    "csrss",
    "winlogon",
    "svchost",
    "lsass",
    "services",
    "system",
    "smss",
    "wininit",
    "dwm",
    "taskmgr",
}


def set_close_confirmation_waiver(app: str, waived: bool) -> str:
    """Grant or revoke the standing 'close without confirmation' exception."""
    key = _store._normalize_app(app)
    if not key:
        return "No app name provided."
    if waived and (key in _PROTECTED_APPS or len(key) < 3):
        return f"I can't waive close confirmation for '{app}' — it stays protected, sir."
    with _store._LOCK:
        data = _store._load_unlocked()
        entry = data["close_confirmation_waived"].get(key) or {}
        entry["active"] = bool(waived)
        entry["granted_at"] = datetime.now(UTC).isoformat()
        data["close_confirmation_waived"][key] = entry
        _store._save(data)
    if waived:
        return f"Understood — I will close {app} without asking for confirmation from now on."
    return f"Understood — from now on closing {app} will require your confirmation again."


def is_close_confirmation_waived(target: str) -> bool:
    """True if the user granted a standing waiver for closing this app.

    `target` may be an app name, exe name, or a window title. Waiver keys are
    matched as WHOLE WORDS of the normalized target ('Spotify Premium' matches
    a waiver for 'spotify', but a waiver for 'note' does NOT match 'notepad')
    so short keys cannot silently cover unrelated apps.
    """
    t = _store._normalize_app(target)
    if not t:
        return False
    with _store._LOCK:
        waivers = _store._load_unlocked().get("close_confirmation_waived", {})
    for key, entry in waivers.items():
        if not (entry.get("active") and key):
            continue
        # Read-time re-check of the grant-time rules: a hand-edited or legacy
        # prefs file must not be able to waive a protected/too-short key.
        if key in _PROTECTED_APPS or len(key) < 3:
            continue
        if re.search(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])", t):
            return True
    return False


def list_close_waivers() -> list[str]:
    with _store._LOCK:
        waivers = _store._load_unlocked().get("close_confirmation_waived", {})
    return sorted(k for k, e in waivers.items() if e.get("active"))
