from __future__ import annotations

import hashlib
import io
import os
import threading
import time

import numpy as np

from core.utils import get_os, get_vision_api_key, load_config

try:
    import cv2

    _CV2 = True
except ImportError:
    _CV2 = False
try:
    import mss
    import mss.tools

    _MSS = True
except ImportError:
    _MSS = False
try:
    import PIL.Image

    _PIL = True
except ImportError:
    _PIL = False
from google import genai
from google.genai import types

from core import vision_resilience as vr
from core.vision_types import VisionResult

# NOTE: `from src.fast_local_vision import fast_local_vision` used to sit here
# ("option (b): fast local path"). The call that used it is gone — the backup
# runtime/protocol_one/backups/manual_20260916 still shows `return
# fast_local_vision()` — so the import was dead and is removed. src/
# fast_local_vision.py itself is LEFT IN PLACE and now has no importers; it is
# a working local-vision path that is simply unreachable, to rewire or retire
# deliberately rather than delete as a side effect of a lint pass.

_VISION_TEXT_MODEL = os.getenv("ERA_VISION_TEXT_MODEL", "gemini-2.5-flash")
_VISION_MAX_WIDTH = int(os.getenv("ERA_VISION_MAX_WIDTH", "1280"))
_JPEG_Q = int(os.getenv("ERA_VISION_JPEG_QUALITY", "70"))
_VISION_TIMEOUT_SECONDS = float(os.getenv("ERA_VISION_TIMEOUT_SECONDS", "12"))
_VISION_CACHE_TTL_SECONDS = float(os.getenv("ERA_VISION_CACHE_TTL_SECONDS", "3"))
# Hard ceiling for one look across ALL providers. Without it the chain could stack
# gemini 12s + ollama 25s + openai 40s, and screen_process could run the chain twice.
_VISION_TOTAL_BUDGET_SECONDS = float(os.getenv("ERA_VISION_TOTAL_BUDGET_SECONDS", "35"))
# The structured-JSON prompt let gemini-2.5-flash think and then enumerate every
# element on a busy page: 6 KB of output, 36-90s, so every look timed out at 12s.
# No thinking + an output cap brings the same request to 2-3s.
_VISION_MAX_OUTPUT_TOKENS = int(os.getenv("ERA_VISION_MAX_OUTPUT_TOKENS", "2048"))
_MAX_CONCURRENT_ANALYSES = 2
_ANALYSIS_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_ANALYSES)
_MAX_RESPONSE_CHARS = 800
_TIMEOUT_MESSAGE = "I could not analyze the screen quickly enough."
_LOOP_BREAK_HINT = (
    " The vision providers are timing out or out of quota right now — do not repeat"
    " the same look immediately; tell the user and ask how to proceed."
)
_VISION_CLIENT = None
_VISION_CLIENT_LOCK = threading.Lock()


def _ollama_available() -> bool:
    from core.vision_config import resolve_ollama_base_url
    from core.vision_ollama import check_ollama_availability

    return check_ollama_availability(resolve_ollama_base_url())


def _openai_available() -> bool:
    from core.vision_config import resolve_openai_api_key

    return bool(resolve_openai_api_key())


def _gemini_available() -> bool:
    # Only proves a key is configured. Quota/429 state is invisible until the real call.
    try:
        return bool(get_vision_api_key())
    except Exception:
        return False


def _call_ollama(
    image_bytes: bytes, mime_type: str, prompt: str, timeout: float | None = None
) -> str | None:
    try:
        from core.vision_config import (
            resolve_ollama_base_url,
            resolve_ollama_keep_alive,
            resolve_ollama_model,
            resolve_ollama_timeout,
        )
        from core.vision_ollama import analyze_image_with_ollama

        ol_result = analyze_image_with_ollama(
            image_bytes=image_bytes,
            prompt=prompt,
            model=resolve_ollama_model(),
            base_url=resolve_ollama_base_url(),
            timeout_s=_cap_timeout(resolve_ollama_timeout(), timeout),
            keep_alive=resolve_ollama_keep_alive(),
        )
        if ol_result:
            print(f"[Vision] Ollama answered ({len(ol_result)} chars)")
            return ol_result
        print("[Vision] Ollama returned empty result")
        return None
    except Exception as ol_e:
        print(f"[Vision] Ollama failed: {ol_e}")
        return None


