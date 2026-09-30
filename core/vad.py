"""
vad.py — Voice Activity Detection for ERA ONE.

Provides voice activity detection via webrtcvad (preferred) or
a simple amplitude-based fallback when webrtcvad is unavailable.

Usage:
    vad = VoiceActivityDetector()
    vad.start()
    is_active = vad.is_speaking()
    vad.stop()
"""

from __future__ import annotations

import threading
import time

import numpy as np

# Try to import webrtcvad
try:
    import webrtcvad

    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False


class VoiceActivityDetector:
    """Lightweight voice activity detection.

    When webrtcvad is available, uses it for accurate VAD.
    Otherwise, falls back to a simple amplitude-based detector
    that monitors the WebSocket for mic activity events from the frontend.
    """

    def __init__(self, aggressiveness: int = 3) -> None:
        """
        Args:
            aggressiveness: VAD aggressiveness (0-3). Higher = more aggressive filtering.
                           0 = Quality mode, 3 = Aggressive mode.
        """
        self._vad: any | None = None
        self._frame_duration_ms = 30  # 30ms frames
        self._sample_rate = 16000
        self._running = False
        self._speaking = False
        self._last_voice_time = 0.0
        self._silence_threshold = 1.2  # Higher: ignore brief ambient / self-talk gaps
        self._lock = threading.Lock()
        # Direct-instruction / ambient filter state
        self._direct_rules = ["era", "assistant", "hey"]
        self._min_words = 3
        self._last_text_context: str = ""

        if _VAD_AVAILABLE:
            self._vad = webrtcvad.Vad(aggressiveness)

    def start(self) -> None:
        """Start the VAD detector."""
        self._running = True

    def stop(self) -> None:
        """Stop the VAD detector."""
        self._running = False

    def update_mic_volume(self, volume: float) -> None:
        """Update with current mic volume from frontend (0.0 - 1.0).

        Called by the WebSocket handler when frontend sends mic volume data.
        This is the fallback method when webrtcvad is not processing raw audio.
        """
        if not self._running:
            return

        with self._lock:
            if volume > 0.05:  # Above noise floor
                self._speaking = True
                self._last_voice_time = time.time()
            else:
                # Check if silence has persisted long enough
                if (
                    self._speaking
                    and (time.time() - self._last_voice_time) > self._silence_threshold
                ):
                    self._speaking = False

    def process_audio_frame(self, frame_bytes: bytes) -> bool:
        """Process a raw audio frame through webrtcvad.

        Speaker verification does NOT happen here. It used to be stubbed in this
        method (a bare `pass` when enrolled) on a path the mic never takes, which
        is why strangers were still heard. Enforcement now lives on the real mic
        path: core/voice_gate.py.
        """
        if not self._vad or not self._running:
            return False

        try:
            is_speech = self._vad.is_speech(frame_bytes, self._sample_rate)
            with self._lock:
                if is_speech:
                    self._speaking = True
                    self._last_voice_time = time.time()
                else:
                    if (
                        self._speaking
                        and (time.time() - self._last_voice_time) > self._silence_threshold
                    ):
                        self._speaking = False
            return is_speech
        except Exception:
            return False

    def is_direct_instruction(self, text_context: str = "") -> bool:
        """Filter ambient conversation / self-talk: only direct/clear commands."""
        t = (text_context or self._last_text_context or "").lower()
        self._last_text_context = t
        has_marker = any(rule in t for rule in self._direct_rules)
        has_command = any(
            v in t for v in ("turn off", "start", "stop", "play", "set", "open", "close")
        )
        not_fragment = len(t.split()) >= self._min_words
        # Hands-free interruption trigger: "era Wait"
        interrupted = ("wait" in t) and ("era" in t or "assistant" in t)
        return (has_marker or has_command or interrupted) and (not_fragment or interrupted)

    def is_speaking(self) -> bool:
        """Check if voice is currently detected."""
        with self._lock:
            return self._speaking

    @property
    def is_available(self) -> bool:
        """Check if webrtcvad is available for raw audio processing."""
        return _VAD_AVAILABLE


# Module-level singleton
_vad_instance: VoiceActivityDetector | None = None


def get_vad() -> VoiceActivityDetector:
    """Get or create the global VAD instance."""
    global _vad_instance
    if _vad_instance is None:
        _vad_instance = VoiceActivityDetector()
    return _vad_instance


# Hands-free interruption bridge: when VAD detects "era Wait" during active TTS,
# feed session_manager to force LISTENING (continuous listen, no button).
# Call site (in main loop / audio handler):
#   if session_manager.current_state == EraState.SPEAKING:
#       session_manager.handle_verbal_interrupt(transcribed_text)


