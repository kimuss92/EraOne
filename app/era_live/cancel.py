"""Cancel one task that is already running (model in core/cancel_registry.py).

HOW THIS DIFFERS FROM "ABORT MISSION"
------------------------------------
`app/era_live/panic.py` trips a process-wide latch that refuses EVERY tool for
the next 30 seconds. That is the right tool for "the model has gone mad", and
the wrong one here: the owner who says "no, I meant the next track" wants the
wrong task dropped and the right one run immediately -- which a 30-second
blanket refusal makes impossible. So this is a scalpel, not the brake:
exactly one task, no latch, no TTL, and the very next command runs normally.

WHY THE WORK IS SPLIT ACROSS TWO THREADS
----------------------------------------
Until a slow tool is backgrounded, Era's engine loop is suspended inside
`async for response in session.receive()` for the whole duration of the call.
Anything hopped onto that loop would therefore land AFTER the task it was
meant to cancel had already finished -- the cancel would be a no-op the owner
watched do nothing. So the two things that must be immediate happen on the
caller's own thread:

  * `REGISTRY.cancel()` sets the threading.Event, which is what the poll loops
    inside actions/*.py read;
  * `hud.tool_cancelled()` flips the row, which reaches the browser through
    `websocket_server.broadcast` -- a hop onto the SERVER's loop, a different
    loop that Era's tool call does not block.

Only the rest (cancelling a detached asyncio task, the log line, the spoken
ack) is deferred onto the engine loop, where it is safe to touch session state.

ACK POLICY
----------
A click gets visual feedback, not a spoken one -- the row going grey is the
answer, and narrating it is noise on top of an action the owner took to reduce
noise. A spoken order gets a spoken ack. An abort gets none (panic.py has
already said ABORT_ACK, and repeating it recreates what the owner is stopping).
A correction gets none either: she is about to do the corrected thing, which is
the only confirmation worth hearing.
"""

from __future__ import annotations

from app.hud.events import hud
from app.text_intents import _say_exactly
from core.cancel_registry import REGISTRY, CancelToken
from core.logger import log_event, main_logger

# Pinned verbatim, like MIC_OFF_ACK and ABORT_ACK. Terse on purpose: the owner
# interrupted Era to save time, so the confirmation must not cost any.
CANCEL_ACK = "Stopped."

# Sources that must stay silent. See ACK POLICY above.
_SILENT_SOURCES = frozenset({"hud", "abort", "correction"})


class CancelMixin:
    # ── public API ───────────────────────────────────────────────────────
    def cancel_task(self, cid: str, source: str = "hud", reason: str = "") -> bool:
        """Cancel the task with this HUD call id. Safe from any thread.

        Returns False when there was nothing to cancel -- an unknown id, or one
        already cancelled. Both are normal: the owner can click the X on a row
        a moment after the work finished on its own.
        """
        token = REGISTRY.cancel(cid, source=source, reason=reason)
        if token is None:
            return False
        self._apply_cancel(token)
        return True

    def cancel_current_task(self, source: str = "hud", reason: str = "") -> bool:
        """Cancel the most recently started task -- "cancel that"."""
        token = REGISTRY.cancel_newest(source=source, reason=reason)
        if token is None:
            return False
        self._apply_cancel(token)
        return True

    def cancel_all_tasks(self, source: str, reason: str = "") -> int:
        """Cancel everything in flight. Used by the abort path."""
        tokens = REGISTRY.cancel_all(source=source, reason=reason)
        for token in tokens:
            self._apply_cancel(token)
        return len(tokens)

    def has_running_task(self) -> bool:
        """Is there anything a "cancel that" could mean right now?

        The voice intercepts gate on this so a stray "stop that" with nothing
        running falls through to normal routing instead of being swallowed.
        """
        return bool(REGISTRY.active())

    # ── the immediate half (caller's thread) ─────────────────────────────
    def _apply_cancel(self, token: CancelToken) -> None:
        """Everything that must not wait for the engine loop."""
        reason = "Aborted." if token.source == "abort" else "Cancelled by you."
        try:
            hud.tool_cancelled(token.cid, reason)
        except Exception as e:  # the HUD is never allowed to block a cancel
            print(f"[CANCEL] HUD row not closed: {e}")

        # Release the single confirmation slot if this call is what armed it.
        # core/pending_confirmation.py holds exactly one question; leaving a
        # cancelled call's question in it means the owner's next action is
        # refused as busy, and a later "yes" meant for something else runs the
        # abandoned action. Same release agent/task_queue.cancel performs.
        if token.confirmation_id:
            try:
                store = getattr(self, "_pending_store", None)
                if store is not None and store.cancel(token.confirmation_id):
                    print(f"[CANCEL] Released the pending confirmation for {token.tool}.")
            except Exception as e:
                print(f"[CANCEL] Pending confirmation not released: {e}")

        log_event(
            main_logger,
            "task.cancelled",
            "WARNING",
            tool=token.tool,
            cid=token.cid,
            source=token.source,
            age_ms=token.age_ms(),
            reason=(token.reason or "")[:120],
        )
        print(f"[CANCEL] {token.tool} cancelled by {token.source} after {token.age_ms()}ms.")

        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._finalize_cancel, token)
        except RuntimeError:
            pass

    # ── the deferred half (engine loop) ──────────────────────────────────
    def _finalize_cancel(self, token: CancelToken) -> None:
        """Session-side bookkeeping. Runs on the engine loop; never raises."""
        # Detached background work CAN be killed at the asyncio level. Work
        # sitting in run_in_executor cannot -- that is what the token's Event
        # and the HUD's terminal row (the soft discard) are for.
        task = token.task
        if task is not None:
            try:
                task.cancel()
            except Exception as e:
                print(f"[CANCEL] Background task not cancelled: {e}")

        try:
            self.ui.write_log(f"SYS: Cancelled {token.tool} ({token.source}).")
        except Exception:
            pass

        if token.source in _SILENT_SOURCES:
            return
        try:
            self.speak(_say_exactly(CANCEL_ACK))
        except Exception as e:
            print(f"[CANCEL] Ack speak failed: {e}")