def _cap_timeout(configured: float, budget: float | None) -> float:
    return configured if budget is None else max(1.0, min(float(configured), budget))


def _call_openai(
    image_bytes: bytes, mime_type: str, prompt: str, timeout: float | None = None
) -> str | None:
    try:
        from core.vision_config import (
            resolve_openai_api_key,
            resolve_openai_model,
            resolve_openai_timeout,
        )
        from core.vision_openai import analyze_image_with_openai

        oa_result = analyze_image_with_openai(
            image_bytes=image_bytes,
            prompt=prompt,
            api_key=resolve_openai_api_key(),
            model=resolve_openai_model(),
            timeout_s=_cap_timeout(resolve_openai_timeout(), timeout),
        )
        if oa_result:
            print(f"[Vision] OpenAI answered ({len(oa_result)} chars)")
            return oa_result
        print("[Vision] OpenAI returned empty result")
        return None
    except Exception as oa_e:
        print(f"[Vision] OpenAI failed: {oa_e}")
        return None


def _call_gemini(
    image_bytes: bytes, mime_type: str, prompt: str, timeout: float = _VISION_TIMEOUT_SECONDS
) -> str | None:
    result_holder: list = [None]
    error_holder: list = [None]
    try:
        client = get_vision_client()
    except Exception as e:
        print(f"[Vision] ❌ Gemini client init failed: {e}")
        return None

    deadline = time.monotonic() + timeout

    def _request_config():
        remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        kwargs: dict = {
            "max_output_tokens": _VISION_MAX_OUTPUT_TOKENS,
            # The HTTP call itself aborts at the deadline, so a timed-out look does not
            # leave a thread running on (and billing) a request nobody will read.
            "http_options": types.HttpOptions(timeout=remaining_ms),
        }
        if "flash" in _VISION_TEXT_MODEL:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**kwargs)

    def _call():
        for attempt in (1, 2):
            try:
                _call_once()
                error_holder[0] = None
                return
            except Exception as e:
                error_holder[0] = e
                # One quick retry for a transient 5xx/overload, and only if it can finish.
                if attempt == 1 and vr.is_transient_error(e) and deadline - time.monotonic() > 4:
                    print(f"[Vision] Gemini transient error, retrying once: {e}")
                    time.sleep(0.75)
                    continue
                return

    def _call_once():
        response = client.models.generate_content(
            model=_VISION_TEXT_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
            config=_request_config(),
        )
        try:
            text = response.text.strip()
        except Exception:
            text = ""
            try:
                if response.candidates:
                    candidate = response.candidates[0]
                    if candidate.content and candidate.content.parts:
                        text = "".join(
                            p.text for p in candidate.content.parts if hasattr(p, "text") and p.text
                        ).strip()
            except Exception:
                pass
        result_holder[0] = text or ""

    thread = threading.Thread(target=_call, name="VisionAnalyzeThread", daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        print(f"[Vision] Gemini timed out after {timeout}s")
        return None
    if error_holder[0] is not None:
        err = error_holder[0]
        print(f"[Vision] Gemini failed: {err}")
        if vr.is_quota_error(err):
            # Quota never clears within one look; don't pay for it again next time.
            vr.bench_provider("gemini", vr.quota_cooldown_seconds(err), "quota exhausted")
        return None
    if result_holder[0]:
        print(f"[Vision] Gemini answered ({len(result_holder[0])} chars)")
        return result_holder[0]
    print("[Vision] Gemini returned empty result")
    return None


_PROVIDERS: dict = {
    "ollama": (_ollama_available, _call_ollama),
    "openai": (_openai_available, _call_openai),
    "gemini": (_gemini_available, _call_gemini),
}


def get_vision_client():
    global _VISION_CLIENT
    if _VISION_CLIENT is None:
        with _VISION_CLIENT_LOCK:
            if _VISION_CLIENT is None:
                _VISION_CLIENT = genai.Client(api_key=get_vision_api_key())
    return _VISION_CLIENT


def _make_cache_key(angle: str, question: str, image_hash: str) -> str:
    return f"{angle}:{question.lower().strip()}:{image_hash[:16]}"


_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


def _get_cached(key: str) -> str | None:
    with _CACHE_LOCK:
        if key in _CACHE:
            expiry, value = _CACHE[key]
            if time.monotonic() < expiry:
                return value
            else:
                del _CACHE[key]
    return None


def _set_cached(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + _VISION_CACHE_TTL_SECONDS, value)


def _normalize_question(question: str) -> str:
    return question.lower().strip()


def _image_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compress_image(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str, int, int]:
    original_size = len(img_bytes)
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}", original_size, original_size
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        if w > _VISION_MAX_WIDTH:
            ratio = _VISION_MAX_WIDTH / w
            new_w = _VISION_MAX_WIDTH
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        optimized = buf.getvalue()
        return optimized, "image/jpeg", original_size, len(optimized)
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}", original_size, original_size


