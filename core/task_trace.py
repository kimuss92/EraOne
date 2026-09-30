"""Task trace: one JSON line per step of every task, to read how a task was
carried out and where it failed.

    logs/task_trace-YYYYMMDD.jsonl   (read it with scripts/task_log.py)

Each line has a `kind`:
    tool   one tool call: what the user said, the tool/args asked for, where the
           router sent it, the safety verdict, success, duration, the result.
    turn   (ZORO) one whole turn: the words, the route taken (instant / fast /
           fast-act / agent), every step, what was spoken, timings, outcome.

Added 25/09/2026 after a session where the agent read the wrong browser tab
for a minute and "switched voices" without calling anything -- neither was
visible in any log. Never raises: tracing must not be able to break a task.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

_LOCK = threading.Lock()
_MAX_TEXT = 400  # per field; a trace line must stay readable


def log_dir() -> Path:
    """ZORO_TASK_LOG_DIR, else ERA_LOG_DIR (the test suite redirects it), else logs/."""
    d = os.environ.get("ZORO_TASK_LOG_DIR") or os.environ.get("ERA_LOG_DIR")
    return Path(d) if d else Path(__file__).resolve().parent.parent / "logs"


def _clip(v):
    if isinstance(v, str):
        return v if len(v) <= _MAX_TEXT else v[:_MAX_TEXT] + "..."
    if isinstance(v, dict):
        return {k: _clip(x) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)):
        return [_clip(x) for x in list(v)[:40]]
    return v


def trace(kind: str, **fields) -> None:
    """Append one trace line. Silent on any error."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind}
        rec.update({k: _clip(v) for k, v in fields.items()})
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"task_trace-{datetime.now():%Y%m%d}.jsonl"
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
