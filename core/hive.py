"""core/hive.py — the shared brain of the Protocol agents.

Protocol Zero (prompt-sync auditor) and Protocol One (health guardian), plus the
background spies that feed them, never talk to each other directly. They read
and write ONE small repository under runtime/hive/, so whatever one of them
learns the others — and Era, for your briefing — see on their next read:

  status.json    agent -> {state, detail, updated}. "What is everyone doing?"
  insights.json  signature -> one finding. Deduplicated: a spy that sees the
                 same syntax error on every tick bumps `count`/`last_seen`
                 instead of minting a new insight each time.
  messages.json  bounded agent-to-agent mailbox (hand-offs, "I fixed X").

Rules this file keeps, and why:
  * Every write is atomic (temp file + os.replace) under a cross-process lock
    file. The spies run inside Era, a Protocol cycle may run in a worker thread,
    and Protocol Zero may run from a different process entirely — a torn JSON
    file would silently erase the whole map.
  * Nothing here raises into a caller. The hive is bookkeeping; a full disk
    must never turn a successful fix into a crashed Era.
  * An insight is only ever marked `resolved` by whoever re-checked the
    condition. Claiming "fixed" without a re-check is exactly the fake
    information the old diagnosis produced.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

SEVERITIES = ("critical", "high", "medium", "low", "info")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# "scheduler" is core/task_scheduler.py: it posts every run of the user's scheduled
# tasks here, plus an insight for each failure and each run awaiting approval.
AGENTS = ("protocol_zero", "protocol_one", "spies", "scheduler")

_MAX_INSIGHTS = 400
_MAX_MESSAGES = 200
_LOCK_TIMEOUT_S = 5.0
_STALE_LOCK_S = 30.0

_thread_lock = threading.RLock()


def hive_dir() -> Path:
    """Overridable so tests never write into the real runtime/hive."""
    override = os.environ.get("ERA_HIVE_DIR", "").strip()
    return Path(override) if override else _REPO_ROOT / "runtime" / "hive"


def severity_rank(severity: str) -> int:
    return _RANK.get(str(severity).lower(), len(SEVERITIES))


# ── storage primitives ───────────────────────────────────────────────────────
@contextmanager
def _locked():
    """Thread lock + a lock FILE so a second process waits its turn too."""
    with _thread_lock:
        d = hive_dir()
        d.mkdir(parents=True, exist_ok=True)
        lock = d / ".lock"
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        fd = None
        while fd is None:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # A crashed writer leaves the lock behind; age breaks the tie.
                try:
                    if time.time() - lock.stat().st_mtime > _STALE_LOCK_S:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    break  # proceed unlocked rather than hang Era
                time.sleep(0.05)
        try:
            yield d
        finally:
            if fd is not None:
                os.close(fd)
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── agent status ─────────────────────────────────────────────────────────────
def set_status(agent: str, state: str, detail: str = "", **extra) -> None:
    try:
        with _locked() as d:
            data = _read(d / "status.json", {})
            data[agent] = {"state": state, "detail": detail[:500], "updated": _now(), **extra}
            _write(d / "status.json", data)
    except Exception:
        pass


def get_status() -> dict:
    data = _read(hive_dir() / "status.json", {})
    for agent in AGENTS:
        data.setdefault(agent, {"state": "not_running", "detail": "", "updated": ""})
    return data


# ── insights ─────────────────────────────────────────────────────────────────
def post_insight(
    source: str,
    signature: str,
    *,
    severity: str,
    category: str,
    title: str,
    evidence: str = "",
    location: str = "",
) -> bool:
    """Record (or refresh) one finding. Returns True when it is NEW or it had
    been resolved and came back — the caller's cue that it is worth announcing."""
    try:
        with _locked() as d:
            data = _read(d / "insights.json", {})
            prev = data.get(signature)
            fresh = prev is None or prev.get("status") != "open"
            entry = {
                "signature": signature,
                "source": source,
                "severity": severity if severity in _RANK else "medium",
                "category": category,
                "title": title[:300],
                "evidence": evidence[:1500],
                "location": location[:300],
                "first_seen": _now() if fresh else prev.get("first_seen", _now()),
                "last_seen": _now(),
                "count": 1 if fresh else int(prev.get("count", 1)) + 1,
                "status": "open",
            }
            data[signature] = entry
            _write(d / "insights.json", _prune(data))
            return fresh
    except Exception:
        return False


