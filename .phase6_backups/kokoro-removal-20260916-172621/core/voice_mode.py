import json
import os

from core.utils import BASE_DIR

# ---- MODES ----
MODE_GEMINI_AUDIO = "gemini_audio"
MODE_KOKORO_TEXT = "kokoro_text"
# Local voice WITHOUT a text-only session. Live-probed 2026-07-29: no
# general-conversation Live model still accepts response_modalities=["TEXT"]
# (gemini-3.1-flash-live-preview answers 1007 "not supported"), which left
# kokoro_text unusable — it could not connect at all. This mode keeps the
# session in AUDIO, throws Gemini's audio away, and synthesizes the
# output_transcription locally instead. Measured on the same probe: the
# transcription streams in 26 chunks with the first full sentence at 1.79s, so
# sentence-wise synthesis pipelines just as it does in kokoro_text.
#
# Because the modality never changes, switching between this and gemini_audio
# needs NO reconnect — it is strictly more seamless than the original design.
# What it does NOT do is save quota: Gemini still generates (and bills) the
# audio that gets discarded. kokoro_text remains the only quota-conserving
# mode, and it stays selectable for the day a TEXT-capable Live model returns.
MODE_KOKORO_AUDIO = "kokoro_audio"

# Fish Audio cloud TTS — Era's PRIMARY voice. Shaped exactly like kokoro_audio:
# the Live session stays in AUDIO (the only modality current Live models accept),
# Gemini's own audio is set aside, and the output_transcription is synthesized
# instead — here by Fish Audio rather than by local Kokoro. Because the modality
# never changes, gemini_audio <-> fish_audio needs NO reconnect, which is what
# makes the Gemini FALLBACK instant when Fish fails mid-conversation.
MODE_FISH_AUDIO = "fish_audio"

VALID_MODES = (MODE_GEMINI_AUDIO, MODE_KOKORO_TEXT, MODE_KOKORO_AUDIO, MODE_FISH_AUDIO)

# The modes in which Kokoro, not Gemini, produces the sound. Deliberately does
# NOT include fish_audio: Fish is a CLOUD backend, and this tuple drives the
# "local, on-device" wording and the quota escape hatch, both of which would be
# wrong for it.
LOCAL_VOICE_MODES = (MODE_KOKORO_TEXT, MODE_KOKORO_AUDIO)

# Modes where SpeechOut owns the speech (text in -> synthesized audio out),
# whichever backend does the synthesis. This is the question every call site
# that used to ask is_kokoro() actually means.
SPEECH_OUT_MODES = (MODE_KOKORO_TEXT, MODE_KOKORO_AUDIO, MODE_FISH_AUDIO)

# Modes running an AUDIO session whose cloud audio is NOT played: the
# output_transcription is synthesized locally/remotely instead.
TRANSCRIPT_SYNTH_MODES = (MODE_KOKORO_AUDIO, MODE_FISH_AUDIO)

VOICE_CONFIG_PATH = BASE_DIR / "config" / "voice.json"

# Kokoro preset voice ids, from the engine's own catalogue
# (backend/backends/kokoro_backend.py::KOKORO_VOICES). Era is male-voiced, so
# only the male ids are offered by name; any valid id is still accepted.
KOKORO_MALE_VOICES = {
    # British English — closest register to Gemini's "Algenib"
    "george": "bm_george",
    "daniel": "bm_daniel",
    "lewis": "bm_lewis",
    "fable": "bm_fable",
    # American English
    "michael": "am_michael",
    "adam": "am_adam",
    "onyx": "am_onyx",
    "puck": "am_puck",
    "fenrir": "am_fenrir",
    "liam": "am_liam",
    "eric": "am_eric",
    "echo": "am_echo",
}

DEFAULT_KOKORO_VOICE = "bm_george"


