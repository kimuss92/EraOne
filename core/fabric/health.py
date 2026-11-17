"""One health registry with a circuit breaker per component (Era Fabric F1).

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md section 5.2. Fed by signals Era
already produces, no new probes:

* the F0 event log: every `tool.result`, and `crew.step` / `crew.result`
  (subscribed through core/fabric/events.py by `install()`);
* direct `record()` calls at the few places that already know a component's state:
  Gemini quota latches, Live upstream errors, the TTS tiers, the vault lock, the
  OmniRoute gateway.

Per component: CLOSED -> OPEN after `faults` health faults (Unavailable / Timeout /
Exhausted / EmptyResult, see faults.BREAKER_KINDS) inside `window_s`. OPEN is skipped
instantly by `allow()`: no timeout is paid. After `cooldown_s` (Exhausted: the quota's
own reset time when it is known) ONE caller is let through as the probe (HALF_OPEN);
its success closes the breaker, its health fault re-opens it. Denied / NotFound /
BadInput / NeedsConfirmation / Cancelled never trip a breaker: they are answers, not
health problems.

Contract:
* nothing here raises; nothing does I/O except emitting `health.*` transition events;
* `allow()` is the only thing a caller consults to skip a component, and in F1 only
  the crew transport and the OmniRoute gateway consult it (both already had a
  fallback). Everything else is observed and shown, not skipped;
* ERA_FABRIC=0 makes `allow()` always True and `record()` a no-op (today's behaviour);
* thresholds (3 faults / 2 min, 60 s cool-down, 120 s for OmniRoute) are the
  section-12 proposal, confirmed by Karim on 2026-11-10; they live in POLICY.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from core.fabric import events, faults

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

# Section 12 decision 3, confirmed by Karim 2026-11-10.
DEFAULT_POLICY = {"faults": 3, "window_s": 120.0, "cooldown_s": 60.0}
POLICY: dict[str, dict] = {
    "gateway.omniroute": {"cooldown_s": 120.0},  # covers its 16-80 s cold start
}
# Event components -> registry names (the crew transports name themselves).
ALIASES = {"crew.hermes_agent": "crew.hermes", "crew.in_process": "crew.inproc"}
# Shown first, in this order, when present; the rest follow alphabetically.
_ORDER = ("live.gemini", "crew.hermes", "crew.inproc", "gateway.omniroute", "llm.gemini")
_LABELS = {
    "live.gemini": "voice (Gemini Live)",
    "crew.hermes": "the crew's Hermes gateway",
    "crew.inproc": "the crew's local loop",
    "gateway.omniroute": "the OmniRoute gateway",
    "llm.gemini": "Gemini text models",
    "tts.fish": "the Fish voice",
    "tts.kokoro": "the Kokoro voice",
    "vault": "the vault",
}
_TOPIC = {CLOSED: "health.closed", OPEN: "health.open", HALF_OPEN: "health.half_open"}
_MAX_COMPONENTS = 200
_lock = threading.RLock()
_clock = time.monotonic  # tests replace it
_installed: list = []  # unsubscribe functions


@dataclass
class _Breaker:
    component: str
    state: str = CLOSED
    window: deque = field(default_factory=deque)  # monotonic ts of recent health faults
    open_until: float = 0.0
    probe_at: float = 0.0  # monotonic ts the HALF_OPEN probe was granted (0 = none)
    last_fault: dict | None = None
    last_fault_wall: float = 0.0
    last_ok_wall: float = 0.0
    oks: int = 0
    fault_count: int = 0
    opened: int = 0


_breakers: dict[str, _Breaker] = {}


def _policy(component: str) -> dict:
    return {**DEFAULT_POLICY, **POLICY.get(component, {})}


def canonical(component: str) -> str:
    name = str(component or "unknown").strip()[:60]
    return ALIASES.get(name, name)


def _get(component: str) -> _Breaker:
    b = _breakers.get(component)
    if b is None:
        if len(_breakers) >= _MAX_COMPONENTS:  # drop the quietest closed one
            for key, old in list(_breakers.items()):
                if old.state == CLOSED and not old.window:
                    del _breakers[key]
                    break
        b = _breakers[component] = _Breaker(component)
    return b


def _trim(b: _Breaker, now: float, window_s: float) -> None:
    while b.window and now - b.window[0] > window_s:
        b.window.popleft()


def _transition(b: _Breaker, new_state: str, summary: str) -> tuple:
    b.state = new_state
    return _TOPIC[new_state], b.component, summary, b.last_fault


def _announce(change: tuple | None) -> None:
    """Emit a transition outside the lock: subscribers run synchronously."""
    if not change:
        return
    topic, component, summary, fault = change
    is_open = topic == "health.open"
    events.emit(
        topic,
        component=component,
        outcome="fault" if is_open else "info",
        summary=summary,
        fault=fault if is_open else None,
    )


# ------------------------------------------------------------------ inputs


def record(component: str, fault: dict | None = None, *, reset_s: float | None = None) -> str:
    """One observation: `fault=None` is a success. Returns the state after it.

    `reset_s`: for an Exhausted fault, when the quota resets (the cool-down then
    lasts that long instead of the policy's)."""
    if not events.enabled():
        return CLOSED
    change = None
    try:
        component = canonical(component)
        pol = _policy(component)
        with _lock:
            now, wall = _clock(), time.time()
            b = _get(component)
            if fault is None:
                b.oks += 1
                b.last_ok_wall = wall
                if b.state != CLOSED:
                    b.window.clear()
                    b.probe_at = 0.0
                    change = _transition(b, CLOSED, "working again")
                return b.state
            kind = fault.get("kind") if isinstance(fault, dict) else None
            b.fault_count += 1
            b.last_fault = dict(fault) if isinstance(fault, dict) else None
            b.last_fault_wall = wall
            if kind not in faults.BREAKER_KINDS:
                return b.state  # a gate or a caller error says nothing about health
            _trim(b, now, pol["window_s"])
            b.window.append(now)
            if b.state == OPEN:
                return b.state
            if b.state == HALF_OPEN or len(b.window) >= int(pol["faults"]):
                cool = float(reset_s) if kind == "Exhausted" and reset_s else pol["cooldown_s"]
                b.open_until = now + max(1.0, cool)
                b.probe_at = 0.0
                b.opened += 1
                detail = str(fault.get("detail") or kind)[:120]
                change = _transition(b, OPEN, f"{kind}: {detail}")
            return b.state
    except Exception:  # noqa: BLE001 -- health is evidence, never a failure source
        return CLOSED
    finally:
        _announce(change)


def note(component: str, kind: str | None = None, detail="", *, reset_s=None) -> None:
    """The one-liner for existing code: `note("tts.fish")` is a success,
    `note("tts.fish", "EmptyResult", "no audio")` a fault. Never raises."""
    try:
        record(component, faults.make(kind, component, detail) if kind else None, reset_s=reset_s)
    except Exception:  # noqa: BLE001
        pass


def record_result(component: str, result) -> str:
    """Record a ToolResult (its fault, or a success)."""
    return record(component, faults.classify(result, canonical(component)))


def _on_event(event) -> None:
    if event.outcome == "ok":
        record(event.component)
    elif event.outcome == "fault" and isinstance(event.fault, dict):
        record(event.component, event.fault)


def install() -> bool:
    """Subscribe the registry to the F0 events. Idempotent; never raises."""
    try:
        with _lock:
            if _installed:
                return True
            for pattern in ("tool.result", "crew.step", "crew.result"):
                _installed.append(events.subscribe(pattern, _on_event))
        return True
    except Exception:  # noqa: BLE001
        return False


def uninstall() -> None:
    with _lock:
        offs = list(_installed)
        _installed.clear()
    for off in offs:
        try:
            off()
        except Exception:  # noqa: BLE001
            pass


def reset() -> None:
    """Forget every breaker (tests, and a config reload)."""
    with _lock:
        _breakers.clear()


# ------------------------------------------------------------------ queries


def allow(component: str) -> bool:
    """May a caller use `component` now? False only while its breaker is OPEN, or
    while another caller holds the HALF_OPEN probe. Never raises."""
    if not events.enabled():
        return True
    change = None
    try:
        component = canonical(component)
        pol = _policy(component)
        with _lock:
            b = _breakers.get(component)
            if b is None or b.state == CLOSED:
                return True
            now = _clock()
            if b.state == OPEN:
                if now < b.open_until:
                    return False
                b.probe_at = now
                change = _transition(b, HALF_OPEN, "cool-down over; probing once")
                return True
            # HALF_OPEN: one probe at a time; a probe that never reported is retried.
            if b.probe_at and now - b.probe_at < pol["cooldown_s"]:
                return False
            b.probe_at = now
            return True
    except Exception:  # noqa: BLE001
        return True
    finally:
        _announce(change)


def state(component: str) -> str:
    """CLOSED / OPEN / HALF_OPEN. An OPEN breaker past its cool-down reads HALF_OPEN
    (the next call will probe) without granting the probe."""
    with _lock:
        b = _breakers.get(canonical(component))
        if b is None:
            return CLOSED
        if b.state == OPEN and _clock() >= b.open_until:
            return HALF_OPEN
        return b.state


def snapshot() -> list[dict]:
    """Every known component, the unhealthy ones first. JSON-safe, for the HUD."""
    rows = []
    with _lock:
        now = _clock()
        for name, b in _breakers.items():
            pol = _policy(name)
            _trim(b, now, pol["window_s"])
            st = HALF_OPEN if b.state == OPEN and now >= b.open_until else b.state
            last = b.last_fault or {}
            rows.append(
                {
                    "component": name,
                    "label": _LABELS.get(name, name),
                    "state": st,
                    "recent_faults": len(b.window),
                    "threshold": int(pol["faults"]),
                    "retry_in_s": max(0, round(b.open_until - now)) if st == OPEN else 0,
                    "last_fault_kind": last.get("kind"),
                    "last_fault": str(last.get("detail") or "")[:160],
                    "last_fault_at": b.last_fault_wall or None,
                    "last_ok_at": b.last_ok_wall or None,
                    "oks": b.oks,
                    "faults": b.fault_count,
                    "times_opened": b.opened,
                }
            )
    rank = {OPEN: 0, HALF_OPEN: 1, CLOSED: 2}
    order = {name: i for i, name in enumerate(_ORDER)}
    rows.sort(key=lambda r: (rank[r["state"]], order.get(r["component"], 99), r["component"]))
    return rows


def unhealthy() -> list[dict]:
    return [r for r in snapshot() if r["state"] != CLOSED]


def self_model_note() -> str:
    """One paragraph for capability_check: what is down right now, or "" when all is
    well. The model reads it, so it says what to tell the user."""
    try:
        bad = unhealthy()
    except Exception:  # noqa: BLE001
        return ""
    if not bad:
        return ""
    parts = []
    for r in bad[:6]:
        if r["state"] == OPEN:
            why = f" ({r['last_fault_kind']})" if r["last_fault_kind"] else ""
            parts.append(f"{r['label']} is DOWN right now{why}; retrying in {r['retry_in_s']}s")
        else:
            parts.append(f"{r['label']} was down and is being re-tested")
    crew_note = ""
    if any(r["component"] == "crew.hermes" for r in bad):
        crew_note = " Crew asks still work: they run on the local loop instead of Hermes."
    return (
        "LIVE HEALTH (measured, not assumed): "
        + "; ".join(parts)
        + "."
        + crew_note
        + " Mention this only if it affects what the user asked for, in one short clause."
    )