# ──────────────────────────────────────────────────────────────────────────────
# Barge-in — full-duplex "stop talking when the user starts" detection
# ──────────────────────────────────────────────────────────────────────────────
def _frame_rms(frame_bytes: bytes) -> float:
    """RMS of int16 mono PCM, normalized to 0..1. 0.0 on empty/garbage.

    Tolerates an odd trailing byte (drops it) so a short or misaligned buffer
    degrades to a number rather than raising into the audio callback.
    """
    if not frame_bytes:
        return 0.0
    if len(frame_bytes) % 2:
        frame_bytes = frame_bytes[:-1]
    arr = np.frombuffer(frame_bytes, dtype="<i2")
    if arr.size == 0:
        return 0.0
    x = arr.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def fade_out_pcm(pcm: bytes, sample_rate: int, fade_ms: int) -> bytes:
    """A fade-to-zero tail taken from the END of `pcm`, ramped linearly 1→0.

    Used to *duck* Era's voice on a barge-in instead of cutting it dead: the last
    ~`fade_ms` of the chunk she just played is re-emitted at falling gain so the
    waveform lands on zero (no click) and the ear reads it as her yielding the
    floor rather than being guillotined. Returns b"" when there is nothing to
    fade or fading is disabled (`fade_ms <= 0`) — the caller then hard-stops.
    """
    if not pcm or fade_ms <= 0 or sample_rate <= 0:
        return b""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    arr = np.frombuffer(pcm, dtype="<i2")
    if arr.size == 0:
        return b""
    n = min(arr.size, int(sample_rate * fade_ms / 1000))
    if n <= 0:
        return b""
    ramp = np.linspace(1.0, 0.0, n, dtype=np.float32)
    tail = arr[-n:].astype(np.float32) * ramp
    return tail.astype("<i2").tobytes()


