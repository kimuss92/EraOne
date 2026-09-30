# core/main_tool_bridge.py
from __future__ import annotations

import json
from typing import Any

from actions import scheduled_task
from core.confirmation_preview import ANNOUNCED_KEY, PREVIEW_KEY
from core.tool_result import ToolResult

_SENSITIVE_KEYS = {"password", "token", "api_key", "secret", "authorization", "cookie"}


def _safe_preview_value(key: str, value: str) -> str:
    if not value:
        return ""
    if any(sk in key.lower() for sk in _SENSITIVE_KEYS):
        return "***"
    s = str(value).strip()
    if len(s) > 120:
        s = s[:117] + "..."
    return s


def fc_args_to_dict(fc) -> dict:
    """Safely normalize Gemini function call args into a dict. Never raises."""
    try:
        if fc.args is None:
            return {}
        if isinstance(fc.args, dict):
            return dict(fc.args)
        # Attempt to convert other iterable types to dict; will raise if not convertible
        return dict(fc.args)
    except Exception:
        # Catch-all for any issues during conversion, return empty dict as safe fallback.
        # This is a deliberate design choice for robustness given external, untrusted input.
        return {}


def _build_suggested_confirmation_question(result: ToolResult) -> str:
    if result.error_type != "ConfirmationRequired":
        return ""
    action = result.action_name or result.tool_name or "this action"
    data = result.data or {}
    tool = result.tool_name or ""

    # A preview builder already resolved this call into concrete steps and
    # described them (core/confirmation_preview.py). That description IS the
    # question — it is the only text that says what will actually happen, which
    # the parameters alone cannot for a call whose effects a model decides.
    preview = data.get(PREVIEW_KEY)
    if isinstance(preview, str) and preview.strip():
        if data.get(ANNOUNCED_KEY):
            # Already read to the user verbatim over a channel the model does
            # not control. Asking it to "suggest" the question again invites a
            # paraphrase that could describe a gentler plan than the stored one.
            return (
                "The exact steps have already been read to the user. "
                "Do not repeat, summarise or reword them — just wait for the answer."
            )
        return preview.strip()

    if tool == "send_message":
        platform = _safe_preview_value("platform", data.get("platform", ""))
        receiver = _safe_preview_value("receiver", data.get("receiver", ""))
        message = _safe_preview_value(
            "message_text", data.get("message_text", data.get("message", ""))
        )
        if platform:
            return f"Sir, do you want me to send this {platform} message to {receiver}: '{message}'? Should I proceed?"
        return (
            f"Sir, do you want me to send a message to {receiver}: '{message}'? Should I proceed?"
        )

    if tool == "forget_memory":
        cat = _safe_preview_value("category", data.get("category", ""))
        key = _safe_preview_value("key", data.get("key", ""))
        if cat and key:
            return f"Sir, do you want me to forget memory {cat}/{key}? Should I proceed?"

    if tool == "file_controller":
        file_action = data.get("action", "")
        name = data.get("name", data.get("path", ""))
        safe_name = _safe_preview_value("name", name)
        if file_action and safe_name:
            return f"Sir, do you want to {file_action} '{safe_name}'? Should I proceed?"

    if tool == "manage_preference":
        pref_action = str(data.get("action", "")).lower()
        if pref_action == "waive_high_risk_confirmation":
            category = _safe_preview_value("category", data.get("category", ""))
            return (
                f"Sir, from now on I would {category or 'do this'} without asking you first. "
                "Should I proceed?"
            )

    if tool == "computer_settings":
        comp_action = data.get("action", "")
        if comp_action:
            return f"Sir, do you want to {comp_action} the system? Should I proceed?"

    if tool == "gmail_control":
        gmail_action = str(data.get("action", "")).lower()
        if gmail_action in ("send", "compose", "send_email"):
            # Name the real recipient and the real subject. A confirmation the
            # user cannot check against what is actually about to leave the
            # machine is not a confirmation.
            recipient = _safe_preview_value("to", data.get("to", data.get("recipient", "")))
            subject = _safe_preview_value("subject", data.get("subject", ""))
            body = _safe_preview_value("body", data.get("body", data.get("message", "")))
            subject_part = f" with the subject '{subject}'" if subject else ""
            return (
                f"Sir, do you want me to email {recipient}{subject_part}: "
                f"'{body}'? Should I proceed?"
            )
        if gmail_action == "reply":
            body = _safe_preview_value("body", data.get("body", data.get("message", "")))
            return f"Sir, do you want me to send this reply: '{body}'? Should I proceed?"
        if gmail_action in ("authorize", "connect", "auth", "login"):
            return (
                "Sir, this will open Google's consent page so I can read your inbox "
                "and send mail as you. Should I proceed?"
            )
        if gmail_action in ("revoke", "disconnect", "logout", "sign_out"):
            return "Sir, do you want me to disconnect your Gmail account? Should I proceed?"

    return f"Sir, do you want to {action}? Should I proceed?"


def tool_result_to_response_payload(result: ToolResult) -> dict:
    """Convert ToolResult to a Gemini function response payload."""
    payload = {
        "result": result.message,
        "success": result.success,
        "verification_status": result.verification_status,
        "verified": result.verified,
        "error": result.error,
        "error_type": result.error_type,
        "risk_level": result.risk_level,
        "requires_confirmation": result.requires_confirmation,
        "duration_ms": result.duration_ms,
        "confirmation_id": result.confirmation_id,
    }
    if result.confirmation_id:
        payload["suggested_confirmation_question"] = _build_suggested_confirmation_question(result)

    # Special handling for 'list_scheduled_tasks' tool results: format data as JSON array
    # and explicitly set the mime-type for correct parsing by the HUD.
    if result.tool_name == "list_scheduled_tasks" and isinstance(result.data, list):
        payload["data"] = json.dumps(result.data)
        payload["mime_type"] = "application/json"
    elif result.data and len(str(result.data)) < 1000:
        payload["data"] = result.data
    return payload


def build_function_response(fc, result: ToolResult, FunctionResponseClass: type) -> object:
    """Build Gemini FunctionResponse from ToolResult."""
    payload = tool_result_to_response_payload(result)
    if fc.name == "save_memory":
        payload["silent"] = True
    return FunctionResponseClass(
        id=fc.id,
        name=fc.name,
        response=payload,
    )


# --- New functions for HUD interaction with scheduled tasks ---


def get_hud_scheduled_tasks() -> list[dict[str, Any]]:
    """
    Retrieves all scheduled tasks in a structured format suitable for the HUD.
    Delegates to the scheduled_task module to fetch task data.
    """
    # This helper returns a list of dictionaries. The JSON formatting for tool responses
    # will be handled in tool_result_to_response_payload for the 'list_scheduled_tasks' tool.
    return scheduled_task.list_tasks()


def update_hud_scheduled_task(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Accepts and processes a formatted task update payload from the HUD.
    Delegates the update operation to the scheduled_task module.

    Args:
        task_id: The unique identifier of the task to update.
        payload: A dictionary containing the fields to update for the task.
                 The scheduled_task module is responsible for validating and
                 applying these updates.

    Returns:
        A dictionary representing the updated task, or an error response.
    """
    return scheduled_task.update_task(task_id, payload)
