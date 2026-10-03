"""Observe cross-thread coroutine failures instead of abandoning their futures."""

from __future__ import annotations

import asyncio
import concurrent.futures

from core.logger import log_event, main_logger


def submit_coroutine(coro, loop):
    def report(exc):
        try:
            log_event(main_logger, "engine.command.failed", "ERROR", error_type=type(exc).__name__)
            from app.hud.events import hud

            hud.error(
                "A background command failed. Please try again; details are in era.log.",
                where="engine",
            )
        except Exception:
            pass

    # run_coroutine_threadsafe accepts a stopped loop and leaves the Future
    # pending forever. Close unscheduled coroutines to avoid leaked warnings.
    if loop is None or loop.is_closed() or not loop.is_running():
        coro.close()
        report(RuntimeError("Engine event loop is unavailable"))
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError as exc:
        coro.close()
        report(exc)
        return None

    def finished(result):
        try:
            result.result()
        except (concurrent.futures.CancelledError, asyncio.CancelledError):
            pass
        except Exception as exc:
            report(exc)

    future.add_done_callback(finished)
    return future
