# core/tool_result.py
from __future__ import annotations

import re
from dataclasses import dataclass, field

NOT_VERIFIED = "NOT_VERIFIED"
VERIFIED = "VERIFIED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"

# Phrases that indicate failure (substring match — multi-word)
_FAILURE_PHRASES = (
    "could not",
    "not found",
    "timed out",
    "timeout",
    "unknown action",
    "unknown tool",
)
# Single words that indicate failure (word-boundary match to avoid false positives)
_FAILURE_WORDS = ("error", "failed")

# A failure message is SHORT and LEADS with the failure ("Path not found: x").
# Content that merely mentions failure is long-form prose or a data payload.
# Beyond this many characters the first "line" is a paragraph of content, not a
# status line — judging it by keyword is what logged a successful web_search for
# "the subprocess module ... input/output/error pipes" as a failed call.
_STATUS_LINE_MAX = 200

# ...unless the over-long line OPENS with a failure clause: "Edit failed: '<a
# 300-char find string>' not found ..." is still a status line, it just quotes
# a lot. The clause before the first colon must BE the failure — lead with
# "could not"/"unknown action"/"unknown tool", or end in failed/error/not found/
# timed out (optionally plus a parenthetical) — so prose such as "Error handling
# in Python: ..." or "Summary: the watchdog failed" stays content. (2026-09-28:
# long find strings and long paths made genuine edit failures read as successes.)
_FAILURE_CLAUSE_MAX = 80
_FAILURE_CLAUSE = re.compile(
    r"^(?:(?:could not|unknown action|unknown tool)\b.*"
    r"|.*\b(?:failed|error|not found|timed out)(?:\s*\([^)]*\))?)$"
)


def looks_like_failure(text: str) -> bool:
    """True when `text` reads as a tool's failure message rather than content.

    The single source of truth for "did this string report a failure", shared by
    ToolResult.from_any and every verifier in core/action_verifiers. Judging the
    WHOLE body was the long-standing phantom-failure bug: reading a file that
    happened to contain "not found" told the model the read had failed and fed
    self_diagnosis errors that never happened. Two guards keep content out:

      * only the FIRST line is judged — failures lead with the failure;
      * a first line that opens a JSON/list payload is content by construction;
        one longer than a status line is content unless it opens with a
        failure clause ("Edit failed: ...", "Path not found: ...").
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    # A serialised payload (screen_process returns one) is data, never a status.
    if stripped[0] in "{[":
        return False
    first = stripped.split("\n", 1)[0].strip()
    lower = first.lower()
    if len(first) > _STATUS_LINE_MAX:
        return _opens_with_failure_clause(lower)
    return any(phrase in lower for phrase in _FAILURE_PHRASES) or any(
        re.search(rf"\b{word}\b", lower) for word in _FAILURE_WORDS
    )


def _opens_with_failure_clause(lower_line: str) -> bool:
    """True when the text before the first colon is itself a failure report."""
    head, sep, _ = lower_line.partition(":")
    head = head.strip()
    if not sep or not head or len(head) > _FAILURE_CLAUSE_MAX:
        return False
    return bool(_FAILURE_CLAUSE.match(head))


@dataclass
class ToolResult:
    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)
    error: str | None = None
    error_type: str | None = None
    action_name: str | None = None
    tool_name: str | None = None
    duration_ms: int | None = None
    verification_status: str = "NOT_VERIFIED"
    risk_level: str = "LOW"
    requires_confirmation: bool = False
    verified: bool = False
    verifier_name: str | None = None
    verification_message: str = ""
    confirmation_id: str | None = None

    @classmethod
    def ok(
        cls,
        message="",
        data=None,
        action_name=None,
        tool_name=None,
        duration_ms=None,
        confirmation_id=None,
    ):
        return cls(
            success=True,
            message=message or "Done.",
            data=data or {},
            action_name=action_name,
            tool_name=tool_name,
            duration_ms=duration_ms,
            confirmation_id=confirmation_id,
        )

    @classmethod
    def fail(
        cls,
        message="",
        error=None,
        error_type=None,
        action_name=None,
        tool_name=None,
        duration_ms=None,
        confirmation_id=None,
        data=None,
    ):
        return cls(
            success=False,
            message=message or "Action failed.",
            data=data or {},
            error=error or message,
            error_type=error_type,
            action_name=action_name,
            tool_name=tool_name,
            duration_ms=duration_ms,
            confirmation_id=confirmation_id,
        )

    @classmethod
    def from_any(cls, value, action_name=None, tool_name=None, duration_ms=None):
        if isinstance(value, ToolResult):
            if action_name is not None:
                value.action_name = action_name
            if tool_name is not None:
                value.tool_name = tool_name
            if duration_ms is not None:
                value.duration_ms = duration_ms
            return value

        if isinstance(value, bool):
            if value:
                return cls.ok(
                    "Done.", action_name=action_name, tool_name=tool_name, duration_ms=duration_ms
                )
            return cls.fail(
                "Action failed.",
                action_name=action_name,
                tool_name=tool_name,
                duration_ms=duration_ms,
            )

        if isinstance(value, str):
            # See looks_like_failure: only a short leading status line counts
            # as a failure, so prose and payloads that merely mention "error"
            # stay successful results.
            if looks_like_failure(value):
                return cls.fail(
                    message=value,
                    error=value,
                    error_type="ActionError",
                    action_name=action_name,
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                )
            return cls.ok(
                message=value, action_name=action_name, tool_name=tool_name, duration_ms=duration_ms
            )

        if isinstance(value, dict):
            d = dict(value)
            success = bool(d.pop("success", True))
            message = d.pop("message", "")
            error = d.pop("error", None)
            error_type = d.pop("error_type", None)
            data = d.pop("data", d)
            return cls(
                success=success,
                message=str(message) if message else ("Done." if success else "Action failed."),
                data=data if isinstance(data, dict) else {},
                error=error,
                error_type=error_type,
                action_name=action_name,
                tool_name=tool_name,
                duration_ms=duration_ms,
            )

        if value is None:
            return cls.ok(
                "Done.", action_name=action_name, tool_name=tool_name, duration_ms=duration_ms
            )

        return cls.ok(
            message=str(value),
            data={"result": value},
            action_name=action_name,
            tool_name=tool_name,
            duration_ms=duration_ms,
        )

    def to_dict(self):
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "error": self.error,
            "error_type": self.error_type,
            "action_name": self.action_name,
            "tool_name": self.tool_name,
            "duration_ms": self.duration_ms,
            "verification_status": self.verification_status,
            "verification_message": self.verification_message,
            "verified": self.verified,
            "verifier_name": self.verifier_name,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_id": self.confirmation_id,
        }

    def __bool__(self):
        return self.success

    def short_message(self, max_len=500):
        msg = self.message
        if len(msg) <= max_len:
            return msg
        return msg[:max_len] + "..."

    def append_verification_phrase(self):
        if self.success and self.verification_status == "NOT_VERIFIED":
            return self.message + " (not independently verified)"
        return self.message
