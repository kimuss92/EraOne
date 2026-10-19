"""Validated view registrations for HUD navigation and capability discovery."""

import re
import threading

_lock = threading.RLock()
_views = {}
_RENDERERS = frozenset(
    {
        "overview",
        "telemetry",
        "activity",
        "memory",
        "context",
        "schedule",
        "diagnostics",
        "tools",
        "telegram",
        "development",
    }
)


def register(view: str, label: str, target: str | None = None) -> None:
    if not isinstance(view, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", view):
        raise ValueError("Invalid HUD view id")
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        raise ValueError("Invalid HUD view label")
    with _lock:
        target = target or view
        if target not in _RENDERERS and target not in _views:
            raise ValueError("Register a renderer before registering its alias")
        if target in _views:
            target = _views[target]["target"]
        _views[view] = {"label": label, "target": target}


def snapshot() -> dict:
    with _lock:
        return {key: dict(value) for key, value in _views.items()}


def resolve(view: str) -> str:
    with _lock:
        if not isinstance(view, str) or view not in _views:
            raise ValueError("Unsupported HUD view; supported: " + ", ".join(_views))
        return _views[view]["target"]


for _view, _label in {
    "overview": "Overview",
    "telemetry": "System telemetry",
    "activity": "Live activity",
    "memory": "Memory",
    "context": "Context",
    "schedule": "Schedule",
    "diagnostics": "Diagnostics",
    "tools": "Tools",
    "telegram": "Telegram",
    "development": "Development",
}.items():
    register(_view, _label)
