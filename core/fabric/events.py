"""Typed events, persisted before anyone hears them (Era Fabric F0, log-only).

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md section 6. One line per event in
<log_dir>/fabric/events-YYYYMMDD.jsonl, written BEFORE fan-out so a crash never
loses the record. In F0 nothing subscribes in production: the bus only records.
F1 (health) and F4 (outbox, HUD, self_heal, lessons) attach subscribers later.

Contract:
* topics are a CLOSED set (TOPICS); an unknown topic is refused, never guessed;
* summary and fault detail are redacted at emit time: the log never holds a secret;
* emit never raises, from any thread; a subscriber that throws is isolated, the
  others still receive the event;
* ERA_FABRIC=0 turns the whole bus off (emit returns None, nothing is written).
"""

from __future__ import annotations

import contextlib
import contextvars
import fnmatch
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

TOPICS = frozenset(
    {
        "task.created",
        "task.step",
        "task.done",
        "tool.start",
        "tool.result",
        "crew.asked",
        "crew.step",
        "crew.result",
        "dev.proposed",
        "dev.verified",
        "dev.failed",
        "dev.activated",
        "dev.rolled_back",
        "dev.rejected",
        "dev.cancelled",
        "health.open",
        "health.half_open",
        "health.closed",
        "report.owed",
        "report.delivered",
        "report.requeued",
    }
)
OUTCOMES = frozenset({"ok", "fault", "progress", "info"})
_SUMMARY_MAX = 200
_lock = threading.Lock()
_subscribers: list[tuple[str, Callable]] = []
_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar("fabric_ctx", default=None)
refused = 0  # emits refused for a bad topic/outcome: a programming error worth seeing


@dataclass(frozen=True)
class Event:
    topic: str
    ctx_id: str | None
    component: str
    outcome: str  # ok | fault | progress | info
    summary: str  # one human line, <= 200 chars, already redacted
    fault: dict | None = None
    provenance: dict = field(default_factory=dict)  # provider, model, fallback_from, verified
    ts: float = field(default_factory=time.time)


def enabled() -> bool:
    return os.environ.get("ERA_FABRIC", "1").strip().lower() not in ("0", "false", "off", "no")


# -------------------------------------------------------------------- context id


def current_ctx() -> str | None:
    return _CTX.get()


@contextlib.contextmanager
def bind_ctx(ctx_id: str | None) -> Iterator[None]:
    """Events emitted inside this block (same thread / task) carry `ctx_id`."""
    token = _CTX.set(ctx_id)
    try:
        yield
    finally:
        _CTX.reset(token)


# ----------------------------------------------------------------------- helpers


def _redact(text) -> str:
    try:
        from core.logger import redact_secrets

        text = redact_secrets(str(text or ""))
        home = str(Path.home())
        if home:
            text = text.replace(home, "~")
        return " ".join(text.split())
    except Exception:  # noqa: BLE001
        return ""


def _path(ts: float | None = None) -> Path:
    from core.task_trace import log_dir

    day = time.strftime("%Y%m%d", time.localtime(ts or time.time()))
    return log_dir() / "fabric" / f"events-{day}.jsonl"


def _persist(event: Event) -> None:
    try:
        path = _path(event.ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(event), ensure_ascii=False, default=str)
        with _lock, path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 -- the log is evidence, never a failure source
        pass


# -------------------------------------------------------------------- the bus


def emit(
    topic: str,
    *,
    component: str,
    outcome: str,
    summary: str = "",
    ctx_id: str | None = None,
    fault: dict | None = None,
    provenance: dict | None = None,
) -> Event | None:
    """Record one event, then hand it to subscribers. Returns it, or None when the
    bus is off or the topic/outcome is not in the closed set. Never raises."""
    global refused
    try:
        if not enabled():
            return None
        if topic not in TOPICS or outcome not in OUTCOMES:
            refused += 1
            return None
        if fault is not None:
            fault = {**fault, "detail": _redact(fault.get("detail", ""))[:300]}
        event = Event(
            topic=topic,
            ctx_id=ctx_id or current_ctx(),
            component=str(component or "unknown")[:60],
            outcome=outcome,
            summary=_redact(summary)[:_SUMMARY_MAX],
            fault=fault,
            provenance=dict(provenance or {}),
        )
    except Exception:  # noqa: BLE001
        return None
    _persist(event)
    _fan_out(event)
    return event


def _fan_out(event: Event) -> None:
    with _lock:
        targets = [fn for pattern, fn in _subscribers if fnmatch.fnmatchcase(event.topic, pattern)]
    for fn in targets:
        try:
            fn(event)
        except Exception as exc:  # noqa: BLE001 -- one bad subscriber never starves the rest
            try:
                from core.logger import log_event, main_logger

                log_event(
                    main_logger,
                    "fabric.subscriber.failed",
                    "WARNING",
                    topic=event.topic,
                    subscriber=getattr(fn, "__qualname__", repr(fn))[:80],
                    error=repr(exc)[:200],
                )
            except Exception:  # noqa: BLE001
                pass


def subscribe(pattern: str, fn: Callable[[Event], None]) -> Callable[[], None]:
    """Call `fn(event)` for every topic matching the glob `pattern` ("crew.*", "*").
    Runs on the emitting thread; keep it short. Returns an unsubscribe function."""
    entry = (str(pattern), fn)
    with _lock:
        _subscribers.append(entry)

    def _off() -> None:
        unsubscribe(entry)

    return _off


def unsubscribe(entry) -> None:
    with _lock:
        if entry in _subscribers:
            _subscribers.remove(entry)


def read(
    ctx_id: str | None = None, topic: str | None = None, days: int = 1, root: Path | None = None
) -> list[dict]:
    """Events from the jsonl, oldest first, optionally for one ctx / topic glob."""
    out: list[dict] = []
    now = time.time()
    for back in range(days - 1, -1, -1):
        path = _path(now - back * 86400)
        if root is not None:
            path = Path(root) / "fabric" / path.name
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if ctx_id and rec.get("ctx_id") != ctx_id:
                continue
            if topic and not fnmatch.fnmatchcase(str(rec.get("topic", "")), topic):
                continue
            out.append(rec)
    return out
