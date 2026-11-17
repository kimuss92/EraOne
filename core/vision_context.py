from __future__ import annotations

import asyncio
import threading
import time

from core.hotkeys import ROUTINE_CHORDS, canonical_chord, resolve_keys
from core.vision_types import VisionResult

_STORE: dict[str, tuple[str, VisionResult, float]] = {}
_LOCK = threading.Lock()

# ── JIT temporal cooldown ────────────────────────────────────────────────
_LAST_JIT_EXECUTION_TIME: float = 0.0
_JIT_COOLDOWN_SECONDS: float = 4.0

# Mutation tool × action matrix — only these trigger JIT capture
_JIT_MUTATION_TOOLS: frozenset[str] = frozenset({"computer_control", "browser_control"})
_JIT_MUTATION_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "type",
        "press",
        "fill_form",
        "smart_click",
        "hotkey",
    }
)


def store_latest_vision_result(request_id: str, result: VisionResult) -> None:
    with _LOCK:
        _STORE["latest"] = (request_id, result, time.monotonic())


def get_latest_vision_result(
    max_age_seconds: float = 120.0,
) -> tuple[str, VisionResult] | None:
    with _LOCK:
        entry = _STORE.get("latest")
        if entry is None:
            return None
        request_id, result, stored_at = entry
        if time.monotonic() - stored_at > max_age_seconds:
            return None
        return request_id, result


def clear_latest_vision_result() -> None:
    with _LOCK:
        _STORE.pop("latest", None)


def _is_routine_hotkey(tool: str, action: str, params: dict | None) -> bool:
    """A listed multi-key shortcut, sent right away: the screen analysis held each one up
    for its full 5 s timeout. Lone keys keep the check, because Enter is what submits a
    payment form. browser_control's hotkeys do not pass through this allowlist."""
    if tool != "computer_control" or action != "hotkey" or not params:
        return False
    return canonical_chord(resolve_keys(params)) in ROUTINE_CHORDS


def _jit_should_capture(tool: str, action: str, params: dict | None = None) -> bool:
    """Return True if tool × action is in the mutation matrix and needs a fresh look."""
    if tool not in _JIT_MUTATION_TOOLS or action not in _JIT_MUTATION_ACTIONS:
        return False
    return not _is_routine_hotkey(tool, action, params)


def _jit_cooldown_ok() -> bool:
    """Return True if enough time has passed since the last JIT capture."""
    global _LAST_JIT_EXECUTION_TIME
    now = time.monotonic()
    if now - _LAST_JIT_EXECUTION_TIME < _JIT_COOLDOWN_SECONDS:
        return False
    _LAST_JIT_EXECUTION_TIME = now
    return True


async def jit_vision_capture(
    tool: str,
    action: str,
    params: dict | None = None,
    *,
    timeout: float = 5.0,
) -> None:
    """Capture a fresh screen frame and store it for vision risk assessment.

    Only activates for mutation tools with state-changing actions. Runs the
    synchronous ``analyze_screen()`` in a thread pool to avoid blocking the
    async event loop.  A strict 4-second temporal cooldown prevents redundant
    captures during rapid action sequences.

    Args:
        tool: The tool name about to be classified.
        action: The action about to be performed.
        params: The call's arguments, which name the keys of a hotkey.
        timeout: Maximum seconds to wait for the capture + analysis.
    """
    if not _jit_should_capture(tool, action, params):
        return

    if not _jit_cooldown_ok():
        return

    try:
        from core.vision_service import analyze_screen
    except ImportError:
        return

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                analyze_screen,
                prompt=(
                    "Describe the current screen state. "
                    "Is there a payment form, destructive action, "
                    "or danger visible? Classify risk as low/medium/high."
                ),
                mode="fast",
                target="active_window",
            ),
            timeout=timeout,
        )
        store_latest_vision_result("jit-capture", result)
    except Exception:
        pass
