"""Versioned, declarative HUD scene. No HTML, JavaScript, or executable CSS."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "runtime" / "visual_scene.json"
_lock = threading.RLock()
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_KINDS = {"text", "metric", "progress", "chart", "button", "field"}
# Built-in HUD panels are not custom scene widgets. Their semantic IDs resolve
# here, beside the real dispatcher, so voice adapters cannot invent a protocol.
PANELS = {
    "hud.live_activity": {
        "label": "Live activity",
        "widget": "activity",
        "aliases": ("live activity", "activity", "live tasks", "active tasks"),
    },
    "hud.context": {
        "label": "Context",
        "widget": "context",
        "aliases": ("context", "intelligence"),
    },
    "hud.memory": {"label": "Memory", "widget": "memory", "aliases": ("memory",)},
    "hud.schedule": {"label": "Schedule", "widget": "schedule", "aliases": ("schedule",)},
    "hud.system": {
        "label": "System",
        "widget": "system",
        "aliases": ("system", "system telemetry", "telemetry", "system metrics"),
    },
    "hud.awareness": {
        "label": "System awareness",
        "widget": "awareness",
        "aliases": ("system awareness", "awareness"),
    },
    "hud.sources": {"label": "Sources", "widget": "sources", "aliases": ("sources",)},
    "hud.weather": {
        "label": "Weather",
        "widget": "weather",
        "aliases": ("weather", "time and weather"),
    },
    "hud.calls": {
        "label": "Telegram calls",
        "widget": "calls",
        "aliases": ("telegram calls", "calls"),
    },
    "hud.diagnostics": {
        "label": "Diagnostics",
        "drawer": "diagnostics",
        "aliases": ("diagnostics", "diagnostic", "health checks"),
    },
    "hud.settings": {
        "label": "Settings",
        "drawer": "settings",
        "aliases": ("settings", "preferences"),
    },
    "hud.development": {
        "label": "Development",
        "drawer": "development",
        "aliases": ("development", "developer"),
    },
    "hud.capabilities": {
        "label": "Capabilities",
        "drawer": "capabilities",
        "aliases": ("capabilities", "capability", "tools"),
    },
    "hud.conversation": {
        "label": "Conversation",
        "drawer": "conversation",
        "aliases": ("conversation", "conversation history", "chat history"),
    },
    "hud.telegram": {"label": "Telegram", "drawer": "telegram", "aliases": ("telegram",)},
    "hud.left": {"label": "Left sidebar", "side": "left", "aliases": ("left", "left sidebar")},
    "hud.right": {"label": "Right sidebar", "side": "right", "aliases": ("right", "right sidebar")},
}
_DEFAULT = {
    "revision": 0,
    "layout": {
        "theme": "dark",
        "density": "comfortable",
        "columns": 2,
        "accent": "#56c7dc",
        "left": True,
        "right": True,
    },
    "widgets": [],
}


def snapshot() -> dict:
    with _lock:
        try:
            raw = json.loads(PATH.read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or type(raw.get("revision")) is not int
                or raw["revision"] < 0
            ):
                raise ValueError("Invalid visual state")
            # Validate persisted data too; edited state is never trusted as markup.
            widgets = [_widget(w) for w in raw.get("widgets", [])]
            if len(widgets) > 24 or len({w["id"] for w in widgets}) != len(widgets):
                raise ValueError("Invalid widget collection")
            layout = _layout(raw.get("layout", {}))
            return {"revision": raw["revision"], "layout": layout, "widgets": widgets}
        except (OSError, ValueError, TypeError, KeyError):
            return copy.deepcopy(_DEFAULT)


def _text(value, limit=300) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"Text must be a string of at most {limit} characters")
    return value


def _number(value, minimum=0, maximum=100):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("A finite number is required")
    if not minimum <= value <= maximum:
        raise ValueError(f"Number must be between {minimum} and {maximum}")
    return value


def _layout(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) - set(_DEFAULT["layout"]):
        raise ValueError("Unsupported layout property")
    result = {**_DEFAULT["layout"], **raw}
    if result["theme"] not in {"dark", "light"} or result["density"] not in {
        "compact",
        "comfortable",
    }:
        raise ValueError("Invalid theme or density")
    if type(result["columns"]) is not int or not 1 <= result["columns"] <= 3:
        raise ValueError("Columns must be 1 to 3")
    if not isinstance(result["accent"], str) or not re.fullmatch(
        r"#[0-9a-fA-F]{6}", result["accent"]
    ):
        raise ValueError("Accent must be a six-digit hex color")
    if type(result["left"]) is not bool or type(result["right"]) is not bool:
        raise ValueError("Panel visibility must be boolean")
    return result


def _widget(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) - {
        "id",
        "kind",
        "label",
        "value",
        "series",
        "binding",
        "action",
    }:
        raise ValueError("Invalid widget properties")
    if not isinstance(raw.get("id"), str) or not _ID.fullmatch(raw["id"]):
        raise ValueError("Invalid widget id")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise ValueError("Unsupported widget kind")
    result = {"id": raw["id"], "kind": kind, "label": _text(raw.get("label", ""), 120)}
    if "value" in raw:
        result["value"] = (
            _number(raw["value"])
            if kind == "progress"
            else (
                _number(raw["value"], -1e12, 1e12)
                if isinstance(raw["value"], (float, int))
                else _text(raw["value"])
            )
        )
    if "series" in raw:
        if kind != "chart" or not isinstance(raw["series"], list) or len(raw["series"]) > 128:
            raise ValueError("Chart series must contain at most 128 points")
        result["series"] = [_number(n, -1e12, 1e12) for n in raw["series"]]
    if "binding" in raw:
        if raw["binding"] not in {"cpu_percent", "ram_percent", "development"}:
            raise ValueError("Unsupported data binding")
        if kind not in {"metric", "progress"}:
            raise ValueError("Only metrics and progress support data bindings")
        result["binding"] = raw["binding"]
    if "action" in raw:
        action = raw["action"]
        if kind != "button" or not isinstance(action, dict) or set(action) != {"drawer"}:
            raise ValueError("Buttons only open supported HUD drawers")
        if action["drawer"] not in {
            "schedule",
            "diagnostics",
            "settings",
            "capabilities",
            "conversation",
        }:
            raise ValueError("Unsupported drawer")
        result["action"] = dict(action)
    return result


def panel_catalog() -> list[dict]:
    return [{"id": key, **copy.deepcopy(value)} for key, value in PANELS.items()]


def _panel(command: dict) -> dict:
    if set(command) != {"action", "panel_id", "visible"}:
        raise ValueError("Panel requires only action, panel_id and visible")
    panel_id, visible = command["panel_id"], command["visible"]
    if not isinstance(panel_id, str) or panel_id not in PANELS:
        raise ValueError("Unknown HUD panel; use action panels for supported semantic IDs")
    if type(visible) is not bool:
        raise ValueError("Panel visibility must be boolean")
    spec = PANELS[panel_id]
    if "widget" in spec:
        from app.hud.layout import get_widget_layout

        layout = get_widget_layout()
        state = layout.apply(layout.snapshot()["revision"], widget=spec["widget"], visible=visible)
        return {
            "success": True,
            "panel_id": panel_id,
            "visible": visible,
            "layout": state,
            "message": f"{spec['label']} panel visibility saved: {'shown' if visible else 'hidden'}.",
        }
    if "side" in spec:
        state = apply({"action": "layout", "layout": {spec["side"]: visible}})
        return {
            "success": True,
            "panel_id": panel_id,
            "visible": visible,
            "revision": state["revision"],
            "message": f"{spec['label']} visibility saved: {'shown' if visible else 'hidden'}.",
        }
    from app.hud.events import hud

    hud._emit(
        "hud.panel.visibility",
        metadata={"panel_id": panel_id, "drawer": spec["drawer"], "visible": visible},
    )
    return {
        "success": True,
        "panel_id": panel_id,
        "visible": visible,
        "message": f"{'Show' if visible else 'Hide'} request sent to connected HUDs for {spec['label']}.",
    }


def apply(command: dict) -> dict:
    if isinstance(command, dict) and command.get("action") == "panels":
        if set(command) != {"action"}:
            raise ValueError("Panels requires only action")
        return {"panels": panel_catalog()}
    if isinstance(command, dict) and command.get("action") == "panel":
        return _panel(command)
    # Navigation is ephemeral: no scene write, layout mutation or machine action.
    if isinstance(command, dict) and command.get("action") == "focus":
        if set(command) != {"action", "view"}:
            raise ValueError("Focus requires only action and view")
        from app.hud.events import hud

        return hud.focus_view(command["view"])
    if not isinstance(command, dict) or set(command) - {
        "action",
        "widget",
        "id",
        "layout",
        "revision",
    }:
        raise ValueError("Invalid visual command")
    with _lock:
        state = snapshot()
        if "revision" in command and command["revision"] != state["revision"]:
            raise ValueError("Scene changed; refresh before editing")
        action = command.get("action")
        if action == "upsert":
            widget = _widget(command.get("widget"))
            state["widgets"] = [w for w in state["widgets"] if w["id"] != widget["id"]] + [widget]
            if len(state["widgets"]) > 24:
                raise ValueError("Maximum 24 widgets")
        elif action == "remove":
            if not isinstance(command.get("id"), str) or not _ID.fullmatch(command["id"]):
                raise ValueError("Invalid widget id")
            state["widgets"] = [w for w in state["widgets"] if w["id"] != command["id"]]
        elif action == "layout":
            partial = command.get("layout", {})
            if not isinstance(partial, dict):
                raise ValueError("Layout must be an object")
            state["layout"] = _layout({**state["layout"], **partial})
        elif action == "reset":
            state = {**copy.deepcopy(_DEFAULT), "revision": state["revision"]}
        else:
            raise ValueError("Use upsert, remove, layout, or reset")
        state["revision"] += 1
        PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        os.replace(temporary, PATH)
    from app.hud.events import hud

    hud._emit("visual.changed", revision=state["revision"])
    return state
