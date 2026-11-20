"""One closed fault taxonomy for every component (Era Fabric F0).

Design: docs/era-enhancement/ERA_FABRIC_DESIGN.md section 4. Today every part says
"it didn't work" its own way: crew ERROR_TYPES, tool error_type strings, raised
exceptions, or prose. A fault is the one structured answer, stored in
ToolResult.data["fault"] so the dataclass, the tool-declaration golden file and
every caller stay unchanged:

    {"kind": "Unavailable", "component": "crew.hermes", "retryable": False,
     "alternative": True, "detail": "connection refused 127.0.0.1:8642"}

Rules:
* Denied, NeedsConfirmation and Cancelled are TERMINAL: nothing may retry them or
  route around them. That is the line between graceful degradation and gate bypass.
* looks_like_failure stays the judge of prose; the fault is the judge of structure.
  When both exist, structure wins.
* Nothing here raises across its public functions; an unknown input classifies as
  Internal (no retry, alternative allowed), never as success.
"""

from __future__ import annotations

import re
from typing import Any

# kind -> (retry the same provider?, try an alternative?)
KINDS: dict[str, tuple[bool, bool]] = {
    "Transient": (True, True),
    "Unavailable": (False, True),
    "Exhausted": (False, True),
    "Timeout": (False, True),
    "EmptyResult": (False, True),
    "BadOutput": (True, True),  # retry once; the caller bounds it
    "NotFound": (False, False),
    "Denied": (False, False),
    "NeedsConfirmation": (False, False),
    "BadInput": (False, False),
    "Cancelled": (False, False),
    "Internal": (False, True),
}
TERMINAL = frozenset({"Denied", "NeedsConfirmation", "Cancelled"})
# Kinds that say something about a component's HEALTH (F1 breakers count these).
BREAKER_KINDS = frozenset({"Unavailable", "Timeout", "Exhausted", "EmptyResult"})

# crew/transport.py ERROR_TYPES, 1:1. A test pins that every crew type is mapped.
CREW_MAP: dict[str, str] = {
    "UnknownBot": "BadInput",
    "BadRequest": "BadInput",
    "InvalidSpec": "Internal",
    "CapabilityDenied": "Denied",
    "UnknownCapability": "BadInput",
    "LLMUnavailable": "Unavailable",
    "BadModelOutput": "BadOutput",
    "StepLimit": "Timeout",
    "HandoffDenied": "Denied",
    "HandoffDepthExceeded": "BadInput",
    "ReviewPaused": "Denied",  # Nabar's review gate held the change
    "ConfirmationRequired": "NeedsConfirmation",
    "ConfirmationBusy": "NeedsConfirmation",
    "TransportDown": "Unavailable",
    "MissingCredential": "Unavailable",
    "BotError": "Internal",
    "Cancelled": "Cancelled",
}

# Other error_type strings seen across Era (core/tool_result.BENIGN_ERROR_TYPES and
# the tool layer). Exact names first, then the suffix rules below.
_EXACT: dict[str, str] = {
    "BlockedAction": "Denied",
    "ConfirmationFailed": "NeedsConfirmation",
    "UserCancelled": "Cancelled",
    "CancelledTask": "Cancelled",
    "PathMissing": "NotFound",
    "FileNotFoundError": "NotFound",
    "TimeoutError": "Timeout",
    "ConnectionError": "Unavailable",
    "ConnectionRefusedError": "Unavailable",
    "ConnectionResetError": "Transient",
    "VaultLockUnavailable": "Transient",
    "ValueError": "BadInput",
    "TypeError": "Internal",
    "KeyError": "Internal",
    "AttributeError": "Internal",
}
# Order matters: the gate words are checked before anything that could loosen them.
_SUFFIX: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(Denied|Refused|Blocked|Forbidden|Tainted\w*|Abort\w*)$"), "Denied"),
    (re.compile(r"(Required|Busy|Expired|Mismatch)$"), "NeedsConfirmation"),
    (re.compile(r"Cancel+ed$"), "Cancelled"),
    (re.compile(r"(Quota|RateLimit|Exhausted|TooManyRequests)", re.I), "Exhausted"),
    (re.compile(r"(Timeout|TimedOut)$", re.I), "Timeout"),
    (re.compile(r"(Unavailable|Down|Offline|Unreachable)$"), "Unavailable"),
    (re.compile(r"(NotFound|Missing)$"), "NotFound"),
    (re.compile(r"(Empty|NoResults?)$"), "EmptyResult"),
    (re.compile(r"(BadRequest|Invalid\w*|BadInput|BadArgs?)$"), "BadInput"),
    (re.compile(r"(BadOutput|ParseError|JSONDecodeError)$"), "BadOutput"),
)
_DETAIL_MAX = 300


