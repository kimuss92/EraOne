from __future__ import annotations

from typing import Any

SystemStateSnapshot = Any


def verify_tool_result(
    result,
    expected=None,
    tool: str | None = None,
    parameters: dict | None = None,
    timeout_seconds: float | None = None,
):
    from core.tool_result import ToolResult

    if not isinstance(result, ToolResult):
        result = ToolResult.from_any(result)

    if not result.success:
        result.verification_status = "FAILED"
        return result

    if expected is None and tool:
        from core.action_verifiers import verify_action_result

        return verify_action_result(tool, result, parameters, timeout_seconds)

    if expected is None:
        result.verification_status = "NOT_VERIFIED"
        return result

    if callable(expected):
        try:
            ok = expected(result)
            if ok:
                result.verification_status = "VERIFIED"
            else:
                result.success = False
                result.verification_status = "FAILED"
                result.error = "Verification callable returned False."
                result.error_type = "VerificationFailed"
        except Exception as e:
            result.success = False
            result.verification_status = "FAILED"
            result.error = str(e)
            result.error_type = "VerificationError"
        return result

    if isinstance(expected, str):
        if expected.lower() in result.message.lower():
            result.verification_status = "VERIFIED"
        else:
            result.verification_status = "NOT_VERIFIED"
        return result

    result.verification_status = "NOT_VERIFIED"
    return result
