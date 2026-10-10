"""What Era listens to: never her own echo, and only speech meant for her.

Karim, 2026-10-10: Era answered HERSELF (the tail of her own Kokoro voice, and
media playback) and answered phone calls and room chat. Two local rules, both
applied to mic audio BEFORE it reaches Gemini:

1. ECHO HOLD. The mic reopened the moment Era's last chunk was handed to the
   sound card, while its last ~0.2-0.5 s (device buffer, Bluetooth, room
   reverb) was still playing, so Gemini heard the end of her own sentence and
   answered it. After Era finishes speaking ON HER OWN, the mic stays closed for
   `echo_hold_ms`. Never after a barge-in: then the user is already mid-sentence.

2. ATTENTION WINDOW. For `attention_window_s` after the last exchange (Era
   finished speaking, an accepted utterance, a wake; never while she is busy
   thinking or replying), speech passes freely, so
   a conversation flows. After that, an utterance reaches Gemini only if its
   first words are Era's name or her wake word, checked locally by Vosk with the
   wake word's own decoy grammar. Anything else (a phone call, room chat, TV
   dialogue) is replaced by silence and never leaves the machine. The window
   stays open while a confirmation waits for the user's answer.

FAIL-OPEN, deliberately: this is a convenience filter, not a security gate (the
owner voiceprint gate, core/voice_gate.py, is the security one). With no Vosk
model, or any error, audio passes exactly as before rather than Era going deaf.

Config: config/voice.json {"listening": {...}} over DEFAULTS. attention_window_s
0 turns the attention rule off; echo_hold_ms 0 turns the hold off.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

DEFAULTS = {
    "echo_hold_ms": 700,
    # Counted from when Era is DONE (she is never "away" while thinking or
    # replying). 20 s expired mid-conversation in the first live test.
    "attention_window_s": 45.0,
    "name_check_s": 1.6,  # speech heard before the name check must decide
    "names": ["era"],  # always accepted, besides the wake word and its aliases
    # Vosk word confidence the name must reach. Measured 2026-10-10 on real
    # speech: "Era ..." 0.95-1.00, "Error in the report" 0.51, "Errors" 0.67;
    # the wake word uses the same 0.90 bar.
    "name_confidence": 0.90,
}
_VOICE_JSON = Path(__file__).resolve().parent.parent / "config" / "voice.json"
_END_SILENCE_S = 0.7  # this much quiet ends an utterance
_PREROLL_FRAMES = 5  # ~0.3 s at 64 ms frames: the word's onset, kept for Gemini

IDLE, CHECKING, PASSING, DROPPING = "idle", "checking", "passing", "dropping"


def get_listening_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        block = json.loads(_VOICE_JSON.read_text(encoding="utf-8-sig")).get("listening")
        if isinstance(block, dict):
            cfg.update({k: v for k, v in block.items() if k in DEFAULTS and v is not None})
    except Exception:
        pass
    try:
        cfg["echo_hold_ms"] = min(max(int(cfg["echo_hold_ms"]), 0), 3000)
        cfg["attention_window_s"] = min(max(float(cfg["attention_window_s"]), 0.0), 600.0)
        cfg["name_check_s"] = min(max(float(cfg["name_check_s"]), 0.6), 4.0)
        cfg["names"] = [str(n).lower().strip() for n in cfg["names"] if str(n).strip()]
        cfg["name_confidence"] = min(max(float(cfg["name_confidence"]), 0.0), 1.0)
    except (TypeError, ValueError):
        return dict(DEFAULTS)
    return cfg


def _names() -> set[str]:
    """Era's name(s) plus the live wake word and its aliases."""
    names = set(get_listening_config()["names"])
    try:
        from app.wake import WAKE_CFG

        names.add(WAKE_CFG.word.lower())
        names.update(a.lower() for a in WAKE_CFG.aliases)
    except Exception:
        pass
    return {n for n in names if n}


def addressed_to_era(text: str, names: set[str] | None = None) -> bool:
    """Is one of the first two words Era's name? ("Era, ..." / "hey era ...")."""
    names = names or _names()
    words = re.findall(r"[a-z']+", (text or "").lower())
    if words and words[0] in {"hey", "ok", "okay"}:
        words = words[1:]
    return any(w in names for w in words[:2])


def _final_words(rec) -> list[tuple[str, float]]:
    """(word, confidence) from Vosk's final result (SetWords(True)); a recognizer
    without per-word results counts its words as certain."""
    result = json.loads(rec.FinalResult())
    if result.get("result"):
        return [(w["word"], float(w.get("conf", 1.0))) for w in result["result"]]
    return [(w, 1.0) for w in (result.get("text") or "").split()]


def _named(words: list[tuple[str, float]], names: set[str], min_conf: float) -> bool:
    """addressed_to_era with confidence: a near-miss ("error") decoded as the
    name at low confidence does not count."""
    if words and words[0][0] in {"hey", "ok", "okay"}:
        words = words[1:]
    return any(w in names and c >= min_conf for w, c in words[:2])


def _rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    arr = np.frombuffer(frame[: len(frame) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(arr * arr)))


