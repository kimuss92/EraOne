"""core/panic_stop.py — "abort mission": the order that always wins.

WHY THIS EXISTS
---------------
On 2026-09-21 Era got stuck in a rapid tool loop (six identical
`browser_control close_tab netflix.com` calls 1.2s apart; five `gmail_control
send` calls inside 77ms). The user said "abort mission". Nothing happened,
because nothing in the codebase had ever listened for it — the only working
escape hatches were "reboot Era One" and the UI mic toggle, and the user had to
use the first one.

Worse, Era had saved a memory claiming "Panic Protocol implemented to stop
runaway execution and provide 'abort mission' control". That note described a
fix that did not exist. This module is that fix, written for real.

THE MODEL
---------
An abort is NOT a tool call. Routing it through the model is precisely the
mistake: the situation it exists for is the one where the model is the thing
that has gone wrong — looping, ignoring the transcript, or queueing a batch
faster than a human can finish a sentence. So the order is matched on the raw
transcript / typed text, ahead of the model, exactly like "reboot Era One".

Three things happen, in this order:

  1. The latch trips. `PanicStop.active()` is process-wide and thread-safe, so
     the tool-batch loop refuses every remaining call in the batch it is
     already executing — including calls the model queued before the user
     spoke.
  2. Era stops talking (barge-in) and the queued audio is dropped.
  3. Pending confirmations and stashed turns are cleared, so nothing
     half-finished can be resumed by a later "yes".

The latch then auto-clears after `DEFAULT_TTL_S`. It is a brake, not a mode:
a latch with no expiry is one crash away from a permanently deaf Era, which is
a worse failure than the one it prevents.

WHAT IS *NOT* AN ABORT ORDER
----------------------------
A bare "stop" / "cancel" / "abort" is deliberately excluded. Those already mean
other things the user relies on:

  * "stop" is a media command (`SAFE_SHORT_COMMANDS` in input_safety_gate).
  * "stop"/"cancel"/"abort" are confirmation answers (`CONFIRMATION_REPLIES`).
  * "cancel the shutdown" aborts the power countdown (`_POWER_ABORT_ACTIONS`
    in safety_guard) and must keep doing exactly that.

So an abort order must carry an explicit *scope* word — "mission",
"everything", "all of it" — or be an unmistakable panic phrase ("abort abort",
"emergency stop"). Requiring the scope word is what keeps "stop" pausing the
music instead of freezing the assistant.

Standard library only: this has to work when the SDK, the network and the model
are all unavailable.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

# How long the latch stays tripped. Long enough to drain a runaway batch and
# for the model's next turn to be refused; short enough that a lost `clear()`
# cannot leave Era permanently unable to act.
DEFAULT_TTL_S = 30.0

# Spoken/typed acknowledgement — pinned verbatim, like MIC_OFF_ACK. Short on
# purpose: the user is trying to make Era shut up, so the confirmation that she
# has must not be a paragraph.
ABORT_ACK = "Aborted. Standing by."

# Model-facing text for a call refused by the latch. Era must not narrate these
# one by one — the user already got ABORT_ACK, and reading out twelve refusals
# would recreate the noise the abort was meant to end.
ABORT_BLOCK_MESSAGE = (
    "Not executed: the user said to abort. Stop what you were doing, run nothing "
    "further, and say nothing about this."
)

# Words allowed BEFORE the order: a greeting or the wake word, nothing else.
_PREFIX_WORDS = ("hey", "ok", "okay", "era", "era one", "please", "yo")

# The scope that separates "abort the mission" from "stop the music".
_ALL = r"(?:everything|every\s*thing|all\s+of\s+it|all\s+that|it\s+all|all)"
_MISSION = r"(?:mission|operation|op)"

_ORDERS = (
    # "abort mission" and its near neighbours
    rf"abort\s+(?:the\s+)?{_MISSION}",
    rf"abort\s+{_ALL}",
    r"abort\W+abort(?:\W+abort)*",  # the classic doubled panic call
    r"mission\s+abort(?:ed)?",
    # "stop everything" family
    rf"(?:stop|halt|cease|kill|drop|cancel)\s+{_ALL}",
    r"(?:stop|halt|cease)\s+(?:what\s+you(?:'re|\s+are)\s+doing|doing\s+that)",
    r"stop\s+stop(?:\s+stop)*",
    # unmistakable panic phrases — no scope word needed, nothing else uses them
    r"(?:emergency|hard|full)\s+stop",
    r"panic\s+(?:stop|button)",
    r"freeze\W*(?:all)?",
    r"stand\s+down",
    # French — the owner switches languages mid-sentence.
    r"(?:arr[eê]te|arr[eê]tez|stoppe|annule)\s+tout",
    r"abandonne\s+(?:la\s+)?mission",
    r"arr[eê]t\s+d'?urgence",
)

# A power-countdown cancel is a DIFFERENT order that must keep its own meaning
# ("cancel the shutdown" stops the reboot, it does not freeze the assistant).
_POWER_WORDS = re.compile(
    r"\b(?:shut\s*down|shutdown|power\s*off|restart|reboot|hibernate|log\s*off"
    r"|[ée]tein\w*|red[ée]marr\w*)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=8)
def _order_pattern_cached(words: tuple[str, ...]) -> re.Pattern:
    """Built once per distinct wake-word set.

    `is_abort_order` runs on every partial transcription chunk — several times
    a second while the user is talking — so the pattern must not be rebuilt
    each call. `re`'s own cache would usually absorb it, but it is bounded and
    shared with the whole process; this one is not.
    """
    prefix = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
    body = "|".join(_ORDERS)
    return re.compile(rf"^\W*(?:(?:{prefix})\W+)*(?:{body})\W*$", re.IGNORECASE)


def _order_pattern(wake_words: Iterable[str] = ()) -> re.Pattern:
    words = [w.strip().lower() for w in (*_PREFIX_WORDS, *wake_words) if w and w.strip()]
    return _order_pattern_cached(tuple(sorted(set(words))))


def is_abort_order(text: str, wake_words: Iterable[str] = ()) -> bool:
    """True only when the WHOLE utterance is an abort order.

    Anchored to the whole message for the same reason `is_reboot_command` is:
    "abort mission" is an order, "why did you abort the mission" is
    conversation, and mistaking the second for the first would cut Era off
    mid-answer. Tolerates a leading greeting or wake word and whatever
    punctuation speech-to-text attaches.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if _POWER_WORDS.search(t):
        # "cancel the shutdown" / "abort the reboot" — a power-countdown cancel,
        # handled by safety_guard's _POWER_ABORT_ACTIONS. Not this.
        return False
    return _order_pattern(wake_words).match(t) is not None


