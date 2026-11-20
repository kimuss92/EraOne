"""Speech-synthesis router — selected voice with verified phrase fallback.

Era has three ways to produce speech:

    fish_audio     Fish Audio cloud TTS.                 -> core/tts_fish.py
    kokoro_audio   Kokoro George on the local GPU.       -> core/tts_kokoro.py
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

from core.speech_frames import EnginePCM
from core.tts_fish import FISH

BACKEND_FISH = "fish"
BACKEND_NONE = "none"


def _fabric(component: str, kind: str | None, detail="") -> None:
    """Era Fabric F1: a backend's outcome on the shared health registry."""
    try:
        from core.fabric import health

        health.note(component, kind, detail)
    except Exception:  # noqa: BLE001
        pass


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
    """Yield PCM chunks for one sentence from the selected backend.

    An empty iterator means the backend produced nothing — the caller falls back
    to Gemini. Only Fish streams; Gemini's audio comes from the Live session, not
    from here. `lang` selects a per-language Fish voice when one is configured.
    """
    text = (text or "").strip()
    if not text:
        return
    from core.voice_mode import VOICE_CFG

    if VOICE_CFG.is_gemini():
        # Fallen back mid-turn (or switched): Gemini's own audio is speaking now.
        # The sentences still queued must stay silent, or a Kokoro fallback
        # would hand them to Fish and two voices would talk at once.
        return
    if VOICE_CFG.is_kokoro():
        from core.tts_kokoro import KOKORO

        if not KOKORO.ready:  # never make a sentence wait ~10 s for the load
            KOKORO.warm_in_background()
            KOKORO.last_error = "Kokoro is still loading"
            return
        async for chunk in KOKORO.stream(text, lang):
            if VOICE_CFG.is_gemini():
                return
            yield EnginePCM(chunk, "kokoro")
        return
    # Transport completion belongs to this request, not FISH.last_error (which
    # an overlapping warm-up can change). Never expose a failed phrase prefix.
    try:
        chunks = [chunk async for chunk in FISH.stream(text, lang)]
    except Exception:
        # Provider exceptions, like incomplete transport, leave no playable prefix.
        chunks = []
    if VOICE_CFG.is_gemini():
        return  # a live voice switch while synthesis awaited owns the output now
    _fabric("tts.fish", None if any(chunks) else "EmptyResult", FISH.last_error or "no audio")
    if not any(chunks):
        from core.tts_kokoro import KOKORO

        # Kokoro emits a fully synthesized phrase. Do not start a cold model
        # load in this hot path or change the user's selected voice permanently.
        if KOKORO.ready:
            async for chunk in KOKORO.stream(text, lang):
                if VOICE_CFG.is_gemini():
                    return
                yield EnginePCM(chunk, "kokoro")
        return
    for chunk in chunks:
        yield EnginePCM(chunk, "fish")


async def stream_text(text: str, lang: str = ""):
    """Speak external text without relying on audio from a Live-model reply.

    CodeMode supplies text even when the selected voice is Gemini. It must not
    use the ordinary stream's deliberate Gemini no-op, or replay unrelated Live
    audio. Selection is preserved; each returned phrase carries its real engine.
    """
    from core.voice_mode import VOICE_CFG

    if not VOICE_CFG.is_gemini():
        chunks = [chunk async for chunk in stream(text, lang)]
        if chunks:
            for chunk in chunks:
                yield chunk
            return
    from core import tts_gemini

    pcm = await tts_gemini.synthesize(text)
    if pcm:
        yield EnginePCM(pcm, "gemini")


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
