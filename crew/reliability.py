"""Bounded model-only retries. A timed-out model cannot execute capabilities."""

import contextvars
import os
import queue
import threading
import time

from core.cancel_registry import CURRENT_TOKEN, bind, current, raise_if_cancelled, sleep_cancellable
from core.logger import log_event, main_logger

_slots = threading.BoundedSemaphore(4)
_lock = threading.Lock()
_states = {}
_workers = {}


def _setting(name, default, minimum, maximum):
    try:
        return min(maximum, max(minimum, float(os.environ.get(name, default))))
    except ValueError:
        return default


def snapshot():
    with _lock:
        return {key: {**value, "workers": _workers.get(key, 0)} for key, value in _states.items()}


def _beat(bot, phase, attempt):
    with _lock:
        if phase == "idle" and _workers.get(bot, 0):
            phase = "recovery"
        if len(_states) >= 100 and bot not in _states:
            _states.pop(next(iter(_states)))
        _states[bot] = {"phase": phase, "attempt": attempt, "heartbeat_at": time.time()}


def _once(call, args, deadline, bot, attempt):
    if not _slots.acquire(blocking=False):
        raise TimeoutError("Crew model workers are still recovering")
    result = queue.Queue(maxsize=1)
    context = contextvars.copy_context()
    token = current() or CURRENT_TOKEN.get()
    with _lock:
        _workers[bot] = _workers.get(bot, 0) + 1

    def run():
        try:

            def invoke():
                with bind(token):
                    return call(*args)

            outcome = (True, context.run(invoke))
        except BaseException as exc:
            outcome = (False, exc)
        finally:
            _slots.release()
            with _lock:
                _workers[bot] -= 1
                if not _workers[bot]:
                    del _workers[bot]
                    if _states.get(bot, {}).get("phase") == "recovery":
                        _states[bot]["phase"] = "idle"
        result.put(outcome)

    threading.Thread(target=run, name=f"crew-model-{bot}", daemon=True).start()
    while True:
        raise_if_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Crew model response deadline exceeded")
        _beat(bot, "waiting_for_model", attempt)
        try:
            ok, value = result.get(timeout=min(0.25, remaining))
        except queue.Empty:
            continue
        if ok:
            return value
        raise value


def call(model, args, *, bot, deadline=0.0, backup=None):
    timeout = _setting("ERA_CREW_MODEL_TIMEOUT_S", 30.0, 0.01, 240.0)
    retries = int(_setting("ERA_CREW_MODEL_RETRIES", 3, 0, 3))
    deadline = deadline or time.monotonic() + 240.0
    last = "model unavailable"
    try:
        for attempt in range(retries + 1):
            try:
                return _once(model, args, min(deadline, time.monotonic() + timeout), bot, attempt)
            except (TimeoutError, ConnectionError) as exc:
                last = type(exc).__name__
                log_event(
                    main_logger,
                    "crew.model.timeout",
                    "WARNING",
                    bot=bot,
                    attempt=attempt,
                    error_type=last,
                    remaining_s=max(0, deadline - time.monotonic()),
                )
                delay = min(4.0, 0.25 * 2**attempt)
                if attempt == retries or time.monotonic() + delay >= deadline:
                    break
                if not sleep_cancellable(delay):
                    raise_if_cancelled()
        if backup is not None and time.monotonic() < deadline:
            _beat(bot, "backup_agent", retries + 1)
            try:
                return _once(
                    backup, args, min(deadline, time.monotonic() + timeout), bot, retries + 1
                )
            except (TimeoutError, ConnectionError) as exc:
                last = type(exc).__name__
        return {
            "__unavailable__": f"{bot} and its backup did not respond ({last}); system_default preserved completed work. Retry the remaining request."
        }
    finally:
        _beat(bot, "idle", 0)
