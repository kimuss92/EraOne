"""Bounded history of files opened through the authenticated HUD endpoint."""

import json
import threading
from pathlib import Path

from memory.safe_store import safe_write_json

PATH = Path(__file__).resolve().parents[2] / "runtime/recent_files.json"
_lock = threading.RLock()


def snapshot():
    with _lock:
        try:
            rows = json.loads(PATH.read_text(encoding="utf-8"))
            return (
                [row for row in rows if isinstance(row, str)][:20] if isinstance(rows, list) else []
            )
        except (OSError, ValueError):
            return []


def record(path):
    with _lock:
        value = str(path)
        safe_write_json(PATH, [value, *[row for row in snapshot() if row != value]][:20])
