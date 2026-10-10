"""Per-task cancel tokens for work already in flight.

WHY THIS EXISTS
---------------
"Abort mission" (core/panic_stop.py) is a brake on the *next* tool call: the
latch is re-read per call in tool_batch.py, so it refuses everything the model
queued but has not started. It cannot touch the call that is already running,
and ~45 of the branches in `_execute_tool_raw` are
`await loop.run_in_executor(None, lambda: blocking_fn(...))` -- a thread Python
cannot interrupt. So a 45-second Spotify UIA search, a 10-second web search or
a 76-second code_helper run to completion no matter what the owner says.

This module is the missing half: one token per in-flight task, keyed by the
HUD call id (the only task id that already reaches the browser), carrying

  * a `threading.Event` for cooperative ("hard") cancellation -- poll loops
    that already exist inside actions/*.py check it and bail within one step;
  * a cancelled-at stamp that survives teardown, so a worker thread which
    finally returns 30 seconds later can have its result discarded ("soft"
    cancellation, the same trick SpeechOut.interrupt uses for in-flight TTS).

Separate from the three cancel flags that already exist (agent/task_queue.py's
Task.cancel_flag, core/local_brain.py's _cancel, autopilot's task set): those
are per-lane and unreachable from the live model's tool path, which is exactly
the gap. Nothing here unifies them.

THE CONSTRAINT THAT SHAPES EVERYTHING HERE
------------------------------------------
`loop.run_in_executor` does NOT propagate contextvars -- only
`asyncio.to_thread` does, via `copy_context()`. A ContextVar token is therefore
invisible inside exactly the worker threads that need it. So the token travels
to the thread through `offload()`, which binds it into a `threading.local` at
the executor boundary. Everything an action module calls (`cancelled()`,
`sleep_cancellable()`) reads that thread-local, never a ContextVar.

`cancelled()` FAILS OPEN -- it returns False when nothing is bound. A tool
called from a test, from the scheduler or from the CM lane behaves exactly as
it did before this module existed.

Standard library only, and no Era imports: `actions/*.py` must be able to use
this without importing `app.era_live`.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# How long a cancelled cid is remembered after teardown. Long enough that the
# slowest measured uninterruptible tool (code_helper, 76s) still finds its own
# tombstone when it returns; short enough to stay a fixed-size structure.
TOMBSTONE_TTL_S = 180.0
_TOMBSTONE_MAX = 64

# Default slice for sleep_cancellable: the latency the owner feels between
# saying "cancel that" and the machine actually stopping.
DEFAULT_SLEEP_STEP_S = 0.2

_gen_counter = itertools.count(1)


class CancelledTask(Exception):
    """Raised by raise_if_cancelled() for call stacks too deep to return a flag."""


@dataclass
class CancelToken:
    """One in-flight task. `cid` is the HUD call id, so the UI can address it."""

    cid: str
    tool: str
    generation: int
    started_at: float
    event: threading.Event = field(default_factory=threading.Event)
    label: str = ""
    source: str = ""  # who cancelled: hud | voice | vosk | correction | abort
    reason: str = ""
    task: Any = None  # asyncio.Task, once detached (set by attach_task)
    cancelled_at: float | None = None
    # Set when THIS call armed the single confirmation slot. Cancelling the
    # call must release the slot, or core/pending_confirmation.py (single-slot
    # by design) refuses the owner's next action as busy -- and worse, a later
    # "yes" meant for something else runs the abandoned one.
    confirmation_id: str = ""

    def cancelled(self) -> bool:
        return self.event.is_set()

    def age_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def public(self) -> dict:
        """The shape the HUD and the logs want. Never the raw args."""
        return {
            "cid": self.cid,
            "tool": self.tool,
            "label": self.label,
            "ageMs": self.age_ms(),
            "cancelled": self.cancelled(),
        }


class CancelRegistry:
    """Every task currently in flight, plus a short memory of cancelled ones.

    Thread-safe with an RLock because the callers sit on different threads:
    registration on the engine loop, `cancel()` from an aiohttp handler thread
    (both websocket_server._spawn and HudApi._call spawn one) or the mic
    thread, and `is_cancelled()` from executor worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._live: OrderedDict[str, CancelToken] = OrderedDict()
        self._recent: deque[tuple[str, float]] = deque(maxlen=_TOMBSTONE_MAX)

    # ── lifecycle ────────────────────────────────────────────────────────
    def register(self, cid: str, tool: str, *, label: str = "") -> CancelToken:
        token = CancelToken(
            cid=cid,
            tool=tool,
            generation=next(_gen_counter),
            started_at=time.monotonic(),
            label=label or tool,
        )
        with self._lock:
            self._live[cid] = token
        return token

    def attach_task(self, cid: str, task: Any) -> None:
        """Record the asyncio task of work that detached from the receive loop.

        Only a detached task is worth cancelling at the asyncio level; an
        inline one is awaited by the batch loop, which must stay intact.
        """
        with self._lock:
            token = self._live.get(cid)
            if token is not None:
                token.task = task

    def finish(self, cid: str) -> None:
        """Teardown. Idempotent -- a cancel and a natural finish can race."""
        with self._lock:
            self._live.pop(cid, None)

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, cid: str) -> CancelToken | None:
        with self._lock:
            return self._live.get(cid)

    def is_cancelled(self, cid: str) -> bool:
        """True while the task is cancelled AND for TOMBSTONE_TTL_S after it.

        The tombstone is the whole soft-cancel contract: an uninterruptible
        worker that returns long after the user moved on must be able to learn
        that its result is unwanted, even though its token is gone.
        """
        if not cid:
            return False
        with self._lock:
            token = self._live.get(cid)
            if token is not None and token.cancelled():
                return True
            cutoff = time.monotonic() - TOMBSTONE_TTL_S
            return any(c == cid and at >= cutoff for c, at in self._recent)

    def note_cancelled_id(self, other_id: str) -> None:
        """Record a second id that identifies the same cancelled work.

        A task is keyed by its HUD row id, but `hud.tool_started` mints that id
        and may rewrite it (a call with no id of its own, or a collision with a
        row still running). So the provider's own call id is NOT reliably the
        same string -- and the batch loop in tool_batch.py only has that one.
        Registering it as an alias lets `is_cancelled()` answer for either.
        """
        if other_id:
            with self._lock:
                self._recent.append((other_id, time.monotonic()))

    def active(self) -> list[CancelToken]:
        """Live tasks not yet cancelled -- i.e. what is still worth cancelling."""
        with self._lock:
            return [t for t in self._live.values() if not t.cancelled()]

    def active_public(self) -> list[dict]:
        return [t.public() for t in self.active()]

    # ── cancellation ─────────────────────────────────────────────────────
    def cancel(self, cid: str, *, source: str, reason: str = "") -> CancelToken | None:
        """Flag one task. Returns the token, or None if there was nothing to flag.

        The Event is set synchronously on the CALLING thread so the hard checks
        inside a worker see it immediately -- a busy event loop must never be
        able to delay the machine actually stopping.
        """
        with self._lock:
            token = self._live.get(cid)
            if token is None or token.cancelled():
                return None
            token.source = source
            token.reason = reason
            token.cancelled_at = time.monotonic()
            token.event.set()
            self._recent.append((cid, token.cancelled_at))
            return token

    def cancel_newest(self, *, source: str, reason: str = "") -> CancelToken | None:
        """Flag the most recently started live task -- "cancel that"."""
        with self._lock:
            for cid in reversed(self._live):
                if not self._live[cid].cancelled():
                    return self.cancel(cid, source=source, reason=reason)
        return None

    def cancel_all(self, *, source: str, reason: str = "") -> list[CancelToken]:
        with self._lock:
            cids = [cid for cid, t in self._live.items() if not t.cancelled()]
        out = [self.cancel(cid, source=source, reason=reason) for cid in cids]
        return [t for t in out if t is not None]


