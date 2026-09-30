"""Standing 'send to Claude Code without a second confirmation' exception.

This waiver ONLY removes the redundant MEDIUM confirmation step on a
claude_bridge ask/reply. It NEVER waives the user-words gate
(_claude_send_authorized): a send still requires the user to have named Claude
in their own words within the last few minutes. That gate is the prompt-injection
boundary — Claude Code can act on this machine outside Era's gates — so no stored
preference may open it. The dispatch applies this waiver only AFTER the user-words
gate has already passed (see app/era_live/tool_dispatch._execute_tool_result).
"""

from __future__ import annotations

from datetime import UTC, datetime

import core.user_prefs as _store

# Accessed as _store.X at call time — see close_waiver.py for why.


def set_claude_send_confirmation_waiver(waived: bool) -> str:
    """Grant or revoke the standing 'just send it to Claude' exception.

    Global toggle. Only downgrades the confirmation step; the user-words gate
    stays in force regardless of this flag.
    """
    with _store._LOCK:
        data = _store._load_unlocked()
        data["claude_send_confirmation_waived"] = bool(waived)
        data["claude_send_confirmation_changed_at"] = datetime.now(UTC).isoformat()
        _store._save(data)
    if waived:
        return (
            "Understood — when you tell me in your own words to message Claude "
            "Code, I'll send it without a second confirmation."
        )
    return "Understood — I'll confirm again before messaging Claude Code."


def is_claude_send_confirmation_waived() -> bool:
    """True when the user granted the standing 'just send it' exception.

    Fails CLOSED on any read error, so a corrupt prefs file reverts to the
    stricter confirm-first behavior rather than silently opening the gate.
    """
    try:
        with _store._LOCK:
            return bool(_store._load_unlocked().get("claude_send_confirmation_waived", False))
    except Exception:
        return False
