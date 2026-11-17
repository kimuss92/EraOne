from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterable
from typing import Any

from core.loop_rate_limit import Debounce
from core.state_store import BrowserTabInfo, ForegroundInfo, StateStore


class _OpenAppEventMonitor:
    """Maintain an application inventory from WMI process lifecycle events.

    WMI is queried once for the initial inventory. Subsequent changes come from
    Win32_ProcessTrace start/stop events, so the state-sync loop never walks the
    complete process table on a timer.
    """

    _EVENT_QUERY = "SELECT * FROM Win32_ProcessTrace"
    _INVENTORY_QUERY = "SELECT ProcessId, Name FROM Win32_Process"
    _WMI_TIMEOUT = 0x80043001

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._processes: dict[int, str] = {}
        self._error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="EraWmiProcessEvents",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = sorted(self._processes.items())
        apps: dict[str, dict[str, Any]] = {}
        for pid, name in rows:
            apps[name.casefold()] = {"pid": pid, "name": name}
        return apps

    def _replace_inventory(self, rows: Iterable[Any]) -> None:
        processes: dict[int, str] = {}
        for row in rows:
            try:
                pid = int(row.ProcessId)
                name = str(row.Name or "").strip()
            except (AttributeError, TypeError, ValueError):
                continue
            if pid > 0 and name:
                processes[pid] = name
        with self._lock:
            self._processes = processes

    def _apply_event(self, event: Any) -> bool:
        try:
            path = getattr(event, "Path_", None)
            event_class = str(getattr(path, "Class", "")).casefold()
            pid = int(event.ProcessID)
        except (AttributeError, TypeError, ValueError):
            return False
        if pid <= 0:
            return False

        with self._lock:
            if event_class.endswith("processstarttrace"):
                name = str(getattr(event, "ProcessName", "") or "").strip()
                if not name or self._processes.get(pid) == name:
                    return False
                self._processes[pid] = name
                return True
            if event_class.endswith("processstoptrace"):
                return self._processes.pop(pid, None) is not None
        return False

    @classmethod
    def _is_timeout(cls, exc: BaseException) -> bool:
        hresult = getattr(exc, "hresult", None)
        return isinstance(hresult, int) and hresult & 0xFFFFFFFF == cls._WMI_TIMEOUT

    def _worker(self) -> None:
        pythoncom = None
        service = None
        source = None
        try:
            import pythoncom as com
            import win32com.client

            pythoncom = com
            pythoncom.CoInitialize()
            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            service = locator.ConnectServer(".", r"root\cimv2")
            self._replace_inventory(service.ExecQuery(self._INVENTORY_QUERY))
            source = service.ExecNotificationQuery(self._EVENT_QUERY)
            self._ready.set()

            while not self._stop.is_set():
                try:
                    event = source.NextEvent(1000)
                except Exception as exc:
                    if self._is_timeout(exc):
                        continue
                    self._error = f"{type(exc).__name__}: {exc}"
                    return
                self._apply_event(event)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()
            source = None
            service = None
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass


async def run_state_sync_loop(state: StateStore) -> None:
    """Synchronize foreground, application, and browser state efficiently."""
    if os.name != "nt":
        while True:
            await asyncio.sleep(10)

    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        while True:
            await asyncio.sleep(10)

    foreground_debounce = Debounce(interval_s=1.5)
    tabs_debounce = Debounce(interval_s=2.0)
    app_monitor = _OpenAppEventMonitor()
    app_monitor.start()

    last_apps: dict[str, dict[str, Any]] = {}
    last_browser_tabs: dict[str, BrowserTabInfo] = {}

    def get_foreground() -> ForegroundInfo:
        hwnd = win32gui.GetForegroundWindow() or 0
        if not hwnd:
            return ForegroundInfo(timestamp=time.time())
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc_name = psutil.Process(pid).name()
            except Exception:
                proc_name = str(pid)

            title = win32gui.GetWindowText(hwnd) or ""
            class_name = win32gui.GetClassName(hwnd) or ""
            return ForegroundInfo(
                hwnd=hwnd,
                process_id=pid,
                process_name=proc_name,
                window_title=title,
                class_name=class_name,
                timestamp=time.time(),
            )
        except Exception:
            return ForegroundInfo(hwnd=hwnd, timestamp=time.time())

    def enumerate_open_apps() -> dict[str, dict[str, Any]]:
        """Return the inventory maintained by WMI process change events."""
        return app_monitor.snapshot()

    async def sample_browser_tabs() -> dict[str, BrowserTabInfo]:
        """Placeholder - integrate with Playwright CDP in production."""
        return last_browser_tabs

    try:
        while True:
            now = time.time()

            if foreground_debounce.allow(now):
                info = get_foreground()
                await state.update_foreground(info)

            apps = enumerate_open_apps()
            if apps != last_apps:
                last_apps = apps
                await state.update_open_apps(apps)

            if tabs_debounce.allow(now):
                tabs = await sample_browser_tabs()
                if tabs != last_browser_tabs:
                    last_browser_tabs = tabs
                    await state.update_browser_tabs(tabs)

            await asyncio.sleep(1.0)
    finally:
        app_monitor.stop()
