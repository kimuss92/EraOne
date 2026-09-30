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


def get_last_target(default: str | None = None) -> str | None:
    state = load_state()
    return state.get("last_media_app") or state.get("last_app") or default


def clear_state() -> None:
    save_state(dict(_DEFAULT_STATE))
