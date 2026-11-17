# actions/file_indexer.py
"""
Persistent file system index for Era One instant file finder.

Uses SQLite FTS5 for fast full-text search across user folders.
Keeps index in sync via a watchdog-based file system watcher.
All heavy I/O runs in background threads; search queries are safe
to call from any thread (WAL mode).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.utils import get_config_dir, log

# ── Skip directories (shared with file_controller) ──────────────────────────
SKIP_DIRS: set[str] = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "$Recycle.Bin",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
}

MAX_WALK_DEPTH = 6
_SKIP_CASEFOLD = frozenset(p.casefold() for p in SKIP_DIRS)

# _compute_final_score grants max(0, 10 - 2 * root_priority): 5 means no bonus.
_NO_ROOT_PRIORITY = 5

# ── Watcher debounce interval (seconds) ─────────────────────────────────────
_WATCHER_DEBOUNCE = 2.0


class FileIndex:
    """Persistent FTS5-backed file index for fast search across user folders."""

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            db_path = get_config_dir() / "file_index.db"
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._building = False
        self._closed = False
        self._build_thread: threading.Thread | None = None
        self._observer = None  # watchdog.Observer, lazy init
        self._watcher_started = False
        self._watch_lock = threading.RLock()
        self._watch_starting = False
        self._watch_roots: list[str] = []
        self._watch_failures: list[str] = []
        self._change_listeners: set = set()
        self._roots: list[Path] = []
        self._last_event_time: float = 0.0
        self._event_timer: threading.Timer | None = None
        self._ready = threading.Event()
        self._discovery = None

    # ── Connection management ────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._closed:
            # Never re-open after close(): a build that starts late used to
            # create a fresh connection here that nobody ever closed, leaving
            # the database file locked (WinError 32 on the owner's cleanup).
            # Raising instead lets the worker take its failure path.
            raise RuntimeError("FileIndex is closed")
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                timeout=10,
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn
        if conn is None:
            return
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                extension TEXT DEFAULT '',
                size INTEGER DEFAULT 0,
                modified REAL DEFAULT 0,
                created REAL DEFAULT 0,
                is_dir INTEGER DEFAULT 0,
                root_priority INTEGER DEFAULT 5,
                indexed_at REAL DEFAULT 0
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                name, path,
                content='files',
                content_rowid='id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
            END;
            CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
            END;
            CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
                INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
                INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
            END;

            CREATE TABLE IF NOT EXISTS opened_paths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                last_opened REAL NOT NULL,
                open_count INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()

    def close(self) -> None:
        self._closed = True
        if self._discovery is not None:
            self._discovery.close()
        if self._observer:
            try:
                self._observer.stop()
                if self._observer.ident is not None:
                    self._observer.join(timeout=5.0)
                if self._observer.is_alive():
                    log("FileIndex", "close(): watcher still running; leaving connection open")
                    return
            except Exception:
                if self._observer.is_alive():
                    return
        self._watcher_started = False
        # The build worker shares this connection (check_same_thread=False), so
        # closing it mid-execute is a native access violation, not a Python
        # exception — it once crashed the whole pytest process. Wait for any
        # in-flight build first; only if it refuses to finish do we prefer a
        # leaked handle (the OS reclaims it) over a guaranteed crash.
        t = self._build_thread
        if t is not None and t is not threading.current_thread() and t.is_alive():
            t.join(timeout=30.0)
            if t.is_alive():
                log("FileIndex", "close(): build still running; leaving the connection open")
                return
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ── Index status ─────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        if self._ready.is_set():
            return True
        # Warm-start: a persisted index from a previous run is usable
        # immediately, so searches stay instant right after launch instead of
        # falling back to a multi-second os.walk while a rebuild runs.
        try:
            if self.file_count() > 0:
                self._ready.set()
                return True
        except Exception:
            pass
        return False

    @property
    def is_building(self) -> bool:
        return self._building

    def file_count(self) -> int:
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def last_indexed_at(self) -> float:
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT value FROM config WHERE key='last_indexed_at'").fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0

    # ── Build index ──────────────────────────────────────────────────────────

    def rebuild(self, roots: list[Path] | None = None, wait: bool = False) -> None:
        if self._building:
            return
        if roots is not None:
            self._roots = roots
        elif not self._roots:
            from actions.file_controller import _get_search_roots

            self._roots = _get_search_roots()
        self._building = True
        self._ready.clear()
        t = threading.Thread(target=self._build_worker, daemon=True)
        self._build_thread = t
        t.start()
        if wait:
            t.join()

    def _build_worker(self) -> None:
        try:
            roots = self._roots

            conn = self._get_conn()
            now = time.time()

            # Clear existing index
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM files_fts")
            conn.commit()

            batch: list[tuple] = []
            count = 0

            for root_idx, root in enumerate(roots):
                if not root.exists() or not root.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

                    try:
                        dirpath_path = Path(dirpath)
                    except Exception:
                        continue

                    rel_depth = len(dirpath_path.relative_to(root).parts)
                    if rel_depth >= MAX_WALK_DEPTH:
                        dirnames.clear()

                    len(dirpath_path.parts)

                    # Index directories
                    for dname in dirnames:
                        try:
                            dp = dirpath_path / dname
                            stat = dp.stat()
                            batch.append(
                                (
                                    str(dp),
                                    dname,
                                    "",
                                    0,
                                    stat.st_mtime,
                                    stat.st_ctime,
                                    1,  # is_dir
                                    root_idx,
                                    now,
                                )
                            )
                            count += 1
                            if len(batch) >= 500:
                                self._flush_batch(conn, batch)
                                batch = []
                        except Exception:
                            continue

                    # Index files
                    for fname in filenames:
                        try:
                            fp = dirpath_path / fname
                            stat = fp.stat()
                            ext = fp.suffix.lower()
                            batch.append(
                                (
                                    str(fp),
                                    fname,
                                    ext,
                                    stat.st_size,
                                    stat.st_mtime,
                                    stat.st_ctime,
                                    0,  # is_dir
                                    root_idx,
                                    now,
                                )
                            )
                            count += 1
                            if len(batch) >= 500:
                                self._flush_batch(conn, batch)
                                batch = []
                        except Exception:
                            continue

            if batch:
                self._flush_batch(conn, batch)

            # Record index timestamp
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES ('last_indexed_at', ?)",
                (str(now),),
            )
            conn.commit()
            self._ready.set()
            log("FileIndex", f"Index built: {count} entries from {len(roots)} roots")
        except Exception as e:
            log("FileIndex", f"Index build failed: {e}")
        finally:
            self._building = False

    def _flush_batch(self, conn: sqlite3.Connection, batch: list[tuple]) -> None:
        conn.executemany(
            "INSERT INTO files "
            "(path, name, extension, size, modified, created, is_dir, root_priority, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET name=excluded.name, "
            "extension=excluded.extension, size=excluded.size, modified=excluded.modified, "
            "created=excluded.created, is_dir=excluded.is_dir, "
            "root_priority=MIN(files.root_priority, excluded.root_priority), "
            "indexed_at=excluded.indexed_at",
            batch,
        )
        conn.commit()
        batch.clear()

    # ── Search ───────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        item_type: str = "both",
        extension: str | None = None,
        max_results: int = 20,
        timeout_sec: float = 2.0,
    ) -> dict:
        if not query or not query.strip():
            return {"status": "complete", "candidates": [], "query": query, "message": None}

        if not self.is_ready:
            return {
                "status": "complete",
                "candidates": [],
                "query": query,
                "message": "Index not ready.",
            }

        start = time.time()
        candidates: list[dict] = []
        seen_paths: set[str] = set()

        try:
            conn = self._get_conn()

            # ── Strategy 0: opened_paths instant lookup (frecency boost) ──────
            opened = self._search_opened_paths(conn, query, item_type, extension, limit=10)
            for c in opened:
                if c["path"] not in seen_paths:
                    candidates.append(c)
                    seen_paths.add(c["path"])

            # ── Strategy 1: FTS5 prefix match (fast, high precision) ──────────
            query_clean = query.strip().replace('"', '""')
            fts_candidates = self._search_fts(
                conn, query_clean, item_type, extension, max_results * 3
            )
            for c in fts_candidates:
                if c["path"] not in seen_paths:
                    candidates.append(c)
                    seen_paths.add(c["path"])

            # ── Strategy 2: Fallback LIKE if FTS returned nothing ─────────────
            if len(candidates) < 3:
                like_candidates = self._search_like(
                    conn, query, item_type, extension, max_results * 3
                )
                for c in like_candidates:
                    if c["path"] not in seen_paths:
                        candidates.append(c)
                        seen_paths.add(c["path"])

            # Apply scoring and ranking
            candidates = self._score_and_rank(candidates, query)
            candidates = candidates[:max_results]

            status = "complete" if time.time() - start < timeout_sec else "partial"
            return {
                "status": status,
                "candidates": candidates,
                "query": query,
                "message": None,
            }
        except Exception as e:
            log("FileIndex", f"Search error: {e}")
            return {
                "status": "complete",
                "candidates": [],
                "query": query,
                "message": f"Index search error: {e}",
            }

    def _search_opened_paths(
        self,
        conn: sqlite3.Connection,
        query: str,
        item_type: str,
        extension: str | None,
        limit: int,
    ) -> list[dict]:
        """Instant lookup from opened_paths cache with frecency scoring."""
        try:
            query_lower = query.lower()
            # Match query against the filename portion of each opened path
            sql = """
                SELECT o.path, o.last_opened, o.open_count,
                       f.name, f.extension, f.size, f.modified, f.is_dir, f.root_priority
                FROM opened_paths o
                LEFT JOIN files f ON o.path = f.path
                ORDER BY o.last_opened DESC
                LIMIT ?
            """
            rows = conn.execute(sql, (limit * 5,)).fetchall()

            results: list[dict] = []
            for row in rows:
                path_str = row["path"]
                p = Path(path_str)
                name = row["name"] or p.name
                name_lower = name.lower()

                # Check if query matches the filename
                if query_lower not in name_lower and query_lower != name_lower:
                    # Also try stem match (without extension)
                    stem = p.stem.lower()
                    if query_lower not in stem:
                        continue

                # Apply item_type filter
                is_dir = bool(row["is_dir"]) if row["is_dir"] is not None else p.is_dir()
                if item_type == "file" and is_dir:
                    continue
                if item_type == "folder" and not is_dir:
                    continue

                # Apply extension filter
                ext = row["extension"] or p.suffix.lower()
                if extension and ext != extension.lower():
                    continue

                # Frecency score: blend of open_count and recency
                open_count = row["open_count"] or 1
                last_opened = row["last_opened"] or 0
                now = time.time()
                recency_hours = (now - last_opened) / 3600 if last_opened else 9999

                # Score formula: higher open_count and more recent = higher score
                frecency = min(open_count, 20) * 3  # cap count contribution
                if recency_hours < 1:
                    frecency += 30
                elif recency_hours < 24:
                    frecency += 20
                elif recency_hours < 168:  # 1 week
                    frecency += 10

                modified_str = "unknown"
                if row["modified"]:
                    try:
                        modified_str = time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(row["modified"])
                        )
                    except Exception:
                        pass

                results.append(
                    {
                        "name": name,
                        "path": path_str,
                        "type": "folder" if is_dir else "file",
                        "score": frecency / 100.0,  # normalized
                        "match_type": "opened_cache",
                        "modified": modified_str,
                        "source": p.parent.name if p.parent else "",
                    }
                )

            return results
        except Exception:
            return []

    def _search_fts(
        self,
        conn: sqlite3.Connection,
        query: str,
        item_type: str,
        extension: str | None,
        limit: int,
    ) -> list[dict]:
        try:
            # FTS5 prefix match: "query*"
            fts_query = f'"{query}"*'
            sql = """
                SELECT f.path, f.name, f.extension, f.size, f.modified, f.is_dir,
                       f.root_priority,
                       rank
                FROM files_fts
                JOIN files f ON files_fts.rowid = f.id
                WHERE files_fts MATCH ?
            """
            params: list[Any] = [fts_query]

            if item_type == "file":
                sql += " AND f.is_dir = 0"
            elif item_type == "folder":
                sql += " AND f.is_dir = 1"

            if extension:
                sql += " AND f.extension = ?"
                params.append(extension.lower())

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_candidate(row) for row in rows]
        except sqlite3.OperationalError:
            # FTS match syntax error — return empty, caller will try LIKE
            return []

    def _search_like(
        self,
        conn: sqlite3.Connection,
        query: str,
        item_type: str,
        extension: str | None,
        limit: int,
    ) -> list[dict]:
        try:
            like_pattern = f"%{query}%"
            sql = """
                SELECT path, name, extension, size, modified, is_dir, root_priority
                FROM files
                WHERE name LIKE ?
            """
            params: list[Any] = [like_pattern]

            if item_type == "file":
                sql += " AND is_dir = 0"
            elif item_type == "folder":
                sql += " AND is_dir = 1"

            if extension:
                sql += " AND extension = ?"
                params.append(extension.lower())

            sql += " LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_candidate(row) for row in rows]
        except Exception:
            return []

    def _row_to_candidate(self, row: sqlite3.Row) -> dict:
        path_str = row["path"]
        name = row["name"]
        p = Path(path_str)
        is_dir = bool(row["is_dir"])

        # Compute match type
        match_type, raw_score = self._compute_match_from_db(name, p, is_dir)

        return {
            "name": name,
            "path": path_str,
            "type": "folder" if is_dir else "file",
            "score": raw_score,
            "match_type": match_type,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["modified"]))
            if row["modified"]
            else "unknown",
            "source": p.parent.name if p.parent else "",
        }

    def _compute_match_from_db(self, name: str, p: Path, is_dir: bool) -> tuple[str, float]:
        """Compute match type and raw score from DB row data."""
        # This is a simplified scoring — full scoring happens in _score_and_rank
        return ("contains", 0.7)

    # ── Scoring and ranking ──────────────────────────────────────────────────

    def _score_and_rank(self, candidates: list[dict], query: str) -> list[dict]:
        """Re-score candidates using the same logic as file_controller._compute_match."""
        from actions.file_controller import _compute_final_score, _compute_match

        for c in candidates:
            # opened_cache results already have a good frecency score — keep it
            if c.get("match_type") == "opened_cache":
                # Still run _compute_match to get a proper match_type label
                p = Path(c["path"])
                match_type, raw_score = _compute_match(query, c["name"], p)
                if match_type != "none":
                    c["match_type"] = match_type
                    # Boost: frecency score + base match score
                    c["score"] = max(c["score"], raw_score * 0.8)
                continue

            p = Path(c["path"])
            match_type, raw_score = _compute_match(query, c["name"], p)

            if match_type == "none":
                c["score"] = 0.0
                continue

            # Determine root_priority (0 = highest priority). A path under none of
            # the usual folders gets the LOWEST rank (bonus 0): defaulting to 0
            # made %TEMP% debris outrank the same file inside Documents.
            root_priority = _NO_ROOT_PRIORITY
            try:
                parts = Path(c["path"]).parts
                # Desktop, Downloads, Documents are highest priority
                for part in parts:
                    lower = part.lower()
                    if lower == "desktop":
                        root_priority = 0
                        break
                    elif lower == "downloads":
                        root_priority = 1
                        break
                    elif lower == "documents":
                        root_priority = 2
                        break
            except Exception:
                pass

            # Parse modified time
            modified_time = 0.0
            if c.get("modified") and c["modified"] != "unknown":
                try:
                    modified_time = time.mktime(time.strptime(c["modified"], "%Y-%m-%d %H:%M"))
                except Exception:
                    modified_time = 0.0

            # Path depth
            try:
                path_depth = len(Path(c["path"]).parts)
            except Exception:
                path_depth = 10

            final = _compute_final_score(
                raw_score,
                match_type,
                c["type"],
                "both",
                root_priority,
                modified_time,
                path_depth,
            )
            c["score"] = round(final, 4)
            c["match_type"] = match_type

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates

    # ── File watcher ─────────────────────────────────────────────────────────

    def subscribe_changes(self, listener):
        """Reuse this indexer's native watcher; subscribers must only enqueue."""
        with self._watch_lock:
            self._change_listeners.add(listener)

        def unsubscribe():
            with self._watch_lock:
                self._change_listeners.discard(listener)

        return unsubscribe

    def watcher_status(self) -> dict:
        with self._watch_lock:
            observer = self._observer
            alive = bool(observer and observer.is_alive())
            emitters = tuple(observer.emitters) if observer else ()
            running = alive and bool(emitters) and all(e.is_alive() for e in emitters)
            return {
                "running": running,
                "roots": list(self._watch_roots),
                "failedRoots": list(self._watch_failures),
                "discovery": self._discovery.snapshot()
                if self._discovery
                else {
                    "pending": 0,
                    "active": False,
                    "dropped": 0,
                    "last": {"indexed": 0, "skipped": 0, "incomplete": False},
                },
            }

    def _watch_path_ignored(self, value: str) -> bool:
        path = Path(value)
        if any(part.casefold() in _SKIP_CASEFOLD for part in path.parts):
            return True
        # SQLite writes its own WAL/shm/journal under the watched user tree.
        # Indexing those notifications would write the WAL again indefinitely.
        key = os.path.normcase(os.path.abspath(value))
        database = os.path.normcase(os.path.abspath(self._db_path))
        return key in {database, database + "-wal", database + "-shm", database + "-journal"}

    def _handle_watch_event(self, event) -> None:
        if self._closed:
            return
        kind = event.event_type
        if kind not in {"created", "modified", "deleted", "moved"}:
            return
        if kind == "modified" and event.is_directory:
            return  # Child events carry the actionable change, not synthetic parent noise.
        src = str(event.src_path)
        dest = str(getattr(event, "dest_path", "") or "")
        is_directory = bool(event.is_directory)
        if kind in {"deleted", "moved"} and not is_directory:
            # Windows cannot stat a path after deletion and watchdog reports
            # deleted directories as files. The index preserves its known type.
            try:
                row = (
                    self._get_conn()
                    .execute("SELECT is_dir FROM files WHERE path = ? COLLATE NOCASE", (src,))
                    .fetchone()
                )
                is_directory = bool(row and row["is_dir"])
            except Exception as exc:
                log("FileIndex", f"Deleted-path type lookup failed: {type(exc).__name__}")
        src_ok = bool(src) and not self._watch_path_ignored(src)
        dest_ok = bool(dest) and not self._watch_path_ignored(dest)
        if not src_ok and not dest_ok:
            return
        if kind in {"deleted", "moved"} and self._discovery is not None:
            self._discovery.invalidate(src)
        if kind == "moved" and not src_ok:
            kind, src, dest = "created", dest, ""
        elif kind == "moved" and not dest_ok:
            kind, dest = "deleted", ""
        data = {
            "kind": kind,
            "path": src,
            "destination": dest or None,
            "isDirectory": is_directory,
            "observedAt": time.time(),
        }
        with self._watch_lock:
            listeners = tuple(self._change_listeners)
        for listener in listeners:
            try:
                listener(dict(data))
            except Exception as exc:
                log("FileIndex", f"File event subscriber failed: {type(exc).__name__}")
        if kind == "deleted":
            if is_directory:
                self._incremental_subtree(src)
            else:
                self._incremental_remove(src)
        elif kind == "moved":
            if is_directory:
                self._incremental_subtree(src, dest)
                self._incremental_add(dest)
                self._discover_tree(dest)
            else:
                self._incremental_remove(src)
                self._incremental_add(dest)
        elif kind == "created" and is_directory:
            self._incremental_add(src)
            self._discover_tree(src)
        elif not is_directory:
            if kind == "created":
                self._incremental_add(src)
            else:
                self._incremental_update(src)

    def _discover_tree(self, path: str) -> None:
        from core.subtree_discovery import SubtreeDiscovery

        with self._watch_lock:
            if self._closed:
                return
            if self._discovery is None:
                self._discovery = SubtreeDiscovery(self._db_path, self._watch_path_ignored)
            self._discovery.enqueue(path)

    def start_watcher(self, roots: list[Path] | None = None) -> None:
        with self._watch_lock:
            if self._closed or self._watch_starting or self.watcher_status()["running"]:
                return
            self._watch_starting = True
        try:
            self._start_watcher(roots)
        finally:
            with self._watch_lock:
                self._watch_starting = False

    def _start_watcher(self, roots: list[Path] | None = None) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log("FileIndex", "watchdog not installed — file watcher disabled")
            return

        if roots is None:
            roots = self._roots
        if not roots:
            return
        if self._observer:
            self._observer.stop()
            if self._observer.ident is not None:
                self._observer.join(timeout=5.0)
            if self._observer.is_alive():
                log("FileIndex", "Old watcher did not stop; refusing a duplicate")
                return

        class _IndexEventHandler(FileSystemEventHandler):
            def __init__(self, index: FileIndex):
                self._index = index

            def on_any_event(self, event):
                self._index._handle_watch_event(event)

        try:
            self._observer = Observer()
            handler = _IndexEventHandler(self)
            scheduled, failed = [], []

            for root in roots:
                if root.exists() and root.is_dir():
                    try:
                        self._observer.schedule(handler, str(root), recursive=True)
                        scheduled.append(str(root))
                    except Exception as e:
                        failed.append(str(root))
                        log("FileIndex", f"Watcher failed to schedule {root}: {e}")
                else:
                    failed.append(str(root))

            with self._watch_lock:
                self._watch_roots, self._watch_failures = scheduled, failed
            if not scheduled:
                return

            self._observer.daemon = True
            self._observer.start()
            self._watcher_started = True
            log("FileIndex", f"File watcher started for {len(roots)} roots")
        except Exception as e:
            log("FileIndex", f"Watcher init failed: {e}")

    def _incremental_add(self, path_str: str) -> None:
        """Add a single file/folder to the index."""
        try:
            p = Path(path_str)
            if not p.exists():
                return
            stat = p.stat()
            is_dir = 1 if p.is_dir() else 0
            ext = p.suffix.lower() if not is_dir else ""
            now = time.time()

            conn = self._get_conn()
            conn.execute(
                "INSERT INTO files "
                "(path, name, extension, size, modified, created, is_dir, root_priority, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET name=excluded.name, "
                "extension=excluded.extension, size=excluded.size, modified=excluded.modified, "
                "created=excluded.created, is_dir=excluded.is_dir, indexed_at=excluded.indexed_at",
                (
                    str(p),
                    p.name,
                    ext,
                    stat.st_size if not is_dir else 0,
                    stat.st_mtime,
                    stat.st_ctime,
                    is_dir,
                    5,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            pass

    def _incremental_remove(self, path_str: str) -> None:
        """Remove a single file/folder from the index."""
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM files WHERE path = ?", (path_str,))
            conn.commit()
        except Exception:
            pass

    def _incremental_subtree(self, source: str, destination: str | None = None) -> None:
        """Repair indexed descendants without walking or mutating the filesystem.

        Use a separate connection: commits from other users of the shared
        connection must not split this transaction or interfere with rollback.
        """
        if self._closed:
            return
        src = str(Path(source))
        prefix = src.rstrip(os.sep) + os.sep
        dst = str(Path(destination)) if destination else None
        if dst == src:
            return
        if dst and os.path.normcase(dst).startswith(os.path.normcase(prefix)):
            log("FileIndex", "Subtree repair refused an overlapping destination")
            return
        scope = (src, len(prefix), prefix)
        conn = None
        try:
            with self._lock:
                self._get_conn()  # Ensure the schema exists before opening the writer.
                conn = sqlite3.connect(str(self._db_path), timeout=5)
                # Replacement conflicts must fire FTS delete triggers as well.
                conn.execute("PRAGMA recursive_triggers=ON")
                with conn:
                    if dst is None:
                        conn.execute(
                            "DELETE FROM files WHERE path = ? COLLATE NOCASE "
                            "OR substr(path, 1, ?) = ? COLLATE NOCASE",
                            scope,
                        )
                        conn.execute(
                            "DELETE FROM opened_paths WHERE path = ? COLLATE NOCASE "
                            "OR substr(path, 1, ?) = ? COLLATE NOCASE",
                            scope,
                        )
                    else:
                        conn.execute(
                            "UPDATE OR REPLACE files SET "
                            "path = ? || substr(path, ?), "
                            "name = CASE WHEN path = ? COLLATE NOCASE THEN ? ELSE name END, "
                            "indexed_at = ? WHERE path = ? COLLATE NOCASE "
                            "OR substr(path, 1, ?) = ? COLLATE NOCASE",
                            (dst, len(src) + 1, src, Path(dst).name, time.time(), *scope),
                        )
                        conn.execute(
                            "UPDATE OR REPLACE opened_paths SET path = ? || substr(path, ?) "
                            "WHERE path = ? COLLATE NOCASE "
                            "OR substr(path, 1, ?) = ? COLLATE NOCASE",
                            (dst, len(src) + 1, *scope),
                        )
        except Exception as exc:
            log("FileIndex", f"Subtree index repair failed: {type(exc).__name__}")
        finally:
            if conn is not None:
                conn.close()

    def _incremental_update(self, path_str: str) -> None:
        """Update metadata for a single file (size, mtime)."""
        try:
            p = Path(path_str)
            if not p.exists() or not p.is_file():
                return
            stat = p.stat()
            conn = self._get_conn()
            conn.execute(
                "UPDATE files SET size = ?, modified = ?, indexed_at = ? WHERE path = ?",
                (stat.st_size, stat.st_mtime, time.time(), str(p)),
            )
            conn.commit()
        except Exception:
            pass

    # ── Record opened path ───────────────────────────────────────────────────

    def record_open(self, path: str) -> None:
        try:
            conn = self._get_conn()
            now = time.time()
            conn.execute(
                "INSERT INTO opened_paths (path, last_opened, open_count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(path) DO UPDATE SET "
                "last_opened = ?, open_count = open_count + 1",
                (path, now, now),
            )
            conn.commit()
        except Exception:
            pass

    def recent_opened_paths(self, limit: int = 200) -> list[str]:
        """Most recently opened paths, newest first (anchors for name lookup)."""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT path FROM opened_paths ORDER BY last_opened DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def get_open_count(self, path: str) -> int:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT open_count FROM opened_paths WHERE path = ?", (path,)
            ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0


# ── Singleton ────────────────────────────────────────────────────────────────

_index_instance: FileIndex | None = None
_index_lock = threading.Lock()


def get_file_index() -> FileIndex:
    global _index_instance
    if _index_instance is None:
        with _index_lock:
            if _index_instance is None:
                _index_instance = FileIndex()
    return _index_instance


def init_file_index(
    roots: list[Path] | None = None,
    max_age_sec: float = 6 * 3600.0,
) -> FileIndex:
    """Initialize the file index for instant, robust search.

    - A persisted index from a previous run is served immediately (warm-start),
      so searches never block on a cold os.walk.
    - A background rebuild is triggered only when the index is empty or stale,
      keeping startup cheap while ensuring freshness.
    - The watchdog watcher is started so new/renamed files stay indexed live.
    """
    idx = get_file_index()
    if roots is None:
        from actions.file_controller import _get_search_roots

        roots = _get_search_roots()

    if not idx.is_building:
        try:
            stale = (time.time() - idx.last_indexed_at()) > max_age_sec
        except Exception:
            stale = True
        if idx.file_count() == 0 or stale:
            idx.rebuild(roots=roots)

    try:
        idx.start_watcher(roots=roots)
    except Exception as e:
        log("FileIndex", f"Watcher start skipped: {e}")

    return idx
