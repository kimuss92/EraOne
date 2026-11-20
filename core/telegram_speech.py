"""Connect explicitly owned local speech to the Telegram delivery worker."""

import asyncio
from collections.abc import Callable

from core.input_envelope import InputEnvelope
from core.speech_frames import SpeechPhrase
from core.telegram_reply import TelegramReply


class TelegramSpeech:
    def __init__(self, factory: Callable[[InputEnvelope], TelegramReply]):
        self._factory = factory
        self._owner: InputEnvelope | None = None
        self._reply: TelegramReply | None = None
        self._phrases: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def pending_owner(self) -> InputEnvelope | None:
        return self._owner if self._reply is not None and self._reply.outcome == "pending" else None

    def _schedule(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def done(completed):
            self._tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                print("[TG] Owned speech delivery worker failed; outcome unconfirmed.")

        task.add_done_callback(done)

    def select(self, owner: InputEnvelope | None, *, reason: str = "interrupted") -> None:
        """Invalidate old frames synchronously before cancellation is awaited."""
        if self._owner is not None and owner is not None and self._owner.id == owner.id:
            return
        reply, self._reply = self._reply, None
        self._owner = owner
        self._phrases.clear()
        if reply is not None:
            self._schedule(reply.cancel(reason=reason))

    def begin(self, owner: InputEnvelope, *, reuse: bool = False) -> bool:
        if owner.source != "telegram" or self._owner is None or self._owner.id != owner.id:
            return False
        # Only one direct utterance may claim this request, avoiding duplicate finals.
        if self._reply is not None:
            return reuse and self._reply.is_open
        self._reply = self._factory(owner)
        return True

    def event(self, phrase: SpeechPhrase, status: str) -> None:
        owner = phrase.owner
        if (
            owner is None
            or self._owner is None
            or owner.id != self._owner.id
            or self._reply is None
        ):
            return
        if status == "started" and phrase.id not in self._phrases:
            self._phrases.add(phrase.id)
            try:
                self._reply.reveal(phrase.text)
            except (ValueError, RuntimeError):
                self.select(None, reason="reply_capacity_or_closed")
        elif status in ("failed", "interrupted"):
            self.select(None, reason="playback_failed" if status == "failed" else "interrupted")

    def end(self, owner: InputEnvelope) -> None:
        if self._owner is not None and self._owner.id == owner.id and self._reply is not None:
            self._schedule(self._reply.finish())
