"""Private-owner buttons over Era's existing single confirmation store.

Network work stays off the Era/audio loop. Tokens are process-local: old cards
cannot authorize anything after restart. No model-generated assent is involved.
"""

from __future__ import annotations

import asyncio
import json
import queue
import secrets
import threading
import time
from dataclasses import dataclass

from core.approval_execution import REMOTE_APPROVAL
from core.tool_result import ToolResult


async def execute_approved(engine, pending):
    """Classifier, exact atomic consumption, verification, and queue continuation."""
    from google.genai import types

    from app.hud.events import hud

    token = REMOTE_APPROVAL.set(pending)
    hud_call, result = None, None
    started = time.perf_counter()
    try:
        live = engine._pending_store.peek(pending.confirmation_id)
        from core.approval_execution import remote_approves

        if not remote_approves(live):
            return ToolResult.fail(
                "This approval is no longer valid.", error_type="ConfirmationFailed"
            )
        if getattr(engine, "_standby_active", False):
            # Remote assent wakes the control surface without reopening voice input.
            await engine._resume_from_standby("remote_approval", speak_ack=False)
        hud_call = hud.tool_started(None, pending.tool, pending.parameters)
        result = await engine._execute_tool_result(
            types.FunctionCall(
                name=pending.tool,
                args={
                    **pending.parameters,
                    "confirmed": True,
                    "confirmation_id": pending.confirmation_id,
                },
            )
        )
        return result
    finally:
        REMOTE_APPROVAL.reset(token)
        if hud_call is not None:
            hud.tool_finished(
                hud_call,
                pending.tool,
                pending.parameters,
                result
                if isinstance(result, ToolResult)
                else ToolResult.fail(
                    "Approval execution was interrupted; completion is not verified.",
                    error_type="ApprovalExecutionInterrupted",
                ),
                duration_ms=(time.perf_counter() - started) * 1000,
            )


@dataclass
class _Card:
    token: str
    pending: object
    owner: str
    message_id: int | None = None
    claimed: bool = False
    resolved: str = ""


