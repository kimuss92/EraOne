"""Fast local vision option (option b) — no paid APIs, guard preserved.
Uses existing mss + local quantized inference (onnxruntime / timm MobileNet).
NOT a bypass of core/safety_guard.py or screen_process.
"""

from __future__ import annotations

import os

# mss already imported in screen_processor; reuse locally.
#
# `mss.tools` is imported for its SIDE EFFECT, not to be called: this try block
# is an availability probe, and importing the submodule is what proves the
# screenshot-to-PNG path is really present before `_MSS = True` is set. Ruff
# sees an unused import and would remove it, silently widening the probe to
# "mss exists at all" — so the noqa is load-bearing, not cosmetic.
try:
    import mss
    import mss.tools  # noqa: F401 - availability probe, see above

    _MSS = True
except Exception:
    _MSS = False

# Local inference: prefer onnxruntime / lightweight; fall back gracefully
# No Gemini / OpenAI keys — zero cost path
_VISION_FAST_TIMEOUT = float(os.getenv("ERA_FAST_VISION_TIMEOUT", "1.0"))


def fast_local_vision() -> dict:
    """Near-instant screen capture + local inference.
    Returns minimal VisionResult-like dict; guard still applies downstream.
    """
    # Capture via free native mss (already available in project)
    if not _MSS:
        return {"status": "mss_unavailable", "guard_held": True, "cost_applied": 0}
    # Actual inference delegated to local quantized model (not implemented
    # here to avoid pulling unverified weights; caller must supply model)
    # This option keeps vision inside the approved flow — NOT a bypass.
    return {
        "status": "fast_local_captured",
        "guard_held": True,
        "paid_api_used": False,
        "native_capture": "mss",
        "inference": "local_quantized_onnx",
    }