def analyze_image_once(
    image_bytes: bytes,
    mime_type: str,
    user_text: str,
    timeout: float = _VISION_TIMEOUT_SECONDS,
    brief: bool = True,
) -> str:
    """Send the image to the first available provider, in configured priority order.

    Whoever takes the job finishes it. Providers that fail their availability check are
    skipped without cost; a provider that accepts but then errors hands off to the next.

    Loop guards: the whole chain shares one time budget, a provider benched by
    vision_resilience (quota hit, repeated failures) is skipped outright, and at most
    _MAX_CONCURRENT_ANALYSES looks run at once so a burst cannot starve them all.
    `brief=False` is for callers that send their own output format (structured JSON) —
    prefixing "answer in 1-3 sentences" there contradicts the prompt.
    """
    from core.vision_config import resolve_vision_provider_order

    if brief:
        instruction = (
            "Answer in 1–3 short sentences. "
            "Focus only on what is visible and relevant to the user question. "
            "Do not describe unnecessary details."
        )
        full_text = f"{instruction}\n\nQuestion: {user_text}"
    else:
        full_text = user_text

    deadline = time.monotonic() + _VISION_TOTAL_BUDGET_SECONDS
    if not _ANALYSIS_SLOTS.acquire(timeout=_VISION_TOTAL_BUDGET_SECONDS):
        print("[Vision] Too many looks already in flight - giving up")
        return _TIMEOUT_MESSAGE
    try:
        for name in resolve_vision_provider_order():
            entry = _PROVIDERS.get(name)
            if entry is None:
                print(f"[Vision] Unknown provider '{name}' - skipping")
                continue
            remaining = deadline - time.monotonic()
            if remaining < 1.5:
                print("[Vision] Time budget spent - not starting another provider")
                break
            benched = vr.provider_cooldown_remaining(name)
            if benched > 0:
                print(f"[Vision] {name} benched ({benched:.0f}s left) - skipping")
                continue
            is_available, call = entry

            probe_start = time.perf_counter()
            if not is_available():
                print(f"[Vision] {name} unavailable - skipping")
                continue
            probe_ms = (time.perf_counter() - probe_start) * 1000
            print(f"[Vision] {name} took the job (probe {probe_ms:.0f}ms)")

            remaining = max(1.0, deadline - time.monotonic())
            if name == "gemini":
                result = call(image_bytes, mime_type, full_text, min(timeout, remaining))
            else:
                result = call(image_bytes, mime_type, full_text, remaining)

            if result:
                vr.record_provider_success(name)
                return result[:_MAX_RESPONSE_CHARS]
            vr.record_provider_failure(name)
    finally:
        _ANALYSIS_SLOTS.release()

    return _TIMEOUT_MESSAGE


def is_analysis_failure(text: str | None) -> bool:
    return not text or text.startswith(_TIMEOUT_MESSAGE)


_CAPTURE_ATTEMPTS = 3
_CAPTURE_RETRY_DELAY_SECONDS = 0.15


