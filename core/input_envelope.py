"""Source identity captured before commands cross thread boundaries."""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

InputSource = Literal["typed", "telegram", "remote", "voice"]

# Propagates through asyncio tasks and to_thread without consulting mutable UI state.
current_input: ContextVar[InputEnvelope | None] = ContextVar("era_current_input", default=None)


@dataclass(frozen=True)
class InputEnvelope:
    id: str
    text: str
    source: InputSource
    received_at: float

    @classmethod
    def create(cls, text: str, source: InputSource) -> InputEnvelope:
        if source not in ("typed", "telegram", "remote", "voice"):
            raise ValueError(f"Unknown input source: {source}")
        if not isinstance(text, str):
            raise TypeError("Command text must be a string")
        return cls(uuid.uuid4().hex, text, source, time.time())
