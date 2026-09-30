"""Audio I/O loops for EraLive (extracted from main.py).

Mixin: microphone capture -> Gemini Live, and Live audio -> speakers, plus the
drop logger. Operates purely on self.* state defined by EraLive.__init__.
"""

import asyncio
import time
import traceback

import sounddevice as sd
from google.genai import types

from app.audio_config import CHANNELS, CHUNK_SIZE, RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE
from app.notify import _tg_notify
from app.text_intents import (
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
from core.vad import fade_out_pcm
from core.voice_mode import MODE_GEMINI_AUDIO, VOICE_CFG

# Cap on held Gemini audio per turn (~43ms per chunk at 24kHz => ~40s of speech).
CLOUD_AUDIO_BUFFER_MAX_CHUNKS = 900

# How long to stay on the Gemini fallback before retrying the primary backend.
TTS_PRIMARY_RETRY_S = 300.0


class AudioIOMixin:
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

    def _tts_fallback(self, reason: str = "") -> None:
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
            buffered = list(getattr(self, "_cloud_audio_buffer", None) or [])
            self._clear_cloud_audio_buffer()
            if self.audio_in_queue is not None:
                for chunk in buffered:
                    self.audio_in_queue.put_nowait(chunk)
            previous = VOICE_CFG.mode
            VOICE_CFG.set_mode(MODE_GEMINI_AUDIO)
            self._tts_primary_retry_at = time.time() + TTS_PRIMARY_RETRY_S
            self._tts_primary_mode = previous
            print(
                f"[TTS] PRIMARY failed ({reason or 'unknown'}); fell back to Gemini "
                f"and replayed {len(buffered)} buffered chunk(s). "
                f"Retrying {previous} in {int(TTS_PRIMARY_RETRY_S)}s."
            )
            try:
                self.ui.write_log("SYS: Primary voice unavailable - using my Gemini voice.")
            except Exception:
                pass
        except Exception as e:
            print(f"[TTS] fallback handler error: {e}")

    def _maybe_restore_primary_tts(self) -> None:
        """Return to the primary backend once its retry window has elapsed."""
        try:
            retry_at = getattr(self, "_tts_primary_retry_at", 0.0)
            preferred = getattr(self, "_tts_primary_mode", "")
            if not retry_at or not preferred or time.time() < retry_at:
                return
            if not VOICE_CFG.is_gemini():
                self._tts_primary_retry_at = 0.0
                return
            from core.tts_fish import FISH, is_configured

            if not is_configured() or FISH.in_cooldown():
                self._tts_primary_retry_at = time.time() + TTS_PRIMARY_RETRY_S
                return
            VOICE_CFG.set_mode(preferred)
            self._tts_primary_retry_at = 0.0
            print(f"[TTS] primary backend recovered; back on {preferred}.")
        except Exception as e:
            print(f"[TTS] primary restore check failed: {e}")

    def trigger_barge_in(self) -> None:
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

            blob = types.Blob(data=msg["data"], mime_type=msg["mime_type"])
            await self.session.send_realtime_input(audio=blob)

    async def _listen_audio(self):
        print("[era] [MIC] Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                era_speaking = self._is_speaking

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
                    loop.call_soon_threadsafe(self._wake_queue.put_nowait, data)
                else:
                    now_ts = time.time()
                    if now_ts - self._last_wake_queue_missing_log_ts > 5.0:
                        self._last_wake_queue_missing_log_ts = now_ts
                        print("[MIC] Standby active but wake_queue is None — dropping wake audio")
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

            # Normal operation: send audio to Gemini only if not muted
            if not self.ui.muted and self._browser_mic_active:
                data = indata.tobytes()
                try:
                    # Thread-safe try-push without exploding if the loop lags
                    loop.call_soon_threadsafe(
                        lambda: (
                            self.out_queue.put_nowait(
                                {"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"}
                            )
                            if not self.out_queue.full()
                            else None
                        )
                    )
                except Exception:
                    pass

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[era] [MIC] Mic stream open")
                while True:
                    if self._go_away_event.is_set():
                        break
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[era] [ERROR] Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[era] [RECV] Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    # Any message from the Live session (audio, transcript,
                    # tool call, ...) is genuine activity. Without this, a
                    # voice-only session never touches
                    # _last_interaction_timestamp (that's only bumped by
                    # typed text and standby-resume), so the 300s idle
                    # timer keeps counting from session start and can
                    # force standby mid-conversation or mid-tool-call.
                    self._last_interaction_timestamp = time.time()

                    # ── Handle GoAway signal ──
                    if self.session_manager.handle_go_away(response):
                        print("[RECV] GoAway received. Preparing graceful reconnect.")
                        self._go_away_event.set()
                        self.session_manager.prepare_reconnect()
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
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        # Swallow Gemini's audio while a CM hand-off window is
                        # open: the utterance belongs to the headless CM lane, so
                        # Gemini's parallel spoken reaction to it (a hallucinated
                        # blocker) must never reach the speakers. The CM lane
                        # speaks its own result later, well past this window.
                        #
                        # kokoro_audio / fish_audio hold it back for a different
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

                        # Native barge-in: Gemini itself heard the user talk over
                        # the model and stopped generating. Flush whatever already
                        # reached the queue and latch the same gate the local
                        # detector uses, so the aborted turn stops playing here too.
                        if getattr(sc, "interrupted", False) and not self._barge_in_requested:
                            self._barge_in_requested = True
                            self._flush_audio_in_queue()

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                out_buf.append(txt)
                                self._live_turn_text = " ".join(out_buf)  # for barge-in resume
                                # ── kokoro_audio: the TRANSCRIPT is the speech ──
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
                                    self.speech_out.feed(txt)

                        # ── kokoro_text: the model's TEXT *is* the speech ──
                        # In this mode there is no response.data and no
                        # output_transcription, so this branch feeds BOTH the
                        # local synthesizer and out_buf. Skipping out_buf here
                        # would silently empty the "era:" log and the Telegram
                        # mirror, which read from it on turn_complete.
                        #
                        # The CM hand-off gate applies here exactly as it does
                        # to response.data above: during a hand-off the
                        # utterance belongs to the headless CM lane, and
                        # Gemini's parallel reaction to it must not be spoken.
                        # kokoro_TEXT only: in kokoro_audio the speech already
                        # came from output_transcription above, and model_turn
                        # there carries audio parts, not text. Gating on the
                        # narrow predicate keeps a future text-bearing
                        # model_turn from feeding the same reply twice.
                        if VOICE_CFG.is_kokoro_text() and sc.model_turn:
                            delta = "".join(p.text for p in (sc.model_turn.parts or []) if p.text)
                            if delta:
                                out_buf.append(delta)
                                self._live_turn_text = " ".join(out_buf)  # for barge-in resume
                                if not self._cm_handoff_active() and not self._barge_in_requested:
                                    self.speech_out.feed(delta)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_text = txt

                        if sc.turn_complete:
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
                                self.speech_out.end_turn()
                            elif self._turn_done_event:
                                self._turn_done_event.set()

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._note_user_utterance(full_in)
                                self.ui.write_log(f"You: {full_in}")
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
                            # The fallback window for this turn is over; release the
                            # held Gemini audio so it can never play late.
                            self._clear_cloud_audio_buffer()
                            self._maybe_restore_primary_tts()
                            out_buf = []

                    # --- HANDLE TOOL CALLS ---
                    if response.tool_call and response.tool_call.function_calls:
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

        # Route Era's OWN voice to the Star Glasses when they are connected, so
        # her speech follows the hardware while music/media stay on the laptop's
        # default output. Resolved at stream-open (per session/reconnect); a
        # mid-session connect/disconnect re-routes on the next reconnect. Falls
        # back to the default device on any error — never lose her voice over a
        # routing preference.
        output_device = None
        try:
            from core.audio_devices import glass_output_index

            output_device = glass_output_index()
            if output_device is not None:
                print(f"[era] [PLAY] Routing speech to Star Glasses (device {output_device})")
        except Exception as _dev_err:
            print(f"[era] [PLAY] glass routing probe failed: {_dev_err}")

        def _open_stream(device):
            return sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=device,
            )

        try:
            stream = _open_stream(output_device)
        except Exception as _open_err:
            if output_device is not None:
                print(f"[era] [PLAY] glasses device unavailable ({_open_err}); using default.")
                stream = _open_stream(None)
            else:
                raise
        stream.start()

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
                                    await asyncio.to_thread(stream.write, tail)
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
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                        _emit_amplitude(0.0)  # settle the sphere when Era goes quiet
                    continue
                self.set_speaking(True)
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

                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[era] [ERROR] Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            _emit_amplitude(0.0)
            stream.stop()
            stream.close()