class VoiceModeConfig:
    """Single source of truth for the active voice mode and the Kokoro voice.

    - `mode`          : gemini_audio | kokoro_text
    - `kokoro_voice`  : Kokoro preset voice id (e.g. "bm_george")
    - `profile_name`  : name of the Voicebox profile Era auto-provisions

    `set_mode()` / `set_kokoro_voice()` validate, apply in place, and persist.
    Neither triggers the session reconnect that a mode change requires — that is
    the caller's job (see the set_voice handler), because the reconnect has to go
    through the existing graceful path rather than a second teardown route.
    """

    def __init__(self) -> None:
        self.mode = MODE_GEMINI_AUDIO
        self.kokoro_voice = DEFAULT_KOKORO_VOICE
        self.profile_name = "Era"
        # ── local engine (phase: LuxTTS switch) ──
        # "kokoro" is a PRESET engine: you pick one of its stock voices.
        # "luxtts" is a CLONING engine: it has no presets at all, so the profile
        # is built from a reference sample instead. Keeping the engine in config
        # rather than hardcoded is what lets the two coexist — Kokoro stays a
        # working fallback if a clone sample goes missing.
        self.engine = "kokoro"
        self.clone_sample_path = ""
        self.clone_reference_text = ""
        # Set when the quota auto-switch moves Era off Gemini, so she can explain
        # *why* her voice changed and the UI can distinguish it from a manual
        # switch. Deliberately not persisted: it describes one incident, not a
        # standing preference.
        self.switched_by_quota = False
        self.reload()

    # ── Queries ──────────────────────────────────────────────────────────────
    def is_kokoro(self) -> bool:
        """True when KOKORO produces the sound — either local mode.

        Call sites that ask this mean "is Era speaking locally?" (feed the
        synthesizer, end the turn through it, skip cloud TTS), and that is true
        of both local modes. The narrower questions have their own predicates
        below, so adding kokoro_audio did not change the meaning of any
        existing check.
        """
        return self.mode in LOCAL_VOICE_MODES

    def is_kokoro_text(self) -> bool:
        """Local voice via a TEXT-only session (no cloud audio billed)."""
        return self.mode == MODE_KOKORO_TEXT

    def is_kokoro_audio(self) -> bool:
        """Local voice over an AUDIO session; Gemini's audio is discarded."""
        return self.mode == MODE_KOKORO_AUDIO

    def is_gemini(self) -> bool:
        return self.mode == MODE_GEMINI_AUDIO

    def is_fish(self) -> bool:
        """Fish Audio is producing the sound (Era's primary voice)."""
        return self.mode == MODE_FISH_AUDIO

    def uses_speech_out(self) -> bool:
        """True when SpeechOut synthesizes the speech, whatever the backend.

        This is what call sites mean by "skip the cloud audio, feed the
        synthesizer, end the turn through it" — true for both Kokoro modes and
        for Fish. is_kokoro() stays narrower (KOKORO specifically) because the
        local-voice UX and the quota fallback genuinely mean that engine.
        """
        return self.mode in SPEECH_OUT_MODES

    def synthesizes_from_transcript(self) -> bool:
        """AUDIO session whose cloud audio is set aside for the transcript path."""
        return self.mode in TRANSCRIPT_SYNTH_MODES

    @property
    def response_modalities(self) -> list[str]:
        """The Live-session modality this mode implies. Consumed by _build_config.

        Only kokoro_text asks for TEXT. kokoro_audio deliberately reports AUDIO:
        that is the whole reason it works on a model that rejects TEXT.
        """
        return ["TEXT"] if self.is_kokoro_text() else ["AUDIO"]

    def needs_reconnect_to(self, other_mode: str) -> bool:
        """True if moving to `other_mode` changes the session shape.

        response_modalities is fixed at connect time, so ONLY a change in it
        forces a reconnect. gemini_audio <-> kokoro_audio are both AUDIO, so
        that switch is instant and keeps the conversation alive.
        """
        current = ["TEXT"] if self.is_kokoro_text() else ["AUDIO"]
        target = ["TEXT"] if other_mode == MODE_KOKORO_TEXT else ["AUDIO"]
        return current != target

    # ── Persistence ──────────────────────────────────────────────────────────
    def reload(self) -> None:
        """Load config/voice.json, seeding it on first run.

        Degrades to the in-memory defaults on any failure: a corrupt voice.json
        must cost the *preference, never the voice. Falling back to
        gemini_audio is the safe direction — it is the mode that works without
        a local backend running.
        """
        try:
            raw = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
            mode = str(raw.get("mode", "")).strip().lower()
            if mode in VALID_MODES:
                self.mode = mode
            voice = str(raw.get("kokoro_voice", "")).strip()
            if voice:
                self.kokoro_voice = self._normalize_voice(voice)
            engine = str(raw.get("engine", "")).strip().lower()
            if engine in ("kokoro", "luxtts"):
                self.engine = engine
            self.clone_sample_path = str(raw.get("clone_sample_path", "") or "").strip()
            self.clone_reference_text = str(raw.get("clone_reference_text", "") or "").strip()
            name = str(raw.get("profile_name", "")).strip()
            if name:
                self.profile_name = name
        except FileNotFoundError:
            self._write()  # seed the file with current defaults
        except Exception as e:
            print(f"[VOICE] Config load failed ({e}); keeping mode '{self.mode}'.")

    def _write(self) -> None:
        try:
            VOICE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            VOICE_CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "mode": self.mode,
                        "kokoro_voice": self.kokoro_voice,
                        "profile_name": self.profile_name,
                        "engine": self.engine,
                        "clone_sample_path": self.clone_sample_path,
                        "clone_reference_text": self.clone_reference_text,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[VOICE] Could not persist voice mode: {e}")

    # ── Mutations ────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_voice(voice: str) -> str:
        """Map a spoken name ("george") to a Kokoro voice id ("bm_george").

        Accepts an already-valid id unchanged so config written by hand, or a
        voice added upstream after this table was written, still works.
        """
        v = (voice or "").strip().lower()
        if not v:
            return DEFAULT_KOKORO_VOICE
        if v in KOKORO_MALE_VOICES:
            return KOKORO_MALE_VOICES[v]
        return v

    # "local"/"kokoro"/"offline" resolve to kokoro_AUDIO, the local mode that
    # actually connects today. kokoro_text stays reachable, but only by naming
    # it exactly — a spoken "use your local voice" must not select the mode that
    # cannot open a session (see MODE_KOKORO_AUDIO above).
    _ALIASES = {
        "local": MODE_KOKORO_AUDIO,
        "kokoro": MODE_KOKORO_AUDIO,
        "offline": MODE_KOKORO_AUDIO,  # Added to support "offline" as a direct alias
        # Explicit commands for standby mode:
        "go offline": MODE_KOKORO_AUDIO,
        "take a break": MODE_KOKORO_AUDIO,
        "standby mode": MODE_KOKORO_AUDIO,
        MODE_KOKORO_AUDIO: MODE_KOKORO_AUDIO,
        MODE_KOKORO_TEXT: MODE_KOKORO_TEXT,
        "fish": MODE_FISH_AUDIO,
        "fish audio": MODE_FISH_AUDIO,
        "fish voice": MODE_FISH_AUDIO,
        "primary": MODE_FISH_AUDIO,
        MODE_FISH_AUDIO: MODE_FISH_AUDIO,
        "normal": MODE_GEMINI_AUDIO,
        "gemini": MODE_GEMINI_AUDIO,
        "cloud": MODE_GEMINI_AUDIO,
        MODE_GEMINI_AUDIO: MODE_GEMINI_AUDIO,
    }

    @classmethod
    def _resolve_alias(cls, mode: str) -> str | None:
        """Map a spoken/typed mode name to a canonical mode id, or None.

        Exposed so callers can ask what a request WOULD resolve to (e.g. to
        decide whether a reconnect is needed) without applying it.
        """
        return cls._ALIASES.get((mode or "").strip().lower())

    def set_mode(self, mode: str, *, by_quota: bool = False) -> str:
        """Validate + apply + persist a voice mode. Returns the applied mode.

        Accepts the spoken synonyms the set_voice tool passes through, so
        "local"/"kokoro" and "normal"/"gemini" both resolve.
        """
        resolved = self._resolve_alias(mode)
        if resolved is None:
            raise ValueError(
                f"unknown voice mode '{mode}' (expected one of: local/kokoro/offline, normal/gemini)"
            )
        self.mode = resolved
        # Only a switch INTO Kokoro can be quota-driven. Any move back to Gemini
        # is by definition deliberate, so it always clears the flag.
        self.switched_by_quota = by_quota and resolved in LOCAL_VOICE_MODES
        self._write()
        return self.mode

    def set_kokoro_voice(self, voice: str) -> str:
        """Validate + apply + persist the Kokoro preset voice. Returns the id."""
        resolved = self._normalize_voice(voice)
        if not resolved.replace("_", "").isalnum():
            raise ValueError(f"invalid Kokoro voice id '{voice}'")
        self.kokoro_voice = resolved
        self._write()
        return self.kokoro_voice


