# core/pending_confirmation.py
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Must comfortably exceed Gemini's Live-session rotation interval (~2.5 min).
# At the old 120s a rotation could outlive the pending confirmation, so a user
# answering "yes" after Era reconnected found the action already expired and got
# a RuntimeError instead of the action running. The confirmation is single-slot
# and still requires an explicit "yes", so a longer window costs no safety.
_PENDING_CONFIRMATION_TTL_SECONDS = 600


@dataclass
class PendingConfirmation:
    tool: str
    parameters: dict
    risk_level: str
    reason: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(
        default_factory=lambda: time.time() + _PENDING_CONFIRMATION_TTL_SECONDS
    )
    confirmation_id: str = field(default_factory=lambda: secrets.token_hex(4))


class PendingConfirmationBusy(RuntimeError):
    """A live, unanswered confirmation already occupies the single slot.

    Raised instead of overwriting it. `confirm(allow_sole=True)` — what lets a
    spoken "yes" work without the model repeating an id — rests entirely on the
    slot holding the action the user was actually asked about. A second
    `store()` broke that premise silently: two risky calls in ONE model turn
    (tool_batch runs them sequentially) both stored, the user heard the first
    question, and "yes" consumed the second. Refusing the second call keeps the
    first, which is the one the user is answering.
    """

    def __init__(self, pending: PendingConfirmation):
        super().__init__(
            f"a confirmation for {pending.tool} is already waiting for the user's answer"
        )
        self.pending = pending


BUSY_CONFIRMATION_MESSAGE = (
    "Something is already waiting for your answer, sir. Say yes or no to that first, or say cancel."
)

# Model-facing: a confirm the USER did not give (app/era_live/tool_dispatch.py
# _user_approved_pending, and the same-batch check in tool_batch.py).
NOT_USER_APPROVED_MESSAGE = (
    "Not executed: the user has not approved this. Ask them out loud and wait for "
    "their answer; do not confirm on their behalf."
)


class PendingConfirmationStore:
    def __init__(self, ttl: float = _PENDING_CONFIRMATION_TTL_SECONDS):
        self._pending: PendingConfirmation | None = None
        self._lock = threading.Lock()
        self._ttl = ttl

    def store(
        self,
        tool: str,
        parameters: dict,
        risk_level: str,
        reason: str,
        *,
        replace: bool = False,
    ) -> str:
        """Arm the single confirmation slot and return its id.

        Raises PendingConfirmationBusy when a live confirmation is already
        waiting, unless `replace=True`. The old behaviour — overwrite, always —
        is what let one model turn swap what a spoken "yes" would approve.
        """
        with self._lock:
            now = time.time()
            existing = self._pending
            if existing is not None and now <= existing.expires_at and not replace:
                raise PendingConfirmationBusy(existing)
            pending = PendingConfirmation(
                tool=tool,
                parameters=parameters,
                risk_level=risk_level,
                reason=reason,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._pending = pending
            return pending.confirmation_id

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        with self._lock:
            if self._pending is None:
                return None
            if self._pending.confirmation_id != confirmation_id:
                return None
            if time.time() > self._pending.expires_at:
                self._pending = None
                return None
            return self._pending

    def confirm(
        self, confirmation_id: str, *, allow_sole: bool = False
    ) -> PendingConfirmation | None:
        """Consume the pending confirmation.

        Strict by default: the id must match. With ``allow_sole=True`` a single
        live pending confirmation is consumed even when the id is empty or does
        not match — the store is single-slot, so there is never ambiguity about
        *which* action is being confirmed. This is what lets a spoken "yes" (and
        model-invented confirmation ids) still confirm the one action in flight.
        """
        with self._lock:
            if self._pending is None:
                return None
            if time.time() > self._pending.expires_at:
                self._pending = None
                return None
            if self._pending.confirmation_id != confirmation_id and not allow_sole:
                return None
            pending = self._pending
            self._pending = None
            return pending

    def cancel(self, confirmation_id: str) -> bool:
        with self._lock:
            if self._pending is None:
                return False
            if self._pending.confirmation_id != confirmation_id:
                return False
            self._pending = None
            return True

    def expire_all(self) -> int:
        with self._lock:
            if self._pending is None:
                return 0
            if time.time() > self._pending.expires_at:
                self._pending = None
                return 1
            return 0

    def is_pending(self) -> bool:
        with self._lock:
            if self._pending is None:
                return False
            if time.time() > self._pending.expires_at:
                self._pending = None
                return False
            return True

    def peek(self, confirmation_id: str = "") -> PendingConfirmation | None:
        with self._lock:
            if self._pending is None:
                return None
            if confirmation_id and self._pending.confirmation_id != confirmation_id:
                return None
            if time.time() > self._pending.expires_at:
                self._pending = None
                return None
            return self._pending

    def clear(self) -> None:
        with self._lock:
            self._pending = None


# ── Process-wide shared store ───────────────────────────────────────────
# Both the live session (EraLive) and the autonomous agent (AgentExecutor)
# must share ONE confirmation store. Previously each created its own instance,
# so a confirmation created on one path could not be confirmed on the other —
# observed in production as `confirm_pending_confirmation` RuntimeErrors. Any
# code needing the store should call get_shared_store().
_shared_store: PendingConfirmationStore | None = None
_shared_store_lock = threading.Lock()


def get_shared_store() -> PendingConfirmationStore:
    """Return the single process-wide PendingConfirmationStore."""
    global _shared_store
    if _shared_store is None:
        with _shared_store_lock:
            if _shared_store is None:
                _shared_store = PendingConfirmationStore()
    return _shared_store


# ── Who may say yes ─────────────────────────────────────────────────────
# The slot is shared by the live session and the autonomous agent (above), which is
# exactly why CONSUMING it needs its own proof that the USER approved: a staged
# approval — a crew-armed replace, any HIGH-risk action — must not be consumable by an
# unrelated agent path that merely emits `confirm_pending_confirmation` inside the
# confirm window. EraLive registers the one real probe (it owns what the user said:
# app/era_live/tool_dispatch._user_approved_pending). With NO probe — headless, a
# test — nobody can approve: this fails CLOSED.
_user_approval_probe: Callable[[PendingConfirmation], bool] | None = None


def set_user_approval_probe(probe: Callable[[PendingConfirmation], bool] | None) -> None:
    global _user_approval_probe
    _user_approval_probe = probe


def user_approved(pending: PendingConfirmation | None) -> bool:
    """True only when the registered probe says the USER approved `pending`."""
    probe = _user_approval_probe
    if probe is None or pending is None:
        return False
    try:
        return bool(probe(pending))
    except Exception:
        return False