def _redact(text: Any) -> str:
    try:
        from core.logger import redact_secrets

        return " ".join(redact_secrets(str(text or "")).split())[:_DETAIL_MAX]
    except Exception:  # noqa: BLE001
        return ""


def make(kind: str, component: str, detail: Any = "", *, alternative: bool | None = None) -> dict:
    """Build a fault dict. An unknown kind is Internal. A terminal kind never allows an
    alternative, whatever the caller asks for."""
    kind = kind if kind in KINDS else "Internal"
    retry, alt = KINDS[kind]
    if alternative is not None:
        alt = bool(alternative)
    if kind in TERMINAL:
        retry, alt = False, False
    return {
        "kind": kind,
        "component": str(component or "unknown")[:60],
        "retryable": retry,
        "alternative": alt,
        "detail": _redact(detail),
    }


def kind_for_error_type(error_type: Any) -> str | None:
    """The fault kind for an error_type string, or None when it says nothing."""
    name = str(error_type or "").strip()
    if not name:
        return None
    if name in CREW_MAP:
        return CREW_MAP[name]
    if name in _EXACT:
        return _EXACT[name]
    if name in KINDS:
        return name
    for pattern, kind in _SUFFIX:
        if pattern.search(name):
            return kind
    return None


def from_error_type(error_type: Any, component: str, detail: Any = "") -> dict:
    return make(kind_for_error_type(error_type) or "Internal", component, detail or error_type)


def from_exception(exc: BaseException, component: str) -> dict:
    """Classify a raised exception. Its class hierarchy decides, not its message."""
    import asyncio

    from core.cancel_registry import CancelledTask

    if isinstance(exc, (CancelledTask, asyncio.CancelledError)):
        kind = "Cancelled"
    elif isinstance(exc, TimeoutError):
        kind = "Timeout"
    elif isinstance(exc, ConnectionResetError):
        kind = "Transient"
    elif isinstance(exc, (ConnectionError, ConnectionRefusedError)):
        kind = "Unavailable"
    elif isinstance(exc, FileNotFoundError):
        kind = "NotFound"
    else:
        kind = kind_for_error_type(type(exc).__name__) or "Internal"
    return make(kind, component, f"{type(exc).__name__}: {exc}")


def classify(result: Any, component: str) -> dict | None:
    """The fault a ToolResult carries, or None for a success.

    An existing data["fault"] wins. A failure with an unrecognised (or no)
    error_type is Internal: unclassified is never read as fine."""
    try:
        if result is None:
            return make("EmptyResult", component, "no result")
        data = getattr(result, "data", None) or {}
        existing = data.get("fault") if isinstance(data, dict) else None
        if isinstance(existing, dict) and existing.get("kind") in KINDS:
            return existing
        if getattr(result, "success", False):
            return None
        detail = getattr(result, "error", None) or getattr(result, "message", "")
        return from_error_type(getattr(result, "error_type", None), component, detail)
    except Exception as exc:  # noqa: BLE001
        return make("Internal", component, f"unclassifiable result: {type(exc).__name__}")


def attach(result: Any, component: str) -> Any:
    """Store the fault on a failed ToolResult's data (never on a success); returns it."""
    try:
        fault = classify(result, component)
        if fault is not None and isinstance(getattr(result, "data", None), dict):
            result.data.setdefault("fault", fault)
    except Exception:  # noqa: BLE001
        pass
    return result


def is_terminal(fault: dict | None) -> bool:
    return bool(fault) and fault.get("kind") in TERMINAL


def may_try_alternative(fault: dict | None) -> bool:
    """True only when the fault itself allows it AND it is not a gate."""
    return bool(fault) and not is_terminal(fault) and bool(fault.get("alternative"))
