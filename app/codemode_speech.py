"""Sentence streaming through the existing offline speaker when Live is absent."""

import asyncio

from app.era_live.speech_out import split_sentences


class LocalMouth:
    def __init__(self, era):
        self.era = era
        self.buffer = ""
        self.queue = asyncio.Queue(maxsize=64)
        self.task = asyncio.create_task(self._play())

    def feed(self, text, *, final=False):
        sentences, self.buffer = split_sentences(self.buffer + text, final=final)
        for sentence in sentences:
            self.queue.put_nowait(sentence)

    def interrupt(self):
        self.task.get_loop().call_soon_threadsafe(self.task.cancel)
        self.buffer = ""

    async def _play(self):
        while True:
            sentence = await self.queue.get()
            try:
                await self.era._local_say(sentence)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.era.ui.write_log("SYS: CodeMode voice unavailable: " + type(exc).__name__)


def local_mouth(era, *, fresh=False):
    if not getattr(era, "local_mode_active", lambda: False)():
        old = getattr(era, "_codemode_local_mouth", None)
        if old is not None and not old.task.done():
            old.interrupt()
        return None
    mouth = getattr(era, "_codemode_local_mouth", None)
    if fresh or mouth is None or mouth.task.done():
        if mouth is not None:
            mouth.interrupt()
        mouth = era._codemode_local_mouth = LocalMouth(era)
    return mouth
