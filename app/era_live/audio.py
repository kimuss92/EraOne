"""Audio I/O loops for EraLive (extracted from main.py).

Mixin: microphone capture -> Gemini Live, and Live audio -> speakers, plus the
drop logger. Operates purely on self.* state defined by EraLive.__init__.
"""

import asyncio
import time
import traceback

import numpy as np
import sounddevice as sd
from google.genai import types

from app.audio_config import CHANNELS, CHUNK_SIZE, RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE
from app.era_live.audio_output import ResilientOutputStream
from app.era_live.claude_voice import claude_voice_order
from app.hud.events import hud
from app.notify import _tg_notify
from app.text_intents import (
    _clean_stream_chunk,
    _clean_transcript,
    _model_intent,
    _theme_intent_mode,
    build_resume_note,
    build_resume_offer_note,
    is_backchannel,
    is_drop_request,
    is_resume_request,
)
from core.logger import log_crash, log_event, main_logger
from core.mic_mute import is_mic_off_order
from core.vad import fade_out_pcm
from core.voice_mode import MODE_GEMINI_AUDIO, MODE_KOKORO_AUDIO, VOICE_CFG

# Cap on held Gemini audio per turn (~43ms per chunk at 24kHz => ~40s of speech).
CLOUD_AUDIO_BUFFER_MAX_CHUNKS = 900

# How long to stay on the Gemini fallback before retrying the primary backend.
TTS_PRIMARY_RETRY_S = 300.0


def _backend_ready(mode: str) -> bool:
    """Can the voice we would restore TO speak right now?"""
    if mode == MODE_KOKORO_AUDIO:
        from core.tts_kokoro import KOKORO

        if not KOKORO.ready:
            KOKORO.warm_in_background()
        return KOKORO.ready
    from core.tts_fish import FISH, is_configured

    return is_configured() and not FISH.in_cooldown()


# Re-enumeration reconnect for a pinned voice device that connected AFTER Era
# started (invisible to PortAudio's frozen device list). How often to check while
# playing, the minimum gap between reconnect requests (so a flapping or genuinely
# absent device can't loop reconnects), and a deadline far enough out that ONLY
# the idle-between-turns path takes it — never a mid-turn cut.
VOICE_REENUM_CHECK_INTERVAL_S = 3.0
VOICE_REENUM_MIN_INTERVAL_S = 60.0
VOICE_REENUM_IDLE_GRACE_S = 3600.0


# Latency marks: safe from bare test hosts, lazily creating the tracker.
def _latency_mark(host, name: str, *args) -> None:
    """Safe one-shot mark on the host's turn-latency tracker.

    Module-level on purpose: tests borrow methods like `_play_audio` onto
    bare objects that have no `AudioIOMixin` base, so an `_latency_mark(self, ...)`
    attribute call would AttributeError inside those borrowed bodies. The
    tracker sits in the audio receive loop: instrumentation that can throw
    is instrumentation that takes the voice down with it. Created lazily so
    hosts that never set `turn_latency` are unaffected rather than broken.
    """
    tl = getattr(host, "turn_latency", None)
    if tl is None:
        from core.turn_latency import TurnLatency

        tl = host.turn_latency = TurnLatency()
    try:
        getattr(tl, name)(*args)
    except Exception:
        pass


