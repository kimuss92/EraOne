from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable

from core.vision_types import VisionRequest, VisionRequestState, VisionResult

_requests: dict[str, VisionRequest] = {}
_requests_lock = threading.Lock()
_request_queue: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None
_worker_start_lock = threading.Lock()

_DEDUP_TTL = 5.0
_DEFAULT_MAX_AGE = 300.0

_QUEUE_POLL_SECONDS = 1.0


def _make_request_key(prompt: str, mode: str, target: str, save_debug: bool) -> str:
    return f"{prompt.lower().strip()}|{mode}|{target}|{save_debug}"


def _find_existing(key: str) -> str | None:
    with _requests_lock:
        now = time.time()
        for req in _requests.values():
            if req.status in (
                VisionRequestState.QUEUED,
                VisionRequestState.RUNNING,
            ):
                req_key = _make_request_key(req.prompt, req.mode, req.target, req.save_debug)
                if req_key == key:
                    return req.request_id
            if req.status == VisionRequestState.DONE and req.finished_at is not None:
                age = now - req.finished_at
                if age < _DEDUP_TTL:
                    req_key = _make_request_key(req.prompt, req.mode, req.target, req.save_debug)
                    if req_key == key:
                        return req.request_id
    return None


def _worker_loop():
    while True:
        try:
            request_id = _request_queue.get(timeout=_QUEUE_POLL_SECONDS)
        except queue.Empty:
            _cleanup_expired_requests()
            continue

        with _requests_lock:
            req = _requests.get(request_id)
            if req is None:
                continue
            if req.status != VisionRequestState.QUEUED:
                continue
            req.status = VisionRequestState.RUNNING
            req.started_at = time.time()
            req.worker_thread = threading.current_thread().name

        try:
            from core.vision_service import analyze_screen

            result = analyze_screen(
                prompt=req.prompt,
                mode=req.mode,
                target=req.target,
                save_debug=req.save_debug,
            )
            with _requests_lock:
                req.result = result
                req.status = VisionRequestState.DONE if result.ok else VisionRequestState.FAILED
                req.finished_at = time.time()
                req.result.metadata["request_id"] = req.request_id
                req.result.metadata["status"] = req.status.value
                req.result.metadata["queued_at"] = req.queued_at
                req.result.metadata["started_at"] = req.started_at
                req.result.metadata["finished_at"] = req.finished_at
                req.result.metadata["duration_ms"] = req.duration_ms
                req.result.metadata["worker_thread"] = req.worker_thread
                req.result.metadata["from_cache"] = result.cached
        except Exception as exc:
            with _requests_lock:
                req.status = VisionRequestState.FAILED
                req.finished_at = time.time()
                req.result = VisionResult(
                    ok=False,
                    mode=req.mode,
                    target=req.target,
                    screen_state="error",
                    error=f"Worker error: {exc}",
                    metadata={
                        "request_id": req.request_id,
                        "status": "failed",
                        "queued_at": req.queued_at,
                        "started_at": req.started_at,
                        "finished_at": req.finished_at,
                        "worker_thread": req.worker_thread,
                        "from_cache": False,
                    },
                )

        # Proactive completion: fire any registered callbacks (e.g. the live
        # voice session's speak()) OUTSIDE the lock, so a slow/misbehaving
        # callback can never block the worker thread from picking up the
        # next request.
        with _requests_lock:
            callbacks = list(req.on_complete)
        _fire_on_complete(callbacks, req.request_id, req.result)


def _fire_on_complete(
    callbacks: list[Callable], request_id: str, result: VisionResult | None
) -> None:
    """Best-effort — a broken callback must never take down the vision
    worker, mirroring agent/task_queue.py's on_complete handling."""
    for cb in callbacks:
        try:
            cb(request_id, result)
        except Exception as exc:
            print(f"[VisionWorker] ⚠️ on_complete callback error: {exc}")


def _ensure_worker():
    global _worker_thread
    with _worker_start_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_worker_loop, name="VisionWorker", daemon=True)
        _worker_thread.start()


