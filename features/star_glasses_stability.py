"""Diagnose Star Glasses connection flapping from supplied Era log text."""

from __future__ import annotations

import re
from datetime import datetime

FEATURE = {
    "id": "star_glasses_stability",
    "description": "Analyze supplied Era logs for repeated Star Glasses connect/disconnect transitions.",
}

_RELEVANT = re.compile(r"(?:star\s+glass|glasses|cy03[\s_-]?cb36)", re.IGNORECASE)
_DISCONNECTED = re.compile(
    r"\b(?:not\s+connected|disconnected|disconnecting|vanished|unavailable)\b",
    re.IGNORECASE,
)
_CONNECTED = re.compile(r"\b(?:reconnected|connected|available)\b", re.IGNORECASE)
_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)")


def _state(line: str) -> str | None:
    if not _RELEVANT.search(line):
        return None
    # Test negative phrases first because "not connected" also contains
    # "connected".
    if _DISCONNECTED.search(line):
        return "disconnected"
    if _CONNECTED.search(line):
        return "connected"
    return None


def _timestamp(line: str) -> float | None:
    match = _TIMESTAMP.search(line)
    if not match:
        return None
    value = match.group(1).replace("T", " ")
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def run(arguments) -> dict:
    """Analyze caller-supplied logs without reading files or probing devices."""
    args = arguments if isinstance(arguments, dict) else {}
    text = str(args.get("log_text") or args.get("logs") or "")
    try:
        window_seconds = max(1.0, float(args.get("window_seconds", 120.0)))
    except (TypeError, ValueError):
        window_seconds = 120.0

    observations: list[dict] = []
    transitions: list[dict] = []
    previous = None
    for line_number, line in enumerate(text.splitlines(), 1):
        state = _state(line)
        if state is None:
            continue
        event = {"line": line_number, "state": state, "timestamp": _timestamp(line)}
        observations.append(event)
        if previous is not None and state != previous:
            transitions.append(event)
        previous = state

    timed = [event["timestamp"] for event in observations if event["timestamp"] is not None]
    span = max(timed) - min(timed) if len(timed) >= 2 else None
    in_window = span is None or span <= window_seconds
    flapping = len(transitions) >= 3 and in_window

    return {
        "ok": True,
        "feature": FEATURE["id"],
        "observations": len(observations),
        "transitions": len(transitions),
        "span_seconds": span,
        "flapping": flapping,
        "assessment": (
            "Repeated Star Glasses route transitions detected; transient enumeration loss is likely."
            if flapping
            else "No rapid Star Glasses connection flapping was established by the supplied logs."
        ),
    }
