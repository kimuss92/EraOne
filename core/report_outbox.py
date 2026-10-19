"""A durable queue of reports Era owes the owner.

THE BUG THIS EXISTS FOR
-----------------------
A background job (an autonomous-development run) finished, verified, and sat in
"awaiting your approval" while Era said nothing: the worker thread only saved a
file, and the one in-memory queue that does exist (_pending_cm_announcement)
is lost on a relaunch and was never fed by development at all.

Rules, so nothing is "passed or forgotten":
- A report is written to disk the moment the work ends, success or failure.
- It is marked delivered ONLY after the pump has spoken it into a live session
  (app/era_live/report_pump.py). A dropped session, a standby, a relaunch: the
  report stays here and is spoken on the next live connection.
- Keys make enqueueing idempotent, so reconciling jobs that finished while Era
  was down can never repeat a report.

Degrade, never raise: producers run on worker threads and must not die here.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "runtime" / "report_outbox.json"
_lock = threading.RLock()
_KEEP_DELIVERED_S = 7 * 86400  # remembered for de-duplication, then pruned
_MAX_ITEMS = 300


def _load() -> list[dict]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("key") and i.get("text")]


def _save(items: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="outbox-", suffix=".tmp", dir=PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"items": items}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, PATH)
    finally:
        Path(name).unlink(missing_ok=True)


def enqueue(key: str, text: str, source: str = "") -> bool:
    """Queue a report once per `key`. True when it was new (and written)."""
    text = " ".join(str(text or "").split())
    if not key or not text:
        return False
    try:
        with _lock:
            items = _load()
            if any(i["key"] == key for i in items):
                return False
            items.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "key": str(key),
                    "source": str(source),
                    "text": text,
                    "created": time.time(),
                    "delivered": None,
                }
            )
            _save(_prune(items))
        return True
    except Exception as exc:  # noqa: BLE001 - never raise into a worker thread
        print(f"[REPORT] outbox write failed: {type(exc).__name__}: {exc}")
        return False


def _prune(items: list[dict]) -> list[dict]:
    cutoff = time.time() - _KEEP_DELIVERED_S
    kept = [i for i in items if not i.get("delivered") or i["delivered"] > cutoff]
    return kept[-_MAX_ITEMS:]


def pending() -> list[dict]:
    """Undelivered reports, oldest first."""
    with _lock:
        return sorted(
            (i for i in _load() if not i.get("delivered")), key=lambda i: i.get("created", 0)
        )


def mark_undelivered(item_id: str) -> None:
    """Put a report back: the session dropped right after it was spoken."""
    try:
        with _lock:
            items = _load()
            for item in items:
                if item.get("id") == item_id:
                    item["delivered"] = None
            _save(items)
    except Exception as exc:  # noqa: BLE001
        print(f"[REPORT] outbox update failed: {type(exc).__name__}: {exc}")


def mark_delivered(item_id: str) -> None:
    try:
        with _lock:
            items = _load()
            for item in items:
                if item.get("id") == item_id:
                    item["delivered"] = time.time()
            _save(items)
    except Exception as exc:  # noqa: BLE001
        print(f"[REPORT] outbox update failed: {type(exc).__name__}: {exc}")
