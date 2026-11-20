"""Where Era's voice actually goes in and out, for the HUD's System awareness.

Read-only evidence from the streams themselves: core.voice_input records the
microphone stream Era opened; the speaker side is status.audio_output (the
stream core.voice_output saw open, current generation only). A stream that has
not opened reports None, so the HUD falls back to the Windows default rather
than invent a device.
"""

from __future__ import annotations


def snapshot() -> dict:
    from app.hud.status import audio_output
    from core import voice_input

    inp = voice_input.active_input()
    out = audio_output()
    return {
        "input": None
        if not inp
        else {
            "name": inp.get("name") or "System default",
            "pinned": inp["pinned"],
            "at": inp["at"],
        },
        "output": None
        if not out.get("device")
        else {
            "name": out["device"],
            "pinned": out["pinned"],
            "fallback": out["fallback"],
            "at": None,
        },
        # Where MEDIA goes — a separate route from Era's voice: the remembered
        # media device and what is actually playing where (core/media_routing.py).
        "media": _media(),
    }


def _media() -> dict:
    try:
        from core import media_routing

        return media_routing.hud_snapshot()
    except Exception:
        return {"target": None, "why": "unavailable", "playing": []}
