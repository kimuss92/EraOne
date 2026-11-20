"""Immutable remote tool context using Era's existing headless execution policies."""

from __future__ import annotations

import asyncio
import contextvars
import re
from copy import deepcopy
from typing import Any

from core.approval_execution import CURRENT_TASK_ID
from core.cancel_registry import REGISTRY, bind
from core.input_envelope import current_input
from core.pending_confirmation import (
    SCOPED_APPROVAL,
    PendingConfirmation,
    get_shared_store,
)
from core.telegram_call_gemini import CallCommand
from core.tool_execution_lane import LANE, REMOTE_OWNER, ToolLaneBusy
from core.tool_result import ToolResult

SUPPORTED = frozenset(
    {
        "save_memory",
        "retrieve_memory_context",
        "list_memory_keys",
        "forget_memory",
        "open_app",
        "web_search",
        "browser_control",
        "file_controller",
        "screen_process",
        "send_message",
        "reminder",
        "youtube_video",
        "weather_report",
        "computer_settings",
        "system_performance",
        "desktop_control",
        "computer_control",
        "flight_finder",
        "confirm_pending_confirmation",
        "cancel_pending_confirmation",
        "file_processor",
        "netflix_control",
        "spotify_control",
        "media_coordinator",
        "window_control",
        "premiere_control",
        "agent_browser",
    }
)


def declarations() -> list[dict[str, Any]]:
    from app.tool_declarations import TOOL_DECLARATIONS

    return [deepcopy(item) for item in TOOL_DECLARATIONS if item["name"] in SUPPORTED]


