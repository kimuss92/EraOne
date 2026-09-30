from __future__ import annotations

import time
from typing import Any

from memory.persistence import MemoryPersistence


def _format_event(e: dict[str, Any]) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts", 0)))
    kind = e.get("kind", "note")
    content = e.get("content", "")
    source = e.get("source", "")
    extra = f" ({source})" if source else ""
    return f"- [{ts}] {kind}{extra}: {content}"


class MemoryInjector:
    """Injects persistent memory into ERA ONE prompt."""

    def __init__(self, persistence: MemoryPersistence | None = None) -> None:
        self.persistence = persistence or MemoryPersistence()
        self._cache: dict[str, Any] = {}
        self._cache_ts = 0.0

    async def get_memory_injection_text(self, max_events: int = 120) -> str:
        """Build text to be injected into the core instructions."""
        now = time.time()
        if not self._cache or now - self._cache_ts > 10.0:
            self._cache = await self.persistence.get_profile()
            self._cache_ts = now

        events = self._cache.get("events", [])
        tail = events[-max_events:]

        lines = [
            "=== ERA ONE PERSISTENT MEMORY PROFILE (LOCAL) ===",
            f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._cache.get('updated_at', 0)))}",
            "",
            "Recent behavioral notes / corrections:",
        ]
        lines += [_format_event(e) for e in tail]
        lines.append("=== END PROFILE ===")
        return chr(10).join(lines)
