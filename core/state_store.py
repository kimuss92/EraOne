# state_store.py
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
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
    playback_status: str = "unknown"


@dataclass
class SystemState:
    foreground: ForegroundInfo = field(default_factory=ForegroundInfo)
    open_apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    browser_tabs: dict[str, BrowserTabInfo] = field(default_factory=dict)
    media_sessions: dict[str, MediaSessionInfo] = field(default_factory=dict)
    last_sync_ts: float = 0.0
    drift_flags: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 0
    media_health: str = "unavailable"
    media_observed_at: float = 0.0


class StateStore:
    """Async-safe in-memory system state cache."""

    def __init__(self) -> None:
        self._state = SystemState()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)

    def _mark_changed(self) -> None:
        """Called only while holding the shared state/notification lock."""
        self._state.revision += 1
        self._state.last_sync_ts = time.time()
        self._changed.notify_all()

    async def update_foreground(self, info: ForegroundInfo) -> None:
        async with self._lock:
            self._state.foreground = deepcopy(info)
            self._mark_changed()

    async def update_open_apps(self, apps: dict[str, dict[str, Any]]) -> None:
        async with self._lock:
            self._state.open_apps = deepcopy(apps)
            self._mark_changed()

    async def update_browser_tabs(self, tabs: dict[str, BrowserTabInfo]) -> None:
        async with self._lock:
            self._state.browser_tabs = deepcopy(tabs)
            self._mark_changed()

    async def update_media_session(self, session: MediaSessionInfo) -> None:
        async with self._lock:
            self._state.media_sessions[session.session_id] = deepcopy(session)
            self._mark_changed()

    async def replace_media_sessions(
        self,
        sessions: list[MediaSessionInfo],
        *,
        health: str = "ready",
        observed_at: float | None = None,
    ) -> None:
        """One complete observation, including removals and source health."""
        async with self._lock:
            self._state.media_sessions = {s.session_id: deepcopy(s) for s in sessions}
            self._state.media_health = health
            self._state.media_observed_at = time.time() if observed_at is None else observed_at
            self._mark_changed()

    async def snapshot(self) -> SystemState:
        async with self._lock:
            return deepcopy(self._state)

    async def wait_for_change(
        self, after_revision: int, *, timeout: float | None = None
    ) -> SystemState:
        """Wait without polling and return the latest detached snapshot.

        Revisions are local to this store instance. Consumers must take a new
        initial snapshot when connecting to a newly created store.
        """
        async with asyncio.timeout(timeout):
            async with self._changed:
                await self._changed.wait_for(lambda: self._state.revision > after_revision)
                return deepcopy(self._state)

    async def add_drift_flag(self, flag: dict[str, Any]) -> None:
        async with self._lock:
            self._state.drift_flags.append(deepcopy(flag))
            if len(self._state.drift_flags) > 200:
                self._state.drift_flags = self._state.drift_flags[-200:]
            self._mark_changed()

    async def clear_drift_flags(self) -> None:
        async with self._lock:
            self._state.drift_flags.clear()
            self._mark_changed()
