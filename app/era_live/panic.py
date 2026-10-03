"""Runtime half of "abort mission" for EraLive (model in core/panic_stop.py).

The order reaches here from three places, none of which involve the model:

  voice (mid-turn)  _receive_audio's INPUT-TRANSCRIPTION branch, the moment the
                    words land — not at turn_complete. This is what makes the
                    abort beat a batch that is already executing.
  voice (turn end)  the transcript backstop beside the "reboot Era One" one,
                    in case the mid-turn check missed a split phrase.
  typed             the top of _on_text_command, above every other intercept.

All three call `trigger_abort()`, which is idempotent within the latch window,
so whichever fires first wins and the rest are no-ops — the same shape as
mute_microphone().

Deliberately NOT a tool. The state this exists for is the one where the model
is the problem, so nothing here may depend on the model answering.
"""

from __future__ import annotations

import time

from app.text_intents import _say_exactly
from core.logger import log_event, main_logger
from core.panic_stop import ABORT_ACK, is_abort_order, panic


class PanicStopMixin:
    def _is_abort_order(self, text: str) -> bool:
        """The user's whole utterance is an abort order (wake word allowed)."""
        try:
            from app.wake import WAKE_CFG

            wake = [WAKE_CFG.word, *WAKE_CFG.aliases]
        except Exception:
            wake = []
        return is_abort_order(text, wake)

    def trigger_abort(self, source: str, text: str = "") -> bool:
        """Stop everything now. Returns False when an abort is already latched.

        Order matters. The latch goes first so that a tool call racing this
        method on the event loop is refused rather than run; only then do we
        spend time on audio, confirmations and the UI.
        """
        already = panic.active()
        record = panic.trip(source, text)
        if already:
            # Re-armed the window (see PanicStop.trip) but everything below has
            # already been done, and repeating the ack would be noise on top of
            # the noise the user is trying to stop.
            return False

        log_event(
            main_logger,
            "era.abort.requested",
            "WARNING",
            source=source,
            text=(text or "")[:120],
            expires_in_s=round(record.expires_at - record.at, 1),
        )
        print(f"[ABORT] Abort ordered ({source}); refusing tool calls for the next few seconds.")

        # 1. Stop talking. Reuses the barge-in path, which already flushes the
        #    playback queue and drops Era to LISTENING.
        try:
            self.trigger_barge_in()
        except Exception as e:
            print(f"[ABORT] Barge-in skipped: {e}")
        # While Gemini is down the turn in flight is the LOCAL brain's
        # (app/era_live/local_fallback.py): stop its remaining tool rounds too.
        cancel_local = getattr(self, "_local_cancel_turn", None)
        if cancel_local is not None:
            try:
                cancel_local()
            except Exception as e:
                print(f"[ABORT] Local turn cancel skipped: {e}")

        cancel_autopilot = getattr(self, "cancel_autopilot", None)
        if cancel_autopilot is not None:
            cancel_autopilot()

        # 2. Drop anything half-finished. A confirmation left armed is the real
        #    hazard: the user aborts, says "yes" to something else a minute
        #    later, and the abandoned action runs after all.
        self._clear_pending_work()

        # 3. Tell the user, in the fewest words that will do. The UI line and
        #    the Telegram ack go first because they are direct writes that
        #    cannot fail on a wedged model; the spoken ack is last and
        #    best-effort. On gemini_audio, speak() routes THROUGH the live
        #    session, so the voice confirmation is exactly as reachable as the
        #    model is — which during a runaway loop may be not very. That is
        #    tolerable only because the stopping already happened in step 1:
        #    the latch does not need the model's cooperation, just the ack does.
        self.ui.write_log(f"SYS: Aborted ({source}). Nothing further will run.")
        try:
            self.ui._broadcast({"type": "abort", "active": True, "ts": time.time()})
        except Exception:
            pass
        try:
            self._tg_ack(ABORT_ACK)
        except Exception:
            pass
        try:
            self.speak(_say_exactly(ABORT_ACK))
        except Exception as e:
            print(f"[ABORT] Ack speak failed: {e}")
        return True

    def _clear_pending_work(self) -> None:
        """Forget confirmations, stashed turns and queued audio.

        Each step is guarded on its own: an abort that half-fails because one
        subsystem is the thing that is wedged would defeat the point.
        """
        try:
            store = getattr(self, "_pending_store", None)
            if store is not None:
                store.clear()
        except Exception as e:
            print(f"[ABORT] Pending confirmation not cleared: {e}")
        try:
            tm = getattr(self, "turn_memory", None)
            if tm is not None:
                tm.clear()
        except Exception as e:
            print(f"[ABORT] Turn memory not cleared: {e}")
        try:
            self._clear_cloud_audio_buffer()
        except Exception:
            pass

    def abort_active(self) -> bool:
        return panic.active()

    def clear_abort(self, source: str = "user") -> bool:
        """Release the latch early, for a UI button or an explicit "as you were".

        Rarely needed — the latch expires on its own — but a user who aborted
        by accident should not have to wait it out.
        """
        rec = panic.clear(source)
        if rec is None:
            return False
        log_event(main_logger, "era.abort.cleared", "INFO", source=source)
        self.ui.write_log("SYS: Abort released.")
        try:
            self.ui._broadcast({"type": "abort", "active": False, "ts": time.time()})
        except Exception:
            pass
        return True
