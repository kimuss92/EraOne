"""Toggle for Era's self-initiated (no 'remember this' keyword) memory saving."""

from __future__ import annotations

from datetime import UTC, datetime

import core.user_prefs as _store

# Accessed as _store.X at call time — see close_waiver.py for why.


def set_auto_memory(enabled: bool) -> str:
    """Turn Era's self-initiated memory saving on or off, live and persistently.

    OFF restores the old behavior: a save only happens when the user asks for
    it out loud, or answers yes to Era's "want me to remember that?".
    """
    with _store._LOCK:
        data = _store._load_unlocked()
        data["auto_memory"] = bool(enabled)
        data["auto_memory_changed_at"] = datetime.now(UTC).isoformat()
        _store._save(data)
    if enabled:
        return (
            "Understood — I'll save what's worth remembering on my own, without asking you first."
        )
    return (
        "Understood — I won't save anything to memory unless you ask me to, "
        "or say yes when I offer."
    )


def is_auto_memory_enabled() -> bool:
    """True when Era may save memories on her own initiative. Defaults to True.

    Fails CLOSED (returns False) on any read error, so a corrupt prefs file
    reverts to the stricter ask-first behavior rather than silently opening
    the memory gate.
    """
    try:
        with _store._LOCK:
            return bool(_store._load_unlocked().get("auto_memory", True))
    except Exception:
        return False
