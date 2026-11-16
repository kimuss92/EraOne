"""What Era listens to: never her own echo, and only speech meant for her.

Karim, 2026-10-10: Era answered HERSELF (the tail of her own Kokoro voice, and
media playback) and answered phone calls and room chat. Two local rules, both
applied to mic audio BEFORE it reaches Gemini:

1. ECHO HOLD. The mic reopened the moment Era's last chunk was handed to the
   sound card, while its last ~0.2-0.5 s (device buffer, Bluetooth, room
   reverb) was still playing, so Gemini heard the end of her own sentence and
   answered it. After Era finishes speaking ON HER OWN, the mic stays closed for
   `echo_hold_ms`. Never after a barge-in: then the user is already mid-sentence.

2. LISTENING SESSION. One rolling window (core/session.py), default 2 minutes
   and owner-adjustable by voice. Inside it, speech passes freely, so a
   conversation flows and a follow-up needs no name. Outside it, an utterance
   reaches Gemini only if its first words are Era's name or her wake word,
   checked locally by Vosk with the wake word's own decoy grammar. Anything
   else (a phone call, room chat, TV dialogue) is replaced by silence and never
   leaves the machine. Every ACCEPTED utterance re-anchors the window; Era's own
   reply re-anchors it only after an explicit address (the 2026-10-10 lyric
   loop). The window applies whether or not media plays — the old media-only
   timer was what made "Era" mandatory with music and unnecessary in silence.

Outside the session, a missing or broken recognizer cannot authorize audio.
Only local wake detection runs until a name is verified; silence keeps the
connection warm. Inside the session the existing fail-open behavior remains.

Config: config/voice.json {"listening": {...}} for echo_hold_ms and the name
check; config/session.json for the window itself.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from core.session import SessionWindow

DEFAULTS = {
    "echo_hold_ms": 700,
    # Counted from when Era is DONE (she is never "away" while thinking or
    # replying). 20 s expired mid-conversation in the first live test.
    "attention_window_s": 120.0,
    "name_check_s": 1.6,  # speech heard before the name check must decide
    "names": ["era"],  # always accepted, besides the wake word and its aliases
    # Vosk word confidence the name must reach. Measured 2026-10-10 on real
    # speech: "Era ..." 0.95-1.00 in quiet, "Error in the report" 0.51-0.61,
    # "Errors" 0.61-0.67. Live with music on, a real "Era" scored 0.85 and the
    # old 0.90 bar refused it; 0.80 still sits above every near-miss measured.
    "name_confidence": 0.80,
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
    """Does speech start with Era's name (optionally after hey/ok/okay)?"""
    names = names or _names()
    words = re.findall(r"[a-z']+", (text or "").lower().replace("[unk]", " "))
    if words and words[0] in {"hey", "ok", "okay"}:
        words = words[1:]
    return bool(words and words[0] in names)


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
    words = [(w, c) for w, c in words if w != "[unk]"]
    if words and words[0][0] in {"hey", "ok", "okay"}:
        words = words[1:]
    return bool(words and words[0][0] in names and words[0][1] >= min_conf)


def _rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    arr = np.frombuffer(frame[: len(frame) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(arr * arr)))


