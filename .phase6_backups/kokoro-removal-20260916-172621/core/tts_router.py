"""Speech-synthesis router — picks the backend for the active voice mode.

Era has three ways to produce speech, and this is the one place that decides
which is used for a given sentence:

    fish_audio     Fish Audio cloud TTS (PRIMARY).  -> core/tts_fish.py
    kokoro_*       Voicebox/Kokoro, local.          -> core/tts_voicebox.py
    gemini_audio   Gemini's own Live audio (FALLBACK). Not synthesized here at
                   all — Gemini already produced it inside the Live session, so
                   "falling back to Gemini" means letting that audio through
                   rather than calling another API. See EraLive._tts_fallback.

Returns ``(pcm, backend)``. An EMPTY pcm with backend "fish" is the fallback
signal: Fish could not speak this sentence, so the caller must hand the turn to
Gemini. Everything degrades — a failure here costs a backend, never the process.
"""

from __future__ import annotations

from core.tts_fish import FISH
from core.tts_voicebox import VOICEBOX
from core.voice_mode import VOICE_CFG

BACKEND_FISH = "fish"
BACKEND_KOKORO = "kokoro"
BACKEND_NONE = "none"


async def synthesize(text: str) -> tuple[bytes, str]:
    """Synthesize one sentence with the backend the active mode calls for.

    (pcm, backend). pcm == b"" means that backend failed; with backend "fish"
    the caller should fall back to Gemini's native audio.
    """
    text = (text or "").strip()
    if not text:
        return b"", BACKEND_NONE

    if VOICE_CFG.is_fish():
        pcm = await FISH.synthesize(text)
        return pcm, BACKEND_FISH

    # Kokoro modes keep their existing behaviour untouched.
    pcm = await VOICEBOX.synthesize(text)
    return pcm, BACKEND_KOKORO


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