@dataclass(frozen=True)
class AbortRecord:
    """What tripped the latch, for the log and the UI line."""

    source: str
    text: str
    at: float
    expires_at: float


class PanicStop:
    """Process-wide "stop now" latch.

    Read from the asyncio event loop (the tool-batch loop), the audio callback
    thread and the Qt thread, so every access takes the lock. `active()` is on
    the hot path of every tool call — it is a lock acquire and two float
    compares, which is nothing next to the tool it guards.
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._lock = threading.Lock()
        self._ttl = ttl_s
        self._record: AbortRecord | None = None
        self._refused = 0

    def trip(self, source: str, text: str = "", now: float | None = None) -> AbortRecord:
        """Latch the abort. Idempotent in effect, but each call re-arms the TTL.

        Re-arming matters: a user who says "abort" twice because the first one
        did not visibly land must not have the second one shorten the window.
        """
        t = now if now is not None else time.time()
        with self._lock:
            self._record = AbortRecord(
                source=source, text=(text or "").strip(), at=t, expires_at=t + self._ttl
            )
            self._refused = 0
            return self._record

    def active(self, now: float | None = None) -> bool:
        t = now if now is not None else time.time()
        with self._lock:
            rec = self._record
            if rec is None:
                return False
            if t >= rec.expires_at:
                self._record = None
                return False
            return True

    def note_refusal(self) -> int:
        """Count a tool call the latch turned away. Returns the running total."""
        with self._lock:
            self._refused += 1
            return self._refused

    def clear(self, source: str = "") -> AbortRecord | None:
        """Release the latch early. Returns what was cleared, or None."""
        with self._lock:
            rec, self._record, self._refused = self._record, None, 0
            return rec

    def snapshot(self, now: float | None = None) -> dict:
        t = now if now is not None else time.time()
        with self._lock:
            rec = self._record
            if rec is None or t >= rec.expires_at:
                return {"active": False, "refused": 0}
            return {
                "active": True,
                "source": rec.source,
                "text": rec.text,
                "refused": self._refused,
                "remaining_s": round(rec.expires_at - t, 1),
            }


# The one latch every call site shares. A per-instance latch would let a tool
# running on a worker thread miss an abort tripped on the event loop.
panic = PanicStop()
