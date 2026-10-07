from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from core.utils import get_base_dir
from core.vision_classifier import build_structured_vision_prompt, parse_structured_vision_response
from core.vision_types import VisionElement, VisionResult

_VISION_TEXT_MODEL = os.getenv("ERA_VISION_TEXT_MODEL", "gemini-3.8-flash")
_VISION_TIMEOUT = float(os.getenv("ERA_VISION_TIMEOUT_SECONDS", "2.5"))
_CACHE_TTL = float(os.getenv("ERA_VISION_CACHE_TTL_SECONDS", "3"))

_DEBUG_DIR = get_base_dir() / ".cache" / "vision_debug"

_FAST_MAX_WIDTH = int(os.getenv("ERA_VISION_FAST_MAX_WIDTH", "800"))
_FAST_JPEG_QUALITY = int(os.getenv("ERA_VISION_FAST_JPEG_QUALITY", "60"))
_FULL_MAX_WIDTH = int(os.getenv("ERA_VISION_MAX_WIDTH", "1280"))
_FULL_JPEG_QUALITY = int(os.getenv("ERA_VISION_JPEG_QUALITY", "70"))
_DEBUG_MAX_WIDTH = int(os.getenv("ERA_VISION_FAST_MAX_WIDTH", "800"))
_DEBUG_JPEG_QUALITY = int(os.getenv("ERA_VISION_FAST_JPEG_QUALITY", "60"))

_CACHE: dict[str, tuple[float, VisionResult]] = {}
_CACHE_LOCK = threading.Lock()


def _get_sp():
    from actions import screen_processor as sp

    return sp


def _image_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _capture_targeted(mode: str, target: str) -> tuple[bytes, str, int, int, dict]:
    sp = _get_sp()
    metadata: dict = {}

    if mode == "full":
        max_width = _FULL_MAX_WIDTH
        jpeg_q = _FULL_JPEG_QUALITY
    elif mode == "debug":
        max_width = _DEBUG_MAX_WIDTH
        jpeg_q = _DEBUG_JPEG_QUALITY
    else:
        max_width = _FAST_MAX_WIDTH
        jpeg_q = _FAST_JPEG_QUALITY

    raw_png, virt_left, virt_top, virt_w, virt_h, capture_ms = sp.capture_screen_raw()
    metadata["capture_ms"] = round(capture_ms, 1)
    metadata["original_size"] = len(raw_png)
    metadata["capture_method"] = "mss_virtual_full"

    crop_rect = None
    fallback_reason = None

    if target in ("active_window", "browser_viewport"):
        try:
            from core.capture_utils import get_active_window_rect, get_browser_viewport_rect

            if target == "browser_viewport":
                crop_rect = get_browser_viewport_rect()
                if crop_rect is None:
                    fallback_reason = "browser_viewport_unavailable"
            else:
                crop_rect = get_active_window_rect()
                if crop_rect is None:
                    try:
                        import win32gui

                        title = (
                            win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
                        ).lower()
                        if any(kw in title for kw in ["mark-xxxix", "j.a.r.v.i.s", "main.py"]):
                            fallback_reason = "active_window_is_era_one"
                        else:
                            fallback_reason = "active_window_bounds_unavailable"
                    except Exception:
                        fallback_reason = "active_window_bounds_unavailable"
        except Exception:
            fallback_reason = "active_window_bounds_error"

    if crop_rect is not None:
        try:
            from core.capture_utils import crop_raw_image

            crop_start = time.perf_counter()
            cropped_raw = crop_raw_image(raw_png, crop_rect, virt_left, virt_top)
            if cropped_raw is not None:
                metadata["crop_box"] = list(crop_rect)
                tag = "browser_viewport" if target == "browser_viewport" else "active_window"
                metadata["capture_method"] = f"crop_{tag}"
                metadata["crop_ms"] = round((time.perf_counter() - crop_start) * 1000, 1)
                image_to_compress = cropped_raw
            else:
                image_to_compress = raw_png
                fallback_reason = fallback_reason or "crop_failed"
        except Exception:
            image_to_compress = raw_png
            fallback_reason = fallback_reason or "crop_error"
    else:
        image_to_compress = raw_png

    try:
        from core.capture_utils import compress_for_mode

        img_bytes, mime_type, _, processed_size = compress_for_mode(
            image_to_compress, mode, max_width, jpeg_q
        )
    except Exception:
        img_bytes, mime_type, _, processed_size = sp._compress_image(image_to_compress, "PNG")

    metadata["processed_size"] = processed_size
    if fallback_reason:
        metadata["fallback_reason"] = fallback_reason
        if "crop_" not in metadata.get("capture_method", ""):
            metadata["capture_method"] = (
                metadata.get("capture_method", "mss_virtual_full") + "_fallback"
            )

    return img_bytes, mime_type, metadata["original_size"], processed_size, metadata


