"""Keep a healthy microphone briefly across provider reconnects, without replay."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class CaptureCache:
    """One physical input stream; callbacks belong to exactly one live lease."""

    def __init__(
        self,
        opener: Callable[..., Any],
        *,
        linger_s: float = 15.0,
        timer_factory: Callable[..., Any] = threading.Timer,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._opener = opener
        self._linger_s = linger_s
        self._timer_factory = timer_factory
        self._on_close = on_close
        self._lock = threading.RLock()
        self._manager: Any = None
        self._stream: Any = None
        self._key: dict[str, Any] = {}
        self._callback: Callable[..., Any] | None = None
        self._leased = False
        self._timer: Any = None
        self._revision = 0
        self.reused = False

    def _relay(self, *args: Any) -> None:
        # Never hold the lifecycle lock in a driver callback: Pa_Start/Stop can
        # wait for that callback. During reconnect there is no consumer/buffer.
        callback = self._callback
        if callback is not None:
            callback(*args)

    def _close_locked(self) -> None:
        self._callback = None
        manager, self._manager = self._manager, None
        self._stream = None
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass  # an absent audio device must not prevent cleanup
            if self._on_close is not None:
                try:
                    self._on_close()
                except Exception:
                    pass

    def _expire(self, revision: int) -> None:
        with self._lock:
            if revision == self._revision and not self._leased:
                self._timer = None
                self._close_locked()

    def close_idle(self) -> bool:
        """Close retained capture; False forbids global device refresh while used."""
        with self._lock:
            if self._leased:
                return False
            self._revision += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._close_locked()
            return True

    @contextmanager
    def lease(self, callback: Callable[..., Any], *, owner: Any, **settings: Any) -> Iterator[Any]:
        with self._lock:
            if self._leased:
                raise RuntimeError("Microphone capture already has an owner")
            self._revision += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self.reused = (
                settings.get("device") is not None
                and self._stream is not None
                and self._key == settings
                and getattr(self._stream, "active", True) is not False
            )
            if not self.reused:
                self._close_locked()
                manager = self._opener(callback=self._relay, **settings)
                try:
                    stream = manager.__enter__()
                except BaseException:
                    try:
                        manager.__exit__(None, None, None)
                    except Exception:
                        pass
                    raise
                self._manager, self._stream, self._key = manager, stream, settings
            self._leased = True
            self._callback = callback
            stream = self._stream
        broken = False
        try:
            yield stream
        except BaseException as exc:
            broken = not isinstance(exc, asyncio.CancelledError)
            raise
        finally:
            with self._lock:
                self._callback = None
                self._leased = False
                if broken or getattr(owner, "_shutdown_requested", False):
                    self._close_locked()
                else:
                    self._timer = self._timer_factory(
                        self._linger_s, self._expire, args=(self._revision,)
                    )
                    self._timer.daemon = True
                    self._timer.start()


def capture_cache(owner: Any, opener: Callable[..., Any]) -> CaptureCache:
    """One cache per Era instance; no global capture or microphone preference."""
    cache = getattr(owner, "_input_stream_cache", None)
    if cache is None:
        from core.voice_input import clear_active_input

        cache = owner._input_stream_cache = CaptureCache(
            opener, on_close=lambda: clear_active_input(owner=cache)
        )
    return cache


def close_retained_input(owner: Any) -> bool:
    cache = getattr(owner, "_input_stream_cache", None)
    return cache is None or cache.close_idle()
