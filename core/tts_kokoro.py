"""Kokoro-82M on the GPU: Era's fast local voice (George by default).

Same model and voices as the Voicebox app (Kokoro v1.0), but run IN-PROCESS on
the RTX 3070 through onnxruntime-gpu, with no server and no extra hop.

Measured 2026-10-10 (i7-12700H + RTX 3070 laptop, a new sentence length on
every call):
    CUDA   ~0.3 s for a short sentence, median 0.43 s, ~10x realtime
    CPU    ~0.7-0.9x realtime (sherpa-onnx, onnxruntime 1.31, fp32 or int8)
    DirectML fails on the model's grouped ConvTranspose
so the GPU is what makes Kokoro a fast voice at all; CPU is only a slow floor.

Where things live (all OUTSIDE Era's Python, so nothing Era imports changes):
    D:\\Kokoro\\kokoro-multi-lang-v1_0   model.onnx, voices.bin, tokens.txt and
                                        lexicon-gb-en.txt (misaki's British dictionary)
    D:\\Kokoro\\rt_gpu                   onnxruntime-gpu 1.31 + CUDA 13 / cuDNN 9
                                        wheels, espeakng-loader, phonemizer-fork
The runtime dir is APPENDED to sys.path, so Era's own numpy/protobuf always win
and only the packages Era lacks (onnxruntime, nvidia, phonemizer) come from it.

Text -> phonemes the way misaki does it: the dictionary first; espeak-ng (en-gb)
for unknown words and anything with digits, mapped into Kokoro's symbols with
misaki's own table. Overrides: config/voice.json {"kokoro": {...}} (see DEFAULTS).

Degrade, never raise: any failure returns b"" and records last_error, so the
caller falls through to the next voice.
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.machinery
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

DEFAULTS = {
    "model_dir": r"D:\Kokoro\kokoro-multi-lang-v1_0",
    "runtime_dir": r"D:\Kokoro\rt_gpu",
    "voice": "bm_george",
    "speed": 1.0,
    "provider": "cuda",  # "cuda" | "cpu"
    # Speech level each sentence is brought to, dBFS RMS. Raw Kokoro measured
    # -20.5 (2026-10-10), ~5 dB under Fish/Gemini (-15..-17), so at full volume
    # George still sounded quiet. Clamped to -30..-9.
    "loudness_db": -15.0,
}
SAMPLE_RATE = 24000
_PEAK_CEILING = 0.89  # -1 dBFS: the soft limiter never lets a peak past this
_MAX_BOOST_DB = 12.0  # never lift near-silence into audible noise
_MAX_TOKENS = 510  # Kokoro's context, padding included
_VOICE_JSON = Path(__file__).resolve().parent.parent / "config" / "voice.json"

# misaki/espeak.py EspeakFallback.E2M (British), longest first.
_E2M = sorted(
    {
        "ʔˌn\u0329": "ʔn",
        "ʔn\u0329": "ʔn",
        "a^ɪ": "I",
        "a^ʊ": "W",
        "d^ʒ": "ʤ",
        "e^ɪ": "A",
        "e": "A",
        "t^ʃ": "ʧ",
        "ɔ^ɪ": "Y",
        "ə^l": "ᵊl",
        "ʲo": "jo",
        "ʲə": "jə",
        "ʲ": "",
        "ɚ": "əɹ",
        "r": "ɹ",
        "x": "k",
        "ç": "k",
        "ɐ": "ə",
        "ɬ": "l",
        "\u0303": "",
    }.items(),
    key=lambda kv: -len(kv[0]),
)
# Numbers keep their inner separators (10:30, 3.5, 1,200, 50%, 4K) but never
# swallow the sentence's closing punctuation. Words are UNICODE letters: A-Z only
# split "tâche" into "t"+"che" and "café" into "caf" (live, 2026-10-10).
_L = r"[^\W\d_]"
_WORD = re.compile(rf"{_L}*\d+(?:[:.,/]\d+)*(?:{_L}|%)*|{_L}+(?:['’]{_L}+)*|[,.!?;:—…\"()]")
_SYMBOLS = (("m²", " square metres"), ("m³", " cubic metres"), ("²", " squared"), ("³", " cubed"))

# George is an English voice. A French or Arabic sentence is left to Gemini's
# own voice (Karim's 2026-10-10 rule for Fish, applied here too): George reading
# French with English rules is what "didn't finish the phrase" sounded like.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_FR_WORDS = re.compile(
    r"\b(?:je|j'|tu|il|elle|nous|vous|ils|est|sont|les|des|une|pour|avec|pas|oui|"
    r"bonjour|bonsoir|merci|monsieur|madame|c'est|qu'|d'accord|que|qui|dans|sur|du|"
    r"aux|et|mais|très|voilà|certainement|bien|votre|vos|mon|mes|cette|aimeriez|"
    r"préférez|fasse)\b",
    re.IGNORECASE,
)


def language_of(text: str) -> str:
    """'ar' | 'fr' | 'en' for one sentence. French needs two function words, so an
    English line with one brand name or loanword ("café", "Céramique") stays English."""
    text = text or ""
    if _ARABIC.search(text):
        return "ar"
    return "fr" if len(_FR_WORDS.findall(text.replace("’", "'"))) >= 2 else "en"


def _spoken_symbols(text: str) -> str:
    for sym, words in _SYMBOLS:
        text = text.replace(sym, words)
    return text


def _under_pytest() -> bool:
    """True from COLLECTION on: PYTEST_CURRENT_TEST alone is unset while pytest
    imports test modules, which is when core/voice_mode installs the redirect."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _log(msg: str) -> None:
    print(f"[KOKORO] {msg}")


