"""Bounded, request-owned Telegram previews and persistent final delivery.

Local speech playback connects through telegram_speech. Model-generated reply
callers must first establish response ownership before revealing text.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable

from core.input_envelope import InputEnvelope
from era_telegram import ReplyDeliveryResult, send_reply_update

Sender = Callable[[str, dict], Awaitable[ReplyDeliveryResult]]


def split_reply(text: str, limit: int = 4000) -> list[str]:
    """Lossless chunks bounded in UTF-16 units, including supplementary Unicode."""
    if limit < 2:
        raise ValueError("Reply chunk limit must be at least two")
    chunks, start, units = [], 0, 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append(text[start:index])
            start, units = index, 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return chunks


async def _send(method: str, payload: dict) -> ReplyDeliveryResult:
    return await asyncio.to_thread(send_reply_update, method, payload)


class TelegramReply:
    """One loop-owned reply, one destination, one coalescing worker.

    reveal/finish/cancel must run on the same event loop. Every network outcome
    is retained in events; the optional reporter receives sanitized metadata.
    Final failure stops later chunks to preserve message order.
    """

    def __init__(
        self,
        owner: InputEnvelope,
        chat_id: str,
        *,
        sender: Sender = _send,
        reporter: Callable[[dict], None] | None = None,
        interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_chars: int = 65536,
    ):
        if owner.source != "telegram":
            raise ValueError("Only a Telegram input can own a Telegram reply")
        if not str(chat_id).strip():
            raise ValueError("A destination is required")
        if not math.isfinite(interval) or interval < 0 or max_chars < 1:
            raise ValueError("Invalid reply bounds")
        self.owner = owner
        self.chat_id = str(chat_id).strip()
        self.draft_id = (uuid.uuid4().int % ((1 << 63) - 1)) + 1
        self._sender, self._reporter = sender, reporter
        self._interval, self._clock, self._sleep = interval, clock, sleep
        self._max_chars = max_chars
        self._text = ""
        self._revision = 0
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._finishing = False
        self._cancelled = False
        self.events: list[dict] = []
        self.outcome = "pending"

    def _event(self, stage: str, result: ReplyDeliveryResult) -> None:
        event = {
            "request_id": self.owner.id,
            "stage": stage,
            "status": result.status,
            "reason": result.reason,
            "message_id": result.message_id,
        }
        # Bound diagnostics as well as text, while retaining the final outcome.
        self.events.append(event)
        del self.events[:-128]
        if self._reporter:
            try:
                self._reporter(dict(event))
            except Exception:
                # A reporting hook must not trigger duplicate network sends.
                self.events.append({**event, "stage": "reporter", "status": "failed"})

    @property
    def is_open(self) -> bool:
        return not self._finishing and not self._cancelled

    def reveal(self, phrase: str) -> None:
        """Append a successfully played phrase; no cloud-generation assumptions."""
        if self._finishing or self._cancelled:
            raise RuntimeError("Reply is closed")
        if not isinstance(phrase, str):
            raise TypeError("Phrase must be text")
        if not phrase:
            return
        addition = (" " if self._text else "") + phrase
        if len(self._text) + len(addition) > self._max_chars:
            self._event("buffer", ReplyDeliveryResult("failed", "reply_capacity"))
            raise ValueError("Reply text capacity exceeded")
        self._text += addition
        self._revision += 1
        self._changed.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def finish(self) -> str:
        """Flush one final sequence; repeated calls share the same result."""
        if self._cancelled:
            return self.outcome
        self._finishing = True
        self._changed.set()
        if self._task is None and not self._cancelled:
            self._task = asyncio.create_task(self._run())
        if self._task is not None:
            # Cancelling a waiter must not cancel another waiter's final send.
            await asyncio.shield(self._task)
        return self.outcome

    async def cancel(self, *, reason: str = "interrupted") -> None:
        """Suppress unsent work; an in-flight HTTP send cannot be recalled."""
        if self._task is not None and self._task.done():
            return
        self._cancelled = True
        self.outcome = "cancelled"
        self._event("reply", ReplyDeliveryResult("cancelled", reason))
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _attempt(self, stage: str, payload: dict) -> ReplyDeliveryResult:
        method = "sendMessageDraft" if stage == "draft" else "sendMessage"
        result = ReplyDeliveryResult("failed", "retry_budget")
        for attempt in range(3):
            try:
                result = await self._sender(method, payload)
            except Exception:
                result = ReplyDeliveryResult("unknown", "transport_exception")
            self._event(stage, result)
            if result.status != "limited":
                return result
            if not math.isfinite(result.retry_after) or result.retry_after <= 0:
                return ReplyDeliveryResult("failed", "invalid_flood_control")
            if attempt < 2:
                await self._sleep(result.retry_after)
        return result

    async def _run(self) -> None:
        sent_revision, next_preview, previews = 0, 0.0, True
        while not self._finishing:
            await self._changed.wait()
            self._changed.clear()
            if self._finishing:
                break
            if not previews or sent_revision == self._revision:
                continue
            delay = max(0.0, next_preview - self._clock())
            if delay:
                await self._sleep(delay)
            if self._finishing:
                break
            sent_revision = self._revision
            # Latest chunk is a preview only. Final delivery includes all text.
            preview = split_reply(self._text)[-1]
            result = await self._attempt(
                "draft", {"chat_id": self.chat_id, "draft_id": self.draft_id, "text": preview}
            )
            next_preview = self._clock() + self._interval
            if result.status != "sent":
                previews = False
        if self._cancelled:
            return
        if not self._text:
            self.outcome = "empty"
            self._event("reply", ReplyDeliveryResult("empty"))
            return
        for chunk in split_reply(self._text):
            result = await self._attempt("final", {"chat_id": self.chat_id, "text": chunk})
            if result.status != "sent":
                self.outcome = result.status
                self._event("reply", result)
                return
        self.outcome = "sent"
        self._event("reply", ReplyDeliveryResult("sent"))
