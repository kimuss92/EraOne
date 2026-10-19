"""Bounded metadata from the existing index watcher, without a second observer."""

from __future__ import annotations

import stat
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from core.foreground_awareness import FRESH_S, ForegroundAwareness, _log

MAX_PENDING = 256
MAX_RECENT = 64


def index_events(service):
    source = service.source
    unsubscribe = source.subscribe_changes(service.enqueue)
    last_read, last_status, next_retry, retry_s = 0.0, None, 0.0, 1.0
    try:
        while not service._stop.is_set():
            status = source.watcher_status()
            now = time.monotonic()
            if (
                not status["running"]
                and (status["roots"] or status["failedRoots"])
                and now >= next_retry
            ):
                try:
                    source.start_watcher(
                        roots=[Path(r) for r in status["roots"] + status["failedRoots"]]
                    )
                except Exception as exc:
                    _log("restart_failed", exc, kind="files")
                status = source.watcher_status()
                next_retry, retry_s = now + retry_s, min(retry_s * 2, 15.0)
            elif status["running"]:
                retry_s = 1.0
            if service._dirty.is_set() or status != last_status or now - last_read >= 15:
                service.refresh(status)
                last_read, last_status = now, status
            service._stop.wait(0.25)
    finally:
        unsubscribe()


class FileAwareness(ForegroundAwareness):
    kind = "files"

    def __init__(self, *, source=None, publisher=None, clock=time.time, excluded_roots=()):
        super().__init__(publisher=publisher, driver=index_events, clock=clock)
        if source is None:
            from actions.file_indexer import get_file_index

            source = get_file_index()
        self.source = source
        self._excluded = tuple(Path(p).resolve() for p in excluded_roots)
        self._pending = OrderedDict()
        self._preferred_roots = ()
        self._recent = OrderedDict()
        self._dirty = threading.Event()
        self._dropped = self._coalesced = 0
        self._signature = None
        self._view = {
            "revision": 0,
            "health": "unavailable",
            "coverage": "none",
            "observedAt": 0.0,
            "roots": [],
            "failedRoots": [],
            "changes": [],
            "droppedEvents": 0,
            "coalescedEvents": 0,
            "exclusions": ["index database, generated/system directories, Era runtime metadata"],
        }

    def _priority(self, path):
        key = str(path).replace("\\", "/").casefold().rstrip("/")
        return any(key == root or key.startswith(root + "/") for root in self._preferred_roots)

    def enqueue(self, event):
        """Native callback only copies bounded values; never stat/read/publish here."""
        if (
            not isinstance(event, dict)
            or set(event) != {"kind", "path", "destination", "isDirectory", "observedAt"}
            or event["kind"] not in {"created", "modified", "moved", "deleted"}
            or type(event["isDirectory"]) is not bool
            or not isinstance(event["path"], str)
            or not 0 < len(event["path"]) <= 4096
            or "\0" in event["path"]
            or (
                event["destination"] is not None
                and (
                    not isinstance(event["destination"], str)
                    or not 0 < len(event["destination"]) <= 4096
                    or "\0" in event["destination"]
                )
            )
        ):
            raise ValueError("invalid file event")
        copied = deepcopy(event)
        copied["observedAt"] = self._clock()  # Never trust a source timestamp.
        key = (copied["path"], copied["destination"])
        with self._lock:
            if key in self._pending:
                self._pending.pop(key)
                self._coalesced += 1
            elif len(self._pending) >= MAX_PENDING:
                self._dropped += 1
                expendable = next(
                    (k for k, v in self._pending.items() if not self._priority(v["path"])), None
                )
                if expendable is None and not self._priority(copied["path"]):
                    return  # Keep foreground/user-folder evidence ahead of background churn.
                self._pending.pop(
                    expendable if expendable is not None else next(iter(self._pending))
                )
            self._pending[key] = copied
            self._dirty.set()

    def _allowed(self, value, roots):
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            return None
        resolved = path.resolve()
        from actions.file_indexer import SKIP_DIRS

        if any(p.casefold() in {s.casefold() for s in SKIP_DIRS} for p in path.parts):
            return None
        if any(resolved == root or resolved.is_relative_to(root) for root in self._excluded):
            return None
        return (
            path
            if any(resolved == root or resolved.is_relative_to(root) for root in roots)
            else None
        )

    def _metadata(self, value):
        try:
            info = Path(value).lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in {
                0xA000000C,
                0xA0000003,
            }:
                return {"state": "link", "sizeBytes": None, "modifiedAt": None}
            return {
                "state": "exists",
                "sizeBytes": None if stat.S_ISDIR(info.st_mode) else info.st_size,
                "modifiedAt": info.st_mtime,
            }
        except FileNotFoundError:
            return {"state": "missing", "sizeBytes": None, "modifiedAt": None}
        except OSError:
            return {"state": "unknown", "sizeBytes": None, "modifiedAt": None}

    def refresh(self, status=None):
        status = self.source.watcher_status() if status is None else status
        discovery = status.get("discovery", {})
        with self._lock:
            self._view["indexDiscovery"] = deepcopy(discovery)
        raw_roots = status["roots"]
        if (
            not isinstance(raw_roots, list)
            or len(raw_roots) > 128
            or any(not isinstance(r, str) or not Path(r).is_absolute() for r in raw_roots)
        ):
            raise ValueError("invalid file coverage")
        roots = tuple(Path(r).resolve() for r in raw_roots)
        failures = list(status["failedRoots"])
        for root in raw_roots:
            try:
                if not Path(root).is_dir() and root not in failures:
                    failures.append(root)
            except OSError:
                if root not in failures:
                    failures.append(root)
        with self._lock:
            self._preferred_roots = tuple(
                str(r).replace("\\", "/").casefold().rstrip("/") for r in roots if r != r.parent
            )
            pending = list(self._pending.values())
            self._pending.clear()
            self._dirty.clear()
        if not status["running"] or not roots:
            self._publish("unavailable", raw_roots, failures, "none")
            return
        touched = set()
        for change in pending:
            try:
                src = self._allowed(change["path"], roots)
                dest = self._allowed(change["destination"], roots)
            except OSError:
                continue
            if src is None and dest is None:
                continue
            if src is None:
                change.update(kind="created", path=str(dest), destination=None)
            elif change["kind"] == "moved" and dest is None:
                change.update(kind="deleted", destination=None)
            self._recent.pop(change["path"], None)
            self._recent[change["path"]] = change
            touched.add(change["path"])
            while len(self._recent) > MAX_RECENT:
                expendable = next(
                    (p for p in self._recent if not self._priority(p)), next(iter(self._recent))
                )
                self._recent.pop(expendable)
        # Reconcile only recent paths. No full-tree scan or file-content reads.
        for path, change in tuple(self._recent.items()):
            try:
                if self._allowed(path, roots) is None:
                    self._recent.pop(path)
                    continue
                destination = change["destination"]
                if destination and self._allowed(destination, roots) is None:
                    change["destination"] = None
                    destination = None
                source = self._metadata(path)
                target = self._metadata(destination) if destination else None
                if (
                    path not in touched
                    and "source" in change
                    and (change["source"] != source or change.get("target") != target)
                ):
                    change["kind"], change["observedAt"] = "reconciled", self._clock()
                change["source"], change["target"] = source, target
                change["reconciledAt"] = self._clock()
            except OSError:
                change["source"] = {"state": "unknown", "sizeBytes": None, "modifiedAt": None}
        health = (
            "degraded"
            if (
                failures
                or self._dropped
                or discovery.get("dropped")
                or discovery.get("last", {}).get("incomplete")
                or any(
                    c["source"]["state"] == "unknown"
                    or (c["target"] and c["target"]["state"] == "unknown")
                    for c in self._recent.values()
                )
            )
            else "ready"
        )
        self._publish(health, raw_roots, failures, "file-events+recent-path-reconciliation")

    def _publish(self, health, roots, failures, coverage):
        if self._stop.is_set():
            health, coverage = "unavailable", "none"
        changes = (
            []
            if health == "unavailable"
            else deepcopy(sorted(self._recent.values(), key=lambda c: self._priority(c["path"])))
        )
        meaningful = [{k: v for k, v in c.items() if k != "reconciledAt"} for c in changes]
        with self._lock:
            if health == "ready" and self._dropped:
                health = "degraded"
            signature = (
                health,
                roots,
                failures,
                coverage,
                meaningful,
                self._dropped,
                self._coalesced,
                deepcopy(self._view.get("indexDiscovery", {})),
            )
            changed = signature != self._signature
            self._signature = deepcopy(signature)
            self._view.update(
                revision=self._view["revision"] + 1,
                health=health,
                coverage=coverage,
                observedAt=self._clock() if health != "unavailable" else 0.0,
                roots=list(roots),
                failedRoots=list(failures),
                changes=changes,
                droppedEvents=self._dropped,
                coalescedEvents=self._coalesced,
            )
            view = deepcopy(self._view)
        if self._publisher:
            try:
                self._publisher(view, changed=changed)
            except Exception as exc:
                _log("publish_failed", exc, kind="files")

    def snapshot(self):
        with self._lock:
            view = deepcopy(self._view)
        if (
            view["health"] in {"ready", "degraded"}
            and not 0 <= self._clock() - view["observedAt"] <= FRESH_S
        ):
            view.update(health="stale", coverage="none", changes=[])
        return view

    def unavailable(self):
        self._publish("unavailable", [], [], "none")