def _grab_verified(sct, region: dict) -> bytes:
    """Grab `region` and prove the frame is real before anything analyzes it.

    A blank grab (locked screen, UAC secure desktop, display asleep, a transient DXGI
    hiccup) is retried briefly; if it stays blank we raise instead of sending a black
    image to a model, whose "I can't see anything" answer just gets asked again.
    """
    last_problem = "no frame"
    for attempt in range(1, _CAPTURE_ATTEMPTS + 1):
        try:
            shot = sct.grab(region)
            width, height = shot.size
            if width <= 0 or height <= 0:
                last_problem = f"empty frame {width}x{height}"
            elif vr.frame_is_blank(shot.rgb):
                last_problem = "uniform/blank frame"
            else:
                png = mss.tools.to_png(shot.rgb, shot.size)
                if png:
                    return png
                last_problem = "PNG encode returned nothing"
        except Exception as e:
            last_problem = f"grab error: {e}"
        if attempt < _CAPTURE_ATTEMPTS:
            time.sleep(_CAPTURE_RETRY_DELAY_SECONDS)
    raise vr.CaptureVerificationError(
        f"screen capture came back blank after {_CAPTURE_ATTEMPTS} tries ({last_problem}) — "
        "the screen may be locked, asleep, or on a secure prompt"
    )


def _mss_session():
    # mss 10.2 renamed the factory to MSS and deprecated mss.mss; requirements
    # leave mss unpinned, so keep older installs working.
    factory = getattr(mss, "MSS", None) or mss.mss
    return factory()


def capture_screen_raw() -> tuple[bytes, int, int, int, int, float]:
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")
    t0 = time.perf_counter()
    with _mss_session() as sct:
        virt = sct.monitors[0]
        png = _grab_verified(sct, virt)
    capture_ms = (time.perf_counter() - t0) * 1000
    return png, virt["left"], virt["top"], virt["width"], virt["height"], capture_ms


def _capture_screen() -> tuple[bytes, str, int, int]:
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")
    t0 = time.perf_counter()
    with _mss_session() as sct:
        monitors = sct.monitors
        target = monitors[1] if len(monitors) > 1 else monitors[0]
        png = _grab_verified(sct, target)
    capture_ms = (time.perf_counter() - t0) * 1000
    optimized_bytes, mime_type, orig_size, opt_size = _compress_image(png, "PNG")
    print(
        f"[Vision] capture={capture_ms:.1f}ms optimize={(time.perf_counter() - t0) * 1000 - capture_ms:.1f}ms "
        f"orig={orig_size:,} opt={opt_size:,}"
    )
    return optimized_bytes, mime_type, orig_size, opt_size


