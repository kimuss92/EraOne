"""The bulkhead around every adapter that crosses a component line (Era Fabric F0).

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md section 5.1.

    @boundary("crew.hermes", fail_mode="open", timeout_s=90, long=True)
    async def ask(turn) -> ToolResult: ...

Guarantees, for sync and async functions alike:
* no exception escapes: it becomes a failed ToolResult whose data["fault"] is typed
  (core/fabric/faults.py). KeyboardInterrupt/SystemExit and an asyncio task's own
  cancellation are the exceptions to the rule -- swallowing those would break the
  process and asyncio themselves; they are recorded, then re-raised;
* an Internal fault (our bug) is written with its frames via core.logger.log_crash,
  the record core/self_heal already reads;
* one result event per call (core/fabric/events.py), with duration and outcome;
* fail_mode has NO default: every adapter decides on purpose whether it fails
  "closed" (stop and tell the user: a fault never offers an alternative) or "open"
  (degrade: the fault says whether another provider may be tried);
* long=True (async only) is "ack now, run in the background, result event later":
  the call returns at once with "Started", the real outcome arrives as an event;
* ERA_FABRIC=0 turns the boundary into a plain call (today's behaviour).

Limits, stated rather than hidden: a SYNC function cannot be pre-empted, so for it
`timeout_s` is only measured (provenance "overran"); blocking work belongs in
core.cancel_registry.offload. The TaskContext deadline arrives with F1/F3.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any

from core.fabric import events, faults

FAIL_MODES = ("open", "closed")
_background: set[asyncio.Task] = set()  # strong refs: a bare task can be GC'd mid-run


def _topics(component: str) -> tuple[str, str]:
    if component.split(".", 1)[0] == "crew":
        return "crew.asked", "crew.result"
    return "tool.start", "tool.result"


def _logger():
    from core.logger import setup_logger

    return setup_logger("era_one.fabric")


def _fault_for(exc: BaseException, component: str, fail_mode: str) -> dict:
    fault = faults.from_exception(exc, component)
    if fault["kind"] == "Internal":
        try:
            from core.logger import log_crash

            log_crash(
                _logger(),
                component,
                exc_value=exc,
                event="fabric.boundary.internal",
                level="ERROR",
            )
        except Exception:  # noqa: BLE001
            pass
    if fail_mode == "closed":
        fault = {**fault, "alternative": False}
    return fault


def _failed(exc: BaseException, component: str, fail_mode: str, ms: int):
    from core.tool_result import ToolResult

    fault = _fault_for(exc, component, fail_mode)
    return ToolResult.fail(
        message=f"{component} failed: {fault['detail'] or type(exc).__name__}",
        error=fault["detail"],
        error_type=type(exc).__name__,
        duration_ms=ms,
        data={"fault": fault},
    )


def _settle(raw: Any, component: str, fail_mode: str, ms: int):
    """Whatever the function returned, as a ToolResult with its fault attached."""
    from core.tool_result import ToolResult

    result = ToolResult.from_any(raw, duration_ms=ms)
    if result.duration_ms is None:
        result.duration_ms = ms
    faults.attach(result, component)
    fault = result.data.get("fault") if isinstance(result.data, dict) else None
    if fail_mode == "closed" and isinstance(fault, dict) and fault.get("alternative"):
        result.data["fault"] = {**fault, "alternative": False}
    return result


def _report(topic: str, component: str, result, fail_mode: str, timeout_s, ms: int) -> None:
    fault = result.data.get("fault") if isinstance(result.data, dict) else None
    first = str(result.message or result.error or "").strip().splitlines()
    events.emit(
        topic,
        component=component,
        outcome="ok" if result.success else "fault",
        summary=first[0] if first else "",
        fault=fault if not result.success else None,
        provenance={
            "duration_ms": ms,
            "fail_mode": fail_mode,
            "overran": bool(timeout_s and ms > timeout_s * 1000),
        },
    )


def boundary(component: str, *, fail_mode: str, timeout_s: float | None = None, long: bool = False):
    """Wrap an adapter. See the module docstring for what it guarantees."""
    if fail_mode not in FAIL_MODES:
        raise ValueError(f"fail_mode must be one of {FAIL_MODES}, got {fail_mode!r}")
    if timeout_s is not None and not timeout_s > 0:
        raise ValueError("timeout_s must be positive")
    component = str(component)
    start_topic, result_topic = _topics(component)

    def decorate(fn):
        is_async = inspect.iscoroutinefunction(fn)
        if long and not is_async:
            raise TypeError("long=True needs an async function (it runs as a background task)")

        if not is_async:

            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                if not events.enabled():
                    return fn(*args, **kwargs)
                t0 = time.monotonic()
                try:
                    raw = fn(*args, **kwargs)
                    ms = int((time.monotonic() - t0) * 1000)
                    result = _settle(raw, component, fail_mode, ms)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:  # noqa: BLE001 -- the whole point of a bulkhead
                    ms = int((time.monotonic() - t0) * 1000)
                    result = _failed(exc, component, fail_mode, ms)
                _report(result_topic, component, result, fail_mode, timeout_s, ms)
                return result

            return sync_wrapper

        async def _run(args, kwargs):
            t0 = time.monotonic()
            try:
                coro = fn(*args, **kwargs)
                raw = await (asyncio.wait_for(coro, timeout_s) if timeout_s else coro)
                ms = int((time.monotonic() - t0) * 1000)
                result = _settle(raw, component, fail_mode, ms)
            except asyncio.CancelledError:
                ms = int((time.monotonic() - t0) * 1000)
                fault = faults.make("Cancelled", component, "task cancelled")
                events.emit(
                    result_topic,
                    component=component,
                    outcome="fault",
                    summary="cancelled",
                    fault=fault,
                    provenance={"duration_ms": ms, "fail_mode": fail_mode},
                )
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 -- includes asyncio's TimeoutError
                ms = int((time.monotonic() - t0) * 1000)
                result = _failed(exc, component, fail_mode, ms)
            _report(result_topic, component, result, fail_mode, timeout_s, ms)
            return result

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            if not events.enabled():
                return await fn(*args, **kwargs)
            if not long:
                return await _run(args, kwargs)
            from core.tool_result import ToolResult

            events.emit(start_topic, component=component, outcome="progress", summary="started")
            task = asyncio.ensure_future(_run(args, kwargs))  # copies the ctx_id contextvar
            _background.add(task)
            task.add_done_callback(_background.discard)
            return ToolResult.ok(
                "Started - I'll report back when it finishes.",
                data={"ctx_id": events.current_ctx(), "background": True},
            )

        return async_wrapper

    return decorate
