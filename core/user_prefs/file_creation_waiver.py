"""Standing 'create files without confirmation' exception (global, not per-app)."""

from __future__ import annotations

from datetime import UTC, datetime

import core.user_prefs as _store

# Accessed as _store.X at call time — see close_waiver.py for why.


def set_file_creation_confirmation_waiver(waived: bool) -> str:
    """Grant or revoke the standing 'create files without confirmation' exception.

    Global toggle (not per-app): file_controller write/create_file/create_folder
    stay MEDIUM-and-gated by default; only an explicit grant here downgrades
    them, and only for creation — delete/overwrite/move keep their own
    unrelated confirmation gates in safety_guard regardless of this flag.
    """
    with _store._LOCK:
        data = _store._load_unlocked()
        data["file_creation_confirmation_waived"] = bool(waived)
        data["file_creation_confirmation_changed_at"] = datetime.now(UTC).isoformat()
        _store._save(data)
    if waived:
        return "Understood — I'll create files without asking for confirmation from now on."
    return "Understood — creating files will require your confirmation again."


def is_file_creation_confirmation_waived() -> bool:
    """True when the user granted a standing waiver for file-creation confirmation.

    Fails CLOSED on any read error, so a corrupt prefs file reverts to the
    stricter confirm-first behavior rather than silently opening the gate.
    """
    try:
        with _store._LOCK:
            return bool(_store._load_unlocked().get("file_creation_confirmation_waived", False))
    except Exception:
        return False
