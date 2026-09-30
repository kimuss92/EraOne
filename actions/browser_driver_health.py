# actions/browser_driver_health.py
"""Liveness checks for the Playwright driver behind a _BrowserSession.

Playwright-python talks to a Node.js driver subprocess over a stdio pipe. When
that process exits (killed, crashed, pipe torn down), NOTHING in Playwright's
state says so: proven live (2026-09-19) — after killing the driver,
`Browser.is_connected()` stays True, `browser.contexts` stays populated and even
the private `Connection._closed_error` stays None. Every later call just dies
with "Connection closed while reading from the driver". The only reliable
signals are the driver process's exit code and that error text, so this module
reads both.

It also lets `_BrowserSession.run()` retry a call after a driver restart: the
coroutine object it was handed is spent, so `rebuild_call` re-invokes the same
bound method with the same arguments.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

DRIVER_DEATH_MARKERS = ("connection closed while reading from the driver",)


def _transport(pw: Any) -> Any:
    return pw._impl_obj._connection._transport


def driver_alive(pw: Any) -> bool:
    """False when the Playwright driver behind `pw` is gone.

    If the private internals can't be read (a Playwright upgrade renamed them),
    answer True and let `is_driver_death()` on the error text catch it instead.
    """
    if pw is None:
        return False
    try:
        conn = pw._impl_obj._connection
        transport = conn._transport
    except Exception:
        return True
    if getattr(conn, "_closed_error", None) is not None:
        return False
    proc = getattr(transport, "_proc", None)
    if proc is not None and getattr(proc, "returncode", None) is not None:
        return False
    err_future = getattr(transport, "on_error_future", None)
    try:
        if err_future is not None and err_future.done():
            return False
    except Exception:
        pass
    return True


def kill_driver(pw: Any) -> None:
    """Hard-kill the driver process (used when it is hung, not dead)."""
    try:
        proc = _transport(pw)._proc
        if proc.returncode is None:
            proc.kill()
    except Exception:
        pass


def consume_driver_error(pw: Any) -> None:
    """Mark the dead driver's error future as seen.

    Nobody else awaits it once the driver is replaced, so asyncio would log
    "Future exception was never retrieved" for every restart.
    """
    try:
        fut = _transport(pw).on_error_future
        if fut.done() and not fut.cancelled():
            fut.exception()
    except Exception:
        pass


def is_driver_death(err: Any) -> bool:
    """True if an exception or a returned error string reports driver death."""
    text = str(err).lower()
    return any(marker in text for marker in DRIVER_DEATH_MARKERS)


def rebuild_call(owner: Any, coro: Any) -> Callable[[], Any] | None:
    """Factory that re-creates `coro` if it is an unstarted `owner.method(...)` call.

    Returns None for anything else (a raw `page.evaluate(...)`, a free
    function, a coroutine that has already started, *args/**kwargs methods),
    which simply means that call is not retried.
    """
    if not inspect.iscoroutine(coro):
        return None
    try:
        if inspect.getcoroutinestate(coro) != inspect.CORO_CREATED:
            return None
        local = dict(inspect.getcoroutinelocals(coro))
    except Exception:
        return None
    if local.get("self") is not owner:
        return None
    code = coro.cr_code
    if code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS):
        return None
    method = getattr(owner, code.co_name, None)
    func = getattr(method, "__func__", None)
    if func is None or getattr(func, "__code__", None) is not code:
        return None
    names = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    try:
        kwargs = {name: local[name] for name in names if name != "self"}
    except KeyError:
        return None
    return lambda: method(**kwargs)
