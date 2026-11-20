"""Voice-controlled microphone mute for EraLive (see core/mic_mute.py for the model).

Mute   — "mute your mic" / "stop listening": caught by the set_microphone tool
         (Gemini), by the voice-transcript backstop in _receive_audio, or by the
         typed path in _on_text_command. All three land in mute_microphone(),
         which is idempotent, so whichever fires first wins and the rest no-op.
Unmute — "activate mic" / "start listening": while muted (by voice, typing or
         the button alike), Gemini hears nothing, so the mic feeds a LOCAL Vosk
         recognizer (_run_unmute_listener) that only knows the unmute phrases.
         The phrase goes through the speaker verifier: with owner verification
         ON only the owner's voice counts; with it OFF (owner's choice) grammar +
         confidence are the only check. Also typed commands and the UI mic button.

Mic-mute is orthogonal to standby: standby/wake never reopens a voice-muted
mic (a network-failure standby must not be a way around a privacy mute). Only
an explicit mic-on order does. The browser/UI hard-off switch remains separate:
a vocal unmute clears the logical voice mute but cannot override hard-off.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.text_intents import _say_exactly
from core.mic_mute import (
    MIC_OFF_ACK,
    MIC_ON_ACK,
    is_mic_off_order,
    is_mic_on_order,
    match_unmute_final,
    unmute_grammar,
    vosk_confidence,
)
from core.voice_gate import WakeAudioTap, WakeVerifier

try:
    import vosk
except ImportError:  # pragma: no cover - vosk is optional
    vosk = None

_VOICE_UNMUTE_HINT = "Say 'start listening' or 'activate mic' to turn it back on."
# Mute sources whose origin was the owner's own voice (kept for the UI/telemetry).
# PF-008 (owner, 2026-11-16): EVERY mute, whatever its source, can be undone by
# voice, typing or the mic button. Ambient noise still cannot reopen it: the local
# listener knows only the unmute phrases (strict grammar + confidence) and skips
# Era's own voice. With owner verification ON the phrase must be the owner's;
# with it OFF any clear speaker saying a phrase can unmute (owner-accepted risk).
_VOICE_UNMUTABLE_SOURCES = frozenset({"voice", "restored"})


class MicMuteMixin:
    _mic_muted: bool = False
    _mic_muted_source: str = "voice"
    _unmute_queue: asyncio.Queue | None = None
    _unmute_task: Any = None
    _unmute_ignore_until: float = 0.0

    # ── State ────────────────────────────────────────────────────────────────
    def mic_effective_active(self) -> bool:
        """What the UI indicator shows: audio can reach Gemini right now."""
        return bool(self._browser_mic_active) and not self._mic_muted

    def _broadcast_mic_state(self) -> None:
        try:
            self.ui._broadcast({"type": "browser_mic", "active": self.mic_effective_active()})
            self.ui._broadcast({"type": "mic_muted", "muted": self._mic_muted, "ts": time.time()})
        except Exception:
            pass

    @staticmethod
    def _persist_mic_muted(value: bool, source: str = "") -> None:
        try:
            from core.user_prefs import set_mic_mute_source, set_mic_muted

            set_mic_muted(value)
            set_mic_mute_source(source if value else "")
        except Exception as e:
            print(f"[MIC-MUTE] Could not persist mic state: {e}")

    def mic_muted_by_voice(self) -> bool:
        """True when the owner's own voice order muted the mic."""
        return self._mic_muted and self._mic_muted_source in _VOICE_UNMUTABLE_SOURCES

    def _say_mic_ack(self, text: str) -> None:
        try:
            self.speak(_say_exactly(text))
        except Exception as e:
            print(f"[MIC-MUTE] Ack speak failed: {e}")

    # ── Transitions ──────────────────────────────────────────────────────────
    def mute_microphone(self, source: str, speak_ack: bool = True) -> bool:
        """Stop sending mic audio anywhere but the local unmute listener.

        Returns False when already muted. Muting never clears
        ``_browser_mic_active``: local capture must remain available to the
        restricted unmute recognizer (owner-verified when verification is on).
        """
        if self._mic_muted:
            return False
        # Flag first: the audio callback and _send_realtime both read it, so no
        # further frame reaches Gemini from this point on.
        self._mic_muted = True
        self._mic_muted_source = str(source or "voice").strip().lower()
        dropped = self._drain_mic_out_queue()
        self._persist_mic_muted(True, self._mic_muted_source)
        print(f"[MIC-MUTE] Microphone muted ({source}); dropped {dropped} queued frame(s)")
        self.ui.write_log(f"SYS: Microphone off ({source}). {self._unmute_hint()}")
        self._broadcast_mic_state()
        # PF-008 (owner, 2026-11-16): any mute can be undone by voice. Live
        # 18:03: after "microphone off" the owner said "activate mic" for 40 s
        # and nothing listened; only the mic button worked.
        self._ensure_unmute_listener()
        if speak_ack:
            self._say_mic_ack(MIC_OFF_ACK)
        return True

    def unmute_microphone(self, source: str, speak_ack: bool = True) -> bool:
        """Reopen the mic to Gemini. Returns False when it was not muted."""
        if not self._mic_muted:
            return False
        self._mic_muted = False
        self._mic_muted_source = ""
        self._stop_unmute_listener()
        self._persist_mic_muted(False)
        self._last_interaction_timestamp = time.time()
        print(f"[MIC-MUTE] Microphone unmuted ({source})")
        self.ui.write_log(f"SYS: Microphone on ({source}).")
        self._broadcast_mic_state()
        if speak_ack:
            self._say_mic_ack(MIC_ON_ACK)
        return True

    def restore_mic_mute(self) -> None:
        """Re-apply a mute that was active before a restart/relaunch."""
        try:
            from core.user_prefs import get_mic_mute_source, get_mic_muted

            if not get_mic_muted():
                return
            previous = (get_mic_mute_source() or "voice").strip().lower()
        except Exception:
            return
        self._mic_muted = True
        # Keep the source for the UI/telemetry; every restored mute re-arms the
        # local unmute listener below (PF-008).
        self._mic_muted_source = "restored" if previous in _VOICE_UNMUTABLE_SOURCES else previous
        print("[MIC-MUTE] Restored muted microphone from previous session")
        self.ui.write_log(f"SYS: Microphone is still off from before. {self._unmute_hint()}")
        self._broadcast_mic_state()
        self._ensure_unmute_listener()

    def _unmute_hint(self) -> str:
        if getattr(self, "_vosk_model", None) is None:
            return (
                "Voice unmute is unavailable (Vosk model missing) — use the mic button "
                "or type 'start listening'."
            )
        return _VOICE_UNMUTE_HINT

    def _drain_mic_out_queue(self) -> int:
        q = getattr(self, "out_queue", None)
        count = 0
        if q is None:
            return 0
        while True:
            try:
                q.get_nowait()
                count += 1
            except Exception:
                return count

    # ── Tool / typed entry points ────────────────────────────────────────────
    def _handle_set_microphone(self, args: dict) -> str:
        """Dispatcher entry point for the registered set_microphone tool."""
        state = str(args.get("state", "")).strip().lower()
        if state in ("off", "mute", "muted", "false", "0"):
            if self.mute_microphone("voice"):
                return "Microphone muted. Say nothing — the system speaks its own notice."
            # A prior listener can have stopped because of a recognizer error;
            # repeating "off" repairs it so voice unmute stays available.
            self._ensure_unmute_listener()
            return "The microphone is already off. Say nothing."
        if state in ("on", "unmute", "unmuted", "true", "1"):
            if self.unmute_microphone("voice"):
                return "Microphone on. Say nothing — the system speaks its own notice."
            return "The microphone is already on. Say nothing."
        return "Unknown microphone state. Use state 'off' or 'on'."

    def _handle_typed_mic_order(self, text: str) -> bool:
        """Typed/Telegram mic order. Returns True when ``text`` was one."""
        if is_mic_off_order(text):
            if self.mute_microphone("typed", speak_ack=False):
                ack = MIC_OFF_ACK
            else:
                self._ensure_unmute_listener()
                ack = "My microphone is already off."
        elif is_mic_on_order(text):
            if self.unmute_microphone("typed", speak_ack=False):
                ack = MIC_ON_ACK
            else:
                ack = "My microphone is already on."
        else:
            return False
        self._tg_ack(ack)
        self._say_mic_ack(ack)
        return True

    # ── Local unmute listener (offline Vosk; owner-verified when enabled) ────
    def _feed_unmute_audio(self, loop, data: bytes) -> None:
        """Called from the sounddevice callback thread while voice-muted."""
        q = self._unmute_queue
        if q is not None:
            loop.call_soon_threadsafe(q.put_nowait, data)

    def _ensure_unmute_listener(self) -> None:
        """Arm or repair the local listener while the logical mute is active."""
        if not self._mic_muted:
            return
        task = self._unmute_task
        if self._unmute_queue is not None and task is not None and not task.done():
            return
        self._start_unmute_listener()

    def _start_unmute_listener(self) -> None:
        if vosk is None or getattr(self, "_vosk_model", None) is None:
            print("[MIC-MUTE] No Vosk model — voice unmute unavailable")
            return
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return
        self._stop_unmute_listener()
        queue: asyncio.Queue = asyncio.Queue()
        self._unmute_queue = queue
        # Era is about to say "Microphone off." — don't decode her own voice.
        self._unmute_ignore_until = time.time() + 1.0

        def _spawn() -> None:
            if self._unmute_queue is queue and self._mic_muted:
                self._unmute_task = loop.create_task(self._run_unmute_listener(queue))

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _spawn()
        else:
            loop.call_soon_threadsafe(_spawn)

    def _stop_unmute_listener(self) -> None:
        task = self._unmute_task
        self._unmute_task = None
        self._unmute_queue = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not None and not task.done() and task is not current:
            task.cancel()

    async def _run_unmute_listener(self, queue: asyncio.Queue) -> None:
        print("[MIC-MUTE] Local unmute listener started")
        rec = vosk.KaldiRecognizer(self._vosk_model, 16000, unmute_grammar())
        try:
            rec.SetWords(True)
        except Exception:
            pass
        tap = WakeAudioTap()
        verifier = getattr(self, "_unmute_verifier", None)
        try:
            while self._mic_muted and self._unmute_queue is queue:
                data = await queue.get()
                if self._is_speaking or time.time() < self._unmute_ignore_until:
                    continue
                tap.append(data)
                if not rec.AcceptWaveform(data):
                    continue
                result = json.loads(rec.Result())
                text = (result.get("text") or "").strip()
                if not text:
                    continue
                confidence = vosk_confidence(result)
                if not match_unmute_final(text, confidence):
                    if "[unk]" not in text and ("listening" in text or "mic" in text):
                        print(f"[MIC-MUTE] Not an unmute order (text={text!r}, conf={confidence})")
                    continue
                if verifier is None:
                    from core.speaker_recognition import get_verifier

                    verifier = self._unmute_verifier = WakeVerifier(get_verifier())
                verdict = await asyncio.to_thread(verifier.check, tap, result)
                if not verdict.accepted:
                    print(f"[MIC-MUTE] Unmute from unverified voice ignored ({verdict.reason})")
                    continue
                print(f"[MIC-MUTE] Unmute phrase {text!r} (conf={confidence}, {verdict.reason})")
                self.unmute_microphone("voice")
                break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[MIC-MUTE] Unmute listener error: {e}")
            self.ui.write_log(
                "WARN: Voice unmute stopped working — use the mic button or type 'start listening'."
            )
        finally:
            current = asyncio.current_task()
            if self._unmute_task is current:
                self._unmute_task = None
            if self._unmute_queue is queue and not self._mic_muted:
                self._unmute_queue = None
            print("[MIC-MUTE] Local unmute listener stopped")
