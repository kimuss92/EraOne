"""Global state listener for Era One.

Monitors OS-level state changes (processes, windows, media, audio) and notifies
the system when the environment changes manually (outside of Era's commands).
This allows Era to maintain an accurate picture of reality even when the user
acts directly on their machine.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

try:
    import psutil
except Exception:
    psutil = None

try:
    import win32gui
    import win32process
except Exception:
    win32gui = None
    win32process = None

from core import process_awareness

logger = logging.getLogger(__name__)

# The probes below import their heavy dependencies on first use, not at import
# time, so loading this module stays cheap and a missing optional backend only
# silences that one probe. They are module-level names (not function-local
# imports) so a test can patch them.
_registry = None  # browser-session registry; resolved lazily, patched by tests


def media_sessions() -> list:
    """Active Windows media sessions (core.media_routing), imported lazily."""
    from core.media_routing import media_sessions as _impl

    return _impl()


def _ensure_com() -> None:
    """Pin the COM apartment before touching audio APIs, imported lazily."""
    from core.audio_endpoints import _ensure_com as _impl

    _impl()


def _browser_registry():
    if _registry is not None:
        return _registry
    from actions.browser_control import _registry as live_registry

    return live_registry


@dataclass
class StateSnapshot:
    """A snapshot of the system state at a point in time."""

    processes: set[str] = field(default_factory=set)
    windows: set[tuple[str, str]] = field(default_factory=set)  # (app, state)
    audio_sessions: dict[str, str] = field(default_factory=dict)  # app -> state
    media_playback: dict[str, str] = field(default_factory=dict)  # app -> playback state
    timestamp: float = field(default_factory=time.time)


class StateListener:
    """Background listener that detects manual state changes."""

    def __init__(self, poll_interval: float = 2.0):
        self.poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_snapshot: StateSnapshot | None = None
        self._callbacks: list[Callable[[StateSnapshot, StateSnapshot], None]] = []
        self._lock = threading.Lock()

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the background polling."""
        if self._running:
            return
        self._running = True
        if loop is None:
            loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._poll_loop(), name="state-listener")
        logger.info("Global state listener started")

    def stop(self) -> None:
        """Stop the background polling."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Global state listener stopped")

    def register_callback(self, callback: Callable[[StateSnapshot, StateSnapshot], None]) -> None:
        """Register a callback to be notified of state changes.

        Callback receives (old_snapshot, new_snapshot).
        """
        with self._lock:
            self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[StateSnapshot, StateSnapshot], None]) -> None:
        """Unregister a callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                new_snapshot = await self._capture_snapshot()
                old_snapshot = self._last_snapshot
                self._last_snapshot = new_snapshot

                if old_snapshot is not None:
                    changes = self._detect_changes(old_snapshot, new_snapshot)
                    if changes:
                        logger.debug(f"Detected state changes: {changes}")
                        await self._notify_callbacks(old_snapshot, new_snapshot, changes)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in state listener poll: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _capture_snapshot(self) -> StateSnapshot:
        """Capture current system state."""
        snapshot = StateSnapshot()

        # Processes
        try:
            procs = process_awareness.list_processes(refresh=True)
            # Windows process names are case-insensitive: compare them lowered.
            snapshot.processes = {str(p.name).lower() for p in procs}
        except Exception:
            pass

        # Windows
        try:
            windows = []

            def collect(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                try:
                    name = psutil.Process(pid).name().lower()
                    state = "minimized" if win32gui.IsIconic(hwnd) else "visible"
                    windows.append((name, state))
                except psutil.NoSuchProcess:
                    pass
                return True

            win32gui.EnumWindows(collect, None)
            snapshot.windows = set(windows)
        except Exception:
            pass

        # Audio sessions & media playback (use existing mechanisms)
        try:
            _ensure_com()
            sessions = media_sessions()
            for s in sessions:
                snapshot.audio_sessions[s.stem] = "active" if s.active else "inactive"
        except Exception:
            pass

        # Browser media (if connected)
        try:
            registry = _browser_registry()
            with registry._lock:
                sessions = list(registry._sessions.values())
            for session in sessions:
                if session._context and not session._context._closed:
                    for page in session._context.pages:
                        if page.is_closed():
                            continue
                        from core.browser_media import READ_MEDIA, media_source

                        source = media_source(page.url)
                        if source in {"youtube", "netflix", "spotify"}:
                            try:
                                data = await asyncio.wait_for(
                                    page.evaluate(READ_MEDIA), timeout=0.3
                                )
                                if data.get("url") == page.url:
                                    snapshot.media_playback[source] = data.get("status", "unknown")
                            except Exception:
                                pass
        except Exception:
            pass

        return snapshot

    def _detect_changes(self, old: StateSnapshot, new: StateSnapshot) -> dict:
        """Detect what changed between snapshots."""
        changes = {
            "processes_started": new.processes - old.processes,
            "processes_stopped": old.processes - new.processes,
            "windows_changed": [],
            "audio_changed": {},
            "media_changed": {},
        }

        # Window changes
        old_windows = {(w[0], w[1]) for w in old.windows}
        new_windows = {(w[0], w[1]) for w in new.windows}
        for w in new_windows - old_windows:
            changes["windows_changed"].append({"app": w[0], "state": w[1], "type": "opened"})
        for w in old_windows - new_windows:
            changes["windows_changed"].append({"app": w[0], "state": w[1], "type": "closed"})

        # Audio session changes
        for app, state in new.audio_sessions.items():
            if old.audio_sessions.get(app) != state:
                changes["audio_changed"][app] = state
        for app in old.audio_sessions:
            if app not in new.audio_sessions:
                changes["audio_changed"][app] = "gone"

        # Media playback changes
        for app, state in new.media_playback.items():
            if old.media_playback.get(app) != state:
                changes["media_changed"][app] = state
        for app in old.media_playback:
            if app not in new.media_playback:
                changes["media_changed"][app] = "gone"

        return changes

    async def _notify_callbacks(
        self, old: StateSnapshot, new: StateSnapshot, changes: dict
    ) -> None:
        """Notify all registered callbacks of changes."""
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback(old, new, changes)
                else:
                    callback(old, new, changes)
            except Exception as e:
                logger.error(f"Error in state listener callback: {e}")


# Global instance
_state_listener: StateListener | None = None


def get_state_listener() -> StateListener:
    """Get the global state listener instance."""
    global _state_listener
    if _state_listener is None:
        _state_listener = StateListener()
    return _state_listener


def start_state_listener(
    loop: asyncio.AbstractEventLoop | None = None, poll_interval: float = 2.0
) -> None:
    """Start the global state listener."""
    global _state_listener
    if _state_listener is None:
        _state_listener = StateListener(poll_interval=poll_interval)
    _state_listener.start(loop)


def stop_state_listener() -> None:
    """Stop the global state listener."""
    global _state_listener
    if _state_listener:
        _state_listener.stop()
        _state_listener = None
