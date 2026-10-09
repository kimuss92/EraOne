"""Era's voice mode — which backend speaks.

Two modes, both running on an ordinary AUDIO Live session:

    fish_audio     PRIMARY. Gemini's audio is held back and the
                   output_transcription is synthesized by Fish Audio instead.
    gemini_audio   FALLBACK. Gemini's own Live audio is played directly.

Because the session modality never changes between them, switching is INSTANT —
no reconnect, no dropped conversation. That is what lets `_tts_fallback` hand a
turn to Gemini mid-sentence when Fish fails, and what makes a spoken "use your
Gemini voice" take effect immediately.

(The local Kokoro/Voicebox engine and its kokoro_text / kokoro_audio modes were
removed on 2026-09-16 — Fish + Gemini replaced them.)
"""

import json

from core.utils import BASE_DIR

# ---- MODES ----
MODE_GEMINI_AUDIO = "gemini_audio"
MODE_FISH_AUDIO = "fish_audio"

VALID_MODES = (MODE_GEMINI_AUDIO, MODE_FISH_AUDIO)

# Modes where SpeechOut owns the speech (text in -> synthesized audio out).
SPEECH_OUT_MODES = (MODE_FISH_AUDIO,)

# Modes running an AUDIO session whose cloud audio is NOT played: the
# output_transcription is synthesized by another backend instead. Gemini's audio
# is HELD (not discarded) in these modes so it can serve as the fallback.
TRANSCRIPT_SYNTH_MODES = (MODE_FISH_AUDIO,)

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
            raise ValueError(f"unknown voice mode '{mode}' (expected: fish or gemini)")
        self.mode = resolved
        # Only a switch AWAY from the primary can be quota-driven. Any move back
        # to Fish is by definition deliberate, so it always clears the flag.
        self.switched_by_quota = by_quota and resolved == MODE_GEMINI_AUDIO
        if persist:
            self.preferred = resolved
            self._write()
        return self.mode


VOICE_CFG = VoiceModeConfig()
