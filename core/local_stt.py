"""Local ears: Silero VAD + Whisper-small, fully offline (sherpa-onnx, CPU).

WHY
  Era normally hears through the Gemini Live session — raw PCM in, no STT stage.
  When that session is down (quota on every key, bad key, no network) she had no
  ears at all. This is the stand-in used by app/era_live/local_fallback.py.

PIPELINE
  16 kHz int16 mic frames -> Silero VAD (segments one utterance, and reports
  speech-in-progress for barge-in) -> Whisper-small int8 with language
  auto-detection. Measured 2026-09-23 on this laptop: exact French and English
  transcripts, language tagged correctly, ~1.4 s per 2 s utterance.

MODELS (models/local_voice/, git-ignored; see docs/LOCAL_FALLBACK.md)
  silero_vad.onnx, whisper-small/small-{encoder,decoder}.int8.onnx + tokens.

Degrade, never raise: a missing model makes available() say why; it never
takes the process down.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.utils import BASE_DIR

MODEL_DIR = BASE_DIR / "models" / "local_voice"
SAMPLE_RATE = 16000
_WINDOW = 512  # Silero's frame size at 16 kHz

# Whisper invents these from silence, breathing or keyboard noise. Matching is
# on the whole normalised utterance, so a real sentence containing "merci" is
# never dropped.
_HALLUCINATIONS = {
    "thank you",
    "thank you.",
    "thanks for watching",
    "thanks for watching!",
    "thank you for watching",
    "merci",
    "merci.",
    "sous-titres réalisés par la communauté d'amara.org",
    "sous-titrage st' 501",
    "you",
    "bye",
    "bye.",
    "...",
}
_MIN_SPEECH_S = 0.35
# Silero marks speech only once it is sure, so its segment starts a little late:
# the benchmark heard "Open notepad" as "The notepad" and "What time is it" as
# "Time is it" (2026-09-23). This much audio from BEFORE the detected start is
# put back on the front of every segment.
_PREROLL = int(0.4 * SAMPLE_RATE)
# Enough history to reach back past the longest segment (max_speech 20 s).
_HISTORY = int(22 * SAMPLE_RATE)


@dataclass(frozen=True)
class Utterance:
    text: str
    lang: str  # "fr", "en", ... ("" when Whisper did not say)
    audio_s: float
    stt_ms: int


def _paths(model_dir: Path) -> dict[str, Path]:
    w = model_dir / "whisper-small"
    return {
        "vad": model_dir / "silero_vad.onnx",
        "encoder": w / "small-encoder.int8.onnx",
        "decoder": w / "small-decoder.int8.onnx",
        "tokens": w / "small-tokens.txt",
    }


def available(model_dir: Path = MODEL_DIR) -> tuple[bool, str]:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False, "sherpa-onnx is not installed (pip install sherpa-onnx)"
    missing = [p.name for p in _paths(model_dir).values() if not p.is_file()]
    if missing:
        return False, f"local STT models missing in {model_dir}: {', '.join(missing)}"
    return True, ""


def is_hallucination(text: str) -> bool:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return not norm or norm in _HALLUCINATIONS or not re.search(r"\w", norm)


# A misheard French utterance usually comes back tagged as a neighbouring
# Romance language; anything else falls to the first configured language.
_ROMANCE = frozenset({"es", "pt", "it", "ro", "ca", "gl", "oc"})


def _decode(rec, samples: np.ndarray) -> tuple[str, str]:
    """(text, detected language) for one segment."""
    stream = rec.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    rec.decode_stream(stream)
    result = stream.result
    lang = str(getattr(result, "lang", "") or "").strip("<|>").lower()
    return (result.text or "").strip(), lang


def _threads() -> int:
    try:
        n = int(os.environ.get("ERA_LOCAL_STT_THREADS", "") or 0)
    except ValueError:
        n = 0
    return n if n > 0 else 4


class LocalListener:
    """Feed mic PCM; get finished utterances. Thread-safe, lazily loaded.

    feed() is cheap (VAD only) and returns raw speech segments; transcribe() is
    the heavy Whisper call and belongs on a worker thread.
    """

    def __init__(self, model_dir: Path = MODEL_DIR, languages: tuple[str, ...] = ()) -> None:
        self._model_dir = model_dir
        # Languages the owner actually speaks (config: routing.local_fallback.languages).
        # Empty = trust Whisper's auto-detect. See _restrict().
        self._languages = tuple(lang.lower() for lang in languages)
        self._forced: dict[str, object] = {}  # language -> recognizer, built on demand
        self._lock = threading.Lock()
        self._vad = None
        self._rec = None
        self._pending = np.zeros(0, dtype=np.float32)
        # Audio already given to the VAD, as (absolute start index, samples),
        # so a segment's pre-roll can be recovered. Indices count from reset().
        self._history: deque[tuple[int, np.ndarray]] = deque()
        self._fed = 0

    def load(self) -> None:
        """Load both models. ~3 s cold; call once off the event loop."""
        with self._lock:
            if self._vad is not None and self._rec is not None:
                return
            ok, why = available(self._model_dir)
            if not ok:
                raise RuntimeError(why)
            import sherpa_onnx as so

            p = _paths(self._model_dir)
            cfg = so.VadModelConfig()
            cfg.silero_vad.model = str(p["vad"])
            cfg.silero_vad.threshold = 0.5
            cfg.silero_vad.min_silence_duration = 0.5  # end of utterance
            cfg.silero_vad.min_speech_duration = 0.25
            cfg.silero_vad.max_speech_duration = 20.0
            cfg.silero_vad.window_size = _WINDOW
            cfg.sample_rate = SAMPLE_RATE
            cfg.num_threads = 1
            self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
            # Auto-detect first: the owner switches between French and English.
            self._rec = self._make_recognizer("")

    def _make_recognizer(self, language: str):
        import sherpa_onnx as so

        p = _paths(self._model_dir)
        return so.OfflineRecognizer.from_whisper(
            encoder=str(p["encoder"]),
            decoder=str(p["decoder"]),
            tokens=str(p["tokens"]),
            language=language,
            task="transcribe",
            num_threads=_threads(),
        )

    @property
    def loaded(self) -> bool:
        return self._vad is not None and self._rec is not None

    def speech_active(self) -> bool:
        """True while the user is mid-utterance (drives barge-in)."""
        vad = self._vad
        try:
            return bool(vad is not None and vad.is_speech_detected())
        except Exception:
            return False

    def reset(self) -> None:
        with self._lock:
            self._pending = np.zeros(0, dtype=np.float32)
            self._history.clear()
            self._fed = 0
            if self._vad is not None:
                self._vad.reset()

    def _audio_between(self, start: int, end: int) -> np.ndarray:
        parts = []
        for at, chunk in self._history:
            lo, hi = max(start, at), min(end, at + len(chunk))
            if lo < hi:
                parts.append(chunk[lo - at : hi - at])
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def feed(self, pcm_int16: bytes) -> list[np.ndarray]:
        """Push 16 kHz mono int16 PCM. Returns completed speech segments."""
        if self._vad is None:
            return []
        samples = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        segments: list[np.ndarray] = []
        with self._lock:
            buf = np.concatenate([self._pending, samples])
            n = (len(buf) // _WINDOW) * _WINDOW
            for i in range(0, n, _WINDOW):
                self._vad.accept_waveform(buf[i : i + _WINDOW])
            if n:
                self._history.append((self._fed, buf[:n].copy()))
                self._fed += n
                while self._history and self._history[0][0] + _HISTORY < self._fed:
                    self._history.popleft()
            self._pending = buf[n:]
            while not self._vad.empty():
                seg = self._vad.front
                speech = np.array(seg.samples, dtype=np.float32)
                start = int(seg.start)
                pre = self._audio_between(max(0, start - _PREROLL), start)
                segments.append(np.concatenate([pre, speech]) if len(pre) else speech)
                self._vad.pop()
        return segments

    def transcribe(self, samples: np.ndarray) -> Utterance | None:
        """Whisper one segment. None for too-short audio or a hallucination."""
        if self._rec is None:
            return None
        audio_s = len(samples) / SAMPLE_RATE
        if audio_s < _MIN_SPEECH_S:
            return None
        started = time.perf_counter()
        text, lang = _decode(self._rec, samples)
        target = self._restrict(lang)
        if target:
            # Whisper-small heard French/English as Turkish, Russian, Portuguese...
            # (2026-09/10 logs: "Резюм Музыка", "Hanıbar izi"). Decode again in
            # the language the owner actually speaks.
            text, _ = _decode(self._forced_recognizer(target), samples)
            lang = target
        stt_ms = int((time.perf_counter() - started) * 1000)
        if is_hallucination(text):
            return None
        return Utterance(text=text, lang=lang, audio_s=round(audio_s, 2), stt_ms=stt_ms)

    def _restrict(self, lang: str) -> str:
        """The allowed language to re-decode in, or "" when `lang` is acceptable."""
        if not self._languages or not lang or lang in self._languages:
            return ""
        if lang in _ROMANCE and "fr" in self._languages:
            return "fr"
        return self._languages[0]

    def _forced_recognizer(self, language: str):
        rec = self._forced.get(language)
        if rec is None:
            rec = self._forced[language] = self._make_recognizer(language)
        return rec
