"""Era's voice mode — which backend speaks.

Three modes, all running on an ordinary AUDIO Live session:

    fish_audio     Gemini's audio is held back and the output_transcription is
                   synthesized by Fish Audio (cloud) instead.
    kokoro_audio   Same, synthesized by Kokoro George on the local GPU
                   (core/tts_kokoro.py, re-added 2026-10-10 as the fast local voice).
    gemini_audio   Gemini's own Live audio is played directly; also the
                   automatic fallback for the other two.

Because the session modality never changes between them, switching is INSTANT —
no reconnect, no dropped conversation. That is what lets `_tts_fallback` hand a
turn to Gemini mid-sentence when Fish or Kokoro fails, and what makes a spoken
"use your Gemini voice" take effect immediately.
"""

import json

from core.utils import BASE_DIR

# ---- MODES ----
MODE_GEMINI_AUDIO = "gemini_audio"
MODE_FISH_AUDIO = "fish_audio"
MODE_KOKORO_AUDIO = "kokoro_audio"

VALID_MODES = (MODE_GEMINI_AUDIO, MODE_FISH_AUDIO, MODE_KOKORO_AUDIO)

# Spoken name of each voice, for confirmations and the HUD log.
MODE_LABELS = {MODE_GEMINI_AUDIO: "Gemini", MODE_FISH_AUDIO: "Fish", MODE_KOKORO_AUDIO: "Kokoro"}

# Modes where SpeechOut owns the speech (text in -> synthesized audio out).
SPEECH_OUT_MODES = (MODE_FISH_AUDIO, MODE_KOKORO_AUDIO)

# Modes running an AUDIO session whose cloud audio is NOT played: the
# output_transcription is synthesized by another backend instead. Gemini's audio
# is HELD (not discarded) in these modes so it can serve as the fallback.
TRANSCRIPT_SYNTH_MODES = (MODE_FISH_AUDIO, MODE_KOKORO_AUDIO)

VOICE_CONFIG_PATH = BASE_DIR / "config" / "voice.json"


