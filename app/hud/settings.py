"""HUD preferences, persisted in runtime/hud_settings.json.

Only presentation preferences live here — never credentials. Every write is
validated field by field; an unknown key or a bad value is rejected, not stored.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

DEFAULTS: dict = {
    # The HUD weather card's location. A setting, not a hard-coded value: the
    # previous UI shipped a fixed list of Tunisian cities in the frontend.
    "weather_city": "Tunis",
    # Subtle HUD cues (wake, tool complete, error). Off unless chosen.
    "hud_sounds": False,
    # Open the HUD automatically when the backend starts on its own (logon).
    "open_hud_on_start": True,
    # Prefer a Chromium app window (no tabs/address bar) over a browser tab.
    "app_mode": True,
}

_CITY_RE = re.compile(r"^[\w\s\-'.,()]{1,60}$", re.UNICODE)
_lock = threading.Lock()


def settings_path() -> Path:
    env = os.environ.get("ERA_HUD_SETTINGS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "runtime" / "hud_settings.json"


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                    data[k] = v
    except (OSError, ValueError):
        pass
    return data


def _validate(key: str, value: object) -> object:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}")
    expected = type(DEFAULTS[key])
    if expected is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if key == "weather_city":
        city = " ".join(str(value).split())
        if not city or not _CITY_RE.match(city):
            raise ValueError("weather_city must be a place name (letters, spaces, - ' . ,)")
        return city
    raise ValueError(f"{key} cannot be set")


def update(patch: dict) -> dict:
    """Validate and persist a partial update. Raises ValueError on bad input."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("expected an object of settings")
    clean = {k: _validate(k, v) for k, v in patch.items()}
    with _lock:
        data = load()
        data.update(clean)
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return data
