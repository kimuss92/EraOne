# core/intent_memory.py
"""
Short-term intent memory for Era One.

This is runtime memory, not personal long-term memory. It tracks the latest
target app, media app, browser, action and active window so commands like
"pause it", "resume", "close this", "go back" can be resolved without guessing.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from core.utils import get_memory_dir
from memory.safe_store import safe_write_json

STATE_PATH = get_memory_dir() / "runtime_intent.json"
_LOCK = threading.RLock()

_DEFAULT_STATE: dict[str, Any] = {
    "last_app": None,
    "last_action": None,
    "last_tool": None,
    "last_media_app": None,
    "last_browser": "brave",
    "last_window": None,
    "previous_window": None,
    "last_result": None,
    "last_error": None,
    "updated_at": 0.0,
    "history": [],
}


def _now() -> float:
    return time.time()


def _load_unlocked() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            state = dict(_DEFAULT_STATE)
            state.update(data)
            if not isinstance(state.get("history"), list):
                state["history"] = []
            return state
    except Exception:
        pass
    return dict(_DEFAULT_STATE)


def load_state() -> dict[str, Any]:
    with _LOCK:
        return _load_unlocked()


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        state = dict(_DEFAULT_STATE | (state or {}))
        state["updated_at"] = _now()
        hist = state.get("history") or []
        state["history"] = hist[-30:]
        safe_write_json(STATE_PATH, state)
        return state


def remember_action(
    *,
    tool: str | None = None,
    action: str | None = None,
    app: str | None = None,
    media_app: str | None = None,
    browser: str | None = None,
    window: str | None = None,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    with _LOCK:
        state = _load_unlocked()
        if window and window != state.get("last_window"):
            state["previous_window"] = state.get("last_window")
            state["last_window"] = window
        if app:
            state["last_app"] = app
        if media_app:
            state["last_media_app"] = media_app
        if browser:
            state["last_browser"] = browser
        if tool:
            state["last_tool"] = tool
        if action:
            state["last_action"] = action
        if result is not None:
            state["last_result"] = str(result)[:500]
            state["last_error"] = None
        if error is not None:
            state["last_error"] = str(error)[:500]

        state.setdefault("history", []).append(
            {
                "time": _now(),
                "tool": tool,
                "action": action,
                "app": app,
                "media_app": media_app,
                "browser": browser,
                "window": window,
                "result": str(result)[:200] if result is not None else None,
                "error": str(error)[:200] if error is not None else None,
            }
        )
        return save_state(state)


# How long a Netflix use keeps "Netflix is the active page" true for the generic
# key/type/click guards in the router. A flag with no expiry hijacked every
# later keypress for as long as the file existed (live 2026-11-15: "type
# instagram.com in Brave" became a 49 s Netflix search).
NETFLIX_CONTEXT_TTL_S = 900.0


def _is_netflix_evidence(entry: dict) -> bool:
    """A history row that shows the owner really used Netflix.

    Not evidence: a failed call, and a bare `status` answer of "unknown". The
    router's own hijack ends in exactly that row, so counting it would let the
    guard renew itself forever.
    """
    if entry.get("media_app") != "netflix" or entry.get("error"):
        return False
    return not str(entry.get("result") or "").lower().startswith("netflix state: unknown")


def netflix_context_fresh(state: dict[str, Any] | None = None, *, now: float | None = None) -> bool:
    """True when Netflix was genuinely in use within NETFLIX_CONTEXT_TTL_S.

    A bare `last_media_app` flag with no history behind it (tests, a hand-built
    state) keeps its old meaning: there is no timeline to judge it by.
    """
    state = load_state() if state is None else state
    if not state or state.get("last_media_app") != "netflix":
        return False
    rows = [e for e in (state.get("history") or []) if isinstance(e, dict)]
    if not any(e.get("media_app") == "netflix" for e in rows):
        return True
    stamps = [float(e.get("time") or 0) for e in rows if _is_netflix_evidence(e)]
    if not stamps:
        return False
    return ((_now() if now is None else now) - max(stamps)) <= NETFLIX_CONTEXT_TTL_S


def get_last_target(default: str | None = None) -> str | None:
    state = load_state()
    return state.get("last_media_app") or state.get("last_app") or default


def clear_state() -> None:
    save_state(dict(_DEFAULT_STATE))
