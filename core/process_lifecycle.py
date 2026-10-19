"""Read-only instance inventory driven by window events and reconciliation."""

from __future__ import annotations

import math
import threading
import time
from copy import deepcopy
from functools import partial

from core.foreground_awareness import FRESH_S, ForegroundAwareness, _log, native_events

MAX_PROCESSES = 4096


def read_inventory():
    import psutil

    rows, omitted = [], 0
    for process in psutil.process_iter(["pid", "name", "create_time"], ad_value=None):
        info = process.info
        if not info["name"] or info["pid"] <= 0:
            omitted += 1
            continue
        if len(rows) >= MAX_PROCESSES:
            omitted += 1
            continue
        born = info["create_time"]
        # Windows can report zero for system-process creation time. Retain the
        # instance as uncertain; never let it invalidate all readable processes.
        if born is not None and (not math.isfinite(born) or born <= 0):
            born = None
        rows.append({"pid": info["pid"], "app": info["name"], "startedAt": born})
    return {"processes": rows, "omitted": omitted}


class ProcessLifecycle(ForegroundAwareness):
    """Reuse the verified worker lifecycle, with a separate inventory contract."""

    kind = "process"

    def __init__(self, *, publisher=None, reader=read_inventory, driver=None, clock=time.time):
        self._reader = reader
        self._fallback_read, self._max_retry_s = reader, 15.0
        super().__init__(
            publisher=publisher,
            clock=clock,
            driver=driver
            or partial(
                native_events,
                reader=reader,
                event_range=(0x8000, 0x8003),
                window_only=True,
                min_interval=0.5,
            ),
        )
        self._view = {
            "revision": 0,
            "health": "unavailable",
            "observedAt": 0.0,
            "coverage": "none",
            "processes": [],
            "omitted": 0,
        }

    def snapshot(self):
        with self._lock:
            view = deepcopy(self._view)
        if (
            view["health"] in ("ready", "degraded")
            and not 0 <= self._clock() - view["observedAt"] <= FRESH_S
        ):
            view["health"], view["processes"], view["coverage"] = "stale", [], "none"
        return view

    def observe(self, data, *, coverage="window-events+reconciliation"):
        if not isinstance(data, dict) or set(data) != {"processes", "omitted"}:
            raise ValueError("invalid process inventory")
        omitted, rows = data["omitted"], data["processes"]
        if (
            type(omitted) is not int
            or omitted < 0
            or not isinstance(rows, list)
            or len(rows) > MAX_PROCESSES
        ):
            raise ValueError("invalid process bounds")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"pid", "app", "startedAt"}:
                raise ValueError("invalid process fields")
            pid, app, born = row["pid"], row["app"], row["startedAt"]
            if (
                type(pid) is not int
                or pid <= 0
                or pid in seen
                or not isinstance(app, str)
                or not app
                or len(app) > 256
                or (
                    born is not None
                    and (type(born) not in (int, float) or not math.isfinite(born) or born <= 0)
                )
            ):
                raise ValueError("invalid process identity")
            seen.add(pid)
            result.append(
                {
                    "id": f"{pid}:{born}",
                    "pid": pid,
                    "app": app,
                    "startedAt": born,
                    "identityVerified": born is not None,
                }
            )
        result.sort(key=lambda row: row["pid"])
        health = (
            "degraded"
            if omitted
            or coverage == "reconciliation-only"
            or any(not row["identityVerified"] for row in result)
            else "ready"
        )
        self._publish(health, result, omitted, coverage, self._clock())

    def unavailable(self):
        self._publish("unavailable", [], 0, "none", 0.0)

    def _publish(self, health, rows, omitted, coverage, observed_at):
        with self._lock:
            old = self._view
            changed = (old["health"], old["processes"], old["omitted"], old["coverage"]) != (
                health,
                rows,
                omitted,
                coverage,
            )
            self._view = {
                "revision": old["revision"] + 1,
                "health": health,
                "observedAt": observed_at,
                "processes": deepcopy(rows),
                "omitted": omitted,
                "coverage": coverage,
            }
            view = deepcopy(self._view)
        if self._publisher:
            try:
                self._publisher(view, changed=changed)
            except Exception as exc:
                _log("publish_failed", exc, kind=self.kind)


_service = None
_service_lock = threading.Lock()


def get_process_lifecycle():
    global _service
    with _service_lock:
        if _service is None:
            from app.hud.events import hud

            _service = ProcessLifecycle(publisher=hud.processes_observed)
        return _service


def process_summary(limit=40):
    view = get_process_lifecycle().snapshot()
    names = {}
    for row in view["processes"]:
        names[row["app"]] = names.get(row["app"], 0) + 1
    selected = dict(sorted(names.items())[:limit])
    return {
        "health": view["health"],
        "coverage": view["coverage"],
        "observedAt": view["observedAt"],
        "apps": selected,
        "unlistedAppNames": max(0, len(names) - len(selected)),
        "omitted": view["omitted"],
    }