class ListeningState:
    """The two clocks: echo hold, and when Era last talked WITH the user."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        media_active: Callable[[], bool] | None = None,
    ) -> None:
        self._clock = clock
        # Music/video playing (core/media_activity): the free window would let
        # lyrics through, and each reply to one re-opened it (live 2026-10-10).
        self._media_active = media_active or (lambda: False)
        self._hold_until = 0.0
        self._last_activity = clock()  # a fresh session starts in conversation

    def era_finished_speaking(self, interrupted: bool) -> None:
        now = self._clock()
        if not interrupted:
            self._hold_until = now + get_listening_config()["echo_hold_ms"] / 1000.0
        self._last_activity = now

    def note_activity(self) -> None:
        """An accepted utterance, a typed command or a wake: conversation is on."""
        self._last_activity = self._clock()

    def echo_hold_active(self) -> bool:
        return self._clock() < self._hold_until

    def window_open(self) -> bool:
        window = get_listening_config()["attention_window_s"]
        if window <= 0:
            return True  # the rule is switched off
        try:
            if self._media_active():
                return False  # media playing: only her name gets through
        except Exception:
            pass
        return self._clock() - self._last_activity <= window


class AttentionGate:
    """Per-utterance gate on mic frames (16 kHz int16), after the voice gate.

    feed(frame, keep_open) -> the frames to send to Gemini now. While the window
    is open everything passes. While closed, an utterance is held (with a short
    pre-roll) while Vosk decodes its first words: named -> the held audio is
    sent and the rest passes; not named -> silence until the utterance ends.
    """

    def __init__(
        self,
        state: ListeningState,
        recognizer_factory: Callable[[], object] | None,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.state = state
        self._make_rec = recognizer_factory
        self._on_event = on_event or (lambda kind, text: None)
        self.mode = IDLE
        self._noise = 0.004
        self._held: list[bytes] = []
        self._preroll: list[bytes] = []
        self._speech_s = 0.0
        self._quiet_s = 0.0
        self._rec = None

    def _is_speech(self, frame: bytes) -> bool:
        level = _rms(frame)
        speech = level > max(0.012, self._noise * 3.0)
        if not speech and self.mode == IDLE:
            self._noise = 0.95 * self._noise + 0.05 * level
        return speech

    def feed(self, frame: bytes, keep_open: bool = False) -> list[bytes]:
        try:
            return self._feed(frame, keep_open)
        except Exception as e:  # fail OPEN: never deafen Era over a filter bug
            self._on_event("error", f"{type(e).__name__}: {e}")
            self.mode, self._held, self._rec = IDLE, [], None
            return [frame]

    def _open(self, keep_open: bool) -> bool:
        return keep_open or self._make_rec is None or self.state.window_open()

    def _feed(self, frame: bytes, keep_open: bool) -> list[bytes]:
        dur = (len(frame) // 2) / 16000.0
        speech = self._is_speech(frame)
        silence = bytes(len(frame))

        if self.mode == IDLE:
            if not speech:
                self._preroll = (self._preroll + [frame])[-_PREROLL_FRAMES:]
                # Closed: quiet frames go as zeros, so faint speech (a phone's
                # earpiece, a TV across the room) never leaks to Gemini.
                return [frame] if self._open(keep_open) else [silence]
            if self._open(keep_open):
                self.mode, self._quiet_s = PASSING, 0.0
                self.state.note_activity()
                self._preroll = []
                return [frame]
            self.mode = CHECKING
            self._held, self._preroll = [*self._preroll, frame], []
            self._speech_s, self._quiet_s = dur, 0.0
            self._rec = self._make_rec()
            for f in self._held:
                self._rec.AcceptWaveform(f)
            return [silence]

        if self.mode == CHECKING:
            self._held.append(frame)
            if speech:
                self._speech_s, self._quiet_s = self._speech_s + dur, 0.0
            else:
                self._quiet_s += dur
            self._rec.AcceptWaveform(frame)
            heard = json.loads(self._rec.PartialResult()).get("partial", "")
            ended = self._quiet_s >= _END_SILENCE_S
            real_words = [w for w in heard.split() if w != "[unk]"]
            if (
                ended
                or self._speech_s >= get_listening_config()["name_check_s"]
                or addressed_to_era(heard)
                or (len(heard.split()) >= 2 and real_words)
            ):
                # Decide on the FINAL words: they carry the confidence a partial lacks.
                return self._decide(_final_words(self._rec), ended=ended, silence=silence)
            return [silence]

        # PASSING / DROPPING until the utterance ends.
        self._quiet_s = 0.0 if speech else self._quiet_s + dur
        passing = self.mode == PASSING
        if self._quiet_s >= _END_SILENCE_S:
            if passing:
                self.state.note_activity()
            self.mode, self._quiet_s = IDLE, 0.0
        return [frame] if passing else [silence]

    def _decide(
        self, words: list[tuple[str, float]], *, ended: bool, silence: bytes
    ) -> list[bytes]:
        held, self._held, self._rec = self._held, [], None
        self._quiet_s = 0.0
        heard = " ".join(f"{w}:{c:.2f}" for w, c in words)
        if _named(words, _names(), get_listening_config()["name_confidence"]):
            self._on_event("accepted", heard)
            self.state.note_activity()
            self.mode = IDLE if ended else PASSING
            return held  # the whole utterance so far, name included, in order
        self._on_event("ignored", heard)
        self.mode = IDLE if ended else DROPPING
        return [silence]
