"""Bounded metadata diagnostics: never store speech, PCM or SDK error messages."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

_LOCK = threading.Lock()


def record(stage: str, generation: str, error: BaseException | None = None) -> None:
    """Keep local breadcrumbs even when native stderr is deliberately discarded."""
    if not re.fullmatch(r"[a-z_]{1,60}", stage):
        return
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        return
    entry: dict[str, object] = {
        "time": time.time(),
        "generation": generation,
        "stage": stage,
        "exception_type": type(error).__name__[:80] if error is not None else "",
    }
    if error is not None:
        code = getattr(error, "code", None)
        if type(code) is int:
            entry["status_code"] = code
        entry["frames"] = [
            {
                "function": frame.name,
                "file": Path(frame.filename).name,
                "line": frame.lineno,
            }
            for frame in traceback.extract_tb(error.__traceback__)[-4:]
        ]
    try:
        from core.telegram_call_setup import private_directory

        # Tests and owned helpers already isolate their logs with ERA_LOG_DIR.
        isolated = os.environ.get("ERA_LOG_DIR")
        directory = Path(isolated) if isolated else private_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "telegram-call-diagnostics.jsonl"
        with _LOCK:
            # Bound retention without touching credentials or other logs.
            if target.exists() and target.stat().st_size > 256_000:
                target.write_text("", encoding="utf-8")
            with target.open("a", encoding="utf-8") as output:
                output.write(json.dumps(entry) + "\n")
    except (OSError, RuntimeError):
        pass  # Diagnostic storage must not break a conversation.
