"""Bound connection setup and release SDK transports on every reconnect."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager


def live_model(default: str) -> str:
    """Resolve the voice alias at connection time, with an explicit env override."""
    override = os.environ.get("ERA_LIVE_MODEL", "").strip()
    if override:
        return override
    from core.llm.registry import resolve

    entry = resolve("era.voice")
    if entry.provider != "gemini" or not entry.capabilities.realtime_audio:
        raise ValueError("era.voice must select a Gemini realtime audio model")
    return entry.model or default


@asynccontextmanager
async def managed_live_connection(client, *, model: str, config, timeout_s: float = 30.0):
    try:
        async with AsyncExitStack() as stack:
            aio = await stack.enter_async_context(client.aio)
            session = await asyncio.wait_for(
                stack.enter_async_context(aio.live.connect(model=model, config=config)),
                timeout=timeout_s,
            )
            yield session
    finally:
        # The SDK owns separate synchronous and asynchronous HTTP pools.
        client.close()


async def stop_on_renewal(event: asyncio.Event, tasks: list[asyncio.Task]) -> None:
    """A normal TaskGroup exit waits for workers; explicitly cancel on renewal."""
    await event.wait()
    for task in tasks:
        task.cancel()
