"""Idempotency ledger for model-requested tool execution.

WHY THIS EXISTS
---------------
A model asks for a tool; a deterministic layer executes it. If that request is
ever *retried* (same generation replayed after a transport error) or
*transferred* (Gemini's turn re-run on Claude), the same tool request can arrive
twice. For a read it costs a round trip. For `send_message`, `file_controller`
delete, or `computer_settings` shutdown it is a second real-world action the
user asked for once.

This ledger is the record of "what has already been done for this request", so
the execution layer can refuse the second one and hand back the first one's
result instead.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not dedupe by wall-clock time. "Volume up, volume up" is a legitimate
double execution, and a TTL-keyed content hash would eat the second one. Keys
are therefore scoped to a *request*, and within a request an `ordinal`
distinguishes "the model genuinely asked twice" (ordinals 0 and 1, both run)
from "the same single call arrived twice" (ordinal 0 both times, second refused).

A FAILED execution is retryable. `fail()` reopens the key, because a transient
error must not poison an action permanently — that would be a worse bug than the
one this module prevents.

State is per-process and in-memory. It covers retry and model-transfer, which
both happen inside one run. It does not survive a crash, and does not claim to:
a durable ledger would need to fsync before every action, which the live voice
path cannot afford.

No Era imports — this stays provider- and application-agnostic so the routing
layer can use it for any model.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# Claim statuses returned by begin().
NEW = "new"  # nothing has run for this key — caller should execute
DUPLICATE = "duplicate"  # already completed — caller must NOT execute
IN_FLIGHT = "in_flight"  # a concurrent caller is executing it right now

# Internal entry states.
_STATE_RUNNING = "running"
_STATE_COMPLETED = "completed"
_STATE_FAILED = "failed"

DEFAULT_MAX_REQUESTS = 64
DEFAULT_TTL_SECONDS = 900.0

_UNSET = object()


def canonical_args(args: Any) -> str:
    """Stable text form of a tool's arguments, for hashing.

    Never raises. A ledger that threw on an odd argument would take down the
    very tool call it exists to protect.

    When a value cannot be canonicalized at all — `json.dumps` fails AND its
    own `repr` raises — this returns a value that is UNIQUE per call rather
    than a shared placeholder. That is deliberate: a shared placeholder would
    make two *different* uncanonicalizable calls hash alike, and the ledger
    would suppress the second one as a duplicate of the first. Failing open
    (always execute) is the only safe direction to be wrong in.
    """
    try:
        return json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        pass
    try:
        return repr(args)
    except Exception:
        return f"<uncanonicalizable:{uuid.uuid4().hex}>"


def signature(tool_name: str, args: Any) -> str:
    """Content hash identifying 'this tool with these arguments'."""
    blob = f"{tool_name}\x00{canonical_args(args)}"
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def assign_ordinals(calls: Sequence[tuple[str, Any]]) -> list[int]:
    """Per-signature occurrence index for a batch of (tool_name, args) pairs.

    Positional index is the wrong key: a batch of [volume_up, mute, volume_up]
    replayed identically must map call 2 back to call 2, and it does so here
    (ordinals 0, 0, 1) regardless of what other tools sit between them.
    """
    seen: dict[str, int] = {}
    ordinals: list[int] = []
    for tool_name, args in calls:
        sig = signature(tool_name, args)
        ordinals.append(seen.get(sig, 0))
        seen[sig] = seen.get(sig, 0) + 1
    return ordinals


def derive_request_id(call_ids: Iterable[Any], *, fallback: str = "anonymous") -> str:
    """Identify a request from the provider's own per-call ids.

    A replayed message carries the same ids, so it maps to the same request and
    dedupes. A freshly generated turn carries new ids and is free to act again —
    which is what makes a genuine re-ask work. Callers that already have a
    request identity (a router retrying across providers) should pass their own
    rather than rely on this.
    """
    ids = [str(cid) for cid in call_ids if cid not in (None, "")]
    if not ids:
        return fallback
    return hashlib.sha256("\x00".join(sorted(ids)).encode("utf-8", "replace")).hexdigest()[:32]


@dataclass(frozen=True)
class Claim:
    """The ledger's answer to 'may I run this?'."""

    request_id: str
    key: str
    tool_name: str
    ordinal: int
    status: str
    cached_result: Any = None
    attempts: int = 1

    @property
    def execute(self) -> bool:
        """True only for a key nothing has run yet."""
        return self.status == NEW

    @property
    def short_key(self) -> str:
        return self.key[:12]


@dataclass
class _Entry:
    key: str
    tool_name: str
    ordinal: int
    state: str
    started_at: float
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    attempts: int = 1