class TelegramApprovals:
    def __init__(self, store, engine_provider, *, transport=None, start_worker=True):
        import era_telegram

        self.store = store
        self.engine_provider = engine_provider
        self.transport = transport or era_telegram.send_approval_update
        self._cards: dict[str, _Card] = {}
        self._lock = threading.Lock()
        self._jobs = queue.Queue(maxsize=256)
        self._closed = False
        self._unsubscribe = store.subscribe(self._on_event)
        if start_worker:
            threading.Thread(target=self._worker, name="telegram-approvals", daemon=True).start()
        pending = store.peek()
        if pending is not None:
            self._offer(pending)

    @staticmethod
    def _log(event, **fields):
        from core.logger import log_event, main_logger

        log_event(main_logger, f"telegram.approval.{event}", "INFO", **fields)

    def _enqueue(self, job):
        if self._closed:
            return
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            self._log("delivery_dropped", reason="worker_queue_full")

    def close(self):
        self._closed = True
        self._unsubscribe()
        with self._lock:
            self._cards.clear()

    def _offer(self, pending):
        import era_telegram

        owner = era_telegram._chat_id()
        if not owner.isdecimal() or int(owner) <= 0:
            self._log("disabled", reason="private_owner_chat_required")
            return
        with self._lock:
            if self._closed or any(
                c.pending.confirmation_id == pending.confirmation_id
                and c.pending.created_at == pending.created_at
                for c in self._cards.values()
            ):
                return
            card = _Card(secrets.token_hex(16), pending, owner)
            self._cards[card.token] = card
        self._enqueue(("send", card))
        try:
            from app.away_calls import automatic_report

            engine = self.engine_provider()
            if engine is not None:
                automatic_report(
                    engine,
                    "A task needs your approval. Please review the Telegram approval buttons before I continue.",
                    "approval",
                    str(pending.confirmation_id),
                )
        except Exception as exc:
            self._log("automatic_call_hook_failed", error_type=type(exc).__name__)

    def _on_event(self, event):
        if event["state"] == "stored":
            pending = self.store.peek(event["confirmation_id"])
            if pending is not None and pending.created_at == event.get("created_at"):
                self._offer(pending)
            return
        with self._lock:
            cards = [
                c
                for c in self._cards.values()
                if c.pending.confirmation_id == event["confirmation_id"]
                and c.pending.created_at == event.get("created_at")
            ]
            for card in cards:
                card.resolved = event["state"]
        for card in cards:
            self._enqueue(("resolve", card))

    def handle_callback(self, callback):
        import era_telegram

        if self._closed or not era_telegram.authorized_callback(callback):
            self._log("rejected", reason="unauthorized_callback")
            return
        data = callback.get("data", "")
        parts = data.split(":") if isinstance(data, str) else []
        card = None
        if len(parts) == 3 and parts[0] == "era" and parts[1] in ("approve", "cancel"):
            with self._lock:
                candidate = self._cards.get(parts[2])
                if (
                    candidate is not None
                    and not candidate.claimed
                    and not candidate.resolved
                    and candidate.owner == str(callback["message"]["chat"]["id"])
                    and candidate.message_id == callback["message"]["message_id"]
                ):
                    candidate.claimed = True
                    card = candidate
        if card is None:
            self._ack(callback, "This approval is no longer available.")
            self._log("rejected", reason="stale_or_replayed_button")
            return
        p = card.pending
        live = self.store.peek(p.confirmation_id)
        if live is None or live.created_at != p.created_at:
            self._ack(callback, "This approval has expired or was already answered.")
            self._log(
                "rejected", reason="pending_generation_changed", confirmation_id=p.confirmation_id
            )
            return
        if parts[1] == "cancel":
            cancelled = self.store.cancel(p.confirmation_id, expected_created_at=p.created_at)
            self._ack(callback, "Cancelled." if cancelled else "Already answered or expired.")
            self._log(
                "decision", decision="cancel", applied=cancelled, confirmation_id=p.confirmation_id
            )
            return
        engine = self.engine_provider()
        loop = getattr(engine, "_loop", None)
        if loop is None or not loop.is_running():
            with self._lock:
                card.claimed = False
            self._ack(callback, "Era is reconnecting. Try this button again when she is ready.")
            self._log("deferred", reason="engine_unavailable", confirmation_id=p.confirmation_id)
            return
        manager = getattr(engine, "session_manager", None)
        state = manager.get_state_snapshot() if manager is not None else {}
        if state.get("is_reconnecting") or (
            state.get("is_muted") and not getattr(engine, "_standby_active", False)
        ):
            with self._lock:
                card.claimed = False
            self._ack(callback, "Era is paused or reconnecting. Resume her, then try again.")
            self._log("deferred", reason="engine_not_ready", confirmation_id=p.confirmation_id)
            return
        coro = execute_approved(engine, p)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            with self._lock:
                card.claimed = False
            self._ack(callback, "Era could not accept this approval. Please try again.")
            self._log("deferred", reason="scheduling_failed", confirmation_id=p.confirmation_id)
            return
        self._ack(callback, "Approval received. I will report the result here.")
        self._log("decision", decision="approve", confirmation_id=p.confirmation_id)
        future.add_done_callback(lambda done: self._completed(card, done))

    def _completed(self, card, future):
        try:
            result = future.result()
            if not isinstance(result, ToolResult):
                raise TypeError("approval outcome must be ToolResult")
            outcome = (
                "Action completed and verified."
                if result.success and result.verification_status == "VERIFIED"
                else "The tool reported completion; independent verification is unavailable."
                if result.success
                else "Action did not complete."
            )
            outcome += f" Verification: {result.verification_status}."
            if result.error_type:
                outcome += f" Reason: {result.error_type}."
            if result.data.get("task_continuation_error"):
                outcome += " The remaining background job could not resume."
            self._log(
                "result",
                confirmation_id=card.pending.confirmation_id,
                success=result.success,
                verification=result.verification_status,
                error_type=result.error_type,
            )
        except BaseException as exc:
            outcome = "Approval execution stopped unexpectedly; completion could not be verified. Check Era's task logs."
            self._log(
                "result",
                confirmation_id=card.pending.confirmation_id,
                success=False,
                error_type=type(exc).__name__,
            )
        self._enqueue(("result", card, outcome))

    def _ack(self, callback, text):
        self._enqueue(("ack", callback, text))

    def _worker(self):
        while not self._closed:
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._deliver(job)
            except Exception as exc:
                self._log("delivery_failed", error_type=type(exc).__name__)

    def _deliver(self, job):
        import era_telegram
        from app.hud.tool_adapter import describe, safe_args

        kind, item, *rest = job
        owner = era_telegram._chat_id() if kind == "ack" else item.owner
        if kind == "ack":
            outcome = self.transport(
                "answerCallbackQuery",
                {
                    "callback_query_id": item["id"],
                    "text": rest[0],
                    "cache_time": 0,
                },
                callback=item,
            )
        elif kind == "send":
            live = self.store.peek(item.pending.confirmation_id)
            if live is None or live.created_at != item.pending.created_at:
                return
            p = item.pending
            desc = describe(p.tool, safe_args(p.parameters))
            details = safe_args(p.parameters)
            for key in ("message_text", "content", "prompt", "body", "text", "script", "code"):
                if key in details:
                    details[key] = "<private content; review in HUD>"
            summary = json.dumps(details, ensure_ascii=False)
            text = (
                f"Your approval is needed: {desc['label']} · {desc['activity']}\n"
                f"Risk: {p.risk_level}\nDetails: {summary[:2000]}\n"
                "Review the full action in the HUD if these details are abbreviated. "
                "Approve this action or cancel it."
            )
            outcome = self.transport(
                "sendMessage",
                {
                    "chat_id": owner,
                    "text": text,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {"text": "Approve", "callback_data": f"era:approve:{item.token}"},
                                {"text": "Cancel", "callback_data": f"era:cancel:{item.token}"},
                            ]
                        ]
                    },
                },
            )
            with self._lock:
                item.message_id = outcome.message_id if outcome.status == "sent" else None
            if item.resolved:
                self._enqueue(("resolve", item))
            if outcome.status != "sent":
                with self._lock:
                    self._cards.pop(item.token, None)
        else:
            if item.message_id is None:
                return
            text = (
                rest[0]
                if kind == "result"
                else {
                    "consumed": "Approval answered. Execution outcome will be reported by Era.",
                    "cancelled": "Action cancelled.",
                    "expired": "Approval expired; nothing was executed by this button.",
                    "replaced": "This approval was replaced. Use the new request.",
                }.get(item.resolved, "This approval is no longer available.")
            )
            outcome = self.transport(
                "editMessageText",
                {
                    "chat_id": owner,
                    "message_id": item.message_id,
                    "text": text,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
            with self._lock:
                self._cards.pop(item.token, None)
        self._log("delivery", kind=kind, status=outcome.status, reason=outcome.reason)
