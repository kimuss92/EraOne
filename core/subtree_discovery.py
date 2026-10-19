"""Bounded metadata-only discovery of new trees outside the watcher callback."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path


class SubtreeDiscovery:
    def __init__(self, database, ignored, *, limit=5000, budget=3.0):
        self.database, self.ignored = database, ignored
        self.limit, self.budget = limit, budget
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._stop = False
        self._dropped = 0
        self._last = {"indexed": 0, "skipped": 0, "incomplete": False}
        self._active = False
        self._active_root = ""
        self._epoch = 0
        self._thread = threading.Thread(target=self._run, name="era-subtree-discovery", daemon=True)
        self._thread.start()

    def enqueue(self, path):
        with self._condition:
            if self._stop:
                return
            key = os.path.normcase(os.path.abspath(path))
            if key not in self._pending and len(self._pending) >= 64:
                self._dropped += 1
                return
            self._pending[key] = str(path)
            self._condition.notify()

    def snapshot(self):
        with self._condition:
            return {
                "pending": len(self._pending),
                "active": self._active,
                "dropped": self._dropped,
                "last": dict(self._last),
            }

    def invalidate(self, path):
        key = os.path.normcase(os.path.abspath(path))
        with self._condition:
            root = self._active_root
            if root and (
                key == root or key.startswith(root + os.sep) or root.startswith(key + os.sep)
            ):
                self._epoch += 1
                if root in self._pending or len(self._pending) < 64:
                    self._pending[root] = root
                else:
                    self._dropped += 1

    def close(self):
        with self._condition:
            self._stop = True
            self._pending.clear()
            self._condition.notify_all()
        self._thread.join(timeout=5)
        return not self._thread.is_alive()

    def _run(self):
        while True:
            with self._condition:
                while not self._stop and not self._pending:
                    self._condition.wait()
                if self._stop:
                    return
                _, path = self._pending.popitem(last=False)
                self._active = True
                self._active_root = os.path.normcase(os.path.abspath(path))
            try:
                result = self.scan(path)
            except Exception:
                result = {"indexed": 0, "skipped": 0, "incomplete": True}
            with self._condition:
                self._last, self._active = result, False
                self._active_root = ""
                self._condition.notify_all()

    def scan(self, root):
        pending = [(Path(root), 0)]
        indexed = skipped = visited = 0
        incomplete = str(root).startswith("\\\\")
        deadline = time.monotonic() + self.budget
        epoch = self._epoch
        if incomplete:
            return {"indexed": 0, "skipped": 1, "incomplete": True}
        with closing(sqlite3.connect(str(self.database), timeout=1)) as connection, connection:
            while pending and not self._stop:
                if visited >= self.limit or time.monotonic() >= deadline:
                    incomplete = True
                    break
                path, depth = pending.pop()
                visited += 1
                if self.ignored(str(path)):
                    skipped += 1
                    continue
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in {
                        0xA000000C,
                        0xA0000003,
                    }:
                        skipped += 1
                        continue
                    directory = stat.S_ISDIR(info.st_mode)
                    with self._condition:
                        if epoch != self._epoch or self._stop:
                            incomplete = True
                            break
                        connection.execute(
                            "INSERT INTO files(path,name,extension,size,modified,created,is_dir,root_priority,indexed_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET name=excluded.name,extension=excluded.extension,size=excluded.size,modified=excluded.modified,created=excluded.created,is_dir=excluded.is_dir,indexed_at=excluded.indexed_at",
                            (
                                str(path),
                                path.name,
                                "" if directory else path.suffix.lower(),
                                0 if directory else info.st_size,
                                info.st_mtime,
                                info.st_ctime,
                                int(directory),
                                5,
                                time.time(),
                            ),
                        )
                    indexed += 1
                    if directory:
                        if depth >= 32:
                            incomplete = True
                            continue
                        with os.scandir(path) as entries:
                            for entry in entries:
                                if len(pending) + visited >= self.limit:
                                    incomplete = True
                                    break
                                pending.append((Path(entry.path), depth + 1))
                except FileNotFoundError:
                    skipped += 1
                except OSError:
                    skipped += 1
                    incomplete = True
            incomplete |= bool(self._stop)
            with self._condition:
                if epoch != self._epoch or self._stop:
                    connection.rollback()
                    indexed, incomplete = 0, True
        return {"indexed": indexed, "skipped": skipped, "incomplete": incomplete}
