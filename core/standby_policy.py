"""Persisted, deliberate auto-standby preference; off unless explicitly enabled."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "standby.json"
_lock = threading.RLock()
_ORDER = re.compile(
    r"^\s*(?:era[,\s]+)?(?:please\s+)?(?P<action>enable|disable|turn on|turn off)\s+"
    r"(?:auto[- ]?standby|automatic standby)(?:\s+please)?[.!]?\s*$",
    re.I,
)


def auto_standby_order(text: str) -> bool | None:
    match = _ORDER.fullmatch(text or "")
    if match is None:
        return None
    return match.group("action").lower() in {"enable", "turn on"}


def auto_standby_enabled() -> bool:
    with _lock:
        try:
            data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            return isinstance(data, dict) and data.get("auto_standby") is True
        except (OSError, ValueError):
            return False  # Invalid/missing preference must not put Era to sleep.


def set_auto_standby(enabled: bool) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("auto-standby must be enabled or disabled explicitly")
    with _lock:
        try:
            data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["auto_standby"] = enabled
        POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=POLICY_PATH.parent, delete=False
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, POLICY_PATH)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
