"""Free-text standing behavior rules, injected into the system prompt."""

from __future__ import annotations

from datetime import UTC, datetime

import core.user_prefs as _store

# Accessed as _store.X at call time — see close_waiver.py for why.

_MAX_BEHAVIOR_RULES = 40


def add_behavior_rule(rule: str) -> str:
    text = (rule or "").strip()
    if not text:
        return "No rule text provided."
    with _store._LOCK:
        data = _store._load_unlocked()
        rules = data["behavior_rules"]
        low = text.lower()
        if any(r.get("rule", "").lower() == low for r in rules):
            return "That rule is already saved."
        rules.append({"rule": text, "added_at": datetime.now(UTC).isoformat()})
        data["behavior_rules"] = rules[-_MAX_BEHAVIOR_RULES:]
        _store._save(data)
    return f"Standing rule saved: {text}"


def remove_behavior_rule(rule: str) -> str:
    """Remove rules whose text contains the given fragment (case-insensitive)."""
    frag = (rule or "").strip().lower()
    if not frag:
        return "No rule text provided."
    with _store._LOCK:
        data = _store._load_unlocked()
        before = len(data["behavior_rules"])
        data["behavior_rules"] = [
            r for r in data["behavior_rules"] if frag not in r.get("rule", "").lower()
        ]
        removed = before - len(data["behavior_rules"])
        if removed:
            _store._save(data)
    return f"Removed {removed} standing rule(s)." if removed else "No matching rule found."


def get_behavior_rules() -> list[str]:
    with _store._LOCK:
        return [
            r.get("rule", "")
            for r in _store._load_unlocked().get("behavior_rules", [])
            if r.get("rule")
        ]