VOICE_CFG = VoiceModeConfig()


def get_voicebox_config() -> dict:
    """Resolve Voicebox connection + storage settings.

    Layered config-key → env-var → default, following get_music_api_key()'s
    shape in core/utils.py. No API key: Voicebox is a LOCAL server with no auth
    on localhost.

    ``models_dir`` and ``data_dir`` default to D: deliberately. Voicebox's own
    defaults put both under the working directory on C: (backend/config.py's
    ``_data_dir = Path("data").resolve()``), and the models dir is assigned
    straight into HF_HUB_CACHE — so a multi-GB HuggingFace pull would land on
    the drive that has the least room. tts_supervisor passes both to every
    spawn, not just the first, or a restart silently reverts to C:.

    ``models_dir`` is ``D:\\Voicebox`` — the HF_HUB_CACHE *root*, which is where
    the desktop app already put Kokoro (``D:\\Voicebox\\models--hexgrad--Kokoro-82M``).
    Pointing at a ``\\models`` subdirectory instead would make a
    supervisor-spawned backend miss the existing download and re-fetch it.

    ``port`` is only the fallback for a backend Era spawns herself. The desktop
    app picks a random port per launch, so an already-running server is found by
    ``tts_voicebox.discover_running_ports()`` instead.
    """
    from core.utils import load_config

    cfg = load_config()
    vb = cfg.get("voicebox", {}) if isinstance(cfg.get("voicebox"), dict) else {}

    def _resolve(key: str, env: str, default: str) -> str:
        val = vb.get(key) or cfg.get(f"voicebox_{key}") or ""
        if val:
            return str(val)
        return os.environ.get(env, "") or default

    return {
        "host": _resolve("host", "VOICEBOX_HOST", "127.0.0.1"),
        "port": int(_resolve("port", "VOICEBOX_PORT", "8000")),
        "models_dir": _resolve("models_dir", "VOICEBOX_MODELS_DIR", r"D:\Voicebox"),
        "data_dir": _resolve("data_dir", "VOICEBOX_DATA_DIR", r"D:\Voicebox\data"),
        # Explicit path to the backend executable/module. Empty means "discover":
        # prefer the bundled voicebox-backend binary, fall back to the module.
        "backend_cmd": _resolve("backend_cmd", "VOICEBOX_BACKEND_CMD", ""),
    }


def voicebox_autostart() -> bool:
    """Whether to load the local voice at launch. Default: NO.

    Preloading Kokoro adds seconds to every startup — the backend spawn plus a
    model load — and Era cannot greet the user until it finishes. That cost is
    wasted on every session that never switches voices, which is most of them.
    So the local voice is now started ON DEMAND (an explicit voice switch, or
    the quota fallback deciding it needs one) rather than warmed at boot.

    The trade: the FIRST switch to the local voice now pays the load, instead of
    every launch paying it. Set ``voicebox.autostart`` (or ``voicebox_autostart``)
    to true to go back to warm-at-boot — worth it only if you rely on the local
    voice being instantly available the moment a quota outage hits.
    """
    from core.utils import load_config

    cfg = load_config()
    vb = cfg.get("voicebox", {}) if isinstance(cfg.get("voicebox"), dict) else {}
    raw = vb.get("autostart", cfg.get("voicebox_autostart", False))
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    c = get_voicebox_config()
    return f"http://{c['host']}:{c['port']}"