class AudioIOMixin:
    def _log_turn_latency(self) -> None:
        """Emit this turn's latency breakdown as one log line, then always reset.

        Board item 22 ("measure first"): a "she responds too late" complaint is
        only actionable once it is split into endpointing wait, model think
        time and playback. The RESET is the load-bearing half — a turn that
        logs nothing (typed input, session wake, the off switch) must still
        clear its marks, or the next turn reports a latency spanning both.
        """
        from core.turn_latency import enabled
        from core.vad import get_endpointing_config

        try:
            tl = getattr(self, "turn_latency", None)
            if tl is None:
                return  # no marks were ever taken: there is no turn to report
            silence_ms = None
            try:
                cfg = get_endpointing_config()
                if cfg.get("enabled"):
                    silence_ms = int(cfg.get("silence_ms") or 0) or None
            except Exception:
                silence_ms = None  # no config is not a guess: report none
            summary = tl.summary(silence_ms=silence_ms)
            if enabled() and summary is not None:
                log_event(main_logger, "turn.latency", "INFO", **summary)
        except Exception as e:  # noqa: BLE001 — diagnostics are never load-bearing
            print(f"[era] [WARN] turn latency log skipped: {e}")
        finally:
            try:
                self.turn_latency.reset()
            except Exception:
                pass

    def _log_audio_drop(self, reason: str) -> None:
        """Print an audio-drop notice at most once every 5s per reason. Without
        this, a muted or mic-off state floods the console with one line per audio
        chunk. Keeps the signal ("mic is off") without the noise."""
        now = time.time()
        if now - self._audio_drop_log_ts.get(reason, 0.0) >= 5.0:
            self._audio_drop_log_ts[reason] = now
            print(f"[SEND] Dropping audio: {reason}")

    # -- Gemini fallback buffer (primary/fallback TTS) ------------------------
    def _buffer_cloud_audio(self, chunk: bytes) -> None:
        """Hold a chunk of Gemini's own audio in case the primary TTS fails.

        Bounded, because a runaway turn must not grow without limit.
        """
        if not chunk:
            return
        buf = getattr(self, "_cloud_audio_buffer", None)
        if buf is None:
            buf = self._cloud_audio_buffer = []
        if len(buf) < CLOUD_AUDIO_BUFFER_MAX_CHUNKS:
            buf.append(chunk)

    def _clear_cloud_audio_buffer(self) -> None:
        buf = getattr(self, "_cloud_audio_buffer", None)
        if buf:
            buf.clear()

    def _prewarm_fish(self) -> None:
        """Wake Fish while the user is still talking (fish_audio mode only).

        Fire-and-forget on the running loop; FISH.prewarm() is rate-limited,
        skips itself when Fish is already warm, and never raises.
        """
        try:
            if not VOICE_CFG.is_fish():
                return
            from core.tts_fish import FISH

            self._fish_prewarm_task = asyncio.get_running_loop().create_task(FISH.prewarm())
        except Exception:  # noqa: BLE001 - an optimisation must never touch the turn
            pass

    def _take_cloud_audio_buffer(self) -> list[bytes]:
        """Hand over this turn's held Gemini audio and start the next turn empty."""
        buf = list(getattr(self, "_cloud_audio_buffer", None) or [])
        self._clear_cloud_audio_buffer()
        return buf

    def _tts_fallback(self, reason: str = "", held_audio: list[bytes] | None = None) -> None:
        """PRIMARY TTS failed -> finish this turn with Gemini's own audio.

        Called by SpeechOut when the Fish backend returns nothing. Three steps,
        in this order:

          1. flush the audio Gemini already produced for THIS turn, so the reply
             the user is waiting on still arrives (uninterrupted service);
          2. flip to gemini_audio. Both are AUDIO sessions, so
             needs_reconnect_to() is False and the switch is instant -- no
             reconnect, no dropped conversation;
          3. remember to retry the primary later, so one bad minute does not
             cost Fish for the rest of the session.

        Degrade, never raise: this is the last line of defence on the audio
        path, so every step is guarded.
        """
        try:
            if VOICE_CFG.is_gemini():
                return  # already fallen back
            # held_audio: the failing sentence's turn already completed on Gemini's
            # side, so SpeechOut kept its audio. Anything in the live buffer belongs
            # to a later turn and plays after it, in order.
            buffered = list(held_audio or []) + self._take_cloud_audio_buffer()
            if self.audio_in_queue is not None:
                for chunk in buffered:
                    self.audio_in_queue.put_nowait(chunk)
            previous = VOICE_CFG.mode
            # TRANSIENT: move the active voice to Gemini WITHOUT persisting. The
            # user's saved preference stays exactly as they set it, so a crash
            # while fallen back never "remembers" Gemini as a deliberate choice.
            VOICE_CFG.set_mode(MODE_GEMINI_AUDIO, persist=False)
            self._tts_primary_retry_at = time.time() + TTS_PRIMARY_RETRY_S
            self._tts_primary_mode = previous
            print(
                f"[TTS] PRIMARY failed ({reason or 'unknown'}); fell back to Gemini "
                f"and replayed {len(buffered)} buffered chunk(s). "
                f"Retrying {previous} in {int(TTS_PRIMARY_RETRY_S)}s."
            )
            log_event(
                main_logger,
                "voice.fallback",
                "WARNING",
                from_backend=previous,
                to_backend=MODE_GEMINI_AUDIO,
                reason=reason or "unknown",
                replayed_chunks=len(buffered),
            )
            try:
                self.ui.write_log("SYS: Primary voice unavailable - using my Gemini voice.")
            except Exception:
                pass
        except Exception as e:
            print(f"[TTS] fallback handler error: {e}")

    def _maybe_restore_primary_tts(self) -> None:
        """Return to the user's preferred backend once the retry window elapses.

        The target is the SAVED preference, not the mode we happened to be on
        before the fallback: if the user deliberately switched to Gemini while
        Era was already fallen back, that becomes the preference and there is
        nothing to restore. The restore is transient too (persist=False) — it
        only moves the active mode back onto the preference the file already
        holds, so it never rewrites config/voice.json.
        """
        try:
            retry_at = getattr(self, "_tts_primary_retry_at", 0.0)
            if not retry_at or time.time() < retry_at:
                return
            if not VOICE_CFG.is_gemini():
                self._tts_primary_retry_at = 0.0
                return
            target = VOICE_CFG.preferred
            if target == VOICE_CFG.mode:
                # The preference already matches the active voice (the user chose
                # Gemini during the outage) — nothing to restore.
                self._tts_primary_retry_at = 0.0
                self._tts_primary_mode = ""
                return
            if not _backend_ready(target):  # check the voice we return TO, not Fish
                self._tts_primary_retry_at = time.time() + TTS_PRIMARY_RETRY_S
                return
            VOICE_CFG.set_mode(target, persist=False)
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""
            print(f"[TTS] primary backend recovered; back on {target}.")
        except Exception as e:
            print(f"[TTS] primary restore check failed: {e}")

    def trigger_barge_in(self, source: str = "voice") -> None:
        """Stop Era's voice output immediately because the user started talking.

        Scheduled onto the event loop from the mic callback, so it runs on the
        loop thread and may safely touch the UI, session and audio queue. It is
        the single "cut the output now" action:

          * drops everything queued and orphans in-flight local synthesis
            (``_flush_audio_in_queue`` → ``speech_out.interrupt`` + drain);
          * sets ``_barge_in_requested`` so ``_receive_audio`` stops enqueuing
            the rest of a Gemini turn that is still streaming server-side;
          * ends the turn and flips to LISTENING, which also re-enables normal
            mic capture so the user's continuing words become the next turn.

        No-op unless Era is actually speaking, so a late or duplicate schedule
        cannot wedge her out of a turn she already finished.
        """
        with self._speaking_lock:
            if not self._is_speaking:
                return
        self._barge_in_requested = True
        hud.barge_in(source)
        _latency_mark(self, "note_interrupted")  # this turn's numbers describe the cut
        dropped = self._flush_audio_in_queue()
        if self._turn_done_event is not None:
            # Let _play_audio settle cleanly out of the (now empty) turn.
            self._turn_done_event.set()
        # Phase 2: stash the unfinished thought so Era can resume it. Skipped
        # during a CM hand-off — that speech belongs to the headless CM lane, not
        # to a thread the user would want continued. Best-effort; never fatal.
        try:
            if not self._cm_handoff_active():
                spoken = (getattr(self, "_live_turn_text", "") or "").strip()
                try:
                    remainder = self.speech_out.pending_text()
                except Exception:
                    remainder = ""
                self.turn_memory.capture(spoken=spoken, remainder=remainder)
        except Exception:
            pass
        # Silent acknowledgment: Era yields the floor by ACTION, not words. The
        # state flip to LISTENING (below) is the cue — sphere settles, mic opens.
        # No spoken/transcript "Interrupted" line: announcing the interruption is
        # exactly the robotic tell we are removing. _play_audio ducks the tail so
        # the stop is a soft hand-off rather than a mid-word cut.
        self.set_speaking(False)
        print(f"[BARGE-IN] User interrupted; dropped {dropped} pending audio frame(s).")

    async def _send_realtime(self):
        while True:
            if self._go_away_event.is_set():
                break
            # Timeout-guarded get so a GoAway teardown is observed even when
            # the mic is idle and no audio is flowing through the queue.
            try:
                msg = await asyncio.wait_for(self.out_queue.get(), timeout=0.25)
            except TimeoutError:
                continue

            # ── Audio-path Safety Gate: drop audio when state forbids it ──
            # Defense-in-depth: _listen_audio already refuses to enqueue audio
            # when muted / mic-off, but we re-check the authoritative flags HERE
            # too. This is what closes the "status shows mic Off but Era still
            # hears you" bug — if the UI toggle and the callback ever desync, a
            # frame that slipped into the queue is still dropped before it can
            # reach Gemini. The mic is never live unless BOTH gates agree.
            sm = self.session_manager.get_state_snapshot()
            drop_reason = None
            if sm.get("is_muted", False):
                drop_reason = "muted"
            elif getattr(self, "_mic_muted", False):
                drop_reason = "mic muted by voice"
            elif not self._browser_mic_active:
                drop_reason = "mic off"
            elif sm.get("is_reconnecting", False):
                drop_reason = "reconnecting"
            elif sm.get("standby_active", False) and not sm.get("wake_word_active", False):
                drop_reason = "standby without wake word"
            if drop_reason is not None:
                self._log_audio_drop(drop_reason)
                continue
            # ──────────────────────────────────────────────────────────────

            # ── Speaker gate: only the enrolled owner's voice leaves the machine ──
            # This is the single mic → Gemini path, so it is the enforcement point:
            # Gemini acts on whatever it hears, and a transcript check would be too
            # late. Strangers' speech is replaced by silence; with no enrolled
            # voice/model the gate is fail-closed. See core/voice_gate.py.
            try:
                frames = self._speaker_gate().feed(msg["data"])
            except Exception as gate_err:  # fail closed, never kill the session
                self._log_audio_drop(f"speaker gate unavailable: {gate_err}")
                continue
            # ── Attention gate: only speech meant for Era (core/attention.py) ──
            # After the voiceprint check: past the conversation window, an
            # utterance must start with her name; a phone call or room chat is
            # replaced by silence. Fail-open by design.
            gate = self._attention_gate()
            keep_open = self._attention_keep_open()
            for frame in frames:
                for out in gate.feed(frame, keep_open=keep_open):
                    self._note_sent_voice(out)
                    blob = types.Blob(data=out, mime_type=msg["mime_type"])
                    await self.session.send_realtime_input(audio=blob)

    def _note_sent_voice(self, frame: bytes) -> None:
        """Evidence for core/live_health.py that the user spoke into a listening
        session: ONLY audio actually sent counts, so it is measured after the voice
        and attention gates. Measured on the raw mic (before 2026-10-10), speech the
        gates silenced made a healthy Gemini look mute and triggered the Claude
        rescue ("heard the user, no answer for 25s")."""
        if not hasattr(self, "live_health") or len(frame) < 2:
            return
        try:
            pcm = np.frombuffer(frame[: len(frame) // 2 * 2], dtype="<i2").astype(np.float32)
            self.live_health.note_voice(
                float(np.sqrt(np.mean(np.square(pcm)))) / 32768.0,
                (len(pcm)) / SEND_SAMPLE_RATE,
            )
        except Exception:
            pass

    # ── Listening rules (core/attention.py): echo hold + attention window ──
    def _listening_state(self):
        state = getattr(self, "_listening", None)
        if state is None:
            from core.attention import ListeningState
            from core.media_activity import media_playing

            state = self._listening = ListeningState(media_active=media_playing)
        return state

    def _attention_gate(self):
        gate = getattr(self, "_attention", None)
        if gate is None:
            from core.attention import AttentionGate, _names

            factory = None
            model = getattr(self, "_vosk_model", None)
            if model is not None:
                try:
                    import json as _json

                    import vosk

                    from app.wake import WAKE_CFG

                    vocab = list(_json.loads(WAKE_CFG.grammar))
                    vocab += [n for n in sorted(_names()) if n not in vocab]
                    grammar = _json.dumps(vocab)

                    def factory():
                        rec = vosk.KaldiRecognizer(model, 16000, grammar)
                        rec.SetWords(True)  # per-word confidence for the name
                        return rec

                except Exception as e:  # no Vosk: the gate passes everything
                    print(f"[ATTENTION] name check unavailable ({type(e).__name__}); fail-open")
            gate = self._attention = AttentionGate(
                self._listening_state(), factory, on_event=self._attention_event
            )
        return gate

    def _attention_keep_open(self) -> bool:
        """Era stays listening while a confirmation waits for the user's answer,
        and while she is busy with his request: thinking, running a tool or
        replying locally. The window counts from when she is DONE (live
        2026-10-10: a slow reply let it expire before she even answered)."""
        try:
            store = getattr(self, "_pending_store", None)
            if store is not None and store.peek() is not None:
                return True
            from core.media_activity import media_playing

            if media_playing():
                return False  # music/video: "busy" must not let lyrics through
            snap = self.session_manager.get_state_snapshot()
            if str(snap.get("current_state", "")).upper().endswith("PROCESSING"):
                return True
            task = getattr(self, "_local_turn_task", None)
            if task is not None and not task.done():
                return True
            return bool(getattr(self, "_local_speaking", False))
        except Exception:
            return True  # unsure: listen (fail-open, like the gate)

    def _attention_event(self, kind: str, heard: str) -> None:
        print(f"[ATTENTION] {kind}: {heard!r}")
        try:
            log_event(main_logger, f"attention.{kind}", "INFO", heard=heard[:80])
        except Exception:
            pass
        if kind == "ignored" and not getattr(self, "_attention_hint_shown", False):
            self._attention_hint_shown = True  # once per quiet spell, not per phrase
            try:
                self.ui.write_log("SYS: Not addressed to me - start with my name to talk to me.")
            except Exception:
                pass
        elif kind == "accepted":
            self._attention_hint_shown = False

    def _speaker_gate(self):
        """Lazily built so EraLive.__init__ stays untouched (same as tool_batch)."""
        gate = getattr(self, "_voice_gate", None)
        if gate is None:
            from core.speaker_recognition import get_verifier
            from core.voice_gate import (
                SHORT_REPLY_HINT,
                UtteranceGate,
                pending_followup_allowed,
                short_reply_hint_due,
            )

            def _report(msg: str) -> None:
                print(f"[VOICE-GATE] {msg}")
                # Only the "Era is deaf until you enroll" notice goes to the UI;
                # a TV or a guest being refused is routine and would spam the log.
                note = f"SYS: {msg}" if "BLOCKED" in msg else ""
                # A short reply dropped while a confirmation that needs a verifiable
                # answer waits must not be silent: say how to answer instead.
                if msg.startswith("short utterance ignored"):
                    try:
                        store = getattr(self, "_pending_store", None)
                        p = store.peek() if store is not None else None
                    except Exception:
                        p = None  # the gate already refused (fail closed); no hint
                    now = time.monotonic()
                    if short_reply_hint_due(p, getattr(self, "_short_reply_hint", None), now):
                        self._short_reply_hint = (p.confirmation_id, now)
                        note = f"SYS: {SHORT_REPLY_HINT}"
                if note:
                    try:
                        self.ui.write_log(note)
                    except Exception:
                        pass

            def _followup_allowed() -> bool:
                # Short replies may answer only allowlisted, bounded confirmations;
                # a store error refuses them (core/voice_gate.pending_followup_allowed).
                return pending_followup_allowed(getattr(self, "_pending_store", None))

            gate = self._voice_gate = UtteranceGate(
                get_verifier(), on_event=_report, followup_allowed=_followup_allowed
            )
        return gate

    async def _listen_audio(self):
        print("[era] [MIC] Mic started")
        loop = asyncio.get_running_loop()
        # Capture THIS connection's queues. A late PortAudio callback must not
        # put old speech into a replacement session's queue.
        outgoing = self.out_queue

        def enqueue(queue, item):
            if queue is not None and not queue.full():
                queue.put_nowait(item)

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                era_speaking = self._is_speaking

            # HUD mic level: the real RMS of this block (~15 Hz, one per block),
            # only while the mic is genuinely open — a hard-off or voice-muted
            # mic shows no level, because nothing it hears goes anywhere.
            if self._browser_mic_active and not getattr(self, "_mic_muted", False):
                try:
                    rms = float(np.sqrt(np.mean(np.square(indata, dtype=np.float32))))
                    hud.voice_level(rms / 32768.0)
                except Exception:
                    pass

            # ----- Handle standby mode: route mic to Vosk or drop -----
            if self._standby_active:
                # Respect an explicit mic-off toggle even in standby. If
                # the user turned the mic OFF, Era must be fully deaf — no
                # wake detection either. Without this gate a "mic Off"
                # state still fed Vosk, so ambient noise could false-wake
                # and resurrect Era (the "mic is off but it hears me" bug).
                if not self._browser_mic_active:
                    return
                if self._wake_queue is not None:
                    data = indata.tobytes()
                    try:
                        loop.call_soon_threadsafe(enqueue, self._wake_queue, data)
                    except RuntimeError:
                        pass  # event loop already shutting down
                else:
                    now_ts = time.time()
                    if now_ts - self._last_wake_queue_missing_log_ts > 5.0:
                        self._last_wake_queue_missing_log_ts = now_ts
                        print("[MIC] Standby active but wake_queue is None — dropping wake audio")
                return
            # -----------------------------------------------------------

            # ----- Voice mic-mute ("stop listening"): local unmute only -----
            # Nothing goes to Gemini. Audio feeds ONLY the offline Vosk unmute
            # listener — and not at all under the hard mic-off toggle, nor while
            # Era speaks (her own "Microphone off." must not be decoded, and a
            # muted mic must never barge in). See core/mic_mute.py.
            if getattr(self, "_mic_muted", False):
                if self._browser_mic_active and not era_speaking:
                    self._feed_unmute_audio(loop, indata.tobytes())
                return
            # -----------------------------------------------------------

            # ----- Barge-in: keep listening WHILE Era speaks -----
            # The mic is otherwise half-duplex (audio dropped while speaking), so
            # this branch is what lets the user cut Era off mid-sentence. Frames
            # go to the barge-in detector instead of Gemini; a positive result
            # stops Era's voice at once (Copilot-style). Respects mute/mic-off so
            # a silenced mic never interrupts, and self-suppresses on echo (see
            # BargeInDetector). When speaking, we never forward audio to Gemini
            # here — a stopped turn resumes normal capture the moment
            # set_speaking(False) flips era_speaking below.
            if era_speaking:
                det = getattr(self, "_barge_in", None)
                if (
                    det is not None
                    and det.enabled
                    and self._browser_mic_active
                    and not self.ui.muted
                ):
                    try:
                        if det.feed(indata.tobytes()):
                            loop.call_soon_threadsafe(self.trigger_barge_in)
                    except Exception:
                        pass
                return
            # -----------------------------------------------------

            # Echo hold: Era's last words are still leaving the speakers (device
            # buffer, Bluetooth, room reverb) right after she stops; Gemini used to
            # hear them and answer herself. See core/attention.py.
            if self._listening_state().echo_hold_active():
                return

            # Normal operation: send audio to Gemini only if not muted
            if not self.ui.muted and self._browser_mic_active:
                data = indata.tobytes()
                try:
                    # Thread-safe try-push without exploding if the loop lags
                    loop.call_soon_threadsafe(
                        enqueue,
                        outgoing,
                        {"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"},
                    )
                except Exception:
                    pass

        retry_delay = 1.0
        while not self._go_away_event.is_set() and not self._shutdown_requested:
            try:
                with sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    callback=callback,
                ) as stream:
                    print("[era] [MIC] Mic stream open")
                    retry_delay = 1.0
                    while not self._go_away_event.is_set() and not self._shutdown_requested:
                        if getattr(stream, "active", True) is False:
                            raise sd.PortAudioError("Microphone stream became inactive")
                        await asyncio.sleep(0.1)
            except (sd.PortAudioError, OSError) as e:
                # A missing/unplugged mic is local. Keep the Live connection and
                # typed commands working while retrying just the input device.
                print(f"[era] [MIC] Device unavailable; retrying in {retry_delay:.0f}s: {e}")
                self.ui.write_log(
                    "SYS: Microphone unavailable; typed commands still work. Retrying audio input."
                )
                try:
                    await asyncio.wait_for(self._go_away_event.wait(), timeout=retry_delay)
                except TimeoutError:
                    pass
                retry_delay = min(retry_delay * 2.0, 8.0)

    async def _receive_audio(self):
        print("[era] [RECV] Recv started")
        out_buf, in_buf = [], []
        # A partial utterance left by a dropped session must not linger as "what the
        # user is saying now" — the approval gates read it (tool_dispatch).
        self._inflight_user_text = ""

        try:
            while True:
                received = False
                async for response in self.session.receive():
                    received = True
                    # Any message from the Live session (audio, transcript,
                    # tool call, ...) is genuine activity. Without this, a
                    # voice-only session never touches
                    # _last_interaction_timestamp (that's only bumped by
                    # typed text and standby-resume), so the 300s idle
                    # timer keeps counting from session start and can
                    # force standby mid-conversation or mid-tool-call.
                    self._last_interaction_timestamp = time.time()

                    # ── Handle GoAway: PLAN the renewal, don't panic into it ──
                    # The server is telling us it will close and how long we
                    # have. Tearing down immediately cuts the answer mid-word
                    # for no reason when there is grace left; the kindest moment
                    # is between turns. So record the deadline and take it at
                    # the first idle point, or when the grace runs out.
                    if self.session_manager.handle_go_away(response):
                        secs = self._go_away_seconds(self.session_manager.go_away_time_left)
                        self._renewal_deadline = time.time() + (secs if secs is not None else 0.0)
                        log_event(
                            main_logger,
                            "engine.session.go_away",
                            "INFO",
                            time_left_s=secs,
                            has_fresh_checkpoint=self.session_manager.has_fresh_checkpoint(),
                            checkpoint_stale=self.session_manager.checkpoint_is_stale,
                            conn_generation=getattr(self, "_conn_generation", 0),
                            conn_age_s=round(
                                time.time() - (getattr(self, "_conn_started_at", 0) or time.time()),
                                1,
                            ),
                        )
                        print(f"[RECV] GoAway received; {secs}s of grace. Planning renewal.")
                        self.session_manager.prepare_reconnect()

                    # A pending renewal is re-evaluated on every server message,
                    # so the turn that is in flight right now gets to finish if
                    # the grace period allows it.
                    if getattr(self, "_renewal_deadline", None) is not None:
                        take, why = self._should_take_renewal_now(
                            tool_running=self._tool_is_running()
                        )
                        if take:
                            log_event(
                                main_logger,
                                "engine.session.renewing",
                                "INFO",
                                reason=why,
                                conn_generation=getattr(self, "_conn_generation", 0),
                            )
                            print(f"[RECV] Taking the planned renewal: {why}")
                            self._renewal_deadline = None
                            self._go_away_event.set()
                            break

                    # ── Capture session resumption token ──
                    if self.session_manager.capture_token(response):
                        # Persist immediately so a crash/restart can resume from
                        # the freshest handle we have.
                        self.session_manager.save_to_disk()
                        now = time.time()
                        if (now - self._last_token_log_ts) >= self._token_log_interval:
                            print("[RECV] Session resumption token updated.")
                            self._last_token_log_ts = now

                    if response.data and self._barge_in_requested:
                        # The user cut in: this is the tail of the aborted turn,
                        # still streaming from the server. Drop it rather than
                        # refilling the queue trigger_barge_in just flushed.
                        pass
                    elif response.data:
                        # Bytes RECEIVED. Counted apart from bytes played (see
                        # _play_audio) because only the second proves the user
                        # heard anything — core/live_turn_state.py explains why
                        # that distinction is the whole recovery decision.
                        self.live_turn.note_audio_received(len(response.data))
                        _latency_mark(self, "note_model_output")  # first answer byte
                        if hasattr(self, "live_health"):
                            self.live_health.note_reply()  # it answered: not "mute"
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        # Swallow Gemini's audio while a CM hand-off window is
                        # open: the utterance belongs to the headless CM lane, so
                        # Gemini's parallel spoken reaction to it (a hallucinated
                        # blocker) must never reach the speakers. The CM lane
                        # speaks its own result later, well past this window.
                        #
                        # fish_audio holds it back for a different
                        # reason: the session is AUDIO (the only modality these
                        # models support), but Kokoro/Fish is the voice, so
                        # playing the cloud audio would speak every reply TWICE,
                        # in two different voices. The transcription branch below
                        # is what reaches the speakers in those modes.
                        #
                        # HELD, NOT DISCARDED: this is Era's Gemini FALLBACK. If
                        # the primary backend cannot synthesize, _tts_fallback
                        # flushes this buffer so the user hears the reply in
                        # Gemini's voice instead of hearing nothing at all.
                        if self._cm_handoff_active():
                            pass
                        elif VOICE_CFG.synthesizes_from_transcript():
                            self._buffer_cloud_audio(response.data)
                        else:
                            self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content
                        if getattr(sc, "grounding_metadata", None):
                            hud.grounding(sc.grounding_metadata)

                        # Native barge-in: Gemini itself heard the user talk over
                        # the model and stopped generating. Flush whatever already
                        # reached the queue and latch the same gate the local
                        # detector uses, so the aborted turn stops playing here too.
                        if getattr(sc, "interrupted", False) and not self._barge_in_requested:
                            # The USER cut in — a deliberate act, not a fault.
                            # Recorded so the reconnect never "finishes" a turn
                            # they chose to stop.
                            self.live_turn.note_interrupted()
                            _latency_mark(self, "note_interrupted")  # excluded from averages
                            self._barge_in_requested = True
                            hud.barge_in("model")
                            self._flush_audio_in_queue()

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                out_buf.append(txt)
                                self._live_turn_text = " ".join(out_buf)  # for barge-in resume
                                self.live_turn.note_text(txt)
                                _latency_mark(self, "note_model_output")  # first answer token
                                if hasattr(self, "live_health"):
                                    self.live_health.note_reply()  # a spoken transcript is an answer
                                hud.response_delta(txt)
                                # ── fish_audio: the TRANSCRIPT is the speech ──
                                # Gemini's own audio was discarded above, so this
                                # text is what Era actually says. It streams
                                # incrementally (measured: 26 chunks, first full
                                # sentence at 1.79s), which is what lets
                                # SpeechOut pipeline sentence-by-sentence instead
                                # of waiting for the whole reply.
                                #
                                # Fed with the RAW-cleaned text rather than the
                                # joined buffer because feed() keeps its own
                                # partial-sentence remainder; re-sending the
                                # accumulated buffer would repeat every sentence.
                                if (
                                    VOICE_CFG.synthesizes_from_transcript()
                                    and not self._cm_handoff_active()
                                    and not self._barge_in_requested
                                ):
                                    # The UN-stripped chunk: its edge spaces are
                                    # the word breaks (see _clean_stream_chunk).
                                    self.speech_out.feed(
                                        _clean_stream_chunk(sc.output_transcription.text)
                                    )
                            elif (
                                VOICE_CFG.synthesizes_from_transcript()
                                and not self._cm_handoff_active()
                                and not self._barge_in_requested
                            ):
                                # A whitespace-only chunk is still a word break.
                                self.speech_out.feed(" ")

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                # The user is talking. Their input outranks any
                                # continuation we were about to send: a recovered
                                # answer must never be spoken over a live human.
                                self.live_turn.note_user_speaking(True)
                                if hasattr(self, "live_health"):
                                    self.live_health.note_heard()  # Gemini can hear: not "deaf"
                                self._user_spoke_since_drop = True
                                _latency_mark(self, "note_user_delta")
                                if not in_buf:
                                    # When the user STARTED saying it: a gate asking
                                    # "said after the task parked?" must not see a
                                    # sentence begun before it as said "now".
                                    self._inflight_started_at = time.time()
                                    self._prewarm_fish()
                                in_buf.append(txt)
                                self._last_user_text = txt
                                self._inflight_user_text = " ".join(in_buf)
                                hud.transcript_partial(self._inflight_user_text)
                                # ── "abort mission", MID-TURN ──
                                # Checked here, on the partial transcript,
                                # rather than only at turn_complete below. A
                                # runaway loop is exactly the case where the
                                # turn does NOT end promptly — and the five
                                # gmail sends of 2026-09-21 went out inside
                                # 77ms, far faster than a turn boundary. Acting
                                # the moment the words land is what lets the
                                # abort refuse calls the model has already
                                # queued. Idempotent, so the turn_complete
                                # backstop below is free to fire too.
                                if self._is_abort_order(self._inflight_user_text):
                                    self.trigger_abort("voice", self._inflight_user_text)

                        # Gemini's own end-of-speech mark on the input transcript:
                        # the user stopped talking, the answer is now pending.
                        if (
                            sc.input_transcription
                            and getattr(sc.input_transcription, "finished", False)
                            and in_buf
                        ):
                            hud.user_speech_finished(" ".join(in_buf))
                            _latency_mark(self, "note_user_finished")

                        # ── The model finished the ANSWER ──
                        # Distinct from turn_complete, which also fires when the
                        # user interrupts. Per https://ai.google.dev/api/live an
                        # interrupted turn goes "interrupted > turn_complete"
                        # with NO generation_complete, so this flag is what tells
                        # a finished answer apart from a severed one — and
                        # therefore whether a reconnect owes the user a
                        # continuation or merely the rest of the buffered audio.
                        if getattr(sc, "generation_complete", False):
                            self.live_turn.note_generation_complete()

                        if sc.turn_complete:
                            if hasattr(self, "live_health"):
                                self.live_health.note_reply()  # the turn ended: it responded
                            # A turn boundary always clears the barge-in latch, so
                            # a cut-off turn cannot leave Era's voice gated off for
                            # the next reply.
                            self._barge_in_requested = False
                            if VOICE_CFG.uses_speech_out():
                                # Gemini has finished TALKING, but Era has not
                                # finished SPEAKING — sentences are still being
                                # synthesized. Setting _turn_done_event here
                                # would let _play_audio end the turn during the
                                # gap between two sentences and drop Era to
                                # LISTENING mid-reply. The streamer sets it
                                # once the last sentence is on the queue.
                                #
                                # The held Gemini audio goes WITH the turn: Fish
                                # is still speaking it, so a late Fish failure
                                # must still find its fallback (2026-10-07: it
                                # was cleared here and the reply went silent).
                                self.speech_out.end_turn(held_audio=self._take_cloud_audio_buffer())
                            elif self._turn_done_event:
                                self._turn_done_event.set()

                            full_in = " ".join(in_buf).strip()
                            self._inflight_user_text = ""
                            if full_in:
                                self._note_user_utterance(
                                    full_in, said_at=getattr(self, "_inflight_started_at", None)
                                )
                                self.ui.write_log(f"You: {full_in}")
                                hud.transcript_final(full_in)
                                # "abort mission" — backstop for the mid-turn
                                # check above, which sees a growing partial and
                                # can miss a phrase split across two chunks.
                                # No-op when the mid-turn check already tripped
                                # the latch. Placed ahead of the reboot order:
                                # an abort must never be escalated to a restart.
                                if self._is_abort_order(full_in):
                                    self.trigger_abort("voice", full_in)
                                # "reboot Era One" — acted on from the transcript,
                                # not left to the model, which may have heard
                                # "reboot" as the computer. No-op if the model's
                                # own shutdown_era(relaunch) already started it.
                                if self._is_reboot_order(full_in) and not self._shutdown_requested:
                                    self._shutdown_era({"relaunch": True, "_source": "voice"})
                                # "mute your mic" / "stop listening" — backstop for
                                # the set_microphone tool. Idempotent: a no-op when
                                # the model's own tool call already muted.
                                if is_mic_off_order(full_in) and not getattr(
                                    self, "_mic_muted", False
                                ):
                                    self.mute_microphone("voice")
                                # "switch to Claude" / "use your backup brain":
                                # deterministic, never left to the model. The
                                # call site was lost in merge 6f0c327.
                                if claude_voice_order(full_in):
                                    self.enter_claude_voice("claude_manual", full_in, manual=True)
                                # ── Contextual resume / drop (Phase 2) ──
                                # Only when a thought is stashed, so a bare
                                # "continue" with nothing pending stays a normal
                                # command. Mirrors the typed intercept in
                                # _on_text_command.
                                if self.turn_memory.has_pending():
                                    if is_drop_request(full_in):
                                        self.turn_memory.clear()
                                        self.ui.write_log("SYS: Dropped the earlier thread, sir.")
                                    elif is_resume_request(full_in) or is_backchannel(full_in):
                                        # A backchannel ("mm", "go on") right after
                                        # cutting Era off means "carry on", not a new
                                        # command — resume rather than sit silent.
                                        turn = self.turn_memory.take()
                                        if turn is not None and self.session is not None:
                                            try:
                                                await self.session.send_realtime_input(
                                                    text=build_resume_note(
                                                        turn.spoken, turn.remainder
                                                    )
                                                )
                                            except Exception as _rz_err:
                                                print(f"[era] [WARN] resume skipped: {_rz_err}")
                                # Voice theme order ("go dark") — the live
                                # model has no theme tool, so act on the
                                # transcript directly, mirroring the typed
                                # intercept in _on_text_command.
                                voice_theme = _theme_intent_mode(full_in)
                                if voice_theme:
                                    _set_theme = getattr(self.ui, "set_theme", None)
                                    if _set_theme:
                                        _set_theme(voice_theme)
                                        self.ui.write_log(
                                            f"SYS: Theme switched to {voice_theme} mode."
                                        )
                                # Shadow: what automatic routing WOULD have
                                # done with this utterance. Voice is where the
                                # real traffic is, so a routing policy argued
                                # from typed input only would be arguing from
                                # the unrepresentative half.
                                try:
                                    self.observe_routing(full_in, handled_by="voice")
                                except Exception as _sh_err:
                                    print(f"[era] [WARN] shadow skipped: {_sh_err}")
                                # Spoken model override ("use Claude", "think
                                # harder"). Mirrors the typed intercept in
                                # _on_text_command — the live model has no
                                # routing tool, so the transcript is the only
                                # place a spoken one can be acted on.
                                spoken_model = _model_intent(full_in)
                                if spoken_model is not None:
                                    try:
                                        self.ui.write_log(
                                            f"SYS: {self.apply_model_override(spoken_model)}"
                                        )
                                    except Exception as _mr_err:
                                        print(f"[era] [WARN] model override skipped: {_mr_err}")
                                # â”€â”€ V9A: Wire feedback detection â”€â”€
                                try:
                                    self._maybe_record_feedback(full_in)
                                except Exception as _fb_err:
                                    print(f"[Memory] [WARN] feedback detection skipped: {_fb_err}")
                            in_buf = []
                            if not full_in:
                                self._last_user_text = ""

                            full_out = " ".join(out_buf).strip()
                            if full_out or full_in:
                                hud.response_completed(full_out)
                            if full_out:
                                self.ui.write_log(f"era: {full_out}")
                                # Did Era just ask something? A short reply typed
                                # after a question is an ANSWER, and the typed
                                # safety gate has no other way to know that — it
                                # sees one message at a time, while this session
                                # remembers the whole exchange. Armed on every
                                # turn, including turns with no Telegram involved:
                                # the command bar has exactly the same blind spot.
                                self._note_era_question(full_out)
                                # Mirror the same line the UI shows back to the
                                # chat, so an order given from another network is
                                # answered there instead of aloud in an empty room.
                                if self._tg_reply_pending:
                                    _tg_notify(full_out)
                            # ── Conversation continuity ──
                            # Keep the exchange so it survives the NEXT session
                            # boundary. Gemini's resumption handle covers the
                            # usual reconnect, but it expires, and after a quota
                            # drop or a relaunch the new session used to start
                            # with no idea what had just been discussed.
                            #
                            # Gated on full_out, and paired with
                            # _recap_pending_user rather than full_in, for two
                            # reasons: a TYPED order has no input transcription
                            # at all (full_in is empty, the text arrived through
                            # _note_user_utterance), and one user request often
                            # spans several turns — a pure tool-call turn then
                            # the spoken reply. Recording per-turn would split
                            # that into two half-exchanges; holding the user
                            # line until Era actually answers keeps it whole.
                            if full_out:
                                try:
                                    self.conversation_log.record(self._recap_pending_user, full_out)
                                    self._recap_pending_user = ""
                                    self.conversation_log.save()
                                except Exception as _cl_err:
                                    print(f"[era] [CONTEXT] recap skipped: {_cl_err}")

                            # Disarm on turn_complete even when the turn produced no
                            # text (a pure tool call, say). Leaving it armed would
                            # mirror whatever Era says next to whoever is in the room.
                            self._tg_reply_pending = False

                            # ── Autonomous resume offer (Phase 2, flagged) ──
                            # After the interrupting exchange is handled, Era may
                            # briefly offer to return to what she was cut off
                            # saying. One-shot (mark_offered) and guarded. The
                            # `startswith` check skips the ABORTED turn itself,
                            # whose full_out is the very text we stashed — we only
                            # offer after a DIFFERENT, completed reply. Off by
                            # default (see get_resume_config); explicit
                            # continue/drop above work regardless.
                            if (
                                self._resume_offer_enabled
                                and full_in
                                and full_out
                                and self.session is not None
                                and not self._cm_handoff_active()
                                and not self._standby_active
                            ):
                                pend = self.turn_memory.peek()
                                if (
                                    pend is not None
                                    and not pend.offered
                                    and not full_out.startswith(pend.spoken[:24])
                                ):
                                    self.turn_memory.mark_offered()
                                    try:
                                        await self.session.send_realtime_input(
                                            text=build_resume_offer_note(pend.spoken)
                                        )
                                    except Exception as _of_err:
                                        print(f"[era] [WARN] resume offer skipped: {_of_err}")

                            self._live_turn_text = ""  # reset per-turn spoken mirror
                            # The turn is closed and its outcome already recorded
                            # (generated / interrupted). Start the next one clean
                            # so a later drop cannot inherit this turn's verdict.
                            self.live_turn.note_turn_complete()
                            self.live_turn.reset()
                            # One latency line per turn — and the reset inside
                            # keeps the NEXT turn's numbers honest.
                            _latency_mark(self, "note_turn_complete")
                            self._log_turn_latency()
                            # Flush any deferred tool failure notices (board item 19)
                            try:
                                self._flush_failure_notices()
                            except Exception:
                                pass
                            # The fallback window for this turn is over; release the
                            # held Gemini audio so it can never play late.
                            self._clear_cloud_audio_buffer()
                            self._maybe_restore_primary_tts()
                            out_buf = []

                    # --- HANDLE TOOL CALLS ---
                    if response.tool_call and response.tool_call.function_calls:
                        if hasattr(self, "live_health"):
                            self.live_health.note_reply()  # a tool call is an answer too
                        # One response per call, always — see ToolBatchMixin.
                        # A gated or turn-ending call now reports that it did
                        # not run rather than going silent, which is what keeps
                        # the batch answerable by a model that requires a
                        # tool_result for every tool_use.
                        fn_responses = await self._run_tool_call_batch(
                            response.tool_call.function_calls
                        )

                        if fn_responses:
                            await self.session.send_tool_response(function_responses=fn_responses)

                        # Instantly drop turn state if entering standby mode —
                        # BUT only after the notification speech has finished.
                        # During the _standby_entering window the notification
                        # TTS audio is still arriving; killing the turn here
                        # would drop it and the user would hear nothing.
                        if self._standby_active and not self._standby_entering:
                            if self._turn_done_event:
                                self._turn_done_event.set()
                            self.set_speaking(False)

                # Exit outer loop on GoAway — lets TaskGroup unwind cleanly
                if self._go_away_event.is_set():
                    break
                if not received:
                    # A closed stream returning immediately otherwise spins at
                    # 100% CPU and never gives the reconnect loop an error.
                    raise ConnectionError("Live receive stream ended without messages")

        except Exception as e:
            # Gemini's routine ~2.5 min Live-session rotation (1008 policy
            # violation / "operation was aborted") surfaces here first, before
            # propagating to run()'s TaskGroup handler. Don't double-log it as
            # a CRITICAL crash — see _is_expected_rotation in run()'s handler,
            # which already logs it once at INFO as engine.session.rotated.
            if self._is_expected_rotation(e):
                print(f"[era] [ROTATE] Recv: session rotated ({e})")
            elif self._is_network_blip(e):
                # The network dropped, not Era. run()'s handler reconnects from
                # the resumption token exactly as it does for a rotation, so
                # logging a CRITICAL engine.crash here was a false alarm — and a
                # loud one: a flaky connection minted a fake crash every drop.
                print(f"[era] [NET] Recv: connection dropped ({e}); reconnecting.")
                log_event(
                    main_logger,
                    "engine.network.blip",
                    "WARNING",
                    source="receive_audio",
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )
            else:
                print(f"[era] [ERROR] Recv: {e}")
                traceback.print_exc()
                log_crash(main_logger, "receive_audio", type(e), e, e.__traceback__)
            raise

    async def _play_audio(self):
        print("[era] [PLAY] Play started")

        # Where Era's OWN voice plays — the user's pinned device if there is
        # one, else the Star Glasses while connected, else the system default
        # (core/voice_output.py owns that decision). Music and media are NOT
        # affected: this binds a PortAudio stream, it never touches the Windows
        # default device. Falls back to the default output on any error — never
        # lose her voice over a routing preference.
        output_device = None
        route_generation = 0
        # How often to ask whether a disconnected pinned device has returned.
        # Only while running on the fallback device, and the probe itself runs
        # off this loop, so this can be short: a Bluetooth device that drops and
        # returns within a couple of seconds must not strand Era's voice on the
        # laptop for the rest of the turn.
        RECLAIM_CHECK_INTERVAL_S = 1.5
        last_reclaim_check = 0.0
        reclaim_probe = None
        # Separate, throttled probe for the "connected after startup, invisible to
        # PortAudio" case, which the reclaim above cannot fix (there is no index to
        # reopen onto). Runs regardless of on_fallback, off the event loop.
        last_reenum_check = 0.0
        reenum_probe = None
        try:
            from core import voice_output as _vo

            output_device = _vo.resolve_output_index()
            route_generation = _vo.generation()
            if output_device is not None:
                print(f"[era] [PLAY] Routing speech to device {output_device}")
        except Exception as _dev_err:
            print(f"[era] [PLAY] voice routing probe failed: {_dev_err}")

        def _open_stream(device):
            return sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=device,
            )

        # A speaker fault (device vanished/switched: PortAudio MME error 6) must
        # never escape this loop — it used to tear down the whole Live session
        # via the TaskGroup, and reconnecting cannot fix a speaker. The wrapper
        # falls back to the default device and reopens with backoff instead.
        def _report_route(device, on_fallback: bool) -> None:
            """Publish where the voice actually opened, so 'where does your voice
            play' answers from the STREAM rather than from the stored pin."""
            try:
                from core import voice_output as _vo2

                name = ""
                if device is not None:
                    try:
                        name = str(sd.query_devices(device)["name"])
                    except Exception:
                        name = ""
                _vo2.note_active_route(device, fallback=on_fallback, name=name)
            except Exception:
                pass

        output = ResilientOutputStream(_open_stream, output_device, on_open=_report_route)
        output.open()

        import numpy as _np

        last_amp_emit = 0.0
        # Barge-in fade state: `last_chunk` is what Era most recently played, so a
        # cut-in can duck ITS tail to zero; `barge_faded` fires that duck once per
        # interruption rather than every idle loop.
        last_chunk = b""
        barge_faded = False

        # EQUALIZER BANDS — the sphere reacts to Era's voice as a spectrum, not
        # just a level, so we hand the UI per-band energies alongside the RMS.
        # Eight logarithmically-spaced bands across the speech range: linear
        # bins would spend most of their resolution on frequencies the voice
        # barely occupies, and the sphere would move as one lump.
        _EQ_BANDS = 8
        _eq_smooth = _np.zeros(_EQ_BANDS, dtype=_np.float32)

        # Bin ranges are cached PER FFT SIZE, not once. Audio chunks are not a
        # fixed length, so caching a single set of ranges meant that the first
        # shorter chunk indexed past the end of its (smaller) spectrum — an
        # empty slice, whose mean is NaN. That NaN then poisoned the smoothing
        # state permanently and serialized as a bare `NaN`, which is invalid
        # JSON: the UI's JSON.parse threw and dropped the WHOLE amplitude
        # message, level included, so the sphere went completely dead.
        _band_edges_by_size: dict[int, list[tuple[int, int]]] = {}

        def _band_edges_for(n_bins: int) -> list[tuple[int, int]]:
            """Bin ranges for each band, for THIS spectrum's size."""
            cached = _band_edges_by_size.get(n_bins)
            if cached is not None:
                return cached
            nyquist = RECEIVE_SAMPLE_RATE / 2.0
            lo_hz, hi_hz = 80.0, min(6000.0, nyquist)
            edges: list[tuple[int, int]] = []
            for b in range(_EQ_BANDS):
                f0 = lo_hz * (hi_hz / lo_hz) ** (b / _EQ_BANDS)
                f1 = lo_hz * (hi_hz / lo_hz) ** ((b + 1) / _EQ_BANDS)
                i0 = min(int(f0 / nyquist * n_bins), max(0, n_bins - 1))
                i1 = min(max(i0 + 1, int(f1 / nyquist * n_bins)), n_bins)
                edges.append((i0, i1))
            _band_edges_by_size[n_bins] = edges
            return edges

        def _set_speaking(value: bool) -> None:
            # A torn-down UI ("wrapped C/C++ object of type MainWindow has been
            # deleted") is not a reason to drop the Live session.
            try:
                self.set_speaking(value)
            except RuntimeError as ui_err:
                print(f"[era] [PLAY] set_speaking({value}) skipped: {ui_err}")

        def _emit_amplitude(level: float, bands: list[float] | None = None) -> None:
            try:
                self.ui.set_amplitude(level, bands)
            except Exception:
                pass

        try:
            while True:
                # Honor GoAway teardown so the TaskGroup can unwind and reconnect.
                if self._go_away_event.is_set():
                    break

                # ── Live re-route: "speak on my glasses" lands NOW ──
                # An integer compare per loop; the expensive re-resolve only
                # runs when the user actually changed the pin. This is what
                # makes the routing command take effect on the current turn
                # instead of at the next reconnect.
                try:
                    from core import voice_output as _vo

                    current_gen = _vo.generation()
                    if current_gen != route_generation:
                        route_generation = current_gen
                        output.set_preferred_device(_vo.resolve_output_index())
                    # While playing on the fallback device, keep asking whether
                    # the CHOSEN one is back. Nothing fails while the fallback
                    # works, so without this a device that reconnected
                    # mid-session was never reclaimed and the pin looked dead.
                    #
                    # The probe runs OFF the event loop. It is a pycaw COM
                    # enumeration - measured at ~117 ms - and awaiting that here
                    # would stall this loop, which is the loop feeding the
                    # speaker: the cure would stutter the very audio it exists
                    # to route. So it is fired as a background task and the
                    # result is collected on a later iteration.
                    elif output.on_fallback:
                        _now = time.time()
                        if (
                            reclaim_probe is None
                            and _now - last_reclaim_check >= RECLAIM_CHECK_INTERVAL_S
                        ):
                            last_reclaim_check = _now
                            reclaim_probe = asyncio.create_task(
                                asyncio.to_thread(_vo.pinned_device_connected)
                            )
                        if reclaim_probe is not None and reclaim_probe.done():
                            came_back = False
                            try:
                                came_back = bool(reclaim_probe.result())
                            except Exception as _probe_err:
                                print(f"[era] [PLAY] reclaim probe failed: {_probe_err}")
                            reclaim_probe = None
                            if came_back:
                                output.reclaim_preferred()
                    elif reclaim_probe is not None:
                        # Back on the chosen device by another route; the answer
                        # in flight is stale.
                        reclaim_probe.cancel()
                        reclaim_probe = None
                except Exception as _route_err:
                    print(f"[era] [PLAY] re-route skipped: {_route_err}")

                # ── Reconnect to pick up a device that connected AFTER startup ──
                # A pinned device first seen after Pa_Initialize is invisible to
                # PortAudio's frozen list, so resolve_output_index() can't route
                # to it and the reclaim above (which needs an index) can't help.
                # The only cure is re-enumeration, which is process-wide and
                # unsafe mid-stream — so we ask the EXISTING renewal scheduler to
                # reconnect. A far deadline means only its idle-between-turns path
                # takes it (never a mid-turn cut), and refresh_between_sessions()
                # then re-enumerates so the fresh _play_audio binds the device.
                # Throttled, rate-limited, and skipped when a renewal is already
                # pending, so a flapping or absent device cannot spin reconnects.
                try:
                    from core import voice_output as _vo

                    _now = time.time()
                    if (
                        reenum_probe is None
                        and _now - last_reenum_check >= VOICE_REENUM_CHECK_INTERVAL_S
                        and getattr(self, "_renewal_deadline", None) is None
                        and _now - getattr(self, "_voice_reroute_requested_at", 0.0)
                        >= VOICE_REENUM_MIN_INTERVAL_S
                    ):
                        last_reenum_check = _now
                        reenum_probe = asyncio.create_task(
                            asyncio.to_thread(_vo.needs_reenumeration)
                        )
                    if reenum_probe is not None and reenum_probe.done():
                        needs = False
                        try:
                            needs = bool(reenum_probe.result())
                        except Exception as _re_err:
                            print(f"[era] [PLAY] re-enum probe failed: {_re_err}")
                        reenum_probe = None
                        if needs and getattr(self, "_renewal_deadline", None) is None:
                            self._voice_reroute_requested_at = time.time()
                            try:
                                self.session_manager.prepare_reconnect()
                            except Exception as _pr_err:
                                print(f"[era] [PLAY] prepare_reconnect skipped: {_pr_err}")
                            self._renewal_deadline = time.time() + VOICE_REENUM_IDLE_GRACE_S
                            print(
                                "[era] [PLAY] pinned voice device is connected but not "
                                "visible to the audio engine; scheduled a reconnect at "
                                "the next idle moment to route to it."
                            )
                            try:
                                log_event(
                                    main_logger,
                                    "audio.output.reenum_scheduled",
                                    "INFO",
                                    source="play_audio",
                                )
                            except Exception:
                                pass
                except Exception as _reenum_err:
                    print(f"[era] [PLAY] re-enum check skipped: {_reenum_err}")

                # ── Barge-in: duck, don't guillotine ──
                # trigger_barge_in has flushed the queue and flipped to LISTENING.
                # Fade the tail of the last chunk once (soft hand-off, no click),
                # then stay quiet until the latch clears on the next turn.
                if self._barge_in_requested:
                    if not barge_faded:
                        if last_chunk and self._barge_in_fade_ms > 0:
                            try:
                                tail = fade_out_pcm(
                                    last_chunk, RECEIVE_SAMPLE_RATE, self._barge_in_fade_ms
                                )
                                if tail:
                                    await output.write(tail)
                            except Exception:
                                pass
                        _emit_amplitude(0.0)  # settle the sphere the instant she yields
                        barge_faded = True
                    await asyncio.sleep(0.02)
                    continue
                barge_faded = False

                try:
                    chunk = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.1)
                except TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        _set_speaking(False)
                        self._turn_done_event.clear()
                        _emit_amplitude(0.0)  # settle the sphere when Era goes quiet
                    continue
                _set_speaking(True)
                last_chunk = chunk  # remembered so a barge-in can fade its tail

                # Drive the UI sphere from Era's *own* voice: overall level plus
                # an 8-band spectrum of the same PCM chunk, so the sphere can
                # move as an equalizer. Throttled to ~33Hz so the FFT stays off
                # the audio hot path's critical timing.
                now = time.time()
                if now - last_amp_emit >= 0.03:
                    try:
                        pcm = _np.frombuffer(chunk, dtype=_np.int16)
                        if pcm.size:
                            mono = pcm.astype(_np.float32) / 32768.0
                            rms = float(_np.sqrt(_np.mean(mono * mono)))
                            # Perceptual gain so normal speech fills the sphere.
                            level = min(1.0, rms * 3.5)

                            # Per-band energy for the equalizer. Hann-windowed
                            # so band leakage doesn't smear one loud formant
                            # across every band and flatten the whole spectrum.
                            spec = _np.abs(
                                _np.fft.rfft(mono * _np.hanning(mono.size).astype(_np.float32))
                            )
                            raw = _np.array(
                                [
                                    float(spec[i0:i1].mean()) if i1 > i0 else 0.0
                                    for i0, i1 in _band_edges_for(spec.size)
                                ],
                                dtype=_np.float32,
                            )
                            # Compress the dynamic range (voice energy is heavily
                            # skewed to the low bands) and normalize to 0..1.
                            # nan_to_num is a belt-and-braces guard: a single
                            # non-finite value here would poison the smoothing
                            # state below forever AND make the JSON unparseable
                            # on the UI side, killing the sphere outright.
                            raw = _np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)
                            raw = _np.clip(_np.sqrt(raw * 18.0), 0.0, 1.0)

                            # Fast attack / slow release: the bars snap to a
                            # consonant but fall back smoothly, which is what
                            # makes it read as an equalizer instead of noise.
                            rise = raw > _eq_smooth
                            _eq_smooth[rise] = raw[rise]
                            _eq_smooth[~rise] += (raw[~rise] - _eq_smooth[~rise]) * 0.35

                            _emit_amplitude(level, _eq_smooth.tolist())
                    except Exception:
                        pass
                    last_amp_emit = now

                await output.write(chunk)
                # Bytes the speaker actually consumed. This is the only evidence
                # the user HEARD the answer — a finished transcript proves the
                # model spoke, not that anything came out. decide_recovery()
                # reads the gap between received and played to tell "finish
                # playing what we hold" apart from "ask for the rest".
                # Wrapped: _play_audio re-raises, which tears down the session
                # TaskGroup. A COUNTER must never be able to do that — this whole
                # loop exists to keep a speaker fault from killing the Live
                # session (see ResilientOutputStream), and it would be absurd for
                # the diagnostics added alongside it to become the new way to
                # kill it. Diagnostics are never load-bearing.
                try:
                    self.live_turn.note_audio_played(len(chunk))
                except Exception:
                    pass
                # First byte the user actually HEARD — the latency headline is
                # measured to this point, not to the bytes Gemini sent.
                _latency_mark(self, "note_audio_played")
        except Exception as e:
            print(f"[era] [ERROR] Play: {e}")
            raise
        finally:
            _set_speaking(False)
            _emit_amplitude(0.0)
            # An in-flight device probe must not outlive the loop that owns it:
            # a pending task at teardown is an "Task was destroyed but it is
            # pending" warning, and inside a TaskGroup an unretrieved failure is
            # worse than noise.
            probes = [p for p in (reclaim_probe, reenum_probe) if p is not None]
            for probe in probes:
                if not probe.done():
                    probe.cancel()
            if probes:
                await asyncio.gather(*probes, return_exceptions=True)
            output.close()  # swallows stop/close errors from a vanished device