REGISTRY = CancelRegistry()

# The async half of the token's journey. Declared here rather than in
# tool_dispatch so that offload() can find it without importing app.era_live.
CURRENT_TOKEN: ContextVar[CancelToken | None] = ContextVar("era_cancel_token", default=None)

# The sync half. A thread-local, NOT a ContextVar: see the module docstring.
_BOUND = threading.local()


def current_async_token() -> CancelToken | None:
    return CURRENT_TOKEN.get()


@contextmanager
def bind(token: CancelToken | None):
    """Bind `token` to THIS thread for the duration of the block."""
    previous = getattr(_BOUND, "token", None)
    _BOUND.token = token
    try:
        yield token
    finally:
        _BOUND.token = previous


def current() -> CancelToken | None:
    """The token bound to this thread, or None."""
    return getattr(_BOUND, "token", None)


def cancelled() -> bool:
    """Has the owner cancelled the task this thread is doing?

    Fails OPEN: False when no token is bound, so an action module called from a
    test, the scheduler or the CM lane behaves exactly as it always has.
    """
    token = getattr(_BOUND, "token", None)
    return token is not None and token.event.is_set()


def raise_if_cancelled() -> None:
    if cancelled():
        raise CancelledTask("The user cancelled this task.")


def sleep_cancellable(total_s: float, step: float = DEFAULT_SLEEP_STEP_S) -> bool:
    """time.sleep that returns False as soon as the task is cancelled.

    The drop-in replacement for the hardcoded `time.sleep(2.0)` chains in
    actions/netflix_control.py and the poll intervals in spotify_control.py.
    Uses Event.wait rather than sleep when a token is bound, so a cancel lands
    within milliseconds instead of at the next slice boundary. `step` is kept
    for call-site readability and for callers that pass it positionally.
    """
    token = getattr(_BOUND, "token", None)
    if token is None:
        if total_s > 0:
            time.sleep(total_s)
        return True
    if token.event.is_set():
        return False
    # Event.wait returns True the moment it is set -- no polling latency at all.
    return not token.event.wait(max(0.0, total_s))


