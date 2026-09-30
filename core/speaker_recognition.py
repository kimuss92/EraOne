"""
Speaker Recognition — local-only voiceprint verification for Era One.

WHY THE FIRST VERSION NEVER WORKED (2026-09-17 post-mortem)
-----------------------------------------------------------
The previous file was a 26-line stub: `SpeakerVerifier.is_enrolled()` only
checked that a file existed, and there was no embedding, no comparison, and no
verify call at all. Its single caller (`VoiceActivityDetector.process_audio_frame`)
did `pass` when enrolled, was dead anyway (webrtcvad is not installed, so it
returned early), and is not on the mic path — mic audio goes straight from
`_listen_audio` to Gemini Live. `config/speaker_enrollment.pth` is a 44-byte
random token, not a voiceprint. Nothing ever stood between a stranger's voice
and a command.

WHAT THIS MODULE DOES NOW
-------------------------
* `SherpaEmbedder` turns 16 kHz int16 PCM into an L2-normalised speaker
  embedding with a local ONNX model (sherpa-onnx, CPU; no cloud dispatch).
* `SpeakerProfile` is the enrolled owner: a centroid of several recordings plus
  thresholds calibrated at enrollment (`tools/enroll_speaker.py`).
* `SpeakerVerifier.verify()` accepts ONLY when the cosine score clears the
  threshold. It is FAIL-CLOSED: no model, no profile, a model/profile mismatch,
  too little speech, or any exception is a rejection, never a pass.

Enforcement points live in `core/voice_gate.py` (live mic → Gemini, and the
standby wake word). Biometric data stays in config/ on this machine.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.utils import BASE_DIR

SAMPLE_RATE = 16000
SETTINGS_PATH = BASE_DIR / "config" / "speaker_verification.json"
PROFILE_PATH = BASE_DIR / "config" / "speaker_profile.json"
# Models go on D: (C: is tight). Override with ERA_SPEAKER_MODEL or settings.
# NVIDIA TitaNet-large, chosen 2026-09-17 by benchmark over wespeaker ResNet34
# (the first model), CAM++ and 3D-Speaker ERes2NetV2: at 1.2 s it had ~4% EER clean
# vs 15% for ResNet34, and — decisive for security — background noise did NOT
# raise impostor scores (with ResNet34, noise let 30-50% of impostor clips pass).
# 72 ms per 1.2 s check on CPU. A profile is tied to its model: re-enroll on change.
DEFAULT_MODEL_PATH = Path(r"D:\era_models\speaker\nemo_en_titanet_large.onnx")

# No threshold below this is ever honoured, whatever a hand-edited file says:
# below ~0.4 cosine, different speakers start to clear the bar.
THRESHOLD_FLOOR = 0.40
DEFAULT_COMMAND_THRESHOLD = 0.55
DEFAULT_WAKE_THRESHOLD = 0.50

DEFAULT_SETTINGS: dict = {
    # True = only the enrolled voice reaches Era. With no model/profile Era is
    # deaf to voice (fail-closed) until tools/enroll_speaker.py has been run.
    "enabled": True,
    "model_path": str(DEFAULT_MODEL_PATH),
    # Optional overrides of the thresholds calibrated into the profile.
    "command_threshold": None,
    "wake_threshold": None,
    # Voiced seconds buffered before the first verdict on an utterance.
    "verify_window_s": 1.2,
    # Shortest voiced audio that may be judged at all (commands / wake word).
    "min_command_speech_s": 0.5,
    "min_wake_speech_s": 0.35,
    # Re-check a long accepted utterance after this much further voiced audio,
    # so a second voice cannot ride on the owner's turn.
    "reverify_every_s": 1.5,
    # Silence that ends an utterance.
    "hangover_s": 0.8,
    # Short replies ("ok", "yeah", "no") carry too little speech for a reliable
    # voiceprint. They are let through ONLY within this many seconds of a FULLY
    # verified owner utterance (or of Era's reply to one), never while a
    # confirmation is pending, and never chained off another short reply.
    "followup_window_s": 8.0,
    # Even a short follow-up is scored when there is enough audio; a score below
    # this is a clearly different voice and is refused.
    "followup_min_score": 0.20,
    # A rejected wake word may be repeated within this window; both takes are
    # then verified together (twice the speech, a far more reliable score).
    "wake_retry_window_s": 6.0,
}


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def load_settings(path: Path | None = None) -> dict:
    """Defaults ← config/speaker_verification.json ← env, with user_prefs override."""
    raw: dict = {}
    try:
        raw = json.loads(Path(path or SETTINGS_PATH).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[SPEAKER] Settings unreadable ({e}); using secure defaults.")
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    env_model = os.environ.get("ERA_SPEAKER_MODEL")
    if env_model:
        s["model_path"] = env_model
    # Apply persisted user preference from memory/user_prefs.json (overrides file/env)
    try:
        from core.user_prefs import get_voice_lock_enabled

        saved = get_voice_lock_enabled()
        s["enabled"] = saved
    except Exception:
        pass
    # Only an explicit JSON false disables enforcement (fail-closed default preserved)
    s["enabled"] = s.get("enabled") is not False
    for key in ("command_threshold", "wake_threshold"):
        if s[key] is not None:
            s[key] = _clamp(s[key], THRESHOLD_FLOOR, 0.95, THRESHOLD_FLOOR)
    s["verify_window_s"] = _clamp(s["verify_window_s"], 0.6, 4.0, 1.2)
    s["min_command_speech_s"] = _clamp(s["min_command_speech_s"], 0.3, 3.0, 0.5)
    s["min_wake_speech_s"] = _clamp(s["min_wake_speech_s"], 0.25, 2.0, 0.35)
    s["reverify_every_s"] = _clamp(s["reverify_every_s"], 0.5, 10.0, 1.5)
    s["hangover_s"] = _clamp(s["hangover_s"], 0.3, 3.0, 0.8)
    s["followup_window_s"] = _clamp(s["followup_window_s"], 0.0, 20.0, 8.0)
    s["followup_min_score"] = _clamp(s["followup_min_score"], 0.0, 0.9, 0.20)
    s["wake_retry_window_s"] = _clamp(s["wake_retry_window_s"], 0.0, 15.0, 6.0)
    return s


def pcm_to_float(pcm: bytes | np.ndarray) -> np.ndarray:
    """int16 PCM bytes/array → float32 in [-1, 1]. Tolerates an odd trailing byte."""
    if isinstance(pcm, np.ndarray):
        arr = pcm
    else:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        arr = np.frombuffer(pcm, dtype="<i2")
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32) / 32768.0
    return arr


def voiced_segment(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Drop silent 30 ms frames so the embedding describes the voice, not the room."""
    frame = int(sample_rate * 0.03)
    n = len(samples) // frame
    if n == 0:
        return samples[:0]
    frames = samples[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    floor = float(np.percentile(rms, 20))
    loud = float(np.percentile(rms, 90))
    # min(): a clip that is speech end-to-end has a high "floor"; capping at a
    # quarter of the loud level keeps it instead of discarding every frame.
    keep = rms > max(0.01, min(floor * 2.5, loud * 0.25))
    return frames[keep].reshape(-1)


class SherpaEmbedder:
    """Local ONNX speaker-embedding extractor (sherpa-onnx). Loaded lazily."""

    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path)
        self._extractor = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self.model_path.name

    def _load(self):
        if self._extractor is None:
            if not self.model_path.is_file():
                raise FileNotFoundError(f"speaker model not found: {self.model_path}")
            import sherpa_onnx  # local import: optional dependency

            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.model_path), num_threads=1, provider="cpu"
            )
            if not cfg.validate():
                raise RuntimeError(f"invalid speaker model config: {self.model_path}")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return self._extractor

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._lock:
            ext = self._load()
            stream = ext.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
            stream.input_finished()
            if not ext.is_ready(stream):
                raise ValueError("not enough audio for a speaker embedding")
            vec = np.asarray(ext.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm == 0.0:
            raise ValueError("degenerate speaker embedding")
        return vec / norm


@dataclass
class SpeakerProfile:
    model_name: str
    centroid: np.ndarray
    command_threshold: float = DEFAULT_COMMAND_THRESHOLD
    wake_threshold: float = DEFAULT_WAKE_THRESHOLD
    samples: int = 0
    created: str = ""
    extra: dict = field(default_factory=dict)
    # Mean embedding of OTHER voices (impostor cohort). This model's embeddings
    # share a large common component — unrelated voices, even noise, score ~0.4-0.8
    # raw cosine — so scoring after subtracting the cohort mean is what separates
    # speakers (measured 2026-09-17: mean owner/impostor gap 0.05 raw → 0.20 centered).
    cohort_mean: np.ndarray | None = None

    def project(self, emb: np.ndarray) -> np.ndarray:
        """Embedding → the (optionally cohort-centered) unit vector the centroid lives in."""
        if self.cohort_mean is not None:
            emb = emb - self.cohort_mean
        norm = float(np.linalg.norm(emb))
        if not np.isfinite(norm) or norm == 0.0:
            raise ValueError("degenerate projected embedding")
        return emb / norm

    def score(self, emb: np.ndarray) -> float:
        return float(np.dot(self.project(emb), self.centroid))

    @classmethod
    def load(cls, path: Path) -> SpeakerProfile:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        centroid = np.asarray(raw["centroid"], dtype=np.float32)
        norm = float(np.linalg.norm(centroid))
        if centroid.ndim != 1 or centroid.size < 16 or not np.isfinite(norm) or norm == 0:
            raise ValueError("speaker profile centroid is invalid")
        cohort = raw.get("cohort_mean")
        cohort_mean = None
        if cohort is not None:
            cohort_mean = np.asarray(cohort, dtype=np.float32)
            if cohort_mean.shape != centroid.shape or not np.all(np.isfinite(cohort_mean)):
                raise ValueError("speaker profile cohort_mean is invalid")
        return cls(
            model_name=str(raw["model_name"]),
            centroid=centroid / norm,
            command_threshold=_clamp(
                raw.get("command_threshold"), THRESHOLD_FLOOR, 0.95, DEFAULT_COMMAND_THRESHOLD
            ),
            wake_threshold=_clamp(
                raw.get("wake_threshold"), THRESHOLD_FLOOR, 0.95, DEFAULT_WAKE_THRESHOLD
            ),
            samples=int(raw.get("samples", 0)),
            created=str(raw.get("created", "")),
            extra=dict(raw.get("extra", {})),
            cohort_mean=cohort_mean,
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "model_name": self.model_name,
            "centroid": [round(float(x), 6) for x in self.centroid],
            "command_threshold": round(self.command_threshold, 4),
            "wake_threshold": round(self.wake_threshold, 4),
            "samples": self.samples,
            "created": self.created or time.strftime("%Y-%m-%d %H:%M:%S"),
            "extra": self.extra,
            "cohort_mean": (
                None if self.cohort_mean is None else [round(float(x), 6) for x in self.cohort_mean]
            ),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)


@dataclass(frozen=True)
class VerifyResult:
    accepted: bool
    score: float | None
    reason: str


class SpeakerVerifier:
    """Fail-closed owner-voice check. Thread-safe; never raises from verify()."""

    def __init__(
        self,
        settings: dict | None = None,
        profile_path: Path = PROFILE_PATH,
        embedder=None,
    ) -> None:
        self.settings = settings if settings is not None else load_settings()
        self._profile_path = Path(profile_path)
        self._embedder = embedder or SherpaEmbedder(self.settings["model_path"])
        self._profile: SpeakerProfile | None = None
        self._profile_error = ""
        self._load_profile()

    # Kept for backward compatibility with older callers.
    def is_enrolled(self) -> bool:
        return self._profile is not None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def set_enabled(self, value: bool) -> tuple[bool, str]:
        """Runtime on/off (the voice_lock tool). Persisted to memory/user_prefs.json
        so the preference survives restarts (fail-closed default preserved)."""
        if value:
            self.reload()
            self.settings["enabled"] = True
            ready, why = self.readiness()
            if not ready:
                self.settings["enabled"] = False
                return False, why
            self.warm_up()
            # Persist ON state
            try:
                from core.user_prefs import set_voice_lock_enabled

                set_voice_lock_enabled(True)
            except Exception:
                pass
            return True, "voice recognition enabled"
        self.settings["enabled"] = False
        # Persist OFF state
        try:
            from core.user_prefs import set_voice_lock_enabled

            set_voice_lock_enabled(False)
        except Exception:
            pass
        return True, "voice recognition disabled"

    def _load_profile(self) -> None:
        try:
            self._profile = SpeakerProfile.load(self._profile_path)
            self._profile_error = ""
        except FileNotFoundError:
            self._profile, self._profile_error = None, "no voice enrolled"
        except Exception as e:
            self._profile, self._profile_error = None, f"voice profile unreadable: {e}"

    def reload(self) -> None:
        self._load_profile()

    def readiness(self) -> tuple[bool, str]:
        """(ready, reason). Not ready while enabled == every voice is refused."""
        if not self.enabled:
            return True, "speaker verification disabled"
        if self._profile is None:
            return False, self._profile_error or "no voice enrolled"
        model_name = getattr(self._embedder, "model_name", "")
        if model_name and model_name != self._profile.model_name:
            return False, (
                f"profile was enrolled with {self._profile.model_name}, "
                f"but the active model is {model_name}; re-enroll"
            )
        model_path = getattr(self._embedder, "model_path", None)
        if model_path is not None and not Path(model_path).is_file():
            return False, f"speaker model missing: {model_path}"
        return True, "ready"

    def warm_up(self) -> None:
        """Load the model ahead of the first utterance (first load takes seconds)."""
        if self.enabled and self._profile is not None:
            try:
                load = getattr(self._embedder, "_load", None)
                if load is not None:
                    load()
            except Exception as e:
                print(f"[SPEAKER] Model warm-up failed: {e}")

    def threshold(self, purpose: str) -> float:
        prof = self._profile
        if purpose == "wake":
            t = self.settings.get("wake_threshold") or (
                prof.wake_threshold if prof else DEFAULT_WAKE_THRESHOLD
            )
        else:
            t = self.settings.get("command_threshold") or (
                prof.command_threshold if prof else DEFAULT_COMMAND_THRESHOLD
            )
        return max(THRESHOLD_FLOOR, float(t))

    def verify(self, pcm: bytes | np.ndarray, purpose: str = "command") -> VerifyResult:
        result = self._verify(pcm, purpose)
        _log_verdict(purpose, result, self.threshold(purpose) if purpose != "followup" else None)
        return result

    def _verify(self, pcm: bytes | np.ndarray, purpose: str) -> VerifyResult:
        if not self.enabled:
            return VerifyResult(True, None, "disabled")
        try:
            ready, why = self.readiness()
            if not ready:
                return VerifyResult(False, None, why)
            voiced = voiced_segment(pcm_to_float(pcm))
            if purpose == "followup":
                # Caller has already established the conversation context; the
                # score here only screens out an obviously different voice.
                if len(voiced) < int(_FOLLOWUP_SCORABLE_S * SAMPLE_RATE):
                    return VerifyResult(True, None, "follow-up (too short to score)")
                emb = self._embedder.embed(voiced)
                prof = self._profile
                if prof is None or emb.shape != prof.centroid.shape:
                    return VerifyResult(False, None, "embedding size mismatch; re-enroll")
                score = prof.score(emb)
                floor = float(self.settings["followup_min_score"])
                if np.isfinite(score) and score >= floor:
                    return VerifyResult(True, score, f"follow-up {score:.2f} >= {floor:.2f}")
                return VerifyResult(False, score, f"follow-up voice mismatch {score:.2f}")
            min_s = self.settings[
                "min_wake_speech_s" if purpose == "wake" else "min_command_speech_s"
            ]
            if len(voiced) < int(min_s * SAMPLE_RATE):
                return VerifyResult(False, None, "too little speech to verify")
            emb = self._embedder.embed(voiced)
            prof = self._profile
            if prof is None or emb.shape != prof.centroid.shape:
                return VerifyResult(False, None, "embedding size mismatch; re-enroll")
            score = prof.score(emb)
            if not np.isfinite(score):
                return VerifyResult(False, None, "non-finite score")
            limit = self.threshold(purpose)
            if score >= limit:
                return VerifyResult(True, score, f"match {score:.2f} >= {limit:.2f}")
            return VerifyResult(False, score, f"voice mismatch {score:.2f} < {limit:.2f}")
        except Exception as e:  # fail closed
            return VerifyResult(False, None, f"verification error: {e}")


_FOLLOWUP_SCORABLE_S = 0.25


def _log_verdict(purpose: str, result: VerifyResult, threshold: float | None) -> None:
    """One structured line per decision in logs/era.log — the data needed to tune
    thresholds from real use instead of guesses. Never raises."""
    try:
        from core.logger import log_event, main_logger

        log_event(
            main_logger,
            "voice.verify",
            purpose=purpose,
            accepted=result.accepted,
            score=None if result.score is None else round(result.score, 3),
            threshold=None if threshold is None else round(threshold, 3),
            reason=result.reason,
        )
    except Exception:
        pass


_verifier: SpeakerVerifier | None = None
_verifier_lock = threading.Lock()


def get_verifier() -> SpeakerVerifier:
    global _verifier
    with _verifier_lock:
        if _verifier is None:
            _verifier = SpeakerVerifier()
        return _verifier
