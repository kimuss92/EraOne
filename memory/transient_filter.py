"""Shared transient-key filter for long-term memory.

Stable user preferences (preferred_browser, voice_style, etc.) are allowed.
Transient media/browser/search state is blocked — it belongs in runtime intent.
"""

from __future__ import annotations

# Key prefixes or exact names that are always transient, regardless of category.
_TRANSIENT_KEY_PREFIXES = (
    "last_media_",
    "current_media_",
    "last_browser_",
    "current_browser_",
    "last_search_query",
    "last_selected_candidate",
    "selected_candidate",
    "current_query",
    "temporary_",
    "session_",
)

# Exact keys that are transient (no prefix match needed).
_TRANSIENT_EXACT = frozenset(
    {
        "last_media_platform",
        "last_media_title",
        "last_media_source",
        "last_browser_platform",
        "last_search_query",
        "last_selected_candidate",
        "selected_candidate",
        "current_query",
    }
)


def is_transient_memory_key(category: str, key: str) -> bool:
    """Return True if this category/key should be blocked from long-term memory."""
    k = (key or "").strip().lower()
    if not k:
        return False
    if k in _TRANSIENT_EXACT:
        return True
    for prefix in _TRANSIENT_KEY_PREFIXES:
        if k.startswith(prefix):
            return True
    return False
