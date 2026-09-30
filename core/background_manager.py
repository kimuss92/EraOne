from __future__ import annotations

import asyncio

from core.state_store import StateStore


class BackgroundManager:
    """Manages Era One background loops lifecycle."""

    def __init__(self, state: StateStore | None = None) -> None:
        self.state = state or StateStore()
        self._tasks: list[asyncio.Task] = []
        self._started = False

    async def start_all(self) -> None:
        """Start all background monitoring loops."""
        if self._started:
            return
        self._started = True

        # Lazy imports to allow non-Windows dev
        try:
            from windows.media_exclusivity import run_media_exclusivity_loop
            from windows.state_sync import run_state_sync_loop
        except Exception:
            # Non-Windows fallback: idle loop
            async def _idle(state: StateStore) -> None:
                while True:
                    await asyncio.sleep(10)

            run_state_sync_loop = _idle
            run_media_exclusivity_loop = _idle

        self._tasks = [
            asyncio.create_task(run_state_sync_loop(self.state), name="state_sync_loop"),
            asyncio.create_task(
                run_media_exclusivity_loop(self.state), name="media_exclusivity_loop"
            ),
        ]

    async def stop_all(self) -> None:
        """Stop all background loops gracefully."""
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False
