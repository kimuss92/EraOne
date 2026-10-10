"""One-response-per-call execution of a model's tool-call batch.

WHY THIS IS ITS OWN MODULE
--------------------------
This loop used to live inline inside `_receive_audio`'s `async for` over the
Live session, which made it unreachable from a test without a live WebSocket.
It is also the layer where a model's *request* becomes a real action, so it is
worth being able to test in isolation.

THE DEFECT THIS FIXES
---------------------
The old loop appended a response only for calls it actually executed:

  * a state gate (muted / reconnecting / standby) did `continue` — no response
  * `shutdown_era` / `enter_standby` did `break` — no response for the rest

So a batch of N calls could send back fewer than N results, and a batch where
every call was gated sent back *nothing at all* (the send was behind
`if fn_responses:`), leaving the model's turn waiting on results that never came.

Gemini tolerates a short reply. Anthropic does not — it rejects a turn whose
`tool_result` blocks do not match its `tool_use` blocks one for one. Since the
whole point of the multi-model work is to let a turn be served by either, the
contract has to hold for both. Every call now gets exactly one response; the
ones that did not run say so, explicitly, instead of vanishing.

That is a real behaviour change on the live path, and an intended one: the model
now learns "I did not do that, because you are muted" where it previously
learned nothing.

DUPLICATE SUPPRESSION
---------------------
Execution is claimed through `core.llm.exec_ledger`, so a replayed batch cannot
run `send_message` or a shutdown twice. Within a single fresh batch the ledger
is a no-op — ordinals keep two genuine "volume up" calls distinct.
"""

from __future__ import annotations

from google.genai import types

from core.cancel_registry import REGISTRY
from core.llm.exec_ledger import ExecLedger, assign_ordinals, derive_request_id
from core.logger import log_event, main_logger
from core.main_tool_bridge import build_function_response, fc_args_to_dict
from core.panic_stop import ABORT_BLOCK_MESSAGE, panic
from core.pending_confirmation import NOT_USER_APPROVED_MESSAGE
from core.tool_result import ToolResult

# Tools that end the turn: whatever the model queued behind them must not run.
# Ordered so the printed reason matches the tool, as the original loop did.
_SHUTDOWN_TOOLS = ("shutdown_era", "close_era", "exit_era")
_STANDBY_TOOLS = ("enter_standby",)
HALTING_TOOLS = _SHUTDOWN_TOOLS + _STANDBY_TOOLS

# Model-facing text for a call that never ran. Era must not narrate these —
# being muted or in standby is precisely the state where speaking is wrong.
_SILENT = "Say nothing about this."

_BLOCK_MESSAGES = {
    "aborted": ABORT_BLOCK_MESSAGE,
    "muted": f"Not executed: Era is muted. {_SILENT}",
    "reconnecting": f"Not executed: Era is reconnecting. {_SILENT}",
    "standby without wake word": (f"Not executed: Era is in standby with no wake word. {_SILENT}"),
}