def resolve_insight(signature: str, by: str, note: str = "") -> bool:
    """Mark resolved. Only call this after RE-CHECKING the condition."""
    try:
        with _locked() as d:
            data = _read(d / "insights.json", {})
            entry = data.get(signature)
            if not entry or entry.get("status") != "open":
                return False
            entry.update(status="resolved", resolved_by=by, resolved_at=_now(), note=note[:300])
            _write(d / "insights.json", data)
            return True
    except Exception:
        return False


def open_insights(source: str | None = None, min_severity: str = "info") -> list[dict]:
    limit = severity_rank(min_severity)
    items = [
        e
        for e in _read(hive_dir() / "insights.json", {}).values()
        if e.get("status") == "open"
        and severity_rank(e.get("severity", "")) <= limit
        and (source is None or e.get("source") == source)
    ]
    return sorted(items, key=lambda e: (severity_rank(e["severity"]), e.get("first_seen", "")))


def _prune(data: dict) -> dict:
    if len(data) <= _MAX_INSIGHTS:
        return data
    # Drop resolved ones first (oldest), never an open finding while any remain.
    ordered = sorted(
        data.values(), key=lambda e: (e.get("status") == "open", e.get("last_seen", ""))
    )
    keep = ordered[len(data) - _MAX_INSIGHTS :]
    return {e["signature"]: e for e in keep}


# ── agent-to-agent mailbox ───────────────────────────────────────────────────
def send_message(sender: str, to: str, kind: str, payload: dict | str) -> None:
    """to='*' broadcasts. Kinds in use: cycle_report, fix_applied, needs_review."""
    try:
        with _locked() as d:
            msgs = _read(d / "messages.json", [])
            msgs.append(
                {
                    "ts": time.time(),
                    "at": _now(),
                    "from": sender,
                    "to": to,
                    "kind": kind,
                    "payload": payload,
                }
            )
            _write(d / "messages.json", msgs[-_MAX_MESSAGES:])
    except Exception:
        pass


def inbox(agent: str, since_ts: float = 0.0) -> list[dict]:
    return [
        m
        for m in _read(hive_dir() / "messages.json", [])
        if m.get("ts", 0) > since_ts and m.get("to") in (agent, "*") and m.get("from") != agent
    ]


# ── briefing for Era ─────────────────────────────────────────────────────────
def briefing(max_items: int = 5) -> str:
    """A short, spoken-friendly summary of the hive for Era to relay."""
    status = get_status()
    items = open_insights(min_severity="medium")
    counts = {s: 0 for s in SEVERITIES}
    for e in items:
        counts[e["severity"]] += 1

    parts = []
    for agent in AGENTS:
        st = status[agent]
        label = agent.replace("_", " ").title()
        when = f" (as of {st['updated']})" if st.get("updated") else ""
        # Short details only: the scheduler's is a one-line tally, and a long
        # cycle report does not belong in a spoken status line.
        detail = st.get("detail") or ""
        detail = f" — {detail}" if detail and len(detail) <= 120 else ""
        parts.append(f"{label}: {st['state']}{detail}{when}.")
    if not items:
        parts.append("No open issues of medium severity or above on the map.")
    else:
        summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
        parts.append(f"Open issues: {summary}.")
        for e in items[:max_items]:
            where = f" in {e['location']}" if e.get("location") else ""
            parts.append(f"[{e['severity']}] {e['title']}{where}.")
    return " ".join(parts)
