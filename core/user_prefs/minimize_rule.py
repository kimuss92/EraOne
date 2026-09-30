"""Standing 'minimize this app once you're done with it' rules, per app."""

from __future__ import annotations

from datetime import UTC, datetime

import core.user_prefs as _store

# Accessed as _store.X at call time — see close_waiver.py for why.


def set_post_action_minimize(app: str, enabled: bool) -> str:
    """Persist "minimize <app> after use" as a rule the ACTION LAYER enforces.

    This exists because the free-text behavior_rules channel could not deliver
    it. Such a rule reaches the model only as advisory system-prompt text, and
    honoring it requires the model to CHAIN a second tool call on its own after
    the first one already returned a success message it was about to speak.
    The live model does not do that reliably, so "minimize Spotify after use"
    was saved, injected, acknowledged — and then never actually happened.

    Storing it structurally lets the action itself close the loop (see
    spotify_control), which turns the preference from a suggestion into
    behavior. The free-text rule is still kept alongside it so the model stays
    aware of the preference when it talks about it.
    """
    key = _store._normalize_app(app)
    if not key or len(key) < 3:
        return "No app name provided."
    with _store._LOCK:
        data = _store._load_unlocked()
        entry = data["post_action_minimize"].get(key) or {}
        entry["active"] = bool(enabled)
        entry["granted_at"] = datetime.now(UTC).isoformat()
        data["post_action_minimize"][key] = entry
        _store._save(data)
    if enabled:
        return f"Understood — I'll minimize {app} once I'm done with it."
    return f"Understood — I'll leave the {app} window as it is from now on."


def is_post_action_minimize(app: str) -> bool:
    """True if the user asked for this app to be minimized after Era uses it."""
    key = _store._normalize_app(app)
    if not key:
        return False
    try:
        with _store._LOCK:
            entry = _store._load_unlocked().get("post_action_minimize", {}).get(key)
        return bool(entry and entry.get("active"))
    except Exception:
        return False


def list_post_action_minimize() -> list[str]:
    with _store._LOCK:
        rules = _store._load_unlocked().get("post_action_minimize", {})
    return sorted(k for k, e in rules.items() if e.get("active"))