class RemoteTools:
    def __init__(self, owner_id: int) -> None:
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("Verified remote owner required.")
        self.owner_id = owner_id
        self._results: dict[tuple[str, str], dict[str, Any]] = {}
        self._running: set[tuple[str, str]] = set()

    async def dispatch(self, command: CallCommand) -> dict[str, Any]:
        """Retain real worker ownership after cancellation; never speak on the PC."""
        from app.hud.events import hud
        from core.main_tool_bridge import tool_result_to_response_payload

        if (
            command.peer_id != self.owner_id
            or type(command.peer_id) is not int
            or command.input.source != "telegram"
            or not re.fullmatch(r"[0-9a-f]{32}", command.generation)
            or not isinstance(command.provider_id, str)
            or not 1 <= len(command.provider_id) <= 128
        ):
            return {"success": False, "error_type": "RemoteIdentityMismatch"}
        key = (command.generation, command.provider_id)
        self._results = {
            saved: value for saved, value in self._results.items() if saved[0] == command.generation
        }
        if key in self._results:
            return deepcopy(self._results[key])
        if key in self._running:
            return {"success": False, "error_type": "AlreadyRunning"}
        if len(self._results) + len(self._running) >= 256:
            return {"success": False, "error_type": "RemoteToolBudgetExceeded"}
        if command.name not in SUPPORTED:
            return dict(
                tool_result_to_response_payload(
                    ToolResult.fail(
                        "This tool is unavailable through a Telegram call.",
                        error_type="UnsupportedRemoteTool",
                        tool_name=command.name,
                    )
                )
            )

        args = deepcopy(command.arguments)
        cid = "call-" + command.generation + "-" + command.provider_id
        token, existing = REGISTRY.claim_unique(cid, command.name, args)
        if token is None:
            return {
                "success": False,
                "error_type": "AlreadyRunning",
                "result": "This activity is already running.",
                "task_id": existing.cid if existing else "",
            }
        cid = hud.tool_started(cid, command.name, args, background=True, source="telegram_call")
        REGISTRY.rekey_claim(token, cid)
        self._running.add(key)
        context = contextvars.copy_context()

        def execute() -> dict[str, Any]:
            envelope_token = current_input.set(command.input)
            scope = "telegram-call:" + command.generation
            owner_token = REMOTE_OWNER.set(scope)
            task_token = CURRENT_TASK_ID.set(scope)
            probe_token = SCOPED_APPROVAL.set(
                (scope, lambda pending: self._approved(command, pending))
            )
            result: ToolResult
            try:
                with bind(token), LANE.enter(command.name, scope):
                    result = self._execute(command, args)
                if token.cancelled():
                    result = ToolResult.fail(
                        "Activity was cancelled; completion could not be verified.",
                        error_type="Cancelled",
                        tool_name=command.name,
                    )
            except ToolLaneBusy:
                result = ToolResult.fail(
                    "Machine automation is already running. Please try again when it finishes.",
                    error_type="ExecutionBusy",
                    tool_name=command.name,
                )
            except Exception:
                result = ToolResult.fail(
                    "The remote tool failed.",
                    error_type="RemoteToolFailed",
                    tool_name=command.name,
                )
            finally:
                SCOPED_APPROVAL.reset(probe_token)
                CURRENT_TASK_ID.reset(task_token)
                REMOTE_OWNER.reset(owner_token)
                current_input.reset(envelope_token)
                REGISTRY.finish(cid)
            hud.tool_finished(cid, command.name, args, result)
            return dict(tool_result_to_response_payload(result))

        worker = asyncio.create_task(asyncio.to_thread(context.run, execute))

        def finished(task: asyncio.Task[dict[str, Any]]) -> None:
            self._running.discard(key)
            if not task.cancelled():
                self._results[key] = task.result()

        worker.add_done_callback(finished)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            REGISTRY.cancel(cid, source="telegram_call", reason="Call request cancelled.")
            hud.tool_cancelled(cid, "Cancellation requested; background completion is unverified.")
            raise

    @staticmethod
    def _approved(command: CallCommand, pending: PendingConfirmation) -> bool:
        from app.era_live.tool_dispatch import _approves_pending

        return bool(
            pending.risk_level != "HIGH"
            and command.input.received_at > pending.created_at
            and _approves_pending(command.input.text)
        )

    @staticmethod
    def _execute(command: CallCommand, args: dict[str, Any]) -> ToolResult:
        from agent.executor import _call_tool_result
        from core.approval_execution import remote_approves
        from core.memory_intent import (
            is_explicit_memory_request,
            is_value_grounded_in_user_speech,
        )
        from core.self_healing_router import route_tool_call

        name, args = route_tool_call(command.name, args, command.input.text)
        if name not in SUPPORTED:
            return ToolResult.fail(
                "The requested action is unavailable through this call.",
                error_type="UnsupportedRemoteTool",
                tool_name=name,
            )
        store = get_shared_store()
        pending = store.peek(args.get("confirmation_id") or None)
        scope = "telegram-call:" + command.generation
        if name in {"confirm_pending_confirmation", "cancel_pending_confirmation"}:
            if pending is None or pending.task_id != scope:
                return ToolResult.fail(
                    "That approval belongs to another conversation or has expired.",
                    error_type="ConfirmationOwnerMismatch",
                    tool_name=name,
                )
            if name == "cancel_pending_confirmation":
                cancelled = store.cancel(
                    pending.confirmation_id, expected_created_at=pending.created_at
                )
                return (
                    ToolResult.from_any("Pending call action cancelled.", tool_name=name)
                    if cancelled
                    else ToolResult.fail(
                        "That approval is no longer active.",
                        error_type="ConfirmationExpired",
                        tool_name=name,
                    )
                )
            if pending.risk_level == "HIGH" and not remote_approves(pending):
                return ToolResult.fail(
                    "Please use the authenticated Approve button in Telegram for this high-risk action.",
                    error_type="ConfirmationChannelRequired",
                    tool_name=name,
                )
        if name == "save_memory" and not (
            is_explicit_memory_request(command.input.text)
            and is_value_grounded_in_user_speech(str(args.get("value", "")), [command.input.text])
        ):
            return ToolResult.fail(
                "Please explicitly ask me to remember information you have provided.",
                error_type="MemoryNotAuthorized",
                tool_name=name,
            )
        return _call_tool_result(name, args, None)
