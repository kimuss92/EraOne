from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.utils import get_memory_dir
from memory.safe_store import safe_append_jsonl, safe_write_json

FEEDBACK_PATH = get_memory_dir() / "feedback_memory.jsonl"
LEARNED_MISTAKES_PATH = get_memory_dir() / "learned_mistakes.json"


@dataclass
class FeedbackEvent:
    timestamp: str
    user_text: str
    assistant_last_tool: str | None = None
    assistant_last_action: str | None = None
    mistake_category: str | None = None
    signature: str | None = None
    expected_correction: str | None = None
    raw: dict[str, Any] | None = None


def _ensure_paths() -> None:
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_MISTAKES_PATH.parent.mkdir(parents=True, exist_ok=True)


def append_feedback(event: FeedbackEvent) -> None:
    _ensure_paths()
    record = {
        "timestamp": event.timestamp,
        "user_text": event.user_text,
        "assistant_last_tool": event.assistant_last_tool,
        "assistant_last_action": event.assistant_last_action,
        "mistake_category": event.mistake_category,
        "signature": event.signature,
        "expected_correction": event.expected_correction,
        "raw": event.raw,
    }
    safe_append_jsonl(FEEDBACK_PATH, record)


def _load_learned() -> dict[str, Any]:
    if not LEARNED_MISTAKES_PATH.exists():
        return {"version": 1, "rules": {}}
    try:
        data = json.loads(LEARNED_MISTAKES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "rules" in data:
            return data
    except Exception:
        pass
    return {"version": 1, "rules": {}}


def _save_learned(data: dict[str, Any]) -> None:
    _ensure_paths()
    safe_write_json(LEARNED_MISTAKES_PATH, data)


def update_learned_rule(
    signature: str, correction: str, payload: dict[str, Any] | None = None
) -> None:
    """Persist an anti-mistake signature -> correction payload."""
    if not signature:
        return
    data = _load_learned()
    rules = data.setdefault("rules", {})
    entry = rules.get(signature) or {
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": None,
        "count": 0,
        "correction": None,
        "payload": {},
    }
    entry["updated_at"] = datetime.utcnow().isoformat()
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["correction"] = correction
    if payload:
        entry.setdefault("payload", {})
        entry["payload"].update(payload)
    rules[signature] = entry
    _save_learned(data)


def load_learned_rules() -> dict[str, Any]:
    data = _load_learned()
    return data.get("rules", {})


# ------------------------------
# Feedback signature extraction
# ------------------------------

_WRONG_PATTERNS = [
    r"\b(wrong|mistake|didn't get it|did not get it|should not have|no|not like that)\b",
    r"\b(you (are|were) doing it wrong)\b",
    r"\b(don't do|stop doing)\b",
    r"\b(next time|from now on)\b",
]

_EXPECT_PATTERNS = [
    r"\b(it should|you should|next time you should|from now on you should|instead)\b",
]


def extract_feedback(event_text: str) -> tuple[str | None, str | None, str | None]:
    """Return (mistake_category, signature, expected_correction)."""
    t = (event_text or "").strip()
    if not t:
        return None, None, None

    low = t.lower()

    mistake_category = None
    signature_parts: list[str] = []

    # Simple categorical signatures for known recurring class of issues.
    if any(x in low for x in ["netflix", "show", "movie", "episode"]) and any(
        x in low for x in ["spotify", "music", "song", "track"]
    ):
        mistake_category = "media_cross_routing_netflix_vs_spotify"
        signature_parts.append("netflix<->spotify")
    elif any(x in low for x in ["youtube", "video", "clip"]) and any(
        x in low for x in ["spotify", "music", "song", "track"]
    ):
        mistake_category = "media_cross_routing_youtube_vs_spotify"
        signature_parts.append("youtube<->spotify")

    # Also capture generic “wrong tool” phrasing.
    if mistake_category is None:
        if any(re.search(p, low) for p in _WRONG_PATTERNS):
            mistake_category = "generic_feedback_wrongness"
            signature_parts.append("generic_wrong")

    # Expected correction heuristic: grab sentence tail after markers.
    expected = None
    if any(re.search(p, low) for p in _EXPECT_PATTERNS):
        expected = t

    # Derive a deterministic signature.
    signature = None
    if signature_parts:
        signature = "|".join(signature_parts)

    return mistake_category, signature, expected


def apply_rule(signature: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Given a signature and runtime context, return correction payload if exists."""
    if not signature:
        return None
    rules = load_learned_rules()
    rule = rules.get(signature)
    if not rule:
        return None
    return rule.get("payload") or {}
