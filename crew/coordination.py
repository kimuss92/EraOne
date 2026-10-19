"""One cancellable work owner across a crew turn and its nested handoffs."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from core.cancel_registry import CURRENT_TOKEN, REGISTRY, bind, current, raise_if_cancelled


@dataclass
class TeamWork:
    id: str
    owner: str
    task: str
    participants: set[str] = field(default_factory=set)
    candidate_id: str = ""
    candidate_digest: str = ""
    authority: dict = field(default_factory=dict)
    # Capabilities that put outside-origin text (e.g. log evidence) into this turn.
    # A tainted turn may not stage: staged tests run before the user's approval.
    tainted_by: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def public(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "owner": self.owner,
                "participants": sorted(self.participants),
                "development_id": self.candidate_id,
            }


CURRENT_WORK: ContextVar[TeamWork | None] = ContextVar("crew_work", default=None)


class AlreadyRunning(RuntimeError):
    def __init__(self, cid):
        self.cid = cid


@contextmanager
def owned_turn(bot, turn):
    work = CURRENT_WORK.get()
    if work is not None:
        raise_if_cancelled()
        with work.lock:
            work.participants.add(bot)
        yield work
        return
    token = current() or CURRENT_TOKEN.get()
    owns_claim = token is None
    if owns_claim:
        token, existing = REGISTRY.claim_unique(
            "crew-" + uuid.uuid4().hex,
            "crew",
            {"action": "ask", "bot": bot, "task": turn.task.strip(), "context": turn.context},
        )
        if token is None:
            raise AlreadyRunning(existing.cid)
    work = TeamWork(token.cid, bot, turn.task, {bot})
    reset = CURRENT_WORK.set(work)
    try:
        with bind(token):
            raise_if_cancelled()
            yield work
    finally:
        CURRENT_WORK.reset(reset)
        if owns_claim:
            REGISTRY.finish(token.cid)