def get_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        block = json.loads(_VOICE_JSON.read_text(encoding="utf-8-sig")).get("kokoro")
        if isinstance(block, dict):
            cfg.update({k: v for k, v in block.items() if k in DEFAULTS and v not in (None, "")})
    except Exception:
        pass
    try:
        cfg["speed"] = min(max(float(cfg["speed"]), 0.5), 2.0)
    except (TypeError, ValueError):
        cfg["speed"] = DEFAULTS["speed"]
    try:
        cfg["loudness_db"] = min(max(float(cfg["loudness_db"]), -30.0), -9.0)
    except (TypeError, ValueError):
        cfg["loudness_db"] = DEFAULTS["loudness_db"]
    return cfg


def normalize_loudness(audio, target_db: float):
    """Bring one sentence's SPEECH to target_db RMS, then soft-limit the peaks.

    Speech RMS ignores the near-silent samples, so pauses do not drag the level
    down. The gain is capped (+_MAX_BOOST_DB) and a tanh knee rounds peaks off
    under _PEAK_CEILING: Kokoro's peaks sit only ~7 dB above its RMS, so a plain
    multiply would clip and crackle.
    """
    import numpy as np

    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    speech = x[np.abs(x) > 0.01]
    if speech.size == 0:
        return x
    rms = float(np.sqrt(np.mean(speech**2)))
    gain_db = min(target_db - 20.0 * np.log10(max(rms, 1e-9)), _MAX_BOOST_DB)
    y = x * np.float32(10.0 ** (gain_db / 20.0))
    knee = 0.6 * _PEAK_CEILING
    a = np.abs(y)
    over = a > knee
    y[over] = np.sign(y[over]) * (
        knee + (_PEAK_CEILING - knee) * np.tanh((a[over] - knee) / (_PEAK_CEILING - knee))
    )
    return y


class _GpuOrtFinder(importlib.abc.MetaPathFinder):
    """Resolves `import onnxruntime` to the GPU build in runtime_dir.

    Era's own Python ships a CPU-only onnxruntime (pulled in by faster-whisper)
    that would otherwise win, and a process can hold only ONE onnxruntime. The
    GPU build includes the CPU engine too, so faster-whisper keeps working on it.
    Only that one top-level name is redirected; numpy, protobuf and everything
    else still come from Era's own site-packages.
    """

    def __init__(self, root: str) -> None:
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if name != "onnxruntime":
            return None
        return importlib.machinery.PathFinder.find_spec(name, [self.root])


def install_gpu_runtime() -> bool:
    """Make the GPU onnxruntime the process-wide one. Call before anything
    imports onnxruntime (core/voice_mode does, at startup). Idempotent; True
    if the redirect is in place. Never under pytest."""
    if _under_pytest():
        return False
    try:
        cfg = get_config()
        rt = str(cfg["runtime_dir"])
        if cfg["provider"] != "cuda" or not (Path(rt) / "onnxruntime").is_dir():
            return False
        if rt not in sys.path:
            sys.path.append(rt)  # appended: phonemizer & friends, never ahead of Era's own
        if not any(isinstance(f, _GpuOrtFinder) for f in sys.meta_path):
            sys.meta_path.insert(0, _GpuOrtFinder(rt))
        loaded = sys.modules.get("onnxruntime")
        if loaded is not None and not str(getattr(loaded, "__file__", "")).startswith(rt):
            _log("onnxruntime was already imported from Era's Python; GPU needs a restart")
            return False
        return True
    except Exception as e:  # a voice optimisation must never break startup
        _log(f"GPU runtime not installed: {type(e).__name__}: {e}")
        return False