async def offload(fn: Callable[[], Any], *, token: CancelToken | None = None) -> Any:
    """run_in_executor(fn) with the cancel token bound INSIDE the worker thread.

    This is the only way the token crosses into a worker, because
    run_in_executor does not copy contextvars. Replaces
    `await loop.run_in_executor(None, lambda: ...)` at the slow dispatch sites.
    """
    loop = asyncio.get_running_loop()
    tok = token if token is not None else CURRENT_TOKEN.get()
    return await loop.run_in_executor(None, functools.partial(_bound_call, tok, fn))


def _bound_call(token: CancelToken | None, fn: Callable[[], Any]) -> Any:
    with bind(token):
        return fn()


async def run_with_grace(coro, grace_s: float) -> tuple[bool, Any, Any]:
    """Await `coro` for `grace_s`, then hand it back still running.

    Returns (finished, result, task):
      finished=True  -- the work completed inline; `result` is real, task None.
      finished=False -- `task` is still running; the caller must answer NOW and
                        arrange for the outcome to be delivered later.

    Unlike crew.runtime.await_with_ack, this RETURNS at the deadline instead of
    acking and then awaiting to completion. Unblocking Gemini's receive loop is
    the entire point: a tool awaited inside `async for ... session.receive()`
    stops the socket being drained, which trips the keepalive (1011) and kills
    the session mid-call.
    """
    task = asyncio.ensure_future(coro)
    try:
        result = await asyncio.wait_for(asyncio.shield(task), grace_s)
        return True, result, None
    except TimeoutError:
        return False, None, task