@dataclass
class _Request:
    request_id: str
    created_at: float
    entries: dict[str, _Entry] = field(default_factory=dict)


class ExecLedger:
    """Thread-safe, bounded, in-memory record of executed tool calls."""

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._max_requests = max_requests
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._requests: OrderedDict[str, _Request] = OrderedDict()
        self._counters = {
            "executed": 0,
            "duplicates": 0,
            "in_flight_hits": 0,
            "failures": 0,
            "evicted_requests": 0,
            "expired_requests": 0,
            "stale_completions": 0,
        }

    # ── public API ────────────────────────────────────────────────────────

    def begin(self, request_id: str, tool_name: str, args: Any, *, ordinal: int = 0) -> Claim:
        """Claim the right to execute one tool call.

        Callers replaying a batch MUST pass the ordinal from
        :func:`assign_ordinals` over that same batch; leaving it at 0 would let
        a second genuine call in one batch look like a duplicate of the first.
        """
        key = f"{signature(tool_name, args)}:{ordinal}"
        with self._lock:
            self._prune()
            request = self._requests.get(request_id)
            if request is None:
                request = _Request(request_id=request_id, created_at=self._clock())
                self._requests[request_id] = request
            self._requests.move_to_end(request_id)

            entry = request.entries.get(key)
            if entry is None:
                request.entries[key] = _Entry(
                    key=key,
                    tool_name=tool_name,
                    ordinal=ordinal,
                    state=_STATE_RUNNING,
                    started_at=self._clock(),
                )
                self._counters["executed"] += 1
                return Claim(request_id, key, tool_name, ordinal, NEW)

            if entry.state == _STATE_COMPLETED:
                self._counters["duplicates"] += 1
                return Claim(
                    request_id,
                    key,
                    tool_name,
                    ordinal,
                    DUPLICATE,
                    cached_result=entry.result,
                    attempts=entry.attempts,
                )

            if entry.state == _STATE_RUNNING:
                self._counters["in_flight_hits"] += 1
                return Claim(
                    request_id, key, tool_name, ordinal, IN_FLIGHT, attempts=entry.attempts
                )

            # _STATE_FAILED — reopen. A transient error must stay retryable.
            entry.state = _STATE_RUNNING
            entry.started_at = self._clock()
            entry.finished_at = None
            entry.error = None
            entry.attempts += 1
            self._counters["executed"] += 1
            return Claim(request_id, key, tool_name, ordinal, NEW, attempts=entry.attempts)

    def complete(self, claim: Claim, result: Any = _UNSET) -> None:
        """Record a successful execution and the result to replay to duplicates."""
        with self._lock:
            entry = self._entry_for(claim)
            if entry is None or entry.state != _STATE_RUNNING:
                self._counters["stale_completions"] += 1
                return
            entry.state = _STATE_COMPLETED
            entry.finished_at = self._clock()
            entry.result = None if result is _UNSET else result

    def fail(self, claim: Claim, error: Any = None) -> None:
        """Record a failure, reopening the key so a retry may execute."""
        with self._lock:
            entry = self._entry_for(claim)
            if entry is None or entry.state != _STATE_RUNNING:
                self._counters["stale_completions"] += 1
                return
            entry.state = _STATE_FAILED
            entry.finished_at = self._clock()
            entry.error = None if error is None else str(error)[:500]
            self._counters["failures"] += 1

    def forget(self, request_id: str) -> bool:
        """Drop one request's record. Returns True if something was dropped."""
        with self._lock:
            return self._requests.pop(request_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            out = dict(self._counters)
            out["tracked_requests"] = len(self._requests)
            out["tracked_calls"] = sum(len(r.entries) for r in self._requests.values())
            return out

    def describe(self, request_id: str) -> list[dict[str, Any]]:
        """Human-readable view of one request, for diagnostics and logs."""
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                return []
            return [
                {
                    "tool": e.tool_name,
                    "ordinal": e.ordinal,
                    "state": e.state,
                    "attempts": e.attempts,
                    "error": e.error,
                }
                for e in request.entries.values()
            ]

    # ── internals ─────────────────────────────────────────────────────────

    def _entry_for(self, claim: Claim) -> _Entry | None:
        request = self._requests.get(claim.request_id)
        if request is None:
            return None
        return request.entries.get(claim.key)

    def _prune(self) -> None:
        """Expire old requests, then cap how many are retained."""
        now = self._clock()
        expired = [rid for rid, r in self._requests.items() if now - r.created_at > self._ttl]
        for rid in expired:
            del self._requests[rid]
            self._counters["expired_requests"] += 1
        while len(self._requests) > self._max_requests:
            self._requests.popitem(last=False)
            self._counters["evicted_requests"] += 1