def _analyze_with_gemini(image_bytes: bytes, mime_type: str, user_prompt: str) -> str | None:
    sp = _get_sp()
    instruction = build_structured_vision_prompt(user_prompt, "", "")
    result = sp.analyze_image_once(image_bytes, mime_type, instruction, brief=False)
    if not result or "I could not analyze the screen quickly enough" in result:
        return None
    return result


def _parse_vision_response(raw: str) -> tuple[str, list[VisionElement], str, str, bool, dict]:
    parsed = parse_structured_vision_response(raw)
    elements = [
        VisionElement(
            label=e["label"],
            type=e["type"],
            bbox=e["bbox"],
            confidence=e["confidence"],
            risk=e["risk"],
            action_hint=e.get("action_hint", "none"),
        )
        for e in parsed.get("elements", [])
    ]
    extra = {
        "app_name": parsed.get("app_name", ""),
        "page_type": parsed.get("page_type", ""),
    }
    return (
        parsed.get("summary", ""),
        elements,
        parsed.get("screen_state", "unknown"),
        parsed.get("recommended_action", "none"),
        parsed.get("needs_confirmation", False),
        extra,
    )


def _make_cache_key(prompt: str, mode: str, target: str, img_hash: str) -> str:
    return f"vs:{prompt.lower().strip()}:{mode}:{target}:{img_hash[:16]}"


def clear_cache():
    """Clear all cached vision results."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _save_debug_artifacts(image_bytes: bytes, result: VisionResult, mode: str) -> str | None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        img_path = _DEBUG_DIR / f"screenshot_{ts}.jpg"
        with open(img_path, "wb") as f:
            f.write(image_bytes)
        result.screenshot_path = str(img_path)
        json_path = _DEBUG_DIR / f"result_{ts}.json"
        with open(json_path, "w") as f:
            json.dump(
                {
                    "ok": result.ok,
                    "mode": result.mode,
                    "target": result.target,
                    "screenshot_path": result.screenshot_path,
                    "screen_state": result.screen_state,
                    "summary": result.summary,
                    "elements": [
                        {
                            "label": e.label,
                            "type": e.type,
                            "bbox": e.bbox,
                            "confidence": e.confidence,
                            "risk": e.risk,
                            "action_hint": e.action_hint,
                        }
                        for e in result.elements
                    ],
                    "needs_confirmation": result.needs_confirmation,
                    "recommended_action": result.recommended_action,
                    "error": result.error,
                    "cached": result.cached,
                    "metadata": dict(result.metadata),
                },
                f,
                indent=2,
            )
        return str(img_path)
    except Exception:
        return None


def analyze_screen(
    prompt: str,
    mode: str = "fast",
    target: str = "active_window",
    save_debug: bool = False,
) -> VisionResult:
    if not prompt or not prompt.strip():
        return VisionResult(
            ok=False,
            mode=mode,
            target=target,
            screen_state="error",
            error="No prompt provided",
        )
    prompt = prompt.strip()
    try:
        image_bytes, mime_type, orig_size, opt_size, metadata = _capture_targeted(mode, target)
    except Exception as e:
        return VisionResult(
            ok=False,
            mode=mode,
            target=target,
            screen_state="error",
            error=f"Capture failed: {e}",
            recommended_action="ask_user",
        )
    save_debug = save_debug or mode == "debug"

    img_hash = _image_hash(image_bytes)
    cache_key = _make_cache_key(prompt, mode, target, img_hash)
    with _CACHE_LOCK:
        if cache_key in _CACHE:
            expiry, cached_result = _CACHE[cache_key]
            if time.monotonic() < expiry:
                cached_result.cached = True
                cached_result.metadata.update(metadata)
                cached_result.metadata["cache_hit"] = True
                return cached_result
            else:
                del _CACHE[cache_key]

    analyze_start = time.perf_counter()
    raw = _analyze_with_gemini(image_bytes, mime_type, prompt)
    metadata["analyze_ms"] = round((time.perf_counter() - analyze_start) * 1000, 1)
    if raw is None:
        # Tells screen_process the provider chain already ran and failed, so it must not
        # run the whole chain a second time on its own capture.
        metadata["analysis_failed"] = True
        return VisionResult(
            ok=True,
            mode=mode,
            target=target,
            screen_state="unknown",
            recommended_action="none",
            error="Analysis timed out or failed",
            needs_confirmation=False,
            metadata=metadata,
        )
    summary, elements, screen_state, recommended_action, needs_confirmation, extra = (
        _parse_vision_response(raw)
    )
    metadata["app_name"] = extra.get("app_name", "")
    metadata["page_type"] = extra.get("page_type", "")
    result = VisionResult(
        ok=True,
        mode=mode,
        target=target,
        screen_state=screen_state,
        summary=summary,
        elements=elements,
        recommended_action=recommended_action,
        needs_confirmation=needs_confirmation,
        raw_text=raw,
        metadata=metadata,
    )
    if save_debug:
        _save_debug_artifacts(image_bytes, result, mode)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + _CACHE_TTL, result)
    return result