class ToolBatchMixin:
    """Executes a batch of model tool calls, one response per call."""

    _tool_ledger: ExecLedger | None = None

    def _get_tool_ledger(self) -> ExecLedger:
        """The per-process execution ledger, created on first use.

        Lazy so EraLive.__init__ stays untouched and a test can drop in its own.
        """
        ledger = getattr(self, "_tool_ledger", None)
        if ledger is None:
            ledger = ExecLedger()
            self._tool_ledger = ledger
        return ledger

    def _tool_state_block_reason(self) -> str | None:
        """Why a tool must not run right now, or None if it may.

        Re-read per call rather than once per batch: a call earlier in the same
        batch can change the state (entering standby is the obvious one) — and,
        since the abort latch is tripped from the audio thread, so can the user.
        That per-call re-read is the whole reason "abort mission" can stop a
        batch the model queued before the user opened their mouth.
        """
        # First, ahead of every other state: the user said stop. An abort
        # outranks muted/reconnecting/standby because those describe what Era
        # cannot do, while this one is what the owner has ordered.
        if panic.active():
            return "aborted"
        sm = self.session_manager.get_state_snapshot()
        if sm.get("is_muted", False):
            return "muted"
        if sm.get("is_reconnecting", False):
            return "reconnecting"
        if sm.get("standby_active", False) and not sm.get("wake_word_active", False):
            return "standby without wake word"
        return None

    def _unexecuted_response(self, fc, *, reason: str, message: str):
        """A well-formed response for a call that was never executed.

        Carries error_type "BlockedAction", which `_execute_tool` already treats
        as not worth speaking aloud — the same classification a safety-gate
        refusal gets, because that is what this is.
        """
        result = ToolResult.fail(
            message=message,
            error=message,
            error_type="BlockedAction",
            tool_name=fc.name,
            data={"executed": False, "reason": reason},
        )
        return build_function_response(fc, result, types.FunctionResponse)

    def _armed_confirmation_id(self) -> str | None:
        """Id of the confirmation awaiting the user's answer, or None."""
        store = getattr(self, "_pending_store", None)
        pending = store.peek() if store is not None else None
        return pending.confirmation_id if pending is not None else None

    def _rekey_response(self, cached, fc):
        """Re-address a replayed result to the call that is asking for it now.

        A provider matches results to calls by id. A literal replay carries the
        same ids and needs nothing — but a request *transferred* to another
        model is a fresh generation with fresh ids, and handing back the first
        model's id would make the result unmatchable. The payload is what was
        computed; only the address changes.
        """
        payload = getattr(cached, "response", None)
        if payload is None:
            return cached  # not a FunctionResponse — hand it back untouched
        if getattr(cached, "id", None) == fc.id:
            return cached
        return types.FunctionResponse(id=fc.id, name=fc.name, response=payload)

    async def _run_tool_call_batch(self, function_calls, *, request_id: str | None = None) -> list:
        """Run a model's tool calls and return exactly one response per call.

        `request_id` defaults to one derived from the provider's own call ids,
        which dedupes a literal replay while leaving a genuine re-ask free to
        act. A router moving one logical request between models must pass its
        own id instead — the second model generates fresh call ids, so the
        derived default would not recognise the work as already done.
        """
        calls = list(function_calls or [])
        if not calls:
            return []

        ledger = self._get_tool_ledger()
        if request_id is None:
            request_id = derive_request_id(getattr(fc, "id", None) for fc in calls)
        ordinals = assign_ordinals([(fc.name or "unknown", fc_args_to_dict(fc)) for fc in calls])

        responses: list = []
        halted_by: str | None = None
        # What was awaiting the user's answer BEFORE this batch. A confirmation
        # armed by a call in this same batch cannot have been answered by the
        # user yet: every call was generated before any result came back.
        armed_before = self._armed_confirmation_id()

        for idx, fc in enumerate(calls):
            name = fc.name or "unknown"

            # ── A halting tool already ran: nothing queued behind it may ──
            if halted_by is not None:
                print(f"[era] [SKIP] Skipping {name} because {halted_by} ended the turn.")
                responses.append(
                    self._unexecuted_response(
                        fc,
                        reason="turn_ended",
                        message=f"Not executed: {halted_by} ended this turn. {_SILENT}",
                    )
                )
                continue

            # ── State-based tool blocking ──
            block = self._tool_state_block_reason()
            if block is not None:
                if block == "aborted":
                    # Audited per call: "how much did the abort actually stop?"
                    # is the first question after a runaway loop, and the answer
                    # has to survive the session it happened in.
                    refused = panic.note_refusal()
                    print(f"[era] [ABORT] Refusing {name}: user aborted (#{refused}).")
                    log_event(
                        main_logger,
                        "tool.call.aborted",
                        "WARNING",
                        tool=name,
                        refused_so_far=refused,
                    )
                else:
                    print(f"[era] [SKIP] Skipping {name} because era is {block}.")
                responses.append(
                    self._unexecuted_response(
                        fc,
                        reason=block.replace(" ", "_"),
                        message=_BLOCK_MESSAGES.get(block, f"Not executed: {block}. {_SILENT}"),
                    )
                )
                continue

            # ── Self-confirmation: [risky_tool, confirm_pending_confirmation] ──
            if name == "confirm_pending_confirmation":
                armed_now = self._armed_confirmation_id()
                if armed_now is not None and armed_now != armed_before:
                    print(f"[era] [SAFETY] Refusing {name}: armed in this same batch.")
                    log_event(main_logger, "tool.confirmation.same_batch", "WARNING", tool=name)
                    responses.append(
                        self._unexecuted_response(
                            fc, reason="confirm_same_batch", message=NOT_USER_APPROVED_MESSAGE
                        )
                    )
                    continue

            # ── Duplicate suppression ──
            claim = ledger.begin(request_id, name, fc_args_to_dict(fc), ordinal=ordinals[idx])
            if not claim.execute:
                print(f"[era] [DUP] Skipping {name}: already handled in this request.")
                log_event(
                    main_logger,
                    "tool.call.duplicate",
                    "WARNING",
                    tool=name,
                    status=claim.status,
                    request=request_id[:12],
                    key=claim.short_key,
                )
                cached = claim.cached_result
                if cached is not None:
                    responses.append(self._rekey_response(cached, fc))
                else:
                    responses.append(
                        self._unexecuted_response(
                            fc,
                            reason=f"duplicate_{claim.status}",
                            message=(
                                "Not executed: this exact call already ran for this "
                                f"request. {_SILENT}"
                            ),
                        )
                    )
                continue

            if name in _SHUTDOWN_TOOLS:
                print(
                    f"[era] [TOOL] {name} "
                    "(Shutdown intercepted, blocking remaining package execution)"
                )
            elif name in _STANDBY_TOOLS:
                print(
                    f"[era] [TOOL] {name} "
                    "(Standby intercepted, blocking remaining package execution)"
                )
            else:
                print(f"[era] [TOOL] {name}")

            try:
                fr = await self._execute_tool(fc)
            except Exception as exc:
                # Reopen the claim before unwinding, or a retry of this batch
                # would be refused as a duplicate of something that never ran.
                ledger.fail(claim, exc)
                raise

            # A cancelled call must NOT be recorded as completed. The ledger
            # caches a completed call's result and refuses a repeat of the same
            # key within the request, so a completed-looking cancel would make
            # the owner's correction come back as "already ran" with the cancel
            # ack replayed -- precisely the failure this feature removes.
            # ledger.fail reopens the key (exec_ledger's _STATE_FAILED).
            if REGISTRY.is_cancelled(getattr(fc, "id", "") or ""):
                ledger.fail(claim, "cancelled")
            else:
                ledger.complete(claim, fr)
            responses.append(fr)

            if name in HALTING_TOOLS:
                halted_by = name

        return responses
