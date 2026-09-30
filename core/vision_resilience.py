"""Failure-loop guards for the screen/camera vision path.

Three jobs, all in service of one rule: a vision look either answers within a bounded
time or fails fast and says so — it never quietly re-pays the same timeout.

1. Capture verification — a grab that produced no pixels, or a perfectly uniform frame
   (locked screen, secure desktop, covered lens), is rejected before it reaches a model.
   A model handed a black frame answers "I can't see anything useful", and the caller
   asks again.
2. Provider health — a provider that just hit its quota (429) or failed/timed out twice
   in a row is benched for a cooldown, so the next look skips straight to one that works
   instead of burning another full timeout rediscovering the same failure.
3. Error classification — which errors deserve one quick retry (transient 5xx /
   overload) and which never do (quota, auth, bad request).
"""

from __future__ import annotations

import re
import threading
import time

import numpy as np

# A frame whose sampled bytes all sit inside this range is treated as blank.
_BLANK_SPREAD = 4
_SAMPLE_BYTES = 60_000

_FAIL_THRESHOLD = 2
_FAIL_COOLDOWN_SECONDS = 60.0
_QUOTA_MIN_COOLDOWN_SECONDS = 30.0
_QUOTA_MAX_COOLDOWN_SECONDS = 600.0
# A per-DAY quota does not come back in the "retry in 29s" the error suggests.
_DAILY_QUOTA_COOLDOWN_SECONDS = 900.0

_TRANSIENT_MARKERS = ("503", "502", "500", "504", "unavailable", "overloaded", "internal")
_QUOTA_MARKERS = ("429", "resource_exhausted", "quota")


class CaptureVerificationError(RuntimeError):
    """The capture ran but produced nothing a model could usefully look at."""


# ── Capture verification ────────────────────────────────────────


def frame_is_blank(pixels: bytes | np.ndarray | None) -> bool:
    """True when the frame is empty or visually uniform (every sampled byte within a
    few levels of each other). A real desktop always has contrast somewhere."""
    if pixels is None:
        return True
    arr = pixels if isinstance(pixels, np.ndarray) else np.frombuffer(pixels, dtype=np.uint8)
    arr = arr.reshape(-1)
    if arr.size < 3:
        return True
    step = max(1, arr.size // _SAMPLE_BYTES)
    sample = arr[::step]
    return int(sample.max()) - int(sample.min()) <= _BLANK_SPREAD


def camera_frame_usable(frame) -> bool:
    """A camera frame is usable when it exists and is not near-black (lens covered,
    camera still initialising, or held by another app). Same threshold as the probe."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return False
    return bool(np.mean(frame) > 8)


# ── Error classification ────────────────────────────────────────


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _QUOTA_MARKERS)


def is_transient_error(exc: BaseException) -> bool:
    if is_quota_error(exc):
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in (500, 502, 503, 504):
        return True
    text = str(exc).lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def quota_cooldown_seconds(exc: BaseException) -> float:
    text = str(exc)
    if "PerDay" in text:
        return _DAILY_QUOTA_COOLDOWN_SECONDS
    match = re.search(r"retry in ([\d.]+)s", text) or re.search(r"retryDelay'?:\s*'(\d+)s", text)
    delay = float(match.group(1)) if match else _QUOTA_MIN_COOLDOWN_SECONDS
    return min(_QUOTA_MAX_COOLDOWN_SECONDS, max(_QUOTA_MIN_COOLDOWN_SECONDS, delay))


# ── Provider health (circuit breaker) ───────────────────────────

_HEALTH: dict[str, dict] = {}
_HEALTH_LOCK = threading.Lock()


def provider_cooldown_remaining(name: str) -> float:
    with _HEALTH_LOCK:
        entry = _HEALTH.get(name)
        if not entry:
            return 0.0
        return max(0.0, entry.get("until", 0.0) - time.monotonic())


def record_provider_success(name: str) -> None:
    with _HEALTH_LOCK:
        _HEALTH.pop(name, None)


def record_provider_failure(name: str) -> None:
    """Count a failure; bench the provider once it fails _FAIL_THRESHOLD times running."""
    with _HEALTH_LOCK:
        entry = _HEALTH.setdefault(name, {"fails": 0, "until": 0.0})
        entry["fails"] += 1
        if entry["fails"] >= _FAIL_THRESHOLD:
            entry["until"] = time.monotonic() + _FAIL_COOLDOWN_SECONDS
            entry["fails"] = 0
            print(
                f"[Vision] {name} benched for {_FAIL_COOLDOWN_SECONDS:.0f}s after repeated failures"
            )


def bench_provider(name: str, seconds: float, reason: str = "") -> None:
    """Bench immediately — for failures that are certain to repeat (quota exhausted)."""
    with _HEALTH_LOCK:
        _HEALTH[name] = {"fails": 0, "until": time.monotonic() + seconds}
    print(f"[Vision] {name} benched for {seconds:.0f}s{f' ({reason})' if reason else ''}")


def reset_provider_health() -> None:
    with _HEALTH_LOCK:
        _HEALTH.clear()