def _cleanup_expired_requests(max_age: float | None = None):
    if max_age is None:
        max_age = _DEFAULT_MAX_AGE
    now = time.time()
    to_remove: list[str] = []
    with _requests_lock:
        for rid, req in list(_requests.items()):
            if req.status in (
                VisionRequestState.DONE,
                VisionRequestState.FAILED,
                VisionRequestState.CANCELLED,
            ):
                if req.finished_at is not None and (now - req.finished_at) > max_age:
                    req.status = VisionRequestState.EXPIRED
                    to_remove.append(rid)
            elif req.status == VisionRequestState.EXPIRED:
                to_remove.append(rid)
        for rid in to_remove:
            _requests.pop(rid, None)


def submit_vision_request(
    prompt: str,
    mode: str = "fast",
    target: str = "active_window",
    save_debug: bool = False,
    on_complete: Callable[[str, VisionResult], None] | None = None,
) -> str:
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be non-empty")
    key = _make_request_key(prompt.strip(), mode, target, save_debug)
    existing = _find_existing(key)
    if existing is not None:
        # Dedup handed this caller someone else's in-flight (or just-finished)
        # request. Their on_complete must still fire — either attached to run
        # alongside the original, or immediately if the result is already in.
        if on_complete is not None:
            fire_now_result = None
            with _requests_lock:
                req = _requests.get(existing)
                if req is not None:
                    if req.status in (VisionRequestState.DONE, VisionRequestState.FAILED):
                        fire_now_result = req.result
                    else:
                        req.on_complete.append(on_complete)
            if fire_now_result is not None:
                _fire_on_complete([on_complete], existing, fire_now_result)
        return existing
    request_id = f"vr-{uuid.uuid4().hex[:12]}"
    req = VisionRequest(
        request_id=request_id,
        prompt=prompt.strip(),
        mode=mode,
        target=target,
        save_debug=save_debug,
        status=VisionRequestState.QUEUED,
        queued_at=time.time(),
        on_complete=[on_complete] if on_complete is not None else [],
    )
    with _requests_lock:
        _requests[request_id] = req
    _request_queue.put(request_id)
    _ensure_worker()
    return request_id


def get_vision_result(request_id: str) -> VisionResult | None:
    with _requests_lock:
        req = _requests.get(request_id)
    if req is None:
        return None
    return req.result


def get_vision_request_status(request_id: str) -> dict | None:
    with _requests_lock:
        req = _requests.get(request_id)
    if req is None:
        return None
    return req.status_dict()


def cancel_vision_request(request_id: str) -> bool:
    with _requests_lock:
        req = _requests.get(request_id)
        if req is None:
            return False
        if req.status == VisionRequestState.QUEUED:
            req.status = VisionRequestState.CANCELLED
            req.finished_at = time.time()
            return True
    return False


def list_vision_requests() -> list[dict]:
    with _requests_lock:
        return [req.status_dict() for req in _requests.values()]


def clear_vision_requests(max_age_seconds: int = 300) -> int:
    before = len(_requests)
    with _requests_lock:
        now = time.time()
        to_remove: list[str] = []
        for rid, req in list(_requests.items()):
            if req.status in (
                VisionRequestState.DONE,
                VisionRequestState.FAILED,
                VisionRequestState.CANCELLED,
                VisionRequestState.EXPIRED,
            ):
                if req.finished_at is not None and (now - req.finished_at) > max_age_seconds:
                    to_remove.append(rid)
                elif req.status == VisionRequestState.EXPIRED:
                    to_remove.append(rid)
        for rid in to_remove:
            _requests.pop(rid, None)
    return before - len(_requests)


def _reset_worker_state_for_tests():
    global _worker_thread
    with _requests_lock:
        _requests.clear()
    while True:
        try:
            _request_queue.get_nowait()
        except queue.Empty:
            break
    if _worker_thread is not None and not _worker_thread.is_alive():
        _worker_thread = None