def _cv2_backend() -> int:
    if not _CV2:
        return 0
    os_name = get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:
    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:
    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            load_config().setdefault("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")
    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    load_config().setdefault("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


_CAMERA_WARMUP_READS = 10
_CAMERA_READ_ATTEMPTS = 5


def _read_camera_frame(index: int, backend: int):
    """Return (frame, None) for a verified frame, or (None, reason). Always releases."""
    cap = cv2.VideoCapture(index, backend)
    try:
        if not cap.isOpened():
            return None, "could not be opened"
        for _ in range(_CAMERA_WARMUP_READS):
            cap.read()
        for _ in range(_CAMERA_READ_ATTEMPTS):
            ret, frame = cap.read()
            if ret and vr.camera_frame_usable(frame):
                return frame, None
            time.sleep(0.05)
        return None, "only empty or dark frames"
    finally:
        cap.release()


def _capture_camera() -> tuple[bytes, str, int, int]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")
    t0 = time.perf_counter()
    index = _get_camera_index()
    backend = _cv2_backend()
    frame, problem = _read_camera_frame(index, backend)
    if frame is None:
        # The configured index is a value in api_keys.json, so a camera that moved index
        # (glasses plugged in/out, a virtual cam installed) failed on EVERY launch.
        # Look at the other indices before giving up; don't rewrite the config.
        print(f"[Vision] Camera index {index}: {problem} - probing other indices")
        for alt in range(6):
            if alt == index:
                continue
            frame, _alt_problem = _read_camera_frame(alt, backend)
            if frame is not None:
                print(f"[Vision] Camera fallback to index {alt}")
                break
    if frame is None:
        raise vr.CaptureVerificationError(
            f"camera {index} gave no usable frame ({problem}) — it may be covered, "
            "unplugged, or in use by another app"
        )
    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        w, h = img.size
        if w > _VISION_MAX_WIDTH:
            ratio = _VISION_MAX_WIDTH / w
            new_w = _VISION_MAX_WIDTH
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        optimized = buf.getvalue()
        orig_size = len(optimized)
        opt_size = orig_size
    else:
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
        optimized = buf.tobytes()
        orig_size = len(optimized)
        opt_size = orig_size
    capture_ms = (time.perf_counter() - t0) * 1000
    print(f"[Vision] capture={capture_ms:.1f}ms orig={orig_size:,} opt={opt_size:,}")
    return optimized, "image/jpeg", orig_size, opt_size


_ANALYSIS_ALIASES = {"screen", "analyze", "analyse", "look", "describe", "see", "capture"}


def _is_netflix_context(user_text: str = "", params: dict | None = None) -> bool:
    """Check if current context is Netflix-related. Only Netflix-specific failures
    should set Netflix Vision cooldown."""
    # Check intent memory for active Netflix app
    try:
        from core.intent_memory import load_state

        state = load_state()
        if state.get("last_media_app") == "netflix":
            return True
    except Exception:
        pass
    # Check if user text mentions Netflix
    if user_text and "netflix" in user_text.lower():
        return True
    # Check params for Netflix-related values
    if params:
        for key in ("app", "media_app", "source"):
            val = params.get(key, "")
            if val and "netflix" in str(val).lower():
                return True
        # Check URL params
        for key in ("url", "link", "address"):
            val = params.get(key, "")
            if val and "netflix.com" in str(val).lower():
                return True
    return False


def _maybe_set_netflix_cooldown(user_text: str = "", params: dict | None = None) -> None:
    """Set Netflix Vision cooldown only if context is Netflix-related."""
    if _is_netflix_context(user_text, params):
        try:
            from actions.netflix_control import _set_netflix_vision_cooldown

            _set_netflix_vision_cooldown()
        except Exception:
            pass


def _speak_vision_completion(speak, prompt: str, request_id: str, result) -> None:
    """Proactive-completion callback for an async vision request: announce
    the outcome the moment the background worker finishes, instead of the
    user having to separately ask for status/result."""
    try:
        if result is None:
            return
        if result.ok and result.summary:
            speak(f"Vision check finished, sir — {result.summary}")
        elif result.ok:
            speak("Vision check finished, sir, but there was nothing notable to report.")
        else:
            speak(f"The vision check didn't work, sir: {result.error or 'unknown error'}")
    except Exception as e:
        print(f"[Vision] ⚠️ speak callback failed: {e}")


def screen_process(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = (params.get("action") or "").lower().strip()
    if action in _ANALYSIS_ALIASES:
        params.pop("action", None)
        action = ""
    elif action:
        if action in (
            "result",
            "get_result",
            "status",
            "check_status",
            "list",
            "requests",
            "clear",
            "cleanup",
        ):
            return _handle_vision_action(action, params)
        user_text = (params.get("text") or params.get("user_text") or "").strip()
        if user_text:
            params.pop("action", None)
            action = ""
        else:
            return _handle_vision_action(action, params)
    user_text = (params.get("text") or params.get("user_text") or "").strip()
    angle = params.get("angle", "screen").lower().strip()
    is_async = params.get("async", False)
    if not user_text:
        print("[Vision] ⚠️  No question provided — aborting")
        _maybe_set_netflix_cooldown(user_text="", params=params)
        return "I could not analyze the screen."
    if is_async:
        from core.vision_worker import submit_vision_request

        on_complete = (
            (
                lambda request_id, result: _speak_vision_completion(
                    speak, user_text, request_id, result
                )
            )
            if speak is not None
            else None
        )
        request_id = submit_vision_request(
            prompt=user_text,
            mode="fast",
            target="active_window",
            on_complete=on_complete,
        )
        return f"Vision analysis started. Request ID: {request_id}"
    print(f"[Vision] ▶ angle={angle!r}  question='{user_text[:80]}'")
    total_start = time.perf_counter()
    try:
        if angle == "camera":
            image_bytes, mime_type, orig_size, opt_size = _capture_camera()
            print(f"[Vision] 📷 Camera: {orig_size:,} bytes (opt: {opt_size:,})")
        else:
            image_bytes, mime_type, orig_size, opt_size = _capture_screen()
            print(f"[Vision] 🖥️  Screen: {orig_size:,} bytes (opt: {opt_size:,})")
    except vr.CaptureVerificationError as e:
        # Verified-bad capture: say exactly why, so the caller fixes the cause (unlock,
        # uncover the lens) instead of retrying a look that cannot succeed.
        print(f"[Vision] ❌ Capture verification failed: {e}")
        _maybe_set_netflix_cooldown(user_text=user_text, params=params)
        return f"I could not analyze the screen: {e}."
    except Exception as e:
        print(f"[Vision] ❌ Capture error: {e}")
        _maybe_set_netflix_cooldown(user_text=user_text, params=params)
        return "I could not analyze the screen."
    img_hash = _image_hash(image_bytes)
    cache_key = _make_cache_key(angle, user_text, img_hash)
    cached = _get_cached(cache_key)
    if cached is not None:
        total_ms = (time.perf_counter() - total_start) * 1000
        print(f"[Vision] ✅ cache hit total={total_ms:.1f}ms")
        return cached
    # Try VisionService for screen captures (structured analysis, falls back on failure)
    if angle == "screen":
        try:
            from core.vision_service import analyze_screen
            from core.vision_service import clear_cache as _vs_clear

            _vs_clear()
            _vr = analyze_screen(prompt=user_text, mode="fast", target="active_window")
            if _vr.ok and _vr.summary:
                try:
                    from core.vision_context import store_latest_vision_result

                    store_latest_vision_result("latest", _vr)
                except Exception:
                    pass
                _set_cached(cache_key, _vr.summary)
                return _vr.summary
            if _vr.metadata.get("analysis_failed"):
                # The service already walked the whole provider chain and it failed.
                # Running the chain again here is what doubled a failed look to 60-150s.
                print("[Vision] VisionService analysis failed - not re-running the chain")
                _maybe_set_netflix_cooldown(user_text=user_text, params=params)
                return _TIMEOUT_MESSAGE + _LOOP_BREAK_HINT
        except Exception:
            pass
    api_start = time.perf_counter()
    result = analyze_image_once(image_bytes, mime_type, user_text)
    api_ms = (time.perf_counter() - api_start) * 1000
    total_ms = (time.perf_counter() - total_start) * 1000
    print(f"[Vision] api={api_ms:.1f}ms total={total_ms:.1f}ms")
    if not result:
        _maybe_set_netflix_cooldown(user_text=user_text, params=params)
        return "I could not analyze the screen."
    if is_analysis_failure(result):
        # Never cache or publish the failure sentence as the "latest" vision result.
        _maybe_set_netflix_cooldown(user_text=user_text, params=params)
        return _TIMEOUT_MESSAGE + _LOOP_BREAK_HINT
    # Store fallback result as latest so action="result" with latest works
    try:
        from core.vision_context import store_latest_vision_result

        _fallback_vr = VisionResult(
            ok=True,
            mode="fast",
            target="active_window",
            screen_state="unknown",
            summary=result,
            recommended_action="none",
            raw_text=result,
        )
        store_latest_vision_result("latest", _fallback_vr)
    except Exception:
        pass
    _set_cached(cache_key, result)
    return result


# ── V5: Vision async action handlers ────────────────────────────


def _handle_vision_action(action: str, params: dict) -> str:
    if action in ("result", "get_result"):
        return _handle_vision_result(params)
    elif action in ("status", "check_status"):
        return _handle_vision_status(params)
    elif action in ("list", "requests"):
        return _handle_vision_list()
    elif action in ("clear", "cleanup"):
        return _handle_vision_clear()
    else:
        return (
            "Unknown vision action.\n"
            "Available actions:\n"
            "  result   — retrieve a completed vision analysis (requires request_id)\n"
            "  status   — check request status (requires request_id)\n"
            "  list     — list recent vision requests\n"
            "  clear    — remove old completed requests"
        )


def _handle_vision_result(params: dict) -> str:
    request_id = (params.get("request_id") or "").strip()
    if not request_id:
        request_id = "latest"
    if request_id == "latest":
        try:
            from core.vision_context import get_latest_vision_result

            ctx = get_latest_vision_result()
            if ctx is None:
                return "No latest vision result is available. Start an analysis first."
            ctx_id, ctx_result = ctx
            return _format_vision_result(ctx_id, ctx_result)
        except Exception:
            return "No latest vision result is available. Start an analysis first."
    from core.vision_worker import get_vision_request_status, get_vision_result

    status = get_vision_request_status(request_id)
    if status is None:
        return f"Request {request_id} not found."
    state = status.get("status", "")
    if state in ("queued", "running"):
        return f"Request {request_id} is {state}. Please try again later."
    if state in ("failed", "cancelled", "expired"):
        result = get_vision_result(request_id)
        error_msg = result.error if result and result.error else f"Request {state}."
        return f"Request {request_id} {state}: {error_msg}"
    result = get_vision_result(request_id)
    if result is None:
        return f"Request {request_id} completed but result unavailable."
    return _format_vision_result(request_id, result)


def _handle_vision_status(params: dict) -> str:
    from core.vision_worker import get_vision_request_status

    request_id = (params.get("request_id") or "").strip()
    if not request_id:
        request_id = "latest"
    if request_id == "latest":
        try:
            from core.vision_context import get_latest_vision_result

            ctx = get_latest_vision_result()
            if ctx is None:
                return "No latest vision result is available. Start an analysis first."
            ctx_id, _ = ctx
            request_id = ctx_id
        except Exception:
            return "No latest vision result is available. Start an analysis first."
    status = get_vision_request_status(request_id)
    if status is None:
        return f"Request {request_id} not found."
    lines = [f"Request {request_id}:"]
    lines.append(f"  Status: {status.get('status', 'unknown')}")
    if status.get("queued_at"):
        lines.append(
            f"  Queued at: {time.strftime('%H:%M:%S', time.localtime(status['queued_at']))}"
        )
    if status.get("started_at"):
        lines.append(
            f"  Started at: {time.strftime('%H:%M:%S', time.localtime(status['started_at']))}"
        )
    if status.get("finished_at"):
        lines.append(
            f"  Finished at: {time.strftime('%H:%M:%S', time.localtime(status['finished_at']))}"
        )
    if status.get("duration_ms") is not None:
        lines.append(f"  Duration: {status['duration_ms']:.0f}ms")
    return "\n".join(lines)


def _handle_vision_list() -> str:
    from core.vision_worker import list_vision_requests

    requests = list_vision_requests()
    if not requests:
        return "No vision requests."
    lines = [f"Vision requests ({len(requests)}):"]
    for req in requests:
        rid = req.get("request_id", "?")
        state = req.get("status", "unknown")
        lines.append(f"  {rid} — {state}")
    return "\n".join(lines)


def _handle_vision_clear() -> str:
    from core.vision_worker import clear_vision_requests

    count = clear_vision_requests()
    return f"Cleared {count} old vision request(s)."


def _format_vision_result(request_id: str, result) -> str:
    lines = [f"Vision result {request_id}:"]
    lines.append(f"Screen: {result.screen_state}")
    lines.append(f"Summary: {result.summary}")
    lines.append(f"Recommended action: {result.recommended_action}")
    lines.append(f"Needs confirmation: {str(result.needs_confirmation).lower()}")
    if result.elements:
        lines.append("")
        lines.append("Visible elements:")
        for i, el in enumerate(result.elements[:3], 1):
            lines.append(
                f"  {i}. {el.label} — {el.type} — {el.risk} "
                f"— {el.action_hint} — {el.confidence:.2f}"
            )
    if result.error:
        lines.append(f"\nWarning: {result.error}")
    try:
        from core.vision_policy import evaluate_vision_recommendation

        policy = evaluate_vision_recommendation(result)
        lines.append("")
        lines.append(f"Recommendation: {policy['recommended_path']}")
        if policy.get("reason"):
            lines.append(f"  Reason: {policy['reason']}")
    except Exception:
        pass
    try:
        from core.vision_context import store_latest_vision_result

        store_latest_vision_result(request_id, result)
    except Exception:
        pass
    return "\n".join(lines)


# ── End V5 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[TEST] screen_processor.py (one-shot mode)")
    print("=" * 52)
    mode = input("angle — screen / camera (default: screen): ").strip().lower() or "screen"
    q = input("Question (Enter = default): ").strip() or "What do you see? Be brief."
    t0 = time.perf_counter()
    print(f"[TEST] Analyzing ({time.perf_counter() - t0:.3f}s start)...\n")
    t1 = time.perf_counter()
    result = screen_process({"angle": mode, "text": q})
    print(f"Result ({time.perf_counter() - t1:.3f}s): {result}")
