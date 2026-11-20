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

import collections
import json
import threading
import time
from pathlib import Path

import numpy as np

# Try to import webrtcvad
try:
    import webrtcvad

    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False


def voice_settings() -> dict:
    """Non-secret voice settings override legacy main-config audio values."""
    try:
        value = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "voice.json").read_text(
                encoding="utf-8-sig"
            )
        )
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


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

    4. adaptive echo floor (2026-10-10) — on open PC speakers her echo passes the
       fixed floor and webrtcvad (it IS speech), and she interrupted herself six
       times in two minutes. So the detector measures her own echo on the mic (the
       loud end of the last ~2 s, seeded during the onset grace) and a frame must
       be `echo_factor` x louder than that to count. On a headset or glasses the
       echo is tiny and the fixed floor still rules, so cut-in feels the same.

    Thread-safety: `feed()` runs on the sounddevice callback thread while
    `note_speaking()` / `reset()` run on the event-loop thread. All shared state
    is under `_lock`. Degrade, never raise: a malformed frame counts as silence.
    """

    _VAD_FRAME_MS = 30  # webrtcvad accepts only 10/20/30 ms frames
    # Runaway breaker: this many fires inside the window pauses interruption.
    RUNAWAY_FIRES = 3
    RUNAWAY_WINDOW_S = 15.0
    RUNAWAY_SUSPEND_S = 300.0

    def __init__(
        self,
        *,
        enabled: bool = True,
        aggressiveness: int = 2,
        amplitude_threshold: float = 0.05,
        min_speech_ms: int = 150,
        onset_grace_ms: int = 300,
        echo_factor: float = 2.0,
        require_speech_vad: bool = False,
        sample_rate: int = 16000,
    ) -> None:
        # Validate at the boundary: clamp to ranges the rest of the class assumes,
        # so a hand-edited config cannot produce a detector that never fires or
        # one that divides by zero.
        self.enabled = bool(enabled)
        self.require_speech_vad = bool(require_speech_vad)
        self._configured_require_speech_vad = self.require_speech_vad
        self.amplitude_threshold = max(0.0, min(1.0, float(amplitude_threshold)))
        self._configured_amplitude_threshold = self.amplitude_threshold
        self._input_wearable = False
        self._output_open_speaker = False  # unknown until the speaker opens
        self._fires: collections.deque[float] = collections.deque()
        self._suspended_until = 0.0
        self.last_trip_ts = 0.0  # monotonic time the breaker last tripped (for logging)
        self.min_speech_ms = max(self._VAD_FRAME_MS, int(min_speech_ms))
        self.onset_grace_ms = max(0, int(onset_grace_ms))
        self.echo_factor = max(0.0, min(10.0, float(echo_factor)))  # 0 = off
        self._echo: collections.deque[float] = collections.deque(maxlen=32)  # ~2 s
        self._burst: list[float] = []  # loud frames awaiting a verdict: user or echo?
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
            self._echo.clear()  # each turn measures its own echo (device may change)
            self._burst = []

    def reset(self) -> None:
        """Disarm and clear accumulated credit (turn ended, or fired)."""
        self.note_speaking(False)

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self.enabled = bool(value)

    @staticmethod
    def _is_wearable(device_name: str) -> bool:
        name = "".join(c for c in str(device_name or "").lower() if c.isalnum())
        return "cy03cb36" in name or "starglass" in name

    def _apply_floor(self) -> None:
        """Pick the floor for the current mic + speaker pair (caller holds _lock).

        The quiet 0.012 floor is only safe when Era's voice plays IN the wearable
        (earpiece: almost no echo reaches its mic). With the glasses mic and her
        voice on open PC speakers, the 8x-boosted mic hears the speakers far above
        0.012 and she interrupted herself, then answered her own echo, every turn
        (2026-11-05 22:10). That pair keeps the configured floor and needs real
        speech.
        """
        open_speaker_echo = self._input_wearable and self._output_open_speaker
        quiet_floor = self._input_wearable and not open_speaker_echo
        self.amplitude_threshold = (
            min(self._configured_amplitude_threshold, 0.012)
            if quiet_floor
            else self._configured_amplitude_threshold
        )
        self.require_speech_vad = self._configured_require_speech_vad or open_speaker_echo

    def configure_input(self, device_name: str) -> bool:
        """A known wearable mic needs a quieter floor than the laptop array."""
        wearable = self._is_wearable(device_name)
        with self._lock:
            if wearable != self._input_wearable:
                self._fires.clear()
                self._suspended_until = 0.0
            self._input_wearable = wearable
            self._apply_floor()
        return wearable

    def configure_output(self, device_name: str) -> bool:
        """Record where Era's voice plays. True == an open speaker (echo-prone).

        An empty/unknown name changes nothing: the floor stays as configured for
        the mic, exactly as before this was known.
        """
        if not str(device_name or "").strip():
            return self._output_open_speaker
        open_speaker = not self._is_wearable(device_name)
        with self._lock:
            if open_speaker != self._output_open_speaker:
                self._fires.clear()
                self._suspended_until = 0.0
            self._output_open_speaker = open_speaker
            self._apply_floor()
        return open_speaker

    def suspended(self) -> bool:
        """True while the runaway breaker holds interruption off."""
        with self._lock:
            return time.monotonic() < self._suspended_until

    def _note_fire(self) -> bool:
        """Record a fire (caller holds _lock). True == this fire trips the breaker.

        A person does not cut Era off within seconds on every reply, several
        times running; her own echo does. Whatever the mic/speaker pair, that
        pattern pauses interruption so the self-answer loop cannot sustain itself.
        """
        now = time.monotonic()
        self._fires.append(now)
        while self._fires and now - self._fires[0] > self.RUNAWAY_WINDOW_S:
            self._fires.popleft()
        if len(self._fires) >= self.RUNAWAY_FIRES:
            self._fires.clear()
            self._suspended_until = now + self.RUNAWAY_SUSPEND_S
            return True
        return False

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
            if self.require_speech_vad:
                return 0  # Missing speech recognition must not turn noise into speech.
            return max(1, len(buffer_bytes) // fb)
        n = 0
        for i in range(0, len(buffer_bytes) - fb + 1, fb):
            try:
                if self._vad.is_speech(buffer_bytes[i : i + fb], self._sample_rate):
                    n += 1
            except Exception:
                return 0  # invalid frame → treat as silence, never raise
        return n

    def _decay(self) -> None:
        self._voiced_ms = max(0.0, self._voiced_ms - self._VAD_FRAME_MS)
        if self._voiced_ms <= 0.0 and self._burst:
            self._echo.extend(self._burst)  # never became an interruption: her echo
            self._burst.clear()

    def _speech_shaped(self, buffer_bytes: bytes) -> bool:
        """Reject DC and broadband noise that WebRTC can call initial speech.

        This applies only to interruption, never to normal Gemini input. Speech
        has a structured spectrum; white/fan-like broadband noise is much flatter.
        """
        samples = np.frombuffer(buffer_bytes[: len(buffer_bytes) // 2 * 2], dtype="<i2").astype(
            np.float32
        )
        if len(samples) < 160:
            return False
        samples /= 32768.0
        power = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
        frequencies = np.fft.rfftfreq(len(samples), 1 / self._sample_rate)
        band = power[(frequencies >= 100) & (frequencies <= 5000)]
        if not band.size or float(band.mean()) < 1e-10:
            return False
        flatness = float(np.exp(np.mean(np.log(band + 1e-12))) / (band.mean() + 1e-12))
        return flatness < 0.45

    def _echo_floor(self) -> float:
        """echo_factor x the loud end (90th percentile) of her recent echo."""
        if self.echo_factor <= 0 or not self._echo:
            return 0.0
        ordered = sorted(self._echo)
        return self.echo_factor * ordered[int(0.9 * (len(ordered) - 1))]

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
            if time.monotonic() < self._suspended_until:
                return False  # runaway breaker: her echo was cutting her off
            level = _frame_rms(buffer_bytes)
            if (time.time() - self._onset_ts) * 1000.0 < self.onset_grace_ms:
                self._echo.append(level)  # her own voice, settling: the echo seed
                return False
            floor = max(self.amplitude_threshold, self._echo_floor())
            if level < floor:
                # Quiet frame: her echo. Decay credit so only *continuous* speech
                # accumulates; a burst that died out was her loud syllable, so its
                # frames now join the echo history (a delayed verdict: counted
                # straight away, the user's own first frames would lift the floor
                # against him before he had spoken 150 ms).
                self._echo.append(level)
                self._decay()
                return False
            voiced = self._count_voiced(buffer_bytes)
            frame_count = len(buffer_bytes) // self._vad_frame_bytes
            if voiced <= 0 or (
                self.require_speech_vad
                and (voiced < 0.75 * frame_count or not self._speech_shaped(buffer_bytes))
            ):
                self._echo.append(level)
                if self.require_speech_vad:
                    self._voiced_ms = 0.0
                    self._burst.clear()
                else:
                    self._decay()
                return False
            self._burst.append(level)
            self._voiced_ms += voiced * self._VAD_FRAME_MS
            if self._voiced_ms >= self.min_speech_ms:
                self._active = False  # one-shot; re-arms on the next note_speaking(True)
                self._burst.clear()
                if self._note_fire():
                    self.last_trip_ts = time.monotonic()
                    return False  # the fire that proves a loop is not acted on
                return True
            return False


def get_barge_in_config() -> dict:
    """Resolve barge-in settings: config key → env var → default.

    Mirrors the config -> env -> default layering used elsewhere. Reads the optional
    ``barge_in`` block of the main config; every field has a safe default so the
    feature works with no config at all.

    Voice-specific settings take precedence over legacy main-config settings.
    Current voice settings use 300 ms of sustained speech and require webrtcvad;
    the amplitude-only compatibility mode is not the live default. If Era
    interrupts HERSELF on open laptop speakers,
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
    current = voice_settings().get("barge_in", {})
    current = current if isinstance(current, dict) else {}

    def _get(key: str, env: str, default):
        val = current.get(key, bi.get(key))
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
        "echo_factor": _as_num(_get("echo_factor", "ERA_BARGE_IN_ECHO", 2.0), 2.0, float),
        "require_speech_vad": _as_bool(
            _get("require_speech_vad", "ERA_BARGE_IN_REQUIRE_VAD", True), True
        ),
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
    current = voice_settings().get("endpointing", {})
    current = current if isinstance(current, dict) else {}

    def _get(key: str, env: str):
        val = current.get(key, ep.get(key))
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
