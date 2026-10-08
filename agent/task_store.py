"""Local SQLite persistence for agent task state and ordered lifecycle events."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_MAX_TASKS = 5000


def default_task_db_path() -> Path:
    """Return a per-user location outside the synchronized project directory."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "EraOne" / "task_history.sqlite3"


class TaskStore:
    """Small thread-safe event log; task text stays in the user's local profile."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_task_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        if os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                priority INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT NOT NULL DEFAULT '',
                error_type TEXT,
                confirmation_id TEXT
            );
            CREATE INDEX IF NOT EXISTS tasks_updated_idx ON tasks(updated_at DESC);
            CREATE TABLE IF NOT EXISTS task_events (
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                ts REAL NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                error_type TEXT,
                PRIMARY KEY (task_id, seq)
            );
            CREATE INDEX IF NOT EXISTS task_events_ts_idx ON task_events(ts);
            """
        )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TaskStore is closed")

    def _append_event(
        self, task_id: str, status: str, ts: float, message: str = "", error_type: str | None = None
    ) -> None:
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO task_events(task_id, seq, ts, status, message, error_type) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, seq, ts, status, message[:1000], error_type),
        )
        return int(seq)

    def create_task(self, task_id: str, goal: str, priority: int, created_at: float) -> int:
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO tasks(task_id, goal, priority, created_at, updated_at, status) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (task_id, goal, priority, created_at, created_at),
                )
                event_seq = self._append_event(task_id, "pending", created_at, "Task queued")
                self._conn.execute(
                    "DELETE FROM tasks WHERE task_id IN "
                    "(SELECT task_id FROM tasks ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
                    (_MAX_TASKS,),
                )
                self._conn.execute("COMMIT")
                return event_seq
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_task(
        self,
        task_id: str,
        status: str,
        *,
        updated_at: float | None = None,
        result: str | None = None,
        error: str | None = None,
        error_type: str | None = None,
        confirmation_id: str | None = None,
        message: str = "",
    ) -> int:
        ts = time.time() if updated_at is None else updated_at
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Unknown task id: {task_id}")
                self._conn.execute(
                    "UPDATE tasks SET status=?, updated_at=?, "
                    "result=COALESCE(?, result), "
                    "error=CASE WHEN ? IN ('pending', 'running', 'completed') THEN '' ELSE COALESCE(?, error) END, "
                    "error_type=CASE WHEN ? IN ('pending', 'running', 'completed') THEN NULL ELSE COALESCE(?, error_type) END, "
                    "confirmation_id=CASE WHEN ?='awaiting_approval' THEN COALESCE(?, confirmation_id) ELSE NULL END "
                    "WHERE task_id=?",
                    (
                        status,
                        ts,
                        result,
                        status,
                        error,
                        status,
                        error_type,
                        status,
                        confirmation_id,
                        task_id,
                    ),
                )
                event_seq = self._append_event(task_id, status, ts, message or result or error or status, error_type)
                self._conn.execute("COMMIT")
                return event_seq
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_events(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT seq, task_id, ts, status, message, error_type FROM "
                "(SELECT seq, task_id, ts, status, message, error_type FROM task_events "
                "WHERE task_id=? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                (task_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT e.seq, e.task_id, e.ts, e.status, e.message, e.error_type, t.goal "
                "FROM task_events AS e JOIN tasks AS t USING(task_id) "
                "ORDER BY e.ts DESC, e.task_id, e.seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def recover_after_restart(self) -> list[dict[str, Any]]:
        """Return queued-but-unstarted work; never silently replay unknown side effects."""
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                interrupted = self._conn.execute(
                    "SELECT task_id, status FROM tasks WHERE status IN ('running', 'awaiting_approval')"
                ).fetchall()
                for row in interrupted:
                    is_approval = row["status"] == "awaiting_approval"
                    kind = "ApprovalLostOnRestart" if is_approval else "InterruptedByRestart"
                    message = (
                        "The pending approval expired when Era One restarted; submit the action again for review."
                        if is_approval
                        else "Era One restarted while this task was running. It was not replayed to avoid repeating side effects."
                    )
                    self._conn.execute(
                        "UPDATE tasks SET status='failed', updated_at=?, error=?, error_type=?, confirmation_id=NULL "
                        "WHERE task_id=?",
                        (now, message, kind, row["task_id"]),
                    )
                    self._append_event(row["task_id"], "failed", now, message, kind)
                pending = self._conn.execute(
                    "SELECT * FROM tasks WHERE status='pending' ORDER BY priority, created_at"
                ).fetchall()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return [dict(row) for row in pending]

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
