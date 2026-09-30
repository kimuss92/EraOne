from __future__ import annotations

import time

from core.tool_result import ToolResult, looks_like_failure


def _generic_success_verifier(result: ToolResult, parameters: dict) -> ToolResult:
    result.verified = True
    result.verifier_name = "generic"
    result.verification_message = "Tool completed without error."
    return result


def _mark_failed(result: ToolResult, name: str, why: str) -> ToolResult:
    """Flip a result to failed from inside a verifier.

    error_type matters as much as the flag: core.claude_error_router and
    actions/self_diagnosis both key off it, and a None error_type slipped past
    every benign-class check and was reported as an unexplained live fault.
    """
    result.verification_status = "FAILED"
    result.success = False
    result.verified = False
    result.verifier_name = name
    result.verification_message = why
    if not result.error_type:
        result.error_type = "ActionError"
    if not result.error:
        result.error = result.message
    return result


def _message_verifier(name: str, extra_markers: tuple[str, ...] = (), ok_message: str = ""):
    """Build a verifier that reads the tool's own message for a failure report.

    Every one of these used to scan the WHOLE message body, so a tool that
    SUCCEEDED at returning content mentioning "not found" or "error" — a file
    read, a search answer, a screen description — was flipped to success=False.
    That told the model its own working tools had failed and filled
    self_diagnosis with faults that never happened. Judgement now goes through
    looks_like_failure, which only reads a short leading status line.
    """
    failed_message = f"{name} reported failure in its own message."
    completed_message = ok_message or f"{name} completed."

    def verifier(result: ToolResult, parameters: dict) -> ToolResult:
        if not result.success:
            return result
        if looks_like_failure(result.message) or _leads_with(result.message, extra_markers):
            return _mark_failed(result, name, failed_message)
        result.verified = True
        result.verifier_name = name
        # "{action}" lets a verifier name the action it verified, which is what
        # makes the log line useful when several actions share one tool.
        action = str((parameters or {}).get("action", "")).strip() or "action"
        result.verification_message = completed_message.replace("{action}", action)
        return result

    return verifier


def _leads_with(message: str, markers: tuple[str, ...]) -> bool:
    """Tool-specific failure markers, held to the same leading-status-line rule."""
    if not markers:
        return False
    stripped = (message or "").strip()
    if not stripped or stripped[0] in "{[":
        return False
    first = stripped.split("\n", 1)[0].strip()
    if len(first) > 200:
        return False
    lower = first.lower()
    return any(marker in lower for marker in markers)


def verify_open_app(result: ToolResult, parameters: dict) -> ToolResult:
    if not result.success:
        return result
    result.verified = True
    result.verifier_name = "open_app"
    result.verification_message = "Application launched successfully."
    return result


verify_browser_control = _message_verifier(
    "browser_control",
    extra_markers=("no tab", "unknown"),
    ok_message="Browser {action} completed.",
)


verify_file_controller = _message_verifier(
    "file_controller",
    extra_markers=(),
    ok_message="File operation completed.",
)


verify_send_message = _message_verifier(
    "send_message",
    extra_markers=("not installed",),
    ok_message="Message sent successfully.",
)


verify_computer_control = _message_verifier(
    "computer_control",
    extra_markers=(),
    ok_message="Computer control action completed.",
)


verify_computer_settings = _message_verifier(
    "computer_settings",
    extra_markers=("missing",),
    ok_message="Settings action completed.",
)


def verify_media(result: ToolResult, parameters: dict) -> ToolResult:
    if not result.success:
        return result
    result.verified = True
    result.verifier_name = "media"
    result.verification_message = "Media action completed."
    return result


verify_web_search = _message_verifier(
    "web_search",
    extra_markers=("no results", "please provide"),
    ok_message="Search completed successfully.",
)


verify_weather = _message_verifier(
    "weather",
    extra_markers=("missing", "couldn't"),
    ok_message="Weather report displayed.",
)


def verify_code_helper(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_dev_agent(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_screen_process(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_reminder(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_youtube_video(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_desktop_control(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_flight_finder(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


def verify_game_updater(result: ToolResult, parameters: dict) -> ToolResult:
    return _generic_success_verifier(result, parameters)


_VERIFIER_MAP: dict[str, callable] = {
    "open_app": verify_open_app,
    "browser_control": verify_browser_control,
    "file_controller": verify_file_controller,
    "send_message": verify_send_message,
    "computer_control": verify_computer_control,
    "computer_settings": verify_computer_settings,
    "media": verify_media,
    "media_coordinator": verify_media,
    "web_search": verify_web_search,
    "weather_report": verify_weather,
    "code_helper": verify_code_helper,
    "dev_agent": verify_dev_agent,
    "screen_process": verify_screen_process,
    "screen_processor": verify_screen_process,
    "reminder": verify_reminder,
    "youtube_video": verify_youtube_video,
    "desktop_control": verify_desktop_control,
    "flight_finder": verify_flight_finder,
    "game_updater": verify_game_updater,
}


def verify_action_result(
    tool: str,
    result: ToolResult,
    parameters: dict | None = None,
    timeout_seconds: float | None = None,
) -> ToolResult:
    if not result.success:
        return result

    verifier = _VERIFIER_MAP.get(tool)
    if verifier is None:
        result.verified = False
        result.verifier_name = None
        result.verification_message = f"No verifier registered for '{tool}'."
        return result

    start = time.perf_counter()
    try:
        result = verifier(result, parameters or {})
    except Exception as e:
        result.verified = False
        result.verifier_name = tool
        result.verification_message = f"Verifier exception: {e}"
    elapsed = time.perf_counter() - start
    if timeout_seconds is not None and elapsed > timeout_seconds:
        result.verified = False
        result.verification_message = f"Verifier timed out ({elapsed:.2f}s > {timeout_seconds}s)"

    if result.verified:
        result.verification_status = "VERIFIED"
    elif result.verification_status not in ("FAILED",):
        result.verification_status = "NOT_VERIFIED"

    return result
