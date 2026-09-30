# state_store.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ForegroundInfo:
    hwnd: int = 0
    process_id: int = 0
    process_name: str = ""
    window_title: str = ""
    class_name: str = ""
    timestamp: float = 0.0


@dataclass
class BrowserTabInfo:
    url: str = ""
    title: str = ""
    browser: str = ""
    timestamp: float = 0.0


@dataclass
class MediaSessionInfo:
    session_id: str = ""
    display_name: str = ""
    player_type: str = ""
    is_playing: bool = False
    is_paused: bool = False
    playback_rate: float = 1.0
    last_change_ts: float = 0.0


@dataclass
class SystemState:
    foreground: ForegroundInfo = field(default_factory=ForegroundInfo)
    open_apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    browser_tabs: dict[str, BrowserTabInfo] = field(default_factory=dict)
    media_sessions: dict[str, MediaSessionInfo] = field(default_factory=dict)
    last_sync_ts: float = 0.0
    drift_flags: list[dict[str, Any]] = field(default_factory=list)


class StateStore:
    """Async-safe in-memory system state cache."""

    def __init__(self) -> None:
        self._state = SystemState()
        self._lock = asyncio.Lock()

    async def update_foreground(self, info: ForegroundInfo) -> None:
        async with self._lock:
            self._state.foreground = info
            self._state.last_sync_ts = time.time()

    async def update_open_apps(self, apps: dict[str, dict[str, Any]]) -> None:
        async with self._lock:
            self._state.open_apps = apps
            self._state.last_sync_ts = time.time()

    async def update_browser_tabs(self, tabs: dict[str, BrowserTabInfo]) -> None:
        async with self._lock:
            self._state.browser_tabs = tabs
            self._state.last_sync_ts = time.time()

    async def update_media_session(self, session: MediaSessionInfo) -> None:
        async with self._lock:
            self._state.media_sessions[session.session_id] = session
            self._state.last_sync_ts = time.time()

    async def snapshot(self) -> SystemState:
        async with self._lock:
            return self._state

    async def add_drift_flag(self, flag: dict[str, Any]) -> None:
        async with self._lock:
            self._state.drift_flags.append(flag)
            if len(self._state.drift_flags) > 200:
                self._state.drift_flags = self._state.drift_flags[-200:]

    async def clear_drift_flags(self) -> None:
        async with self._lock:
            self._state.drift_flags.clear()
