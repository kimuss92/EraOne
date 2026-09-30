# core/browser_grid_state.py
"""
In-memory short-term grid snapshot + scroll-context state.

Pure in-process storage. No disk I/O. No persistence across restarts.
No cookies, no tokens, no full HTML, no personal data.

Public API:
  GridCandidate            — single visible/safe grid entry
  GridSnapshot             — a complete visible-grid snapshot for one platform
  save_grid(snapshot)      — store/overwrite snapshot for a platform
  get_recent_grid(...)     — get a fresh snapshot, or None
  load_recent_grids()      — all fresh snapshots keyed by platform
  clear_expired()          — drop expired entries
  clear_all_for_tests()    — reset everything (tests only)
  set_scroll_context(platform)         — note recent platform activity
  get_scroll_context(ttl_seconds=90)   — last YT/NF platform within window
  set_last_user_query(query, platform) — store last user media query (runtime only)
  get_last_user_query()                — dict with 'query' and 'platform'
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

GRID_TTL_SECONDS: int = 300
SCROLL_CONTEXT_TTL_SECONDS: int = 90

_PLATFORMS: tuple[str, ...] = ("youtube", "netflix")

_grids: dict[str, GridSnapshot] = {}
_scroll_context: dict[str, float] = {}
_last_user_query: dict[str, str] = {"query": "", "platform": ""}


@dataclass
class GridCandidate:
    visible_order: int
    title: str
    url: str
    kind: str
    safe: bool = True
    rejected_reason: str = ""
    source: str = ""


@dataclass
class GridSnapshot:
    platform: str
    query: str
    timestamp: float
    candidates: list[GridCandidate] = field(default_factory=list)


def _now() -> float:
    return time.time()


def clear_expired(ttl_seconds: int = GRID_TTL_SECONDS) -> None:
    """Drop grid snapshots older than ttl_seconds."""
    cutoff = _now() - ttl_seconds
    for platform in list(_grids.keys()):
        snap = _grids[platform]
        if snap is None or snap.timestamp < cutoff:
            _grids.pop(platform, None)


def clear_all_for_tests() -> None:
    """Reset all in-memory state. Intended for test isolation only."""
    _grids.clear()
    _scroll_context.clear()
    _last_user_query["query"] = ""
    _last_user_query["platform"] = ""


def save_grid(snapshot: GridSnapshot) -> None:
    """Store or overwrite a snapshot for its platform."""
    if snapshot is None:
        return
    platform = (snapshot.platform or "").lower().strip()
    if platform not in _PLATFORMS:
        return
    snapshot.platform = platform
    snapshot.timestamp = float(snapshot.timestamp or _now())
    _grids[platform] = snapshot


def get_recent_grid(platform: str, ttl_seconds: int = GRID_TTL_SECONDS) -> GridSnapshot | None:
    p = (platform or "").lower().strip()
    snap = _grids.get(p)
    if snap is None:
        return None
    if (_now() - snap.timestamp) > ttl_seconds:
        _grids.pop(p, None)
        return None
    return snap


def load_recent_grids(ttl_seconds: int = GRID_TTL_SECONDS) -> dict[str, GridSnapshot]:
    clear_expired(ttl_seconds)
    return {p: _grids[p] for p in _PLATFORMS if p in _grids}


def set_scroll_context(platform: str) -> None:
    p = (platform or "").lower().strip()
    if p not in _PLATFORMS:
        return
    _scroll_context[p] = _now()


def get_scroll_context(ttl_seconds: int = SCROLL_CONTEXT_TTL_SECONDS) -> str | None:
    """Return the most-recent YouTube/Netflix platform within the TTL window.

    Returns None if no platform context is fresh, or if the latest context
    is older than ttl_seconds.
    """
    cutoff = _now() - ttl_seconds
    best_platform: str | None = None
    best_ts = 0.0
    for p, ts in _scroll_context.items():
        if ts >= cutoff and ts > best_ts:
            best_ts = ts
            best_platform = p
    return best_platform


def clear_scroll_context_for_tests() -> None:
    _scroll_context.clear()


# ── Runtime last-user-query ──────────────────────────────────────────────────


def set_last_user_query(query: str, platform: str = "") -> None:
    """Store the last user media query for cross-turn platform disambiguation.

    In-memory only — no persistence, no long-term memory write.
    """
    _last_user_query["query"] = (query or "").strip()
    _last_user_query["platform"] = (platform or "").lower().strip()


def get_last_user_query() -> dict[str, str]:
    """Return dict with 'query' and 'platform' from the last user utterance."""
    return dict(_last_user_query)


def clear_last_user_query_for_tests() -> None:
    _last_user_query["query"] = ""
    _last_user_query["platform"] = ""