class VoiceModeConfig:
    """Source of truth for the voice mode, split into two facts:

    * ``mode``      — the ACTIVE backend, i.e. what is speaking RIGHT NOW. The
                      auto-fallback machinery moves this when Fish fails mid-turn
                      and moves it back when Fish recovers. Every reader on the
                      live audio path (is_fish/is_gemini/response_modalities/...)
                      wants this one.
    * ``preferred`` — the user's DELIBERATE, standing choice. Only an explicit
                      switch changes it, and it is the ONLY thing persisted to
                      config/voice.json. It is what a relaunch restores and what
                      a Fish recovery returns to.

    Keeping these separate is the fix for "my Gemini choice reverts to Fish": a
    transient fallback used to persist through the same set_mode() a real switch
    uses, so the fallback/recovery pair rewrote the user's saved preference.
    Now a transient switch passes ``persist=False`` — it moves ``mode`` only and
    never touches ``preferred`` or the file.

    set_mode() validates and applies in place. It does NOT trigger a reconnect,
    and no longer needs to: both modes share one session shape.
    """

    def __init__(self) -> None:
        # First-run default only: Gemini's own voice (Karim, 2026-10-09). The
        # user switches by voice at any time and that choice persists in
        # config/voice.json, which reload() applies over this.
        self.mode = MODE_GEMINI_AUDIO
        # The durable user preference. Mirrors config/voice.json; only a
        # persisting set_mode() (a deliberate switch) changes it.
        self.preferred = MODE_GEMINI_AUDIO
        # Set when the quota auto-switch moves Era off her primary voice, so she
        # can explain *why* her voice changed and the UI can distinguish it from
        # a manual switch. Deliberately not persisted: it describes one incident,
        # not a standing preference.
        self.switched_by_quota = False
        self.reload()

    # -- Queries --------------------------------------------------------------
    def is_fish(self) -> bool:
        """Fish Audio is producing the sound (Era's primary voice)."""
        return self.mode == MODE_FISH_AUDIO

    def is_gemini(self) -> bool:
        return self.mode == MODE_GEMINI_AUDIO

    def is_kokoro(self) -> bool:
        """Kokoro George (local GPU) is producing the sound."""
        return self.mode == MODE_KOKORO_AUDIO

    def uses_speech_out(self) -> bool:
        """True when SpeechOut synthesizes the speech, whatever the backend."""
        return self.mode in SPEECH_OUT_MODES

    def synthesizes_from_transcript(self) -> bool:
        """AUDIO session whose cloud audio is held back for the transcript path."""
        return self.mode in TRANSCRIPT_SYNTH_MODES

    @property
    def response_modalities(self) -> list[str]:
        """The Live-session modality this mode implies. Consumed by _build_config.

        Always AUDIO now. Only the removed kokoro_text mode ever asked for TEXT,
        and no current Live model accepts a text-only session anyway.
        """
        return ["AUDIO"]

    def needs_reconnect_to(self, other_mode: str) -> bool:
        """True if moving to `other_mode` changes the session shape.

        Always False: every mode is an AUDIO session. Kept as a method because
        call sites read it to decide whether to tear the session down, and this
        is the single place that answer belongs.
        """
        return False

    # -- Persistence ----------------------------------------------------------
    def reload(self) -> None:
        """Load config/voice.json into BOTH the active mode and the preference,
        seeding the file on first run.

        The file stores the deliberate preference, so on start-up the active mode
        equals it — a relaunch begins on the voice the user chose, never on a
        stale transient fallback (which is never written).

        Degrades to the in-memory default on any failure: a corrupt voice.json
        must cost the preference, never the voice.
        """
        try:
            raw = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
            mode = str(raw.get("mode", "")).strip().lower()
            if mode in VALID_MODES:
                self.mode = mode
                self.preferred = mode
            # Always, in the background (~10 s): Kokoro is the voice choice AND,
            # since Piper's removal (2026-10-10), the only voice when Gemini and
            # Fish are both down. A cold Kokoro is never waited for.
            _warm_kokoro()
        except FileNotFoundError:
            self._write()  # seed the file with current defaults
        except Exception as e:
            print(f"[VOICE] Config load failed ({e}); keeping mode '{self.mode}'.")

    def _write(self) -> None:
        # Only ever writes the PREFERENCE. The active mode may be a transient
        # fallback, which must never reach the file.
        try:
            VOICE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Read-modify-write: the file also holds other voice settings (e.g.
            # "fish_voices", the per-language Fish voice map). A switch must
            # never erase them.
            try:
                data = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            data["mode"] = self.preferred
            VOICE_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[VOICE] Could not persist voice mode: {e}")

    # -- Mutations ------------------------------------------------------------
    # Spoken/typed synonyms. "primary"/"fish"/"clone" mean the Fish voice;
    # "normal"/"gemini"/"cloud"/"fallback" mean Gemini's own voice.
    _ALIASES = {
        "fish": MODE_FISH_AUDIO,
        "fish audio": MODE_FISH_AUDIO,
        "fish voice": MODE_FISH_AUDIO,
        "primary": MODE_FISH_AUDIO,
        "cloned": MODE_FISH_AUDIO,
        "clone": MODE_FISH_AUDIO,
        MODE_FISH_AUDIO: MODE_FISH_AUDIO,
        "normal": MODE_GEMINI_AUDIO,
        "gemini": MODE_GEMINI_AUDIO,
        "cloud": MODE_GEMINI_AUDIO,
        "default": MODE_GEMINI_AUDIO,
        "fallback": MODE_GEMINI_AUDIO,
        MODE_GEMINI_AUDIO: MODE_GEMINI_AUDIO,
        "kokoro": MODE_KOKORO_AUDIO,
        "kokoro voice": MODE_KOKORO_AUDIO,
        "local": MODE_KOKORO_AUDIO,
        "local voice": MODE_KOKORO_AUDIO,
        "offline": MODE_KOKORO_AUDIO,
        "offline voice": MODE_KOKORO_AUDIO,
        "george": MODE_KOKORO_AUDIO,
        MODE_KOKORO_AUDIO: MODE_KOKORO_AUDIO,
    }

    @classmethod
    def _resolve_alias(cls, mode: str) -> str | None:
        """Map a spoken/typed mode name to a canonical mode id, or None."""
        return cls._ALIASES.get((mode or "").strip().lower())

    def set_mode(self, mode: str, *, by_quota: bool = False, persist: bool = True) -> str:
        """Validate + apply a voice mode. Returns the applied mode.

        ``persist=True`` (default) is a DELIBERATE switch: it also updates the
        saved preference and writes config/voice.json, so it survives a relaunch.
        ``persist=False`` is a TRANSIENT switch (the TTS fallback and its
        recovery): it moves the active mode only and leaves the saved preference
        — and the file — untouched. That is what stops an auto-fallback from
        overwriting the user's standing choice.
        """
        resolved = self._resolve_alias(mode)
        if resolved is None:
            raise ValueError(f"unknown voice mode '{mode}' (expected: fish, kokoro or gemini)")
        self.mode = resolved
        # Only a switch AWAY from the primary can be quota-driven. Any move back
        # to Fish is by definition deliberate, so it always clears the flag.
        self.switched_by_quota = by_quota and resolved == MODE_GEMINI_AUDIO
        if persist:
            self.preferred = resolved
            self._write()
        return self.mode


# ── Gemini's own voice ──────────────────────────────────────────────────────
# The 30 prebuilt voices Gemini LIVE really uses, with Google's style word and
# gender (Google's /v1beta/voices list; Kore measured at ~200 Hz, female).
#
# Measured 2026-10-10 on gemini-3.8-live by voice pitch: Charon 109 Hz (male),
# Kore 200 Hz. AI Studio's 1,000+ library voices ("Finn", "en-us-finn", "Arlo")
# are ACCEPTED by Live but silently replaced by a default female voice (~200-220
# Hz), even though the same names are male on the TTS model (Finn 113-124 Hz).
# So only these 30 are offered; any other name falls back to the default.
# Set from the HUD (Settings -> VOICE) or config/voice.json "gemini_voice".
GEMINI_VOICES = {
    "Zephyr": "Bright",
    "Puck": "Upbeat",
    "Charon": "Informative",
    "Kore": "Firm",
    "Fenrir": "Excitable",
    "Leda": "Youthful",
    "Orus": "Firm",
    "Aoede": "Breezy",
    "Callirrhoe": "Easy-going",
    "Autonoe": "Bright",
    "Enceladus": "Breathy",
    "Iapetus": "Clear",
    "Umbriel": "Easy-going",
    "Algieba": "Smooth",
    "Despina": "Smooth",
    "Erinome": "Clear",
    "Algenib": "Gravelly",
    "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat",
    "Achernar": "Soft",
    "Alnilam": "Firm",
    "Schedar": "Even",
    "Gacrux": "Mature",
    "Pulcherrima": "Forward",
    "Achird": "Friendly",
    "Zubenelgenubi": "Casual",
    "Vindemiatrix": "Gentle",
    "Sadachbia": "Lively",
    "Sadaltager": "Knowledgeable",
    "Sulafat": "Warm",
}
GEMINI_VOICE_GENDER = {
    "Zephyr": "female",
    "Puck": "male",
    "Charon": "male",
    "Kore": "female",
    "Fenrir": "male",
    "Leda": "female",
    "Orus": "male",
    "Aoede": "female",
    "Callirrhoe": "female",
    "Autonoe": "female",
    "Enceladus": "male",
    "Iapetus": "male",
    "Umbriel": "male",
    "Algieba": "male",
    "Despina": "female",
    "Erinome": "female",
    "Algenib": "male",
    "Rasalgethi": "male",
    "Laomedeia": "female",
    "Achernar": "female",
    "Alnilam": "male",
    "Schedar": "male",
    "Gacrux": "female",
    "Pulcherrima": "neutral",
    "Achird": "male",
    "Zubenelgenubi": "male",
    "Vindemiatrix": "female",
    "Sadachbia": "male",
    "Sadaltager": "male",
    "Sulafat": "female",
}
DEFAULT_GEMINI_VOICE = "Algenib"

_refused_voice = {"name": ""}  # this run only: a name Gemini said it does not know


def _canonical(name: str) -> str:
    """Google's exact spelling of one of the 30, or "" if it is not one of them."""
    wanted = str(name or "").strip().lower()
    return next((n for n in GEMINI_VOICES if n.lower() == wanted), "")


def configured_gemini_voice() -> str:
    """What config/voice.json asks for, as written (the HUD shows this)."""
    try:
        raw = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8")).get("gemini_voice")
    except Exception:
        return ""
    return str(raw or "").strip()


def gemini_voice_name() -> str:
    """The voice Era connects with: the configured one if it is one of the 30
    Live voices (and Gemini has not refused it this run), else the default."""
    wanted = configured_gemini_voice()
    name = _canonical(wanted)
    if wanted and not name:
        print(f"[VOICE] {wanted!r} is not a Gemini Live voice; using {DEFAULT_GEMINI_VOICE}.")
    if not name or name == _refused_voice["name"]:
        return DEFAULT_GEMINI_VOICE
    return name


def set_gemini_voice(name: str) -> str:
    """Save Gemini's voice to config/voice.json (keeping every other key) and
    return the stored name. Only the 30 Live voices: anything else raises
    ValueError, so nothing odd is ever written."""
    canonical = _canonical(name)
    if not canonical:
        raise ValueError(f"{str(name)[:40]!r} is not one of Gemini Live's 30 voices")
    try:
        data = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["gemini_voice"] = canonical
    VOICE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOICE_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if _refused_voice["name"] != canonical:
        _refused_voice["name"] = ""  # a new choice gets a fresh chance
    return canonical


def gemini_voice_view() -> dict:
    """What the HUD shows: the configured voice, the one in use, the 30 to pick
    from (with gender), and why they differ when they do."""
    configured = configured_gemini_voice() or DEFAULT_GEMINI_VOICE
    active = gemini_voice_name()
    note = ""
    if not _canonical(configured):
        note = (
            f"“{configured}” is an AI Studio text-to-speech voice; Gemini Live "
            f"cannot use it (it would sound like a default female voice). Using {active}."
        )
    elif active.lower() != configured.lower():
        note = f"Gemini refused “{configured}”. Using {active}."
    return {
        "gemini_voice": _canonical(configured) or active,
        "gemini_voice_active": active,
        "gemini_voice_note": note,
        "gemini_voices": [
            {"name": n, "style": st, "gender": GEMINI_VOICE_GENDER[n]}
            for n, st in GEMINI_VOICES.items()
        ],
    }


def reject_gemini_voice() -> str:
    """Gemini answered 1007 "No matching speaker voice": stop asking for that name
    until restart. Returns the refused name, or "" if the default itself was
    refused (nothing left to fall back to, so no retry loop)."""
    name = gemini_voice_name()
    if name == DEFAULT_GEMINI_VOICE:
        return ""
    _refused_voice["name"] = name
    print(f"[VOICE] Gemini refused the voice {name!r}; using {DEFAULT_GEMINI_VOICE}.")
    return name


def _prefer_gpu_onnxruntime() -> None:
    """Before ANY import of onnxruntime: Era's Python has a CPU-only copy that
    left Kokoro on the CPU (~1x realtime) in the first live test, 2026-10-10."""
    try:
        from core.tts_kokoro import install_gpu_runtime

        install_gpu_runtime()
    except Exception as e:
        print(f"[VOICE] GPU onnxruntime redirect skipped: {type(e).__name__}")


_prefer_gpu_onnxruntime()


def _warm_kokoro() -> None:
    try:
        from core.tts_kokoro import KOKORO

        KOKORO.warm_in_background()
    except Exception as e:  # a voice warm-up must never cost the config
        print(f"[VOICE] Kokoro warm-up skipped: {type(e).__name__}")


VOICE_CFG = VoiceModeConfig()
