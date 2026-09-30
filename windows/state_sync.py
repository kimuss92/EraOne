from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from core.loop_rate_limit import Debounce
from core.state_store import BrowserTabInfo, ForegroundInfo, StateStore

if os.name != "nt":

    async def run_state_sync_loop(state: StateStore) -> None:
        while True:
            await asyncio.sleep(10)


async def run_state_sync_loop(state: StateStore) -> None:
    """High-efficiency sync loop for foreground window + open apps."""
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        # Fallback for non-Windows
        async def _idle():
            while True:
                await asyncio.sleep(10)

        await _idle()
        return

    foreground_debounce = Debounce(interval_s=1.5)
    apps_debounce = Debounce(interval_s=2.0)
    tabs_debounce = Debounce(interval_s=2.0)

    last_apps: dict[str, dict[str, Any]] = {}
    last_browser_tabs: dict[str, BrowserTabInfo] = {}

    def get_foreground() -> ForegroundInfo:
        hwnd = win32gui.GetForegroundWindow() or 0
        if not hwnd:
            return ForegroundInfo(timestamp=time.time())
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = ""
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
        apps: dict[str, dict[str, Any]] = {}
        try:
            for p in psutil.process_iter(["pid", "name"]):
                name = (p.info.get("name") or "").strip()
                if not name:
                    continue
                apps[name.lower()] = {"pid": p.info.get("pid"), "name": name}
        except Exception:
            pass
        return apps

    async def sample_browser_tabs() -> dict[str, BrowserTabInfo]:
        """Placeholder - integrate with Playwright CDP in production."""
        return last_browser_tabs

    while True:
        now = time.time()

        if foreground_debounce.allow(now):
            info = get_foreground()
            await state.update_foreground(info)

        if apps_debounce.allow(now):
            apps = enumerate_open_apps()
            if apps != last_apps:
                last_apps = apps
                await state.update_open_apps(apps)

        if tabs_debounce.allow(now):
            tabs = await sample_browser_tabs()
            if tabs != last_browser_tabs:
                last_browser_tabs = tabs
                await state.update_browser_tabs(tabs)

        await asyncio.sleep(0.05)
