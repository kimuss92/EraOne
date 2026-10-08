from __future__ import annotations

from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    LOW = 3
    NORMAL = 2
    HIGH = 1
