from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class VisionRequestState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class VisionElement:
    label: str = ""
    type: str = "unknown"
    bbox: list[int] = field(default_factory=list)
    confidence: float = 0.0
    risk: str = "low"
    action_hint: str = "none"


@dataclass
class VisionResult:
    ok: bool = True
    mode: str = "fast"
    target: str = "active_window"
    screenshot_path: str | None = None
    screen_state: str = "unknown"
    summary: str = ""
    elements: list[VisionElement] = field(default_factory=list)
    recommended_action: str = "none"
    error: str | None = None
    raw_text: str = ""
    cached: bool = False
    needs_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisionRequest:
    request_id: str
    prompt: str
    mode: str
    target: str
    save_debug: bool
    status: VisionRequestState
    queued_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: VisionResult | None = None
    worker_thread: str | None = None
    # Proactive-completion callbacks: fired with (request_id, result) once the
    # worker finishes, so a caller who submitted with a live speak() handle
    # gets the outcome spoken the moment it's ready instead of only on poll.
    # A list because dedup (_find_existing) can hand the same in-flight
    # request to more than one caller.
    on_complete: list[Callable[[str, VisionResult], None]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        if self.finished_at is not None and self.queued_at is not None:
            return round((self.finished_at - self.queued_at) * 1000, 1)
        return None

    def status_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "mode": self.mode,
            "target": self.target,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "worker_thread": self.worker_thread,
            "from_cache": bool(self.result and self.result.cached) if self.result else False,
            "ok": self.result.ok if self.result else None,
        }