def _espeak_to_kokoro(ps: str) -> str:
    """misaki EspeakFallback.__call__ for British English."""
    for old, new in _E2M:
        ps = ps.replace(old, new)
    ps = re.sub(r"(\S)\u0329", r"ᵊ\1", ps).replace(chr(809), "")
    ps = ps.replace("e^ə", "ɛː").replace("iə", "ɪə").replace("ə^ʊ", "Q")
    ps = ps.replace("o", "ɔ").replace("ɾ", "T").replace("ʔ", "t")
    return ps.replace("^", "")


class KokoroVoice:
    """Model, voices, dictionary and espeak, loaded once on first use. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._sess = None
        self._voices = None
        self._speakers: dict[str, int] = {}
        self._tokens: dict[str, int] = {}
        self._lexicon: dict[str, list[str]] = {}
        self._espeak = None
        self._warming = False
        self._callbacks: list = []
        self._cb_lock = threading.Lock()
        self.provider = ""
        self.last_error = ""

    # ── loading ──────────────────────────────────────────────────────────────
    def available(self) -> tuple[bool, str]:
        cfg = get_config()
        d = Path(cfg["model_dir"])
        missing = [f for f in ("model.onnx", "voices.bin", "tokens.txt") if not (d / f).is_file()]
        if missing:
            return False, f"Kokoro files missing in {d}: {', '.join(missing)}"
        if not Path(cfg["runtime_dir"]).is_dir():
            return False, f"Kokoro runtime missing: {cfg['runtime_dir']}"
        return True, ""

    def _load(self) -> None:
        cfg = get_config()
        ok, why = self.available()
        if not ok:
            raise RuntimeError(why)
        install_gpu_runtime()
        import numpy as np
        import onnxruntime as ort

        if (
            cfg["provider"] == "cuda"
            and "CUDAExecutionProvider" not in ort.get_available_providers()
        ):
            # Seen live 2026-10-10: Era's own Python has a CPU-only onnxruntime
            # (for faster-whisper), and it was imported first. Kokoro still
            # speaks, but at ~1x realtime: say so loudly instead of hiding it.
            _log(f"WARNING: CPU onnxruntime loaded from {ort.__file__}; Kokoro will be slow")

        d = Path(cfg["model_dir"])
        t0 = time.perf_counter()
        so = ort.SessionOptions()
        so.log_severity_level = 3
        providers: list = ["CPUExecutionProvider"]
        if cfg["provider"] == "cuda":
            ort.preload_dlls()
            providers.insert(0, ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}))
        sess = ort.InferenceSession(str(d / "model.onnx"), so, providers=providers)
        self.provider = sess.get_providers()[0]
        names = sess.get_modelmeta().custom_metadata_map.get("speaker_names", "")
        self._speakers = {n: i for i, n in enumerate(names.split(",")) if n}
        self._voices = np.fromfile(d / "voices.bin", dtype=np.float32).reshape(-1, 510, 256)
        for line in (d / "tokens.txt").read_text(encoding="utf-8").splitlines():
            sym, _, idx = line.rpartition(" ")
            if idx.isdigit():
                self._tokens[sym] = int(idx)
        lex = d / "lexicon-gb-en.txt"
        if lex.is_file():
            for line in lex.read_text(encoding="utf-8").splitlines():
                word, *syms = line.split(" ")
                if word and syms:
                    self._lexicon.setdefault(word, syms)
        try:
            import espeakng_loader
            from phonemizer.backend import EspeakBackend
            from phonemizer.backend.espeak.wrapper import EspeakWrapper

            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
            self._espeak = EspeakBackend(
                language="en-gb", preserve_punctuation=True, with_stress=True, tie="^"
            )
        except Exception as e:  # dictionary words still speak without it
            _log(f"espeak fallback unavailable ({type(e).__name__}); dictionary words only")
        self._sess = sess
        self._ready = True
        _log(
            f"loaded on {self.provider} in {time.perf_counter() - t0:.1f}s "
            f"({len(self._lexicon)} dictionary words, {len(self._speakers)} voices)"
        )

    def warm(self) -> bool:
        """Load and run one short sentence, so the first real one is fast."""
        return bool(self.synthesize("Ready."))

    @property
    def ready(self) -> bool:
        return self._ready

    def warm_in_background(self, on_done=None) -> None:
        """Start loading once, off the audio path: the load takes ~8-11 s, and no
        sentence may ever wait for it (callers use another voice until ready).

        on_done(ok) runs once loading ends (at once if already loaded); this is
        how a voice switch waits for George without blocking anything. Inert
        under pytest (on_done(False)), so the suite never loads the GPU model.
        """
        if self._ready:
            if on_done:
                on_done(True)
            return
        if _under_pytest() or not self.available()[0]:
            if on_done:
                on_done(False)
            return
        with self._cb_lock:
            if on_done:
                self._callbacks.append(on_done)
            if self._warming:
                return  # one load at a time; this caller is told when it ends
            self._warming = True

        def _run() -> None:
            ok = False
            try:
                ok = self.warm()
            finally:
                with self._cb_lock:
                    self._warming = False
                    callbacks, self._callbacks = self._callbacks, []
                for cb in callbacks:
                    try:
                        cb(ok)
                    except Exception as e:
                        _log(f"ready callback failed: {type(e).__name__}: {e}")

        threading.Thread(target=_run, name="kokoro-warm", daemon=True).start()

    # ── text -> token ids ────────────────────────────────────────────────────
    def _espeak_word(self, word: str) -> list[str]:
        if self._espeak is None:
            return []
        try:
            ps = self._espeak.phonemize([word], strip=True)
        except Exception:
            return []
        out: list[str] = []
        for part in _espeak_to_kokoro(ps[0] if ps else "").split():
            if out:
                out.append(" ")
            out.extend(part)
        return out

    def phonemes(self, text: str) -> list[str]:
        syms: list[str] = []
        for tok in _WORD.findall(_spoken_symbols(text).replace("’", "'")):
            if tok in ',.!?;:—…"()':
                if syms and syms[-1] == " ":
                    syms.pop()
                syms.extend([tok, " "])
                continue
            word = None
            if not any(c.isdigit() for c in tok):
                word = self._lexicon.get(tok.lower()) or self._lexicon.get(tok)
            if word is None:  # unknown word or a number: espeak, as misaki does
                word = self._espeak_word(tok)
            if word:
                syms.extend(word)
                syms.append(" ")
        while syms and syms[-1] == " ":
            syms.pop()
        return syms

    def token_ids(self, text: str) -> list[int]:
        ids = [self._tokens[s] for s in self.phonemes(text) if s in self._tokens]
        return ids[: _MAX_TOKENS - 2]

    # ── synthesis ────────────────────────────────────────────────────────────
    def synthesize(self, text: str, voice: str = "") -> bytes:
        """24 kHz int16 mono PCM for one sentence. b"" on any failure."""
        text = (text or "").strip()
        if not text:
            return b""
        lang = language_of(text)
        if lang != "en":  # before any load: nothing to synthesize
            self.last_error = f"not English ({lang}); George speaks English only"
            return b""
        try:
            import numpy as np

            with self._lock:
                if not self._ready:
                    self._load()
                cfg = get_config()
                ids = self.token_ids(text)
                if not ids:
                    self.last_error = "no speakable phonemes"
                    return b""
                sid = self._speakers.get(voice or cfg["voice"], self._speakers.get("bm_george", 0))
                style = self._voices[sid, min(len(ids) - 1, 509)][None, :]
                audio = self._sess.run(
                    None,
                    {
                        "tokens": np.array([[0, *ids, 0]], dtype=np.int64),
                        "style": style,
                        "speed": np.array([cfg["speed"]], dtype=np.float32),
                    },
                )[0]
            from core.local_tts import to_pcm16

            self.last_error = ""
            return to_pcm16(normalize_loudness(audio, cfg["loudness_db"]), SAMPLE_RATE)
        except Exception as e:
            self.last_error = f"Kokoro failed: {type(e).__name__}"
            _log(f"synthesis failed: {type(e).__name__}: {e}")
            return b""

    async def stream(self, text: str, lang: str = ""):
        """tts_router shape: one chunk per sentence (Kokoro does not stream)."""
        pcm = await asyncio.to_thread(self.synthesize, text)
        if pcm:
            yield pcm


KOKORO = KokoroVoice()


def available() -> tuple[bool, str]:
    return KOKORO.available()
