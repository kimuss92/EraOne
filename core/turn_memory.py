"""Interrupted-turn memory for contextual resumption (Phase 2 barge-in).

When the user cuts Era off mid-sentence, we stash WHAT she was saying so the
thread is not simply lost. She can then pick it back up — because the user asks
("go on", "where were you"), or, if enabled, because Era briefly offers once the
interrupting request is handled.

Pure and dependency-free (capture / peek / take / clear + TTL expiry) so the
logic is unit-testable without audio or a live session. Holds exactly ONE
interrupted turn: a second interruption overwrites the first, because a majordomo
juggling two dropped threads at once is more confusing than helpful.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Below this many characters of (spoken + unspoken) text, the captured thought is
# too thin to be worth resuming ("As I—") — stashing it would only produce an
# awkward offer to continue almost nothing.
_MIN_SUBSTANTIVE_CHARS = 40

# How long a stashed thought stays offerable. After this the moment has passed;
# resuming a two-minute-old aside unprompted feels non-sequitur, not attentive.
DEFAULT_TTL_S = 120.0


@dataclass
class InterruptedTurn:
    spoken: str  # what Era had already said this turn before being cut off
    remainder: str  # text produced but not yet voiced (local voice only); "" otherwise
    ts: float
    offered: bool = False  # set once Era has proactively offered to resume it
    # WHO cut the turn off. "user" is a barge-in: the user chose to take the
    # floor, so Era waits to be asked before picking the thread back up.
    # "disconnect" is the session dying underneath her mid-sentence — nobody
    # chose that, so the reconnect resumes it automatically (see
    # EraLive._resume_after_disconnect). The two must stay distinguishable:
    # auto-resuming a barge-in would talk straight over the user.
    cut_by: str = "user"
    # The user line that was still awaiting an answer when the turn died. Set
    # only for the disconnect case, and the reason a drop BEFORE Era got a word
    # out is still resumable — there is no `spoken` text to carry, but the
    # question is just as unanswered.
    pending_user: str = ""

    def topic(self, max_words: int = 8) -> str:
        """A short label for the thread, for logs/UI — never spoken verbatim."""
        words = self.spoken.split()
        head = " ".join(words[:max_words])
        return head + ("…" if len(words) > max_words else "")


class TurnMemory:
    """Single-slot store for the most recent interrupted turn."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._ttl_s = float(ttl_s)
        self._turn: InterruptedTurn | None = None

    def capture(
        self,
        *,
        spoken: str,
        remainder: str = "",
        now: float | None = None,
        cut_by: str = "user",
        pending_user: str = "",
    ) -> bool:
        """Stash an interrupted turn. Returns False (and stashes nothing) when
        there was too little substance to be worth resuming.

        `pending_user` counts toward that substance test on purpose: a drop that
        lands between "what did the report say?" and Era's first word leaves
        `spoken` empty, and the old test would have thrown away the one thing
        actually worth resuming.
        """
        spoken = (spoken or "").strip()
        remainder = (remainder or "").strip()
        pending_user = (pending_user or "").strip()
        if len(spoken) + len(remainder) + len(pending_user) < _MIN_SUBSTANTIVE_CHARS:
            return False
        self._turn = InterruptedTurn(
            spoken=spoken,
            remainder=remainder,
            ts=now if now is not None else time.time(),
            cut_by=cut_by,
            pending_user=pending_user,
        )
        return True

    def peek(self, now: float | None = None) -> InterruptedTurn | None:
        """The pending turn, or None if absent or expired. Expiry is lazy: a
        stale turn is dropped on the read that discovers it."""
        if self._turn is None:
            return None
        clock = now if now is not None else time.time()
        if clock - self._turn.ts > self._ttl_s:
            self._turn = None
            return None
        return self._turn

    def take(self, now: float | None = None) -> InterruptedTurn | None:
        """Return the pending turn and clear the slot (resume consumes it)."""
        turn = self.peek(now)
        self._turn = None
        return turn

    def mark_offered(self) -> None:
        if self._turn is not None:
            self._turn.offered = True

    def clear(self) -> None:
        self._turn = None

    def has_pending(self, now: float | None = None) -> bool:
        return self.peek(now) is not None
