"""Speech-synthesis router — Fish Audio primary, Gemini fallback.

Era has exactly two ways to produce speech:

    fish_audio     Fish Audio cloud TTS (PRIMARY).  -> core/tts_fish.py
    gemini_audio   Gemini's own Live audio (FALLBACK). Not synthesized here at
                   all — Gemini already produced it inside the Live session, so
                   "falling back to Gemini" means letting that held audio
                   through rather than calling another API. See
                   AudioIOMixin._tts_fallback.

Returns ``(pcm, backend)``. An EMPTY pcm with backend "fish" is the fallback
signal: Fish could not speak this sentence, so the caller must hand the turn to
Gemini. Everything degrades — a failure here costs a backend, never the process.
"""

from __future__ import annotations

from core.tts_fish import FISH

BACKEND_FISH = "fish"
BACKEND_NONE = "none"


async def synthesize(text: str) -> tuple[bytes, str]:
    """Synthesize one sentence with the primary backend.

    (pcm, backend). pcm == b"" with backend "fish" means the caller should fall
    back to Gemini's native audio.
    """
    text = (text or "").strip()
    if not text:
        return b"", BACKEND_NONE
    pcm = await FISH.synthesize(text)
    return pcm, BACKEND_FISH


async def stream(text: str, lang: str = ""):
    """Yield PCM chunks for one sentence from the primary backend, as they arrive.

    An empty iterator means the backend produced nothing — the caller falls back
    to Gemini. Only Fish streams; Gemini's audio comes from the Live session, not
    from here. `lang` selects a per-language Fish voice when one is configured.
    """
    text = (text or "").strip()
    if not text:
        return
    async for chunk in FISH.stream(text, lang):
        yield chunk


def primary_status() -> str:
    """One-line health summary of the primary backend, for logs/doctor."""
    from core.tts_fish import is_configured

    if not is_configured():
        return "Fish Audio: no API key configured (set FISH_AUDIO_API_KEY)"
    if FISH.in_cooldown():
        return (
            f"Fish Audio: cooling down {int(FISH.cooldown_remaining())}s "
            f"after '{FISH.last_error}' — Gemini is covering"
        )
    return "Fish Audio: ready"
