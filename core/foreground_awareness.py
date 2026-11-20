"""Read-only foreground app events with bounded freshness and clean shutdown."""

from __future__ import annotations

import os
import threading
import time
from copy import deepcopy

FRESH_S = 35.0


def read_foreground():
    import psutil
    import win32gui
    import win32process

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    name = psutil.Process(pid).name()
    if win32gui.GetForegroundWindow() != hwnd:
        raise RuntimeError("foreground changed during read")
    _, current_pid = win32process.GetWindowThreadProcessId(hwnd)
    if current_pid != pid:
        raise RuntimeError("foreground identity changed during read")
    return {"hwnd": hwnd, "pid": pid, "app": name}


def native_events(
    observer, *, reader=read_foreground, event_range=(3, 3), window_only=False, min_interval=0.0
):
    """Register and pump native events on this thread; never mutate windows."""
    if os.name != "nt":
        raise OSError("Windows foreground events unavailable")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )

    def sample(*_ignored):
        try:
            observer.observe(reader())
        except Exception as exc:
            observer.unavailable()
            _log("read_failed", exc, kind=getattr(observer, "kind", "foreground"))

    dirty = False

    def notify(_hook, _event, _hwnd, object_id, child_id, _thread, _time):
        nonlocal dirty
        if not window_only or (object_id == 0 and child_id == 0):
            dirty = True

    callback = callback_type(notify)
    user32.SetWinEventHook.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        callback_type,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    user32.UnhookWinEvent.restype = wintypes.BOOL
    hook = user32.SetWinEventHook(*event_range, None, callback, 0, 0, 0)
    if not hook:
        raise OSError("foreground hook registration failed")
    try:
        sample()
        message = wintypes.MSG()
        next_read = time.monotonic() + 15.0
        last_read = time.monotonic()
        while not observer._stop.wait(0.05):
            while not observer._stop.is_set() and user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, 1
            ):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            now = time.monotonic()
            if now >= next_read or (dirty and now - last_read >= min_interval):
                dirty = False
                sample()
                last_read = now
                next_read = now + 15.0
    finally:
        if not user32.UnhookWinEvent(hook):
            _log("unhook_failed", OSError(), kind=getattr(observer, "kind", "foreground"))


def _log(event, exc, *, kind="foreground"):
    from core.logger import log_event, main_logger

    log_event(main_logger, f"awareness.{kind}.{event}", "WARNING", error_type=type(exc).__name__)


class ForegroundAwareness:
    def __init__(self, *, publisher=None, driver=native_events, clock=time.time):
        self._publisher, self._driver, self._clock = publisher, driver, clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._view = {"revision": 0, "health": "unavailable", "observedAt": 0.0, "foreground": None}

    def snapshot(self):
        with self._lock:
            view = deepcopy(self._view)
        if view["health"] == "ready" and not 0 <= self._clock() - view["observedAt"] <= FRESH_S:
            view["health"], view["foreground"] = "stale", None
        return view

    def observe(self, foreground):
        if foreground is not None:
            if (
                not isinstance(foreground, dict)
                or set(foreground) != {"hwnd", "pid", "app"}
                or type(foreground["hwnd"]) is not int
                or foreground["hwnd"] <= 0
                or type(foreground["pid"]) is not int
                or foreground["pid"] <= 0
                or not isinstance(foreground["app"], str)
                or not foreground["app"]
                or len(foreground["app"]) > 256
            ):
                raise ValueError("invalid foreground evidence")
        self._update("ready", foreground, self._clock())

    def unavailable(self):
        self._update("unavailable", None, 0.0)

    def _update(self, health, foreground, observed_at):
        with self._lock:
            changed = (self._view["health"], self._view["foreground"]) != (health, foreground)
            self._view = {
                "revision": self._view["revision"] + 1,
                "health": health,
                "observedAt": observed_at,
                "foreground": deepcopy(foreground),
            }
            view = deepcopy(self._view)
        if self._publisher:
            try:
                self._publisher(view, changed=changed)
            except Exception as exc:
                _log("publish_failed", exc)

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name=type(self).__name__)
            self._thread.start()
            return True

    def _run(self):
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._driver(self)
                if not self._stop.is_set():
                    raise RuntimeError("foreground observer exited")
            except Exception as exc:
                _log("observer_failed", exc, kind=getattr(self, "kind", "foreground"))
                if self._stop.is_set():
                    break
                reader = getattr(self, "_fallback_read", None)
                try:
                    if reader:
                        self.observe(reader(), coverage="reconciliation-only")
                    else:
                        self.unavailable()
                except Exception as read_exc:
                    self.unavailable()
                    _log("read_failed", read_exc, kind=getattr(self, "kind", "foreground"))
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, getattr(self, "_max_retry_s", 30.0))

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self.unavailable()
        return not (thread and thread.is_alive())


_service = None
_service_lock = threading.Lock()


def get_foreground_awareness():
    global _service
    with _service_lock:
        if _service is None:
            from app.hud.events import hud

            _service = ForegroundAwareness(publisher=hud.foreground_observed)
        return _service
