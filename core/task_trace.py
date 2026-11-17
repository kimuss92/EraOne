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
    if kind == "tool" and fields.get("app") == "era":
        _learn(fields)
        _event(fields)


_TAIL_BYTES = 262144  # enough for hundreds of calls; never read a whole day's file
# Read-only questions: their answer IS the evidence, so "not verified" is noise.
_READ_ACTIONS = frozenset(
    {"status", "list", "read", "get", "report", "recent", "search", "current_track", "info"}
)


def recent_tool_calls(limit: int = 8, max_age_s: float = 1800.0) -> list[dict]:
    """The latest finished Era tool calls, oldest first, from today's trace.

    Whatever the dispatcher did is in here (inline AND detached calls both
    write a `tool` line), so this is the one place that can say what really
    ran. Never raises.
    """
    try:
        path = log_dir() / f"task_trace-{datetime.now():%Y%m%d}.jsonl"
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
        cutoff = datetime.now().timestamp() - max_age_s
        rows = []
        for line in raw.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "tool" or rec.get("app") != "era":
                continue
            try:
                if datetime.fromisoformat(rec["ts"]).timestamp() < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
            rows.append(rec)
        return rows[-max(1, int(limit)) :]
    except Exception:
        return []


def _call_label(tool, args) -> str:
    args = args if isinstance(args, dict) else {}
    action = args.get("action")
    return f"{tool}({action})" if action else str(tool)


def outcome_word(rec: dict) -> str:
    """One honest word for what the runtime can PROVE about a finished call."""
    if not rec.get("ok"):
        return f"FAILED ({rec.get('error_type') or 'error'})"
    if rec.get("verification") == "VERIFIED":
        return "CONFIRMED"
    action = str((rec.get("args") or {}).get("action") or "").lower()
    from core.completion_contract import is_read_only

    if action in _READ_ACTIONS or is_read_only(rec.get("tool"), action):
        return "answered (read-only)"
    return "returned but NOT CONFIRMED"


def render_recent_calls(rows: list[dict]) -> str:
    """Plain text for Era: what was asked, what actually ran, and the proof."""
    if not rows:
        return "No tool calls recorded recently."
    lines = []
    for rec in rows:
        asked = _call_label(rec.get("requested"), rec.get("requested_args"))
        ran = _call_label(rec.get("tool") or rec.get("requested"), rec.get("args"))
        routed = f" -> Era actually ran {ran}" if ran != asked else ""
        said = str(rec.get("user_text") or "").strip()
        said = f' for "{said[:70]}"' if said else ""
        result = str(rec.get("result") or "").strip().splitlines()
        lines.append(
            f"- {str(rec.get('ts', ''))[11:19]} {asked}{routed}{said}: "
            f"{outcome_word(rec)}" + (f" - {result[0][:140]}" if result else "")
        )
    return "\n".join(lines)


def _event(fields: dict) -> None:
    """Era Fabric F0: every finished Era tool call is also a typed `tool.result` event
    (core/fabric/events.py), with its fault classified. Log-only. Never raises."""
    try:
        from core.fabric import events, faults

        tool = str(fields.get("tool") or fields.get("requested") or "unknown")
        component = f"tool.{tool}"
        ok = bool(fields.get("ok"))
        result = str(fields.get("result") or "").strip().splitlines()
        fault = None
        if not ok:
            fault = faults.from_error_type(
                fields.get("error_type"), component, result[0] if result else ""
            )
        events.emit(
            "tool.result",
            component=component,
            outcome="ok" if ok else "fault",
            summary=result[0] if result else "",
            fault=fault,
            provenance={
                "duration_ms": fields.get("duration_ms"),
                "held": bool(fields.get("held")),
                "routed_from": fields.get("requested") if fields.get("tool") else None,
            },
        )
    except Exception:
        pass


def _learn(fields: dict) -> None:
    """Every finished Era tool call is also a lesson candidate (core/task_lessons.py).

    Here rather than at each call site so the inline AND detached paths both
    teach. Only enqueues: the learning itself runs on a background worker, because
    this is called on the live receive loop. Never raises.
    """
    try:
        from core.task_lessons import observe_async

        # The routed tool goes with the routed args. Falling back to the REQUESTED
        # args under the routed tool's name would key the lesson to the wrong call.
        if fields.get("tool"):
            tool, args = fields["tool"], fields.get("args") or {}
        else:
            tool, args = fields.get("requested", ""), fields.get("requested_args") or {}
        observe_async(
            fields.get("user_text", ""),
            tool,
            args,
            bool(fields.get("ok")),
            fields.get("error_type"),
            fields.get("result"),
            fields.get("verification"),
            fields.get("safety"),
        )
    except Exception:
        pass
