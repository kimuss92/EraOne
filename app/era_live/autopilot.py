"""Run the lane router's offline lanes before any Gemini request or local brain turn."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextvars import ContextVar
from datetime import date

from google.genai import types

from app.hud.events import hud
from core.async_submit import submit_coroutine
from core.autopilot import LOCAL, MAIN, SPECIALIST, Decision, classify, enabled
from core.logger import log_event, main_logger
from core.utils import env_float

# Shared with the tool dispatcher: local confirmations and results use HUD text,
# not Gemini's speak() path. Context propagates to asyncio.to_thread workers.
LOCAL_EXECUTION = ContextVar("autopilot_local_execution", default=False)


class AutopilotMixin:
    def _autopilot_reply(self, message: str) -> None:
        self.ui.write_log(f"Era: {message}")
        hud.response_completed(message)
        self._tg_ack(message)

    def _autopilot_confirmation(self, text: str) -> Decision | None:
        pending = getattr(self, "_autopilot_pending", None)
        if pending is None:
            return None
        cid, tool, args = pending
        if self._pending_store.peek(cid) is None:
            self._autopilot_pending = None
            return None
        low = text.strip().lower().rstrip(".! ")
        if low in {"no", "cancel", "non", "annule"}:
            self._pending_store.cancel(cid)
            self._autopilot_pending = None
            return Decision(LOCAL, "User cancelled pending plan", rule="cancel")
        if low in {"yes", "confirm", "oui", "confirme"}:
            return Decision(LOCAL, "User approved the stored plan", tool=tool,
                            args={**args, "confirmed": True, "confirmation_id": cid}, rule="confirm")
        return None

    def _try_autopilot_text(self, text: str) -> bool:
        """Synchronous interception for the HUD/Telegram thread; no model calls."""
        if not enabled() or getattr(self, "_standby_active", False):
            return False
        # CM replies and other pending questions keep their original owner.
        decision = self._autopilot_confirmation(text)
        if decision is None:
            if getattr(self, "_pending_cm_ask", None) or self._pending_store.is_pending():
                return False
            decision = classify(text)
        if decision.lane == MAIN:
            return False
        gate = self.safety_gate.validate_input(
            transcript=text, confidence=None, state_snapshot=self._text_state_snapshot(), typed=True,
        )
        if gate.action == "BLOCK_SILENTLY":
            self.safety_gate.record_blocked(text)
            self._autopilot_reply("I cannot act on that input right now.")
            return True
        if not self._loop or not self._loop.is_running():
            self._autopilot_reply("The command engine is starting. Please try again shortly.")
            return True
        self._note_user_utterance(text)
        self._last_interaction_timestamp = time.time()
        submit_coroutine(self._run_autopilot(text, decision), self._loop)
        return True

    async def _run_autopilot(self, text: str, decision: Decision) -> None:
        from core.panic_stop import panic

        if panic.active() or getattr(self, "_shutdown_requested", False):
            self._autopilot_reply("Command cancelled: Era is stopped.")
            return
        # Re-check mutable capabilities/pending state ON the engine loop. No
        # prequeued command may displace an approval question that appeared later.
        if decision.rule not in {"confirm", "cancel"} and self._pending_store.is_pending():
            self._autopilot_reply("Please answer the pending confirmation first.")
            return
        lane_key = "coding" if decision.lane == SPECIALIST else "command"
        tasks = getattr(self, "_autopilot_tasks", None)
        if tasks is None:
            tasks = self._autopilot_tasks = {}
        if lane_key in tasks:
            self._autopilot_reply(f"The {lane_key} lane is busy. Please retry when it finishes.")
            return
        tasks[lane_key] = asyncio.current_task()
        started = time.monotonic()
        hud.routing_started()
        hud.routing_finished("Claude Code" if decision.lane == SPECIALIST else "local command", 0)
        token = LOCAL_EXECUTION.set(True)
        try:
            if decision.rule == "cancel":
                self._autopilot_reply("Cancelled the pending action.")
                return
            if decision.lane == SPECIALIST:
                from core.claude_headless import available

                if decision.tool and not available():
                    self._autopilot_coding_retry_at = time.monotonic() + 60
                    await self._autopilot_fallback(text, "Claude Code is unavailable or disabled.")
                    return
                today = date.today().isoformat()
                day, count = getattr(self, "_autopilot_coding_count", (today, 0))
                count = count if day == today else 0
                cap = int(env_float("ERA_AUTOPILOT_CODING_MAX_PER_DAY", 10, minimum=1, maximum=100))
                if count >= cap or time.monotonic() < getattr(self, "_autopilot_coding_retry_at", 0):
                    await self._autopilot_fallback(text, "The coding lane is cooling down or capped.")
                    return
                self._autopilot_coding_count = (today, count + 1)
                if not decision.tool:
                    from core.autopilot_coding import plan

                    self._autopilot_reply("Claude Code is investigating. Repository files will not be changed by this investigation.")
                    reply = await plan(text)
                    if not reply.ok:
                        self._autopilot_coding_retry_at = time.monotonic() + 60
                        await self._autopilot_fallback(text, reply.reason)
                    else:
                        self._autopilot_reply(reply.text)
                    return
            fc = types.FunctionCall(name=decision.tool, args=dict(decision.args), id=f"autopilot-{uuid.uuid4().hex}")
            result = await asyncio.wait_for(self._execute_tool_result(fc), timeout=30)
            if result.error_type == "ConfirmationRequired" and result.confirmation_id:
                self._autopilot_pending = (result.confirmation_id, decision.tool, dict(decision.args))
                preview = result.data.get("confirmation_preview") or (
                    f"Action: {decision.tool}; parameters: {decision.args}"
                )
                self._autopilot_reply((preview + "\n" if preview else "") + "Reply yes to approve this stored action, or no to cancel.")
            else:
                if decision.rule == "confirm":
                    self._autopilot_pending = None
                # A failed operation is not retried via Gemini: it could already
                # have changed the machine before reporting its failure.
                self._autopilot_reply(result.message or "The local action did not return a result.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(main_logger, "autopilot.failed", "ERROR", lane=decision.lane, error_type=type(exc).__name__)
            if decision.lane == SPECIALIST and not decision.tool:
                self._autopilot_coding_retry_at = time.monotonic() + 60
                await self._autopilot_fallback(text, "The coding specialist failed.")
            else:
                self._autopilot_reply("The local action failed. Check era.log before retrying; it was not replayed through a model.")
        finally:
            LOCAL_EXECUTION.reset(token)
            tasks.pop(lane_key, None)
            log_event(main_logger, "autopilot.completed", "INFO", lane=decision.lane, rule=decision.rule,
                      duration_ms=int((time.monotonic() - started) * 1000))

    async def _autopilot_fallback(self, text: str, reason: str) -> None:
        self._autopilot_reply(f"{reason} No coding change was applied. Trying Gemini for analysis only.")
        session = getattr(self, "session", None)
        if session is None:
            self._autopilot_reply("Gemini is also unavailable. Retry later or configure Claude Code.")
            return
        if getattr(self, "_last_user_text", text) != text:
            self._autopilot_reply("The conversation has moved on. Request the coding analysis again when ready.")
            return
        try:
            self._autopilot_analysis_only = True
            await asyncio.wait_for(session.send_realtime_input(text=(
                "The coding specialist is unavailable. Give analysis and a proposed plan only. "
                "Do not call tools, modify files, or claim execution. User request:\n" + text
            )), timeout=10)
            hud.request_sent("autopilot_fallback")
        except Exception:
            self._autopilot_reply("Gemini could not accept the request. No coding change was applied.")

    def cancel_autopilot(self) -> None:
        # Called on the engine loop by panic/shutdown; subprocess cancellation
        # tears down the read-only coding runner in its finally block.
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        def cancel():
            for task in tuple(getattr(self, "_autopilot_tasks", {}).values()):
                task.cancel()
        loop.call_soon_threadsafe(cancel)
