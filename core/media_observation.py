"""Fresh no-key media evidence, with monotonic publication and bounded lifetime."""

from __future__ import annotations

import copy
import threading
import time

_lock = threading.Lock()
_latest: dict[str, dict] = {}
FRESH_SECONDS = 5.0


def publish(source: str, observation: dict) -> dict:
    row = copy.deepcopy(observation)
    with _lock:
        previous = _latest.get(source, {})
        if row.get("sample_started_at", 0) >= previous.get("sample_started_at", 0):
            _latest[source] = row
    return row


def snapshot(source: str) -> dict | None:
    with _lock:
        row = copy.deepcopy(_latest.get(source))
    if row is None or not 0 <= time.time() - row.get("observed_at", 0) <= FRESH_SECONDS:
        return None
    return row


def spotify() -> dict:
    """One request reads metadata and playback from the same Windows session."""
    from actions.spotify_gsmtc_host import request

    started = time.time()
    data = request({"op": "current_track"}, timeout=2.0)
    ok = isinstance(data, dict) and data.get("ok") is True
    row = {
        "source": "windows_gsmtc",
        "sample_started_at": started,
        "observed_at": time.time(),
        "title": str(data.get("title") or "") if ok else "",
        "artist": str(data.get("artist") or "") if ok else "",
        "playback": ("playing" if data.get("is_playing") else "paused") if ok else "unknown",
        "position_ms": data.get("position_ms") if ok else None,
        "available": ok,
    }
    return publish("spotify", row)


def matches_track(row: dict, query: str) -> bool:
    if not row.get("available") or not row.get("title"):
        return False
    from actions.spotify_control import _track_matches

    return _track_matches(query, (row["title"], row.get("artist", "")))


async def settle_spotify(query: str, *, timeout: float = 6.0) -> dict:
    """Poll only observations; never replay a media command to verify it."""
    import asyncio

    from core.cancel_registry import raise_if_cancelled

    started = time.time()
    deadline = time.monotonic() + timeout
    row = {}
    while time.monotonic() < deadline:
        raise_if_cancelled()
        try:
            row = await asyncio.wait_for(
                asyncio.to_thread(spotify), max(0.01, deadline - time.monotonic())
            )
        except TimeoutError:
            break
        if (
            row.get("sample_started_at", 0) >= started
            and row.get("playback") == "playing"
            and matches_track(row, query)
        ):
            return {**row, "matched": True}
        await asyncio.sleep(0.25)
    return {**row, "matched": False}
