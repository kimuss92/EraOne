"""Backend-owned, revisioned HUD widget layout; no executable widget content."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path

CATALOG = {
    "core": "Era neural core",
    "activity": "Live activity",
    "calls": "Telegram calls",
    "schedule": "Schedule",
    "system": "System",
    "awareness": "System awareness",
    "context": "Context",
    "sources": "Sources",
    "memory": "Memory",
    "weather": "Weather",
}

# Twelve-column cockpit. Geometry contains only bounded display coordinates.
POSITIONS = {
    "core": (3, 0, 6, 28),
    "activity": (0, 0, 3, 8),
    "calls": (9, 5, 3, 5),
    "weather": (9, 0, 3, 5),
    "schedule": (0, 8, 3, 7),
    "system": (0, 21, 3, 7),
    "memory": (0, 15, 3, 6),
    "awareness": (9, 21, 3, 7),
    "context": (9, 10, 3, 6),
    "sources": (9, 16, 3, 5),
}


def geometry(widget):
    return dict(zip(("x", "y", "w", "h"), POSITIONS[widget], strict=True))


def overlaps(a, b):
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


class LayoutConflict(ValueError):
    pass


class WidgetLayout:
    def __init__(self, path: Path, notify=None):
        self.path, self.notify = path, notify
        self._lock = threading.RLock()
        self._data = {
            "schemaVersion": 3,
            "revision": 0,
            "view": "command-center",
            "widgets": [{"id": key, "visible": True, **geometry(key)} for key in CATALOG],
        }
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))
            # Upgrade the original nine-widget layout without changing visibility,
            # ordering, revision or focus view. Persist only after a successful edit.
            if isinstance(self._data, dict) and self._data.get("schemaVersion") == 1:
                old = deepcopy(self._data)
                self._validate(old, legacy=True)
                old["schemaVersion"] = 3
                slots = [key for key in CATALOG if key != "core"]
                for index, item in enumerate(old["widgets"]):
                    item.update(geometry(slots[index]))
                old["widgets"].insert(0, {"id": "core", "visible": True, **geometry("core")})
                self._data = old
            elif isinstance(self._data, dict) and self._data.get("schemaVersion") == 2:
                # v2 rendered fixed docks, ignoring its saved coordinates. Move
                # those visible docks to the new arrangement and preserve order
                # and visibility. Only a successful user edit persists migration.
                self._validate(self._data, version=2)
                self._data["schemaVersion"] = 3
                movable = ["activity", "calls", "schedule", "context", "sources", "memory"]
                ordered = [w for w in self._data["widgets"] if w["id"] in movable]
                for item in self._data["widgets"]:
                    item.update(geometry(item["id"]))
                for slot, item in zip(movable, ordered, strict=True):
                    item.update(geometry(slot))
            self._validate(self._data)

    @staticmethod
    def _validate(data, legacy=False, version=3):
        if (
            not isinstance(data, dict)
            or set(data) != {"schemaVersion", "revision", "view", "widgets"}
            or type(data["schemaVersion"]) is not int
            or data["schemaVersion"] != (1 if legacy else version)
            or type(data["revision"]) is not int
            or data["revision"] < 0
            or not isinstance(data["view"], str)
            or data["view"] not in {"command-center", "conversation"}
            or not isinstance(data["widgets"], list)
            or len(data["widgets"]) != len(CATALOG) - int(legacy)
        ):
            raise ValueError("Invalid widget layout")
        seen = set()
        for widget in data["widgets"]:
            if (
                not isinstance(widget, dict)
                or set(widget)
                != ({"id", "visible"} if legacy else {"id", "visible", "x", "y", "w", "h"})
                or not isinstance(widget["id"], str)
                or widget["id"] not in CATALOG
                or widget["id"] in seen
                or (legacy and widget["id"] == "core")
                or type(widget["visible"]) is not bool
            ):
                raise ValueError("Invalid widget identity")
            seen.add(widget["id"])
            if not legacy and (
                any(type(widget[k]) is not int for k in ("x", "y", "w", "h"))
                or not 3 <= widget["w"] <= 12
                or not 4 <= widget["h"] <= (24 if version == 2 else 32)
                or not 0 <= widget["x"] <= 12 - widget["w"]
                or not 0 <= widget["y"] <= 200
            ):
                raise ValueError("Invalid widget geometry")
        if not legacy:
            visible = [w for w in data["widgets"] if w["visible"]]
            if any(overlaps(a, b) for i, a in enumerate(visible) for b in visible[i + 1 :]):
                raise ValueError("Widgets cannot overlap")

    def snapshot(self):
        with self._lock:
            return {**deepcopy(self._data), "catalog": dict(CATALOG)}

    def apply(
        self,
        expected_revision,
        *,
        widget=None,
        visible=None,
        before=None,
        view=None,
        placement=None,
        reorder_only=False,
    ):
        with self._lock:
            if type(reorder_only) is not bool or (
                reorder_only and (before is None or placement is not None)
            ):
                raise ValueError("Order-only edits require a reorder target and no placement")
            if type(expected_revision) is not int or expected_revision != self._data["revision"]:
                raise LayoutConflict("Layout changed; refresh before editing.")
            data = deepcopy(self._data)
            if widget is None and (
                visible is not None or before is not None or placement is not None
            ):
                raise ValueError("Widget identity required")
            if widget is not None:
                if not isinstance(widget, str) or widget not in CATALOG:
                    raise ValueError("Unknown widget")
                selected = next(w for w in data["widgets"] if w["id"] == widget)
                if placement is not None:
                    if not isinstance(placement, dict) or set(placement) != {"x", "y", "w", "h"}:
                        raise ValueError("Invalid placement fields")
                    selected.update(placement)
                if visible is not None:
                    if type(visible) is not bool:
                        raise ValueError("Visibility must be boolean")
                    selected["visible"] = visible
                if before is not None:
                    if not isinstance(before, str) or before not in CATALOG or before == widget:
                        raise ValueError("Invalid reorder target")
                    data["widgets"].remove(selected)
                    data["widgets"].insert(
                        next(i for i, w in enumerate(data["widgets"]) if w["id"] == before),
                        selected,
                    )
                    if not reorder_only:
                        selected.update(
                            {
                                k: next(w for w in data["widgets"] if w["id"] == before)[k]
                                for k in ("x", "y")
                            }
                        )
                        selected["x"] = min(selected["x"], 12 - selected["w"])
                # Keep the chosen panel where the user dropped it; move collisions
                # downward deterministically. Hidden panels occupy no space.
                if (
                    placement is not None
                    or (before is not None and not reorder_only)
                    or visible is True
                ):
                    self._validate(
                        {**data, "widgets": [dict(w, visible=False) for w in data["widgets"]]}
                    )
                    placed = [selected] if selected["visible"] else []
                    for other in sorted(
                        (w for w in data["widgets"] if w is not selected and w["visible"]),
                        key=lambda w: w["y"],
                    ):
                        while any(overlaps(other, p) for p in placed):
                            other["y"] = max(p["y"] + p["h"] for p in placed if overlaps(other, p))
                        placed.append(other)
            if view is not None:
                data["view"] = view
            data["revision"] += 1
            self._validate(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".hud-layout-", suffix=".tmp")
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Defender and Windows readers can briefly deny replacement.
                # Keep the previous revision until the atomic write succeeds.
                for attempt in range(6):
                    try:
                        os.replace(temporary, self.path)
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.025 * (2**attempt))
            finally:
                temporary.unlink(missing_ok=True)
            self._data = data
            snapshot = self.snapshot()
            # Emit under this service's lock so concurrent updates cannot reorder.
            if self.notify:
                self.notify(snapshot)
            return snapshot


_service = None
_lock = threading.Lock()


def get_widget_layout():
    global _service
    with _lock:
        if _service is None:
            from app.hud.events import hud

            root = os.environ.get("LOCALAPPDATA")
            if not root:
                raise RuntimeError("Local application-data directory unavailable")
            _service = WidgetLayout(
                Path(root) / "EraOne" / "hud-layout.json",
                notify=hud.widget_layout_observed,
            )
            hud.widget_layout_observed(_service.snapshot())
        return _service
