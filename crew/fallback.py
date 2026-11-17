"""Crew transport fallback: Hermes refused or its breaker is OPEN -> the in-process loop.

Era Fabric F1 (docs/era-enhancement/ERA_FABRIC_DESIGN.md sections 5.2, 5.3, 8). The
fabric chooses the TRANSPORT only; which model answers inside Hermes stays Hermes'
job ("Era classifies, Hermes routes").

When the in-process loop is used instead:
* `crew.hermes` breaker OPEN -> Hermes is skipped without being called (0 s);
* Hermes refused BEFORE the turn started (TransportDown / MissingCredential from its
  gateway check: unreachable, 401/404, tools enabled, no key) -> the same turn runs
  in-process.

Never instead:
* any other Hermes failure (a model error mid-turn, a bad reply): steps may already
  have run, so the turn is not re-run; the fault still counts against the breaker;
* a terminal fault (Denied / NeedsConfirmation / Cancelled): faults.may_try_alternative
  is False for those, so a gate is never routed around.

Equal-or-stricter permissions (pathway invariant 1): the in-process transport is
handed the SAME BotSpecs the Hermes transport holds, so every capability check and
gate runs exactly as it would have.

Every hop is an event (`crew.step` for the Hermes hop) so the health registry and the
log both see it. ERA_FABRIC=0, or a transport that is not Hermes: a plain dispatch.
This module must not import lane_router / lane_dispatch (see crew/transport.py).
"""

from __future__ import annotations

import threading

from core.logger import log_event
from crew.transport import Transport, Turn

HERMES = "hermes_agent"
COMPONENT = "crew.hermes"
# Refusals raised by HermesAgentTransport._gateway_check before any turn runs.
PREFLIGHT = frozenset({"TransportDown", "MissingCredential"})
NOTE = "(The Hermes gateway was unreachable, so the crew answered on the local loop.)"
_lock = threading.Lock()
_inproc: tuple[int, Transport] | None = None  # (id of the primary it mirrors, transport)


def _logger():
    from crew.runtime import runtime_logger

    return runtime_logger


def _build_inproc(primary: Transport) -> Transport:
    """An in-process transport holding the primary's own BotSpecs."""
    from crew.runtime import InProcessTransport, _default_caches

    t = InProcessTransport(caches=_default_caches())
    for bot_id, spec in dict(getattr(primary, "_bots", {}) or {}).items():
        t.register(bot_id, spec)
    return t


def inproc_for(primary: Transport) -> Transport:
    global _inproc
    with _lock:
        if _inproc is None or _inproc[0] != id(primary):
            _inproc = (id(primary), _build_inproc(primary))
        return _inproc[1]


def reset() -> None:
    global _inproc
    with _lock:
        _inproc = None


def _hop(outcome: str, summary: str, fault=None) -> None:
    try:
        from core.fabric import events

        events.emit(
            "crew.step",
            component="crew.hermes_agent",
            outcome=outcome,
            summary=summary,
            fault=fault,
            provenance={"transport": HERMES},
        )
    except Exception:  # noqa: BLE001
        pass


def _disclose(result, reason: str):
    """Record the fallback on the result and say it in one clause."""
    prov = {"transport": "in_process", "fallback_from": HERMES, "reason": reason}
    try:
        result.data = {**(result.data or {}), "provenance": prov}
        if result.success:
            result.message = f"{result.message or ''}\n{NOTE}".strip()
    except Exception:  # noqa: BLE001
        pass
    log_event(_logger(), "crew.fallback.inproc", "WARNING", reason=reason)
    return result


def dispatch(transport: Transport, bot: str, turn: Turn):
    """Run one crew turn, falling back to in-process when Hermes is down.

    Returns (result, transport_that_answered). Transports never raise from dispatch,
    and nothing here adds a way to."""
    try:
        from core.fabric import events, faults, health
    except Exception:  # noqa: BLE001 -- the fabric is optional
        return transport.dispatch(bot, turn), transport
    if getattr(transport, "name", "") != HERMES or not events.enabled():
        return transport.dispatch(bot, turn), transport
    health.install()
    if not health.allow(COMPONENT):
        _hop("info", "skipped: breaker open")
        fallback = inproc_for(transport)
        return _disclose(fallback.dispatch(bot, turn), "breaker_open"), fallback
    result = transport.dispatch(bot, turn)
    if result.success or str(result.error_type or "") not in PREFLIGHT:
        return result, transport  # run_tool's crew.result event feeds the breaker
    fault = faults.classify(result, COMPONENT)
    _hop("fault", result.error or result.message or "", fault)
    if not faults.may_try_alternative(fault):
        return result, transport
    fallback = inproc_for(transport)
    return _disclose(fallback.dispatch(bot, turn), f"refused:{result.error_type}"), fallback
