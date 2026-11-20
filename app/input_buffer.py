"""Keep undelivered inputs through reconnect; never replay consent or sent work."""

import asyncio
import re
import time
from collections import deque

_CONSENT = re.compile(r"^(?:yes|no|ok|okay|confirm|approve|reject|apply it|go ahead)\b", re.I)


class InputBuffer:
    def __init__(self, *, limit=10, ttl=120.0, clock=time.time):
        self.items = deque()
        self.limit, self.ttl, self.clock = limit, ttl, clock

    def put(self, owner):
        if _CONSENT.match(owner.text.strip()) or owner.text.startswith("__"):
            return False
        if any(row.id == owner.id for row in self.items):
            return True
        if len(self.items) >= self.limit:
            return False
        self.items.append(owner)
        return True

    def take(self):
        while self.items:
            row = self.items.popleft()
            if 0 <= self.clock() - row.received_at <= self.ttl:
                return row
        return None

    def clear(self):
        count = len(self.items)
        self.items.clear()
        return count


def buffer_for(era):
    buffer = getattr(era, "_offline_input_buffer", None)
    if buffer is None:
        buffer = era._offline_input_buffer = InputBuffer()
    return buffer


async def drain(era):
    buffer = buffer_for(era)
    while buffer.items and getattr(era, "session", None) is not None:
        from app.codemode_runtime import owns_reply

        tracker = getattr(era, "_model_reply_ownership", None)
        local = getattr(era, "_local_turn_task", None)
        if (
            owns_reply()
            or getattr(era, "_standby_active", False)
            or (tracker and tracker.active)
            or (local and not local.done())
        ):
            await asyncio.sleep(0.1)
            continue
        owner = buffer.take()
        if owner is None:
            era.ui.write_log("SYS: Buffered requests expired; please send them again.")
            return
        # Dequeue before dispatch. An ambiguous send failure is never replayed.
        era._accept_input(owner)
        era._handle_owned_text(owner)
        await asyncio.sleep(0.25)


def start_drain(era):
    task = getattr(era, "_offline_input_drain", None)
    if task is not None and not task.done():
        return
    if not buffer_for(era).items:
        return
    era.ui.write_log("SYS: Connection restored. Sending buffered requests one at a time.")
    era._offline_input_drain = asyncio.create_task(drain(era), name="offline-input-recovery")
