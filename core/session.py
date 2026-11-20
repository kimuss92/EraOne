"""Era's listening session: one wake word, then a rolling conversation window.

WHY THIS REPLACES TWO TIMERS
  Before 2026-11-11 the owner had to say "Era" again after 45 s whenever music
  played (core/attention.py's media-only window), while a bare command worked in
  a silent room; and "standby" tore the microphone down and reopened it through
  a separate Vosk wake listener with its own word. Three clocks, three answers.

  One session, one rule:

    * the window opens when Era starts, and every ACCEPTED utterance (already
      past the owner voiceprint) re-anchors it;
    * inside the window, bare commands work;
    * outside it, an utterance must begin with the name / wake word;
    * Era's own reply re-anchors a window only after an explicit address, so a
      lyric cannot keep reopening it (the 2026-10-10 loop);
    * "standby" no longer means "disconnect": it closes the session, and the
      next "Era, ..." runs in the same warm session.

CONFIG (read on every call, so a voice order applies to the next utterance)
  config/session.json   {"window_s": 120.0, "standby_enabled": false}

  `window_s` is owner-adjustable by voice through the `manage_preference` tool
  (actions `set_session_window` / `get_session_window`).
  `standby_enabled` is false while the session model is being tested: standby
  mode is deliberately not in use, so there is only one way for Era to be quiet.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "session.json"

DEFAULT_WINDOW_S = 120.0
MIN_WINDOW_S = 10.0
MAX_WINDOW_S = 3600.0
DEFAULTS = {"window_s": DEFAULT_WINDOW_S, "standby_enabled": False}

_lock = threading.RLock()

_DURATION = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_UNIT_S = {
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
}


def parse_duration(text: str) -> float | None:
    """'2 min' / '90 seconds' / '1 minute 30 seconds' -> seconds (clamped).

    The model is asked to pass `seconds`, but a spoken order can arrive as the
    raw phrase; parsing it here keeps the voice command working either way.
    Returns None when no number+unit is present.
    """
    body = str(text or "").strip().lower()
    if not body:
        return None
    total = 0.0
    found = False
    for match in _DURATION.finditer(body):
        unit = match.group("unit").lower()
        factor = _UNIT_S.get(unit) or _UNIT_S.get(unit.rstrip("s"))
        if factor is None:
            continue
        try:
            total += float(match.group("value").replace(",", ".")) * factor
            found = True
        except ValueError:
            continue
    if not found:
        return None
    return _clamp(total)


def _clamp(seconds: float) -> float:
    return min(max(float(seconds), MIN_WINDOW_S), MAX_WINDOW_S)


def get_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            if isinstance(raw.get("window_s"), (int, float)):
                cfg["window_s"] = _clamp(raw["window_s"])
            if isinstance(raw.get("standby_enabled"), bool):
                cfg["standby_enabled"] = raw["standby_enabled"]
    except (OSError, ValueError, TypeError):
        pass
    return cfg


def window_seconds() -> float:
    return float(get_config()["window_s"])


def standby_enabled() -> bool:
    return bool(get_config()["standby_enabled"])


def _write(**updates) -> dict:
    cfg = get_config()
    cfg.update(updates)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="session-", suffix=".json", dir=CONFIG_PATH.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(cfg, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, CONFIG_PATH)
    finally:
        temp.unlink(missing_ok=True)
    return cfg


def human_duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f"{hours} hour" + ("" if hours == 1 else "s")
    if seconds >= 60:
        minutes, rest = divmod(int(round(seconds)), 60)
        if rest:
            return f"{minutes} min {rest} s"
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{int(round(seconds))} second" + ("" if int(round(seconds)) == 1 else "s")


def set_window_seconds(seconds: float) -> str:
    """Persist the session window and return the sentence Era should speak."""
    value = _clamp(seconds)
    _write(window_s=value)
    return f"Session window set to {human_duration(value)}."


def set_window_from_text(text_or_seconds) -> str:
    """Accept seconds from the model, or a raw phrase like '2 minutes'."""
    if isinstance(text_or_seconds, (int, float)):
        return set_window_seconds(float(text_or_seconds))
    parsed = parse_duration(str(text_or_seconds or ""))
    if parsed is None:
        return (
            "Not changed: I could not read a duration. Say for example "
            "'set the session time to 2 minutes'."
        )
    return set_window_seconds(parsed)


def set_standby_enabled(enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise ValueError("standby must be enabled or disabled explicitly")
    _write(standby_enabled=enabled)
    return "Standby mode enabled." if enabled else "Standby mode disabled."


def describe() -> str:
    cfg = get_config()
    state = "standby mode is ON" if cfg["standby_enabled"] else "standby mode is OFF"
    return f"Session window: {human_duration(cfg['window_s'])}; {state}."


class SessionWindow:
    """The clock: when the last accepted exchange happened.

    Deliberately tiny and clock-injectable so the rule can be tested without
    sleeping. `close()` pushes the anchor far enough back that the window is
    shut without inventing a second state machine.
    """

    _CLOSED = 1e9

    def __init__(self, clock=time.monotonic, window_s=None) -> None:
        self._clock = clock
        self._window_s = window_s  # None = read the config every time
        self._anchor = clock()

    @property
    def window_s(self) -> float:
        return float(self._window_s if self._window_s is not None else window_seconds())

    def note_accepted(self) -> None:
        """An utterance reached Era and was addressed to her (or inside the window)."""
        self._anchor = self._clock()

    def close(self) -> None:
        """Close the session: the next utterance must carry the name."""
        self._anchor = self._clock() - self._CLOSED

    def open(self) -> bool:
        return (self._clock() - self._anchor) <= self.window_s

    def remaining_s(self) -> float:
        return max(0.0, self.window_s - (self._clock() - self._anchor))