class BargeInDetector:
    """Detects the user talking WHILE Era is speaking, so Era can stop instantly.

    Era's mic loop is half-duplex — it drops mic audio while she speaks. That is
    what this detector re-enables during SPEAKING: the audio callback feeds every
    frame here instead of discarding it, and a positive `feed()` means "the user
    has started talking, cut the output." It fires at most once per spoken turn.

    THE HAZARD IS ECHO. Era's own voice leaks from the speakers back into the
    mic; a naive "any sound stops her" makes her interrupt herself on every
    sentence. Without pulling in an acoustic-echo-cancellation library, three
    cheap guards make the trigger mean *the user*, not the speakers:

      1. amplitude floor  — direct speech into the mic is markedly louder than
                            speaker bleed, so a frame below `amplitude_threshold`
                            does not count (and decays accumulated credit);
      2. webrtcvad gating — only voiced (speech-shaped) frames count, so fan/key
                            noise above the floor is ignored;
      3. minimum duration — `min_speech_ms` of *continuous* voiced-and-loud audio
                            is required, so a single echo blip cannot trigger,
      plus an `onset_grace_ms` settle window after Era starts, because the first
      moments of playback are the most echo-prone and the AGC is still adapting.

    On open laptop speakers, raise `amplitude_threshold` (or disable) if Era cuts
    herself off; a headset or OS echo cancellation removes the problem outright.

    Thread-safety: `feed()` runs on the sounddevice callback thread while
    `note_speaking()` / `reset()` run on the event-loop thread. All shared state
    is under `_lock`. Degrade, never raise: a malformed frame counts as silence.
    """

    _VAD_FRAME_MS = 30  # webrtcvad accepts only 10/20/30 ms frames

    def __init__(
        self,
        *,
        enabled: bool = True,
        aggressiveness: int = 2,
        amplitude_threshold: float = 0.05,
        min_speech_ms: int = 150,
        onset_grace_ms: int = 300,
        sample_rate: int = 16000,
    ) -> None:
        # Validate at the boundary: clamp to ranges the rest of the class assumes,
        # so a hand-edited config cannot produce a detector that never fires or
        # one that divides by zero.
        self.enabled = bool(enabled)
        self.amplitude_threshold = max(0.0, min(1.0, float(amplitude_threshold)))
        self.min_speech_ms = max(self._VAD_FRAME_MS, int(min_speech_ms))
        self.onset_grace_ms = max(0, int(onset_grace_ms))
        self._sample_rate = (
            int(sample_rate) if sample_rate in (8000, 16000, 32000, 48000) else 16000
        )
        self._vad_frame_bytes = int(self._sample_rate * self._VAD_FRAME_MS / 1000) * 2

        self._vad = None
        if _VAD_AVAILABLE:
            self._vad = webrtcvad.Vad(max(0, min(3, int(aggressiveness))))

        self._lock = threading.Lock()
        self._active = False  # True only between note_speaking(True) and a fire/reset
        self._onset_ts = 0.0
        self._voiced_ms = 0.0

    # ── State edges (event-loop thread) ────────────────────────────────────────
    def note_speaking(self, speaking: bool) -> None:
        """Arm on the rising edge of SPEAKING, disarm otherwise.

        MUST be called only on the True<->False transition, never once per audio
        chunk: re-stamping the onset every chunk would keep resetting the grace
        window and the detector could never accumulate enough voiced audio to
        fire. `main.set_speaking` guards this with a `changed` check.
        """
        with self._lock:
            self._active = bool(speaking)
            self._onset_ts = time.time() if speaking else 0.0
            self._voiced_ms = 0.0

    def reset(self) -> None:
        """Disarm and clear accumulated credit (turn ended, or fired)."""
        self.note_speaking(False)

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self.enabled = bool(value)

    # ── Detection (mic-callback thread) ────────────────────────────────────────
    def _count_voiced(self, buffer_bytes: bytes) -> int:
        """How many 30 ms sub-frames of `buffer_bytes` are voiced speech.

        Without webrtcvad, credits the buffer by its length in frame-units (it
        already passed the amplitude gate in `feed`), so the amplitude-only
        fallback still accumulates at the same rate as the VAD path.
        """
        fb = self._vad_frame_bytes
        if fb <= 0:
            return 0
        if self._vad is None:
            return max(1, len(buffer_bytes) // fb)
        n = 0
        for i in range(0, len(buffer_bytes) - fb + 1, fb):
            try:
                if self._vad.is_speech(buffer_bytes[i : i + fb], self._sample_rate):
                    n += 1
            except Exception:
                return 0  # invalid frame → treat as silence, never raise
        return n

    def feed(self, buffer_bytes: bytes) -> bool:
        """Feed one mic buffer captured while Era speaks. True == interrupt now.

        Returns False (never raises) when disabled, disarmed, still inside the
        onset grace window, too quiet, or not yet sustained long enough.
        """
        if not self.enabled:
            return False
        with self._lock:
            if not self._active:
                return False
            if (time.time() - self._onset_ts) * 1000.0 < self.onset_grace_ms:
                return False
            if _frame_rms(buffer_bytes) < self.amplitude_threshold:
                # Quiet frame — decay so only *continuous* speech accumulates.
                self._voiced_ms = max(0.0, self._voiced_ms - self._VAD_FRAME_MS)
                return False
            voiced = self._count_voiced(buffer_bytes)
            if voiced <= 0:
                self._voiced_ms = max(0.0, self._voiced_ms - self._VAD_FRAME_MS)
                return False
            self._voiced_ms += voiced * self._VAD_FRAME_MS
            if self._voiced_ms >= self.min_speech_ms:
                self._active = False  # one-shot; re-arms on the next note_speaking(True)
                return True
            return False


def get_barge_in_config() -> dict:
    """Resolve barge-in settings: config key → env var → default.

    Mirrors the config -> env -> default layering used elsewhere. Reads the optional
    ``barge_in`` block of the main config; every field has a safe default so the
    feature works with no config at all.

    Defaults are tuned for RESPONSIVENESS on a headset or a mic with usable echo
    separation: a 0.05 amplitude floor (so normal-volume speech interrupts, not
    only a raised voice) and a 150 ms sustain minimum (~40% faster cut-in than the
    old 250 ms). The webrtcvad speech gating and the 300 ms onset grace still guard
    against a stray echo blip. If Era interrupts HERSELF on open laptop speakers,
    raise ``amplitude_threshold`` (run ``tools/calibrate_barge_in.py`` for a
    measured value), or set ``barge_in.enabled`` false to restore half-duplex.
    """
    import os

    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    bi = cfg.get("barge_in", {}) if isinstance(cfg.get("barge_in"), dict) else {}

    def _get(key: str, env: str, default):
        val = bi.get(key)
        if val is None:
            val = cfg.get(f"barge_in_{key}")
        if val is None:
            val = os.environ.get(env)
        return default if val is None else val

    def _as_bool(v, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def _as_num(v, default, cast):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": _as_bool(_get("enabled", "ERA_BARGE_IN", True), True),
        "aggressiveness": _as_num(_get("aggressiveness", "ERA_BARGE_IN_AGGR", 2), 2, int),
        "amplitude_threshold": _as_num(
            _get("amplitude_threshold", "ERA_BARGE_IN_AMP", 0.05), 0.05, float
        ),
        "min_speech_ms": _as_num(_get("min_speech_ms", "ERA_BARGE_IN_MIN_MS", 150), 150, int),
        "onset_grace_ms": _as_num(_get("onset_grace_ms", "ERA_BARGE_IN_GRACE_MS", 300), 300, int),
    }


def build_barge_in_detector() -> BargeInDetector:
    """Construct a BargeInDetector from the resolved config."""
    return BargeInDetector(**get_barge_in_config())


def get_barge_in_fade_ms() -> int:
    """Duck length (ms) applied when Era's voice is interrupted. 0 = hard stop.

    Kept OUT of get_barge_in_config so that dict can be splatted straight into
    BargeInDetector (which takes no fade arg). Same config → env → default
    layering; default 20 ms is a soft, click-free duck.
    """
    import os

    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    bi = cfg.get("barge_in", {}) if isinstance(cfg.get("barge_in"), dict) else {}
    raw = bi.get("fade_ms")
    if raw is None:
        raw = cfg.get("barge_in_fade_ms")
    if raw is None:
        raw = os.environ.get("ERA_BARGE_IN_FADE_MS")
    if raw is None:
        return 20
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 20


def get_resume_config() -> dict:
    """Contextual-resumption settings (Phase 2): {offer, ttl_s}.

    Same barge_in config block, same layering. `offer` is the AUTONOMOUS "shall I
    finish about X?" prompt and defaults to False: it makes Era speak on her own
    initiative, whose timing wants one live conversation to validate before it is
    trusted in a real session. Explicit "continue"/"never mind" resumption and
    the memory itself are always on regardless of this flag. `ttl_s` is how long
    an interrupted thought stays offerable.
    """
    import os

    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    bi = cfg.get("barge_in", {}) if isinstance(cfg.get("barge_in"), dict) else {}

    def _get(key: str, env: str):
        val = bi.get(key)
        if val is None:
            val = cfg.get(f"barge_in_{key}")
        if val is None:
            val = os.environ.get(env)
        return val

    offer_raw = _get("resume_offer", "ERA_BARGE_IN_RESUME_OFFER")
    if isinstance(offer_raw, bool):
        offer = offer_raw
    elif offer_raw is None:
        offer = False
    else:
        offer = str(offer_raw).strip().lower() in ("1", "true", "yes", "on")

    ttl_raw = _get("resume_ttl_s", "ERA_BARGE_IN_RESUME_TTL_S")
    try:
        ttl_s = float(ttl_raw) if ttl_raw is not None else 120.0
    except (TypeError, ValueError):
        ttl_s = 120.0

    return {"offer": offer, "ttl_s": ttl_s}


def get_endpointing_config() -> dict:
    """Server-side turn endpointing (Phase 3): how patiently Gemini waits before
    deciding the user has finished talking.

    {enabled, silence_ms, prefix_ms, start_sensitivity, end_sensitivity}. Longer
    `silence_ms` + `end_sensitivity='low'` make Era wait through the pauses in a
    trailing thought instead of cutting in — the core of "let me finish my
    sentence". `start_sensitivity='high'` keeps her quick to notice the user has
    STARTED (responsive resume after a barge-in). `enabled=False` omits the
    config entirely, restoring Gemini's built-in defaults. Sensitivities are
    returned as 'low'/'high' strings; the genai enum mapping lives in
    tool_dispatch, so this module stays free of the google-genai dependency.
    """
    import os

    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    ep = cfg.get("endpointing", {}) if isinstance(cfg.get("endpointing"), dict) else {}

    def _get(key: str, env: str):
        val = ep.get(key)
        if val is None:
            val = cfg.get(f"endpointing_{key}")
        if val is None:
            val = os.environ.get(env)
        return val

    def _as_bool(val, default: bool) -> bool:
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _as_int(val, default: int) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _as_sens(val, default: str) -> str:
        s = str(val).strip().lower() if val is not None else default
        return s if s in ("low", "high") else default

    return {
        "enabled": _as_bool(_get("enabled", "ERA_ENDPOINTING"), True),
        "silence_ms": _as_int(_get("silence_ms", "ERA_ENDPOINTING_SILENCE_MS"), 800),
        "prefix_ms": _as_int(_get("prefix_ms", "ERA_ENDPOINTING_PREFIX_MS"), 300),
        "start_sensitivity": _as_sens(_get("start_sensitivity", "ERA_ENDPOINTING_START"), "high"),
        "end_sensitivity": _as_sens(_get("end_sensitivity", "ERA_ENDPOINTING_END"), "low"),
    }
