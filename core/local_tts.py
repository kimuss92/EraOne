"""Era's speech chain while the Gemini Live session is down.

    Fish Audio (cloud, her cloned voice)  -> used whenever it is configured,
                                             reachable and not cooling down
    Kokoro George (local GPU)             -> English (core/tts_kokoro.py)

Era has exactly three voices: Gemini, Fish and Kokoro (Karim, 2026-10-10). Piper,
the old CPU floor, was removed that day, so a French sentence with Fish down has
no voice and is shown as text (voice_unavailable_notice), never mangled by George.

Output is 24 kHz mono int16 — RECEIVE_SAMPLE_RATE, the format Era's player and
Fish already use — so both sources are interchangeable per sentence.
"""

from __future__ import annotations

import asyncio
import os
import re

import numpy as np

from app.audio_config import RECEIVE_SAMPLE_RATE

# Cheap language guess for text with no STT tag (typed, Telegram, tool output).
_FR_HINT = re.compile(
    r"[àâçéèêëîïôûùüÿœ]|\b(je|tu|vous|nous|est|les|des|une|pour|avec|pas|oui|"
    r"bonjour|merci|monsieur|c'est|qu'|d'accord)\b",
    re.IGNORECASE,
)


def guess_lang(text: str) -> str:
    return "fr" if len(_FR_HINT.findall(text or "")) >= 2 else "en"


def available() -> tuple[bool, str]:
    """Can the local loop speak at all? Kokoro installed, or Fish configured."""
    try:
        from core.tts_kokoro import available as kokoro_available

        ok, why = kokoro_available()
        if ok:
            return True, ""
    except Exception as e:
        why = f"Kokoro unavailable: {type(e).__name__}"
    try:
        from core.tts_fish import is_configured

        if is_configured():
            return True, ""
    except Exception:
        pass
    return False, f"no local voice: {why}; Fish Audio has no key"


def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    n_out = int(round(len(samples) * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def to_pcm16(samples: np.ndarray, src_rate: int) -> bytes:
    out = _resample(np.asarray(samples, dtype=np.float32), src_rate, RECEIVE_SAMPLE_RATE)
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


async def synthesize_best(
    text: str, lang: str = "", *, allow_fish: bool = True
) -> tuple[bytes, str]:
    """(pcm, backend) — Fish, else Kokoro (English, GPU), else (b"", "none").

    Fish is only attempted when configured and not cooling down after a
    failure; with the network down its first failure sets that cooldown, so
    every later sentence goes straight to Kokoro without a network wait.
    allow_fish=False is the user choosing the local voice (cascade mode).
    Kokoro warms at startup (core/voice_mode); a cold one is never waited for.

    (b"", "none") means text only; voice_unavailable_notice() then hands the
    caller one HUD line for the outage.
    """
    failed: list[tuple[str, str]] = []  # (backend, why) in the order they failed
    try:
        from core.tts_fish import FISH, detect_lang, is_configured

        if allow_fish and is_configured() and not FISH.in_cooldown():
            pcm = await FISH.synthesize(text, detect_lang(text))
            if pcm:
                _voice_back()
                return pcm, "fish"
            failed.append(("fish", FISH.last_error or "no audio"))
    except Exception as e:
        failed.append(("fish", type(e).__name__))
        print(f"[LOCAL-TTS] Fish unavailable ({type(e).__name__}); trying Kokoro.")
    lang_k = _kokoro_language(text)
    if lang_k == "en":
        pcm, why = await _kokoro_pcm(text)
        if pcm:
            return _spoke(pcm, "kokoro", failed)
        failed.append(("kokoro", why or "still loading"))
    else:
        failed.append(("kokoro", f"not English ({lang_k})"))
    _voice_lost("; ".join(f"{b}: {why}" for b, why in failed))
    return b"", "none"


def _kokoro_language(text: str) -> str:
    """George is English-only; the same judge Kokoro itself uses."""
    try:
        from core.tts_kokoro import language_of

        return language_of(text)
    except Exception:
        return guess_lang(text)


async def _kokoro_pcm(text: str) -> tuple[bytes, str]:
    """(pcm, why-not). why is "" when Kokoro was simply not tried (still
    loading, not installed, under pytest): that is not a failure."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return b"", ""
    try:
        from core.tts_kokoro import KOKORO

        if not KOKORO.ready:
            KOKORO.warm_in_background()
            return b"", ""
        pcm = await asyncio.to_thread(KOKORO.synthesize, text)
        return pcm, "" if pcm else (KOKORO.last_error or "no audio")
    except Exception as e:
        return b"", f"Kokoro failed: {type(e).__name__}"


def _spoke(pcm: bytes, backend: str, failed: list[tuple[str, str]]) -> tuple[bytes, str]:
    if failed:
        _event(
            "voice.fallback",
            from_backend=failed[0][0],
            to_backend=backend,
            reason="; ".join(why if b == "fish" else f"{b}: {why}" for b, why in failed),
        )
    _voice_back()
    return pcm, backend


# ── Visibility: every fall-through is an event; total loss is said once ──────
_outage = {"reason": "", "noticed": True}


def _event(name: str, **fields) -> None:
    try:
        from core.logger import log_event, main_logger

        log_event(main_logger, name, "WARNING", **fields)
    except Exception:
        pass  # logging must never cost the voice


def _voice_lost(reason: str) -> None:
    if not _outage["reason"]:
        _outage.update(reason=reason, noticed=False)
        _event("voice.unavailable", from_backend="kokoro", to_backend="text", reason=reason)


def _voice_back() -> None:
    if _outage["reason"]:
        _event("voice.restored", reason=_outage["reason"])
        _outage.update(reason="", noticed=True)


def voice_unavailable_notice() -> str:
    """The HUD line for a total voice outage, ONCE per outage; "" otherwise."""
    if not _outage["reason"] or _outage["noticed"]:
        return ""
    _outage["noticed"] = True
    return f"SYS: Voice unavailable ({_outage['reason']}) - replying in text only."
