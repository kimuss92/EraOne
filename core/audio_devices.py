"""Dynamic audio-device detection for Era — Star Glass routing.

The routing must follow the hardware, never a hard-coded assumption: Era's
spoken voice goes to the Star Glasses when they are connected and to the
laptop's default output when they are not.

PortAudio enumeration can briefly omit a Bluetooth endpoint while Windows is
refreshing it. Treating one omission as a real disconnect made the route flap:
the voice stream fell back, the next enumeration rediscovered the glasses, and
the cycle repeated. Positive detections are therefore accepted immediately,
while a previously connected endpoint must remain absent for several probes
and a bounded grace period before it is published as disconnected.

Identifiers are configurable through ``audio.glass_identifiers`` and matched
case-insensitively as substrings. Every entry point degrades rather than
raising; no probe failure is allowed to break the audio path.
"""

from __future__ import annotations

import time

_DEFAULT_IDENTIFIERS = ("star glass", "cy03cb36")

# Normal probes are cached so playback does not enumerate devices per chunk.
_CACHE_TTL_S = 4.0

# A single Windows/PortAudio refresh commonly lasts one cache interval. Require
# both multiple misses and enough elapsed time to distinguish that refresh from
# an actual removal. The bound still lets a real disconnect fall back quickly.
_DISCONNECT_GRACE_S = 8.0
_DISCONNECT_MISSES = 2

_cache: dict = {"ts": 0.0, "state": None}
_stability: dict = {
    "published": None,
    "absent_since": None,
    "misses": 0,
}


def _empty_state() -> dict:
    return {"connected": False, "output_index": None, "input_index": None, "name": ""}


def _identifiers() -> tuple[str, ...]:
    """Configured glass name identifiers, lower-cased, or built-in defaults."""
    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    audio = cfg.get("audio", {}) if isinstance(cfg.get("audio"), dict) else {}
    raw = audio.get("glass_identifiers")
    if isinstance(raw, (list, tuple)):
        cleaned = tuple(str(item).strip().lower() for item in raw if str(item).strip())
        if cleaned:
            return cleaned
    return _DEFAULT_IDENTIFIERS


def matches_glass(name: str, ids: tuple[str, ...] | None = None) -> bool:
    """Return whether a device name contains a configured glass identifier."""
    normalized = (name or "").lower()
    return any(identifier in normalized for identifier in (ids or _identifiers()))


def _device_name(device) -> str:
    if isinstance(device, dict):
        return str(device.get("name", ""))
    return str(getattr(device, "name", ""))


def _device_channels(device, key: str) -> int:
    try:
        value = device.get(key, 0) if isinstance(device, dict) else getattr(device, key, 0)
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _raw_probe() -> dict:
    """Perform one PortAudio enumeration without caching or stabilization."""
    state = _empty_state()
    try:
        import sounddevice as sd

        identifiers = _identifiers()
        for index, device in enumerate(sd.query_devices()):
            if not matches_glass(_device_name(device), identifiers):
                continue
            state["connected"] = True
            if not state["name"]:
                state["name"] = _device_name(device)
            if (
                state["output_index"] is None
                and _device_channels(device, "max_output_channels") > 0
            ):
                state["output_index"] = index
            if state["input_index"] is None and _device_channels(device, "max_input_channels") > 0:
                state["input_index"] = index
    except Exception:
        return _empty_state()
    return state


def _stabilize(raw: dict, now: float) -> dict:
    """Publish confirmed routing state while suppressing transient omissions."""
    if raw["connected"]:
        published = dict(raw)
        _stability["published"] = published
        _stability["absent_since"] = None
        _stability["misses"] = 0
        return dict(published)

    previous = _stability["published"]
    if not previous or not previous.get("connected"):
        disconnected = _empty_state()
        _stability["published"] = disconnected
        _stability["absent_since"] = None
        _stability["misses"] = 0
        return dict(disconnected)

    if _stability["absent_since"] is None:
        _stability["absent_since"] = now
    _stability["misses"] += 1
    absent_for = now - float(_stability["absent_since"])

    if _stability["misses"] < _DISCONNECT_MISSES or absent_for < _DISCONNECT_GRACE_S:
        return dict(previous)

    disconnected = _empty_state()
    _stability["published"] = disconnected
    _stability["absent_since"] = None
    _stability["misses"] = 0
    return dict(disconnected)