_service = None
_service_lock = threading.Lock()


def get_file_awareness():
    global _service
    with _service_lock:
        if _service is None:
            from app.hud.events import hud
            from core.utils import BASE_DIR

            _service = FileAwareness(
                publisher=hud.files_observed,
                excluded_roots=[
                    BASE_DIR / "logs",
                    BASE_DIR / "memory",
                    BASE_DIR / "graphify-out",
                ],
            )
        return _service


def file_summary():
    view = get_file_awareness().snapshot()
    # HUD keeps exact paths; bounded reasoning hints must not expand with drive coverage.
    truncated = False

    def hint(path):
        nonlocal truncated
        if path is not None and len(path) > 256:
            truncated = True
            return path[:120] + "…" + path[-135:]
        return path

    roots, failures, changes = view["roots"], view["failedRoots"], view["changes"]
    view.update(
        roots=[hint(p) for p in roots[:4]],
        failedRoots=[hint(p) for p in failures[:4]],
        unlistedRoots=max(0, len(roots) - 4),
        unlistedFailedRoots=max(0, len(failures) - 4),
        changes=deepcopy(changes[-6:]),
        unlistedChanges=max(0, len(changes) - 6),
    )
    for change in view["changes"]:
        change["path"] = hint(change["path"])
        change["destination"] = hint(change["destination"])
    view["pathHintsTruncated"] = truncated
    return view
