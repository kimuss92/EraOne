"""Nonblocking admission between remote calls and local machine automation.

Local work retains its existing concurrency. Remote machine work requires a
quiet local lane; local machine work reports busy while that remote lease is held.
The lease lives through real worker completion, not a cancelled wrapper task.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

MACHINE_TOOLS = frozenset(
    {
        "open_app",
        "browser_control",
        "file_controller",
        "code_helper",
        "dev_agent",
        "screen_process",
        "send_message",
        "youtube_video",
        "computer_settings",
        "system_performance",
        "desktop_control",
        "computer_control",
        "file_processor",
        "netflix_control",
        "spotify_control",
        "media_coordinator",
        "window_control",
        "premiere_control",
        "agent_browser",
        "agent_task",
        "generated_code",
        "game_updater",
    }
)
REMOTE_OWNER: ContextVar[str | None] = ContextVar("remote_tool_owner", default=None)


class ToolLaneBusy(RuntimeError):
    pass


class ExecutionLane:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = 0
        self._owner: str | None = None
        self._thread: int | None = None
        self._depth = 0

    @contextmanager
    def enter(self, tool: str, owner: str | None = None) -> Iterator[None]:
        if tool not in MACHINE_TOOLS:
            yield
            return
        with self._lock:
            if owner:
                if (
                    self._local
                    or self._owner not in (None, owner)
                    or (self._owner == owner and self._thread != threading.get_ident())
                ):
                    raise ToolLaneBusy("Machine automation is already running.")
                self._owner = owner
                self._thread = threading.get_ident()
                self._depth += 1
            else:
                if self._owner is not None:
                    raise ToolLaneBusy("Machine automation is running for a Telegram call.")
                self._local += 1
        try:
            yield
        finally:
            with self._lock:
                if owner:
                    self._depth -= 1
                    if not self._depth:
                        self._owner = None
                        self._thread = None
                else:
                    self._local -= 1


LANE = ExecutionLane()


def resource_tool(tool: str, parameters: dict[str, Any] | None = None) -> str:
    """Confirmation tools inherit the resource of their stored concrete action."""
    if tool == "confirm_pending_confirmation":
        from core.pending_confirmation import get_shared_store

        pending = get_shared_store().peek((parameters or {}).get("confirmation_id") or "")
        if pending is not None:
            return str(pending.tool)
    return tool