def probe_glass(force: bool = False) -> dict:
    """Detect the Star Glasses and return routing indices.

    The result has ``connected``, ``output_index``, ``input_index`` and
    ``name`` fields. ``force`` bypasses only the hot-path cache; disconnect
    hysteresis remains active because recovery loops also issue forced probes.
    """
    now = time.time()
    cached = _cache["state"]
    if not force and cached is not None and now - _cache["ts"] < _CACHE_TTL_S:
        return dict(cached)

    state = _stabilize(_raw_probe(), now)
    _cache["state"] = dict(state)
    _cache["ts"] = now
    return dict(state)


def invalidate() -> None:
    """Force re-enumeration on the next probe without discarding stability.

    Keeping the last published state is intentional: media-route changes call
    this function and often coincide with a momentary PortAudio list refresh.
    Clearing stability here would reintroduce the reconnect loop.
    """
    _cache["state"] = None
    _cache["ts"] = 0.0
    try:
        from core.voice_output import reassert_after_media_change

        reassert_after_media_change()
    except Exception as exc:
        print(f"[AudioDevices] voice re-assert skipped: {type(exc).__name__}: {exc}")


def _reset_probe_state() -> None:
    """Reset process-local probe state. Intended for deterministic tests."""
    _cache["state"] = None
    _cache["ts"] = 0.0
    _stability["published"] = None
    _stability["absent_since"] = None
    _stability["misses"] = 0


def glasses_connected(force: bool = False) -> bool:
    return bool(probe_glass(force)["connected"])


def glass_output_index(force: bool = False):
    """Return the sounddevice output index, or None when disconnected."""
    return probe_glass(force)["output_index"]


def routing_summary(force: bool = False) -> str:
    """Return live voice routing plus the independently managed media route."""
    return _voice_routing_summary(force) + _media_summary()


def _media_summary() -> str:
    try:
        from core.media_routing import media_target

        target, reason = media_target()
    except Exception:
        return ""
    where = target.name if target else "the system default"
    return (
        f" MEDIA OUTPUT — music and video play on {where} ({reason}), separately from your "
        "voice. Move them with audio_control(action='route_media'); check with "
        "audio_control(action='media_routing') before saying where anything plays."
    )


def _voice_routing_summary(force: bool = False) -> str:
    try:
        from core.voice_output import summary_line

        pinned = summary_line()
    except Exception as exc:
        print(f"[AudioDevices] voice-pin summary skipped: {type(exc).__name__}: {exc}")
        pinned = ""
    if pinned:
        return pinned

    state = probe_glass(force)
    if state["connected"]:
        name = state["name"] or "Star Glasses"
        return (
            f"AUDIO ROUTING — Star Glasses CONNECTED in the engine's device list ({name}). "
            "The automatic voice policy prefers glasses; this is not playback verification. "
            "Query voice-output status before claiming where speech is playing. Media follows "
            "its own routing. 'volume up', 'volume down', 'louder', 'quieter' mean the SYSTEM "
            "(laptop) volume. To change the glasses volume use the glasses-only actions "
            "audio_control(action='glass_volume_up'/'glass_volume_down'/'glass_volume_set' "
            "value=0-100/'glass_mute'/'glass_unmute') — they drive the glasses even with no "
            "device given."
        )
    return (
        "AUDIO ROUTING — Star Glasses NOT connected in the engine's device list. The automatic "
        "voice policy requests the system default; actual playback is unverified. "
        "'volume up/down' means the system volume. If asked for 'glass volume', say the "
        "glasses aren't connected rather than changing anything."
    )
