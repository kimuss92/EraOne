"""Dynamic audio-device detection for Era — Star Glass routing.

The routing must follow the HARDWARE, never a hard-coded assumption: Era's spoken
voice goes to the Star Glasses when they are connected and to the laptop's
default output when they are not, decided at runtime from the actual device list.
This module is the single place that answers "are the Star Glasses here, and
which device index are they?".

Identifiers are configurable (config: ``audio.glass_identifiers``) and matched
case-insensitively as substrings of the device name, so a firmware rename or a
second pair is a config edit, not a code change. Degrade, never raise: any probe
failure reports "not connected" rather than breaking the audio path. Results are
cached briefly so the playback hot path never enumerates devices per chunk.
"""

from __future__ import annotations

import time

# Matched case-insensitively as substrings of the device name. The model number
# is included so a generic "Star Glass" rename still resolves via CY03CB36.
_DEFAULT_IDENTIFIERS = ("star glass", "cy03cb36")

_CACHE_TTL_S = 4.0
_cache: dict = {"ts": 0.0, "state": None}


def _identifiers() -> tuple[str, ...]:
    """Configured glass name identifiers (lower-cased), or the built-in default."""
    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    audio = cfg.get("audio", {}) if isinstance(cfg.get("audio"), dict) else {}
    raw = audio.get("glass_identifiers")
    if isinstance(raw, (list, tuple)):
        cleaned = tuple(str(x).strip().lower() for x in raw if str(x).strip())
        if cleaned:
            return cleaned
    return _DEFAULT_IDENTIFIERS


def matches_glass(name: str, ids: tuple[str, ...] | None = None) -> bool:
    """True when a device name matches any configured glass identifier."""
    n = (name or "").lower()
    return any(i in n for i in (ids if ids is not None else _identifiers()))


def _device_name(dev) -> str:
    if isinstance(dev, dict):
        return str(dev.get("name", ""))
    return str(getattr(dev, "name", ""))


def _device_channels(dev, key: str) -> int:
    if isinstance(dev, dict):
        try:
            return int(dev.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0
    return int(getattr(dev, key, 0) or 0)


def probe_glass(force: bool = False) -> dict:
    """Detect the Star Glasses. Returns a dict, cached for ~4s.

    {connected, output_index, input_index, name}. The indices are sounddevice
    device indices — output_index is what _play_audio binds Era's voice to.
    """
    now = time.time()
    if not force and _cache["state"] is not None and now - _cache["ts"] < _CACHE_TTL_S:
        return _cache["state"]

    state = {"connected": False, "output_index": None, "input_index": None, "name": ""}
    try:
        import sounddevice as sd

        ids = _identifiers()
        for idx, dev in enumerate(sd.query_devices()):
            if not matches_glass(_device_name(dev), ids):
                continue
            state["connected"] = True
            if not state["name"]:
                state["name"] = _device_name(dev)
            if state["output_index"] is None and _device_channels(dev, "max_output_channels") > 0:
                state["output_index"] = idx
            if state["input_index"] is None and _device_channels(dev, "max_input_channels") > 0:
                state["input_index"] = idx
    except Exception:
        state = {"connected": False, "output_index": None, "input_index": None, "name": ""}

    _cache["state"] = state
    _cache["ts"] = now
    return state


def invalidate() -> None:
    """Drop the cache so the next probe re-enumerates (e.g. after a reconnect).

    Also the moment media routing has just moved (set_default_device calls this),
    so the voice is re-asserted onto its chosen device here: the glasses keep
    Era's voice while media goes to the speakers and back.
    """
    _cache["state"] = None
    _cache["ts"] = 0.0
    try:
        from core.voice_output import reassert_after_media_change

        reassert_after_media_change()
    except Exception as e:  # a cache drop must never fail because of the voice
        print(f"[AudioDevices] voice re-assert skipped: {type(e).__name__}: {e}")


def glasses_connected(force: bool = False) -> bool:
    return bool(probe_glass(force)["connected"])


def glass_output_index(force: bool = False):
    """sounddevice output index for the glasses, or None when not connected."""
    return probe_glass(force)["output_index"]


def routing_summary(force: bool = False) -> str:
    """One-line live routing state for the system prompt (fed into _build_config).

    A user-pinned voice device OVERRIDES the automatic description below: once
    the user has chosen where Era speaks, telling the model "your voice follows
    the glasses" would be a lie, and the model would then answer routing
    questions from the prompt instead of from the tool.
    """
    try:
        from core.voice_output import summary_line

        pinned = summary_line()
    except Exception as e:
        print(f"[AudioDevices] voice-pin summary skipped: {type(e).__name__}: {e}")
        pinned = ""
    if pinned:
        return pinned

    st = probe_glass(force)
    if st["connected"]:
        name = st["name"] or "Star Glasses"
        return (
            f"AUDIO ROUTING — Star Glasses CONNECTED ({name}). Your spoken voice plays "
            "through the glasses; the laptop speakers carry music/media. 'volume up', "
            "'volume down', 'louder', 'quieter' mean the SYSTEM (laptop) volume. To change "
            "the glasses volume use the glasses-only actions "
            "audio_control(action='glass_volume_up'/'glass_volume_down'/'glass_volume_set' "
            "value=0-100/'glass_mute'/'glass_unmute') — they drive the glasses even with no "
            "device given."
        )
    return (
        "AUDIO ROUTING — Star Glasses NOT connected. Everything uses the default laptop "
        "output and 'volume up/down' means the system volume. If asked for 'glass volume', "
        "say the glasses aren't connected rather than changing anything."
    )
