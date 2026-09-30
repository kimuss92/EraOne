from __future__ import annotations

import asyncio
import threading
import time

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


def _jit_should_capture(tool: str, action: str) -> bool:
    """Return True if tool × action is in the mutation matrix."""
    return tool in _JIT_MUTATION_TOOLS and action in _JIT_MUTATION_ACTIONS


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
        timeout: Maximum seconds to wait for the capture + analysis.
    """
    if not _jit_should_capture(tool, action):
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
