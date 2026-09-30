"""Local voice: Piper (French + English) via sherpa-onnx, fully offline.

Era's speech chain while the Gemini Live session is down:

    Fish Audio (cloud, her cloned voice)  -> used whenever it is configured,
                                             reachable and not cooling down
    Piper (local, this module)            -> the floor: works with no network

Output is 24 kHz mono int16 — RECEIVE_SAMPLE_RATE, the format Era's player and
Fish already use — so both sources are interchangeable per sentence.

Measured 2026-09-23: ~0.2 s to synthesize a 2 s sentence on CPU.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import numpy as np

from app.audio_config import RECEIVE_SAMPLE_RATE
from core.utils import BASE_DIR

TTS_DIR = BASE_DIR / "models" / "local_voice" / "tts"
VOICES = {
    "fr": ("vits-piper-fr_FR-siwis-medium", "fr_FR-siwis-medium.onnx"),
    "en": ("vits-piper-en_US-lessac-medium", "en_US-lessac-medium.onnx"),
}

# Cheap language guess for text with no STT tag (typed, Telegram, tool output).
_FR_HINT = re.compile(
    r"[àâçéèêëîïôûùüÿœ]|\b(je|tu|vous|nous|est|les|des|une|pour|avec|pas|oui|"
    r"bonjour|merci|monsieur|c'est|qu'|d'accord)\b",
    re.IGNORECASE,
)


def guess_lang(text: str) -> str:
    return "fr" if len(_FR_HINT.findall(text or "")) >= 2 else "en"


def available(tts_dir: Path = TTS_DIR) -> tuple[bool, str]:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False, "sherpa-onnx is not installed"
    missing = [d for d, onnx in VOICES.values() if not (tts_dir / d / onnx).is_file()]
    if missing:
        return False, f"Piper voices missing in {tts_dir}: {', '.join(missing)}"
    return True, ""


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


class PiperVoice:
    """Both Piper voices, loaded lazily on first use. Thread-safe."""

    def __init__(self, tts_dir: Path = TTS_DIR) -> None:
        self._dir = tts_dir
        self._engines: dict[str, object] = {}
        self._lock = threading.Lock()

    def _engine(self, lang: str):
        lang = lang if lang in VOICES else "en"
        with self._lock:
            eng = self._engines.get(lang)
            if eng is None:
                import sherpa_onnx as so

                folder, onnx = VOICES[lang]
                d = self._dir / folder
                cfg = so.OfflineTtsConfig(
                    model=so.OfflineTtsModelConfig(
                        vits=so.OfflineTtsVitsModelConfig(
                            model=str(d / onnx),
                            tokens=str(d / "tokens.txt"),
                            data_dir=str(d / "espeak-ng-data"),
                        ),
                        num_threads=2,
                        provider="cpu",
                    )
                )
                eng = so.OfflineTts(cfg)
                self._engines[lang] = eng
            return eng

    def warm(self) -> None:
        for lang in VOICES:
            self._engine(lang)

    def synthesize(self, text: str, lang: str = "") -> bytes:
        """24 kHz int16 PCM for one sentence. b"" on any failure."""
        text = (text or "").strip()
        if not text:
            return b""
        try:
            audio = self._engine(lang or guess_lang(text)).generate(text, sid=0, speed=1.0)
            return to_pcm16(np.array(audio.samples, dtype=np.float32), audio.sample_rate)
        except Exception as e:
            print(f"[LOCAL-TTS] Piper failed: {type(e).__name__}: {e}")
            return b""


PIPER = PiperVoice()


async def synthesize_best(
    text: str, lang: str = "", *, allow_fish: bool = True
) -> tuple[bytes, str]:
    """(pcm, backend) — Fish when it can speak right now, else Piper.

    Fish is only attempted when configured and not cooling down after a
    failure; with the network down its first failure sets that cooldown, so
    every later sentence goes straight to Piper without a network wait.
    allow_fish=False is the user choosing the local voice (cascade mode).
    """
    try:
        from core.tts_fish import FISH, is_configured

        if allow_fish and is_configured() and not FISH.in_cooldown():
            pcm = await FISH.synthesize(text)
            if pcm:
                return pcm, "fish"
    except Exception as e:
        print(f"[LOCAL-TTS] Fish unavailable ({type(e).__name__}); using Piper.")
    pcm = await asyncio.to_thread(PIPER.synthesize, text, lang)
    return pcm, "piper"