class ListeningState:
    """The two clocks: echo hold, and the listening session.

    The session itself lives in core/session.py: a rolling window (default
    2 minutes, owner-adjustable by voice) that every ACCEPTED utterance
    re-anchors. Inside it, bare commands work; outside it, the first two words
    must be Era's name or her wake word. Era's reply re-anchors a window only
    after an explicit address, so a lyric that slipped through cannot keep
    re-opening it (the 2026-10-10 loop).

    The window used to apply only while media played, which made "Era" mandatory
    with music and unnecessary in a silent room — two different answers from
    the same assistant. It now applies always (owner, 2026-11-11).
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        media_active: Callable[[], bool] | None = None,
        window_s: float | None = None,
    ) -> None:
        self._clock = clock
        # The window applies whether or not media plays; media only decides
        # whether unnamed passing speech may re-anchor it (AttentionGate).
        self._media_active = media_active or (lambda: False)
        self._session = SessionWindow(clock=clock, window_s=window_s)
        self._hold_until = 0.0
        # The user deliberately addressed Era (her name, a wake, typing).
        self._explicit_reply_pending = False

    def era_finished_speaking(self, interrupted: bool, internal: bool = False) -> None:
        """Era's reply re-anchors the window only after an explicit address.

        internal=True (PF-034): she spoke for her own reasons (a scheduled
        report, a background result). That is not conversation with the owner,
        so it neither re-anchors the window nor consumes a pending address.
        """
        if self._explicit_reply_pending and not internal:
            self._session.note_accepted()
            self._explicit_reply_pending = False
        if not interrupted:
            self._hold_until = self._clock() + get_listening_config()["echo_hold_ms"] / 1000.0

    def _media_playing(self) -> bool:
        try:
            return bool(self._media_active())
        except Exception:
            return True  # unsure: be strict, only an explicit address re-anchors

    def note_activity(self, explicit: bool = False) -> None:
        """An utterance reached Era: it counts as conversation either way.

        explicit=True: the user addressed Era by name, by typing or by waking
        her. Only an explicit address lets Era's OWN reply re-anchor the window.
        """
        self._session.note_accepted()
        if explicit:
            self._explicit_reply_pending = True

    def close_session(self) -> None:
        """Close the session ("standby" in the one-mode model)."""
        self._explicit_reply_pending = False
        self._session.close()

    def echo_hold_active(self) -> bool:
        return self._clock() < self._hold_until

    def note_transcript(self, text: str) -> None:
        """Recognize an explicit address even while the free window is open."""
        if addressed_to_era(text):
            self.note_activity(explicit=True)

    def window_open(self) -> bool:
        """Is Era in session, or must the next utterance carry her name?"""
        return self._session.open()

    def session_remaining_s(self) -> float:
        return self._session.remaining_s()


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
        self._dropped_s = 0.0
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
        except Exception as e:
            self._on_event("error", f"{type(e).__name__}: {e}")
            self.mode, self._held, self._rec = IDLE, [], None
            return [frame] if self.state.window_open() else [bytes(len(frame))]

    def _reanchor_passing(self) -> None:
        """Unnamed passing speech re-anchors only a REALLY open window, with no
        media playing (PF-034). Live 2026-11-16: a scheduled report's busy
        exemption let room audio pass and re-open an expired window, and music
        pauses kept re-anchoring it, so Era answered without her name."""
        state = self.state
        if not state.window_open():
            return  # passing only because of a busy/confirmation exemption
        media = getattr(state, "_media_playing", None)
        if callable(media) and media():
            return  # a lyric must not keep the window open (2026-10-10 loop)
        note = getattr(state, "note_activity", None)
        if callable(note):
            note()

    def _open(self, keep_open: bool) -> bool:
        return keep_open or self.state.window_open()

    def _feed(self, frame: bytes, keep_open: bool) -> list[bytes]:
        dur = (len(frame) // 2) / 16000.0
        speech = self._is_speech(frame)
        silence = bytes(len(frame))

        # A continuous noise/TV segment may never provide an utterance-ending
        # silence. PASSING is not authority to outlive the existing session.
        if self.mode == PASSING and not self._open(keep_open):
            self.mode, self._held, self._rec = IDLE, [], None
            self._preroll = []
            self._quiet_s = 0.0
        if not self._open(keep_open) and self._make_rec is None:
            return [silence]  # unavailable wake recognition never bypasses inactivity

        if self.mode in (CHECKING, DROPPING) and self._open(keep_open):
            # A stale ignored utterance must not survive media stopping, an
            # explicit wake or an opened conversation. Do not leak held media.
            self.mode, self._held, self._rec = PASSING, [], None
            self._quiet_s, self._dropped_s = 0.0, 0.0
            self._on_event("reopened", "listening context became active")
            return [frame]

        if self.mode == IDLE:
            if not speech:
                self._preroll = (self._preroll + [frame])[-_PREROLL_FRAMES:]
                # Closed: quiet frames go as zeros, so faint speech (a phone's
                # earpiece, a TV across the room) never leaks to Gemini.
                return [frame] if self._open(keep_open) else [silence]
            if self._open(keep_open):
                self.mode, self._quiet_s = PASSING, 0.0
                self._reanchor_passing()
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
        if not passing:
            self._dropped_s += dur
            if self._dropped_s >= get_listening_config()["name_check_s"]:
                # Continuous TV/room sound may never have 700 ms of silence.
                # Re-arm bounded name checks rather than ignoring forever.
                self.mode, self._dropped_s = IDLE, 0.0
                self._preroll = []
        if self._quiet_s >= _END_SILENCE_S:
            if passing:
                self._reanchor_passing()
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
            self.state.note_activity(explicit=True)
            self.mode = IDLE if ended else PASSING
            return held  # the whole utterance so far, name included, in order
        self._on_event("ignored", heard)
        self.mode = IDLE if ended else DROPPING
        self._dropped_s = 0.0
        return [silence]
