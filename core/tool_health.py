"""Tool lifecycle observations; stale work is unknown, never proof of cancellation."""

import os
import threading
import time
import uuid

_lock = threading.RLock()
_tools = {}


def _timeout():
    try:
        return min(3600, max(1, float(os.environ.get("ERA_TOOL_STALE_SECONDS", "240"))))
    except ValueError:
        return 240.0


def begin(tool):
    with _lock:
        if len(_tools) >= 128 and tool not in _tools:
            for key, row in list(_tools.items()):
                if not row["active"]:
                    del _tools[key]
                    break
        row = _tools.setdefault(tool, {"state": "idle", "active": {}, "last_error": "", "stale": 0})
        identity = uuid.uuid4().hex
        row["state"] = "recovery" if row["state"] == "failed" else "active"
        row["active"][identity] = time.monotonic()
        return identity


def finish(tool, identity, success, error=""):
    with _lock:
        row = _tools[tool]
        row["active"].pop(identity, None)
        row["last_error"] = "" if success else str(error)[:80]
        row["state"] = "active" if row["active"] else "idle" if success else "failed"


def snapshot():
    with _lock:
        now, timeout = time.monotonic(), _timeout()
        result = {}
        for tool, row in _tools.items():
            stale = [
                identity for identity, started in row["active"].items() if now - started > timeout
            ]
            for identity in stale:
                del row["active"][identity]
            if stale:
                row["stale"] += len(stale)
                row["last_error"] = "Execution exceeded the observation deadline; outcome unknown"
                row["state"] = "active" if row["active"] else "failed"
            result[tool] = {**row, "active": len(row["active"])}
        return result
