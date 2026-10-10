"""Local fallback voice loop — Era keeps hearing and acting while Gemini is down.

BEFORE (main.py run loop): quota exhausted on every key -> sleep 15 min - 2 h
with no mic, no wake word, and typed/Telegram answered "my session isn't up".
Network down -> 6 retries -> standby that could not hear the wake word either.

NOW: those same waits are spent here, serving locally:

    mic (16 kHz) -> Silero VAD -> Whisper-small (FR/EN)       core/local_stt.py
                 -> input safety gate -> LocalBrain (Ollama)  core/local_brain.py
                    (CodeCraft in cascade mode — app/era_live/cascade.py)
                 -> Fish, or Kokoro George (local GPU)        core/local_tts.py
                 -> speaker (Era's voice device)

Tool calls go through self._execute_tool — the exact gate Gemini's use — so
routing, classify_action, confirmation and memory authorization all still
apply. Barge-in reuses the session path's BargeInDetector.

The run loop regains control when the wait is over (it then retries Gemini),
on shutdown, or when the user says "reconnect". Degrade, never raise: if the
local stack is not ready, serve_locally_during_outage() returns False and the
old wait happens unchanged. Kill switch: ERA_LOCAL_FALLBACK=0.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

import sounddevice as sd

from app.audio_config import CHANNELS, CHUNK_SIZE, RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE
from app.era_live.local_guards import owner_voice, user_approved
from app.era_live.tts_text import sanitize_for_tts
from core.mic_mute import is_mic_off_order

_STOP = re.compile(
    r"^\W*(stop|stop it|shut up|silence|cancel|cancel (it|that)|no,? cancel|"
    r"tais[- ]toi|arr[êe]te|annule)\W*$",
    re.I,
)
_RECONNECT = re.compile(
    r"\b(reconnect|try (gemini|the cloud) again|reconnecte[- ]toi|retry (gemini|connection))\b",
    re.I,
)
_ANNOUNCE = {
    "quota": "My cloud quota is exhausted, sir. I'm running on my local backup brain.",
    "auth": "My cloud key was rejected, sir. I'm running on my local backup brain.",
    "config": "My cloud session is refusing to start, sir. I'm on my local backup brain.",
    "network": "The internet connection is down, sir. I'm running on my local backup brain.",
    # Gemini itself failing (1011 / 503) while the internet works.
    "upstream": "My main voice connection dropped, sir. I'm still here on my backup brain.",
    # Claude voice mode (app/era_live/claude_voice.py). Without these two the
    # fallback line above would announce "the internet connection is down".
    "claude": "My cloud voice stopped answering, sir. I'm using my Claude backup brain.",
    "claude_manual": "Switching to my Claude brain, sir.",
}
# Outage reasons for which Claude (when enabled and ready) is the brain instead of
# Ollama. "network" is deliberately absent: Claude needs the network too.
# "upstream" (Gemini failing, internet fine) is present: the brain is OmniRoute
# era-gateway first, which does not depend on Google at all.
_CLAUDE_BRAIN_REASONS = ("claude", "claude_manual", "quota", "auth", "config", "upstream")
_BACK = "Cloud connection restored, sir."
_PLAY_SLICE = RECEIVE_SAMPLE_RATE // 10 * 2  # 100 ms of int16 mono


def handover_reason(
    *, offline: bool, attempts: int, max_attempts: int, upstream_after: int
) -> str | None:
    """Should the run loop serve locally after a failed reconnect, and why?

    "network"  — the internet is down (two "no internet" failures in a row).
    "upstream" — Gemini itself is failing (1011 / 503) while the internet works:
                 after `upstream_after` failed reconnects, not after all of them
                 (that was ~150 s of deafness). The brain is then OmniRoute.
    None       — keep retrying Gemini.
    """
    if offline:
        return "network"
    if (upstream_after > 0 and attempts >= upstream_after) or attempts >= max_attempts:
        return "upstream"
    return None


class LocalFallbackMixin:
    # ── readiness ───────────────────────────────────────────────────────────
    def _local_policy(self) -> dict[str, Any]:
        try:
            from core.llm.router import local_fallback_policy

            return local_fallback_policy()
        except Exception:
            return {"enabled": False}

    def local_fallback_ready(self, reason: str = "") -> tuple[bool, str]:
        if not self._local_policy().get("enabled"):
            return False, "local fallback disabled (routing.local_fallback / ERA_LOCAL_FALLBACK)"
        from core import local_stt, local_tts

        checks = [local_stt.available(), local_tts.available()]
        # Ollama is needed only when it will be the brain. Requiring it always
        # kept the loop from starting with the Claude/OmniRoute brain whenever
        # Ollama happened to be down ("Ollama not reachable" x4 in the logs).
        if not self._claude_serves(reason):
            from core.llm.adapters.ollama_chat import ping

            checks.append(ping())
        for ok, why in checks:
            if not ok:
                return False, why
        return True, ""

    def _claude_serves(self, reason: str) -> bool:
        """Sync twin of _claude_brain_wanted, for the readiness check."""
        ready = getattr(self, "claude_voice_ready", None)
        if reason not in _CLAUDE_BRAIN_REASONS or ready is None:
            return False
        try:
            return bool(ready()[0])
        except Exception:
            return False

    def local_mode_active(self) -> bool:
        return bool(getattr(self, "_local_mode_active", False))

    def _in_cascade(self) -> bool:
        """This loop is serving the cascade voice mode (app/era_live/cascade.py)."""
        return self.local_mode_active() and getattr(self, "_local_outage_reason", "") == "cascade"

    def _local_fish_ok(self) -> bool:
        """An outage always tries Fish; cascade honours the voice the user chose."""
        if not self._in_cascade():
            return True
        from core.voice_mode import VOICE_CFG

        return VOICE_CFG.is_fish()

    def failure_is_offline(self, exc: BaseException) -> bool:
        """True when a Live-session failure means "no internet" (DNS failed,
        connection refused/aborted), not a Gemini-side problem. Stricter than
        _is_network_blip on purpose: an ordinary socket close must not start
        the local loop."""
        from core.llm.quota_failover import is_offline_error

        return any(is_offline_error(leaf) for leaf in self._flatten_exc(exc))

    # ── entry point from the run loop ───────────────────────────────────────
    async def serve_locally_during_outage(self, seconds: float, reason: str) -> bool:
        """Serve locally for up to `seconds`. False = could not; caller waits as before."""
        ok, why = await asyncio.to_thread(self.local_fallback_ready, reason)
        if not ok:
            print(f"[LOCAL] Fallback unavailable: {why}")
            return False
        probe = float(self._local_policy().get("probe_gemini_every_s") or 300)
        try:
            await self._run_local_fallback(min(seconds, probe), reason)
        except Exception as e:  # the fallback must never take the run loop down
            print(f"[LOCAL] Fallback loop failed: {type(e).__name__}: {e}")
            return False
        return True

    def note_cloud_restored(self) -> None:
        """Called by the run loop after a successful Gemini connect."""
        if getattr(self, "_local_outage_reason", ""):
            self._local_outage_reason = ""
            self.ui.write_log(f"SYS: {_BACK} Back on Gemini.")

    # ── the loop ────────────────────────────────────────────────────────────
    async def _local_prepare(self, reason: str = "") -> None:
        from core.local_brain import LocalBrain
        from core.local_stt import LocalListener

        if getattr(self, "_local_listener", None) is None:
            langs = self._local_policy().get("languages") or ()
            self._local_listener = LocalListener(languages=tuple(str(x) for x in langs))
        # Cascade mode (app/era_live/cascade.py) swaps in its CodeCraft brain;
        # the Ollama one is kept aside for the next real outage.
        if reason == "cascade":
            self._local_brain = self._cascade_brain()
        elif await self._claude_brain_wanted(reason):
            self._local_brain = self._claude_brain()
        elif getattr(self, "_fallback_brain", None) is not None:
            self._local_brain = self._fallback_brain
        elif getattr(self, "_local_brain", None) is None or reason:
            pol = self._local_policy()
            self._local_brain = self._fallback_brain = LocalBrain(
                alias=str(pol.get("alias") or "era.local"),
                tools=None if pol.get("tools", True) else [],
                history_turns=int(pol.get("history_turns") or 8),
                max_tool_rounds=int(pol.get("max_tool_rounds") or 4),
            )
        await asyncio.to_thread(self._local_listener.load)
        from core.tts_kokoro import KOKORO

        KOKORO.warm_in_background()  # normally already warm since startup (core/voice_mode)

    async def _claude_brain_wanted(self, reason: str) -> bool:
        """Claude (subscription CLI) is the brain for every reason it can help with,
        but only when claude voice is enabled and ready. ClaudeVoiceMixin is optional
        here — a Stub or a bare mixin without it must keep the Ollama brain."""
        if reason not in _CLAUDE_BRAIN_REASONS:
            return False
        ready = getattr(self, "claude_voice_ready", None)
        if ready is None:
            return False
        try:
            ok, _why = await asyncio.to_thread(ready)
        except Exception:
            return False
        return bool(ok)

    async def _local_warm_brain(self) -> None:
        """Load the model + tool prefix (~20 s cold) while Era is announcing."""
        from core.local_brain import LocalBrain

        warm = LocalBrain(alias=self._local_brain.alias, tools=self._local_brain.tools())
        try:
            async for _ in warm.respond("ping"):
                pass
        except Exception as e:
            print(f"[LOCAL] Brain warm-up failed: {e}")

    async def _run_local_fallback(self, seconds: float, reason: str) -> None:
        await self._local_prepare(reason)
        loop = asyncio.get_running_loop()
        self._local_exit = asyncio.Event()
        self._local_speaking = False
        self._local_turn_task: asyncio.Task | None = None
        frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._local_frames = frames

        def on_mic(indata, _frames, _time, _status) -> None:
            data = indata.tobytes()

            def put() -> None:
                if not frames.full():
                    frames.put_nowait(data)

            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass

        self._local_mode_active = True
        # Cascade is a chosen mode, not an outage: nothing to announce or warm.
        first = getattr(self, "_local_outage_reason", "") == "" and reason != "cascade"
        self._local_outage_reason = reason
        self.set_status(
            "Cascade voice — CodeCraft brain"
            if reason == "cascade"
            else f"Local mode — Gemini unavailable ({reason})"
        )
        if not self._standby_active:
            self.ui.set_state("LISTENING")
        print(f"[LOCAL] Serving locally for up to {seconds / 60:.0f} min ({reason}).")
        # Nothing to load for the Claude brain (a CLI, no model in memory): warming it
        # would only spend a real turn on a "ping".
        claude_brain = getattr(self, "_claude_brain_obj", None)
        warm = (
            asyncio.create_task(self._local_warm_brain())
            if first and not (claude_brain is not None and self._local_brain is claude_brain)
            else None
        )
        deadline = time.monotonic() + seconds
        try:
            if first:
                self.ui.write_log(f"SYS: {_ANNOUNCE.get(reason, _ANNOUNCE['network'])}")
                await self._local_say(_ANNOUNCE.get(reason, _ANNOUNCE["network"]))
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=on_mic,
            ):
                while time.monotonic() < deadline and not self._shutdown_requested:
                    if self._local_exit.is_set():
                        break
                    try:
                        frame = await asyncio.wait_for(frames.get(), timeout=0.5)
                    except TimeoutError:
                        continue
                    await self._local_on_frame(frame)
        finally:
            self._local_cancel_turn()
            tasks = [t for t in (self._local_turn_task, warm) if t is not None]
            if warm is not None and not warm.done():
                warm.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._local_mode_active = False
            self._local_listener.reset()

    def _mic_open(self) -> bool:
        return (
            self._browser_mic_active
            and not self.ui.muted
            and not getattr(self, "_mic_muted", False)
        )

    async def _local_on_frame(self, frame: bytes) -> None:
        if not self._mic_open():
            return
        if self._local_speaking:
            det = getattr(self, "_barge_in", None)
            try:
                if det is not None and det.enabled and det.feed(frame):
                    print("[LOCAL] Barge-in — stopping.")
                    self._local_cancel_turn()
            except Exception:
                pass
            return
        for segment in self._local_listener.feed(frame):
            said_at = time.time()
            if not await asyncio.to_thread(owner_voice, self, segment):
                continue
            utt = await asyncio.to_thread(self._local_listener.transcribe, segment)
            if utt is not None:
                print(f"[LOCAL] Heard ({utt.lang}, stt {utt.stt_ms} ms): {utt.text}")
                await self._local_on_utterance(utt.text, utt.lang, said_at)

    async def _local_on_utterance(self, text: str, lang: str, said_at: float) -> None:
        if self._standby_active:
            if self._wake_word_found(text):
                await self._resume_from_standby("local_wake", speak_ack=False)
                await self._local_say("Yes, sir.")
            return
        # The same transcript-level orders the Gemini path acts on without the
        # model (app/era_live/audio.py): abort first, never escalated to reboot.
        if self._is_abort_order(text):
            self.trigger_abort("voice", text)
            return
        if self._is_reboot_order(text) and not self._shutdown_requested:
            self._shutdown_era({"relaunch": True, "_source": "voice"})
            return
        if is_mic_off_order(text) and not getattr(self, "_mic_muted", False):
            self.mute_microphone("voice")
            return
        if _STOP.match(text):
            # "Stop" drops half-finished work too: a confirmation left armed
            # would run on the next unrelated "yes".
            self._local_cancel_turn()
            self._clear_pending_work()
            return
        if self._in_cascade() and await self._cascade_voice_order(text):
            return
        if _RECONNECT.search(text):
            self.ui.write_log(f"You: {text}")
            await self._local_say("Trying the cloud again, sir.")
            if self._in_cascade():
                self._cascade_pause()
            # Closes a Claude window too: left open, the run loop would serve this
            # same local loop again at once and "reconnect" would not reconnect.
            release = getattr(self, "release_claude_voice", None)
            if release is not None:
                release()
            self._local_exit.set()
            return
        if self._local_turn_task is not None and not self._local_turn_task.done():
            print(f"[LOCAL] Busy — ignored while thinking: {text}")
            return
        # Attention window (core/attention.py), on the transcript here since the
        # local ear already decoded it. Safety orders above always pass.
        if not self._local_addressed(text):
            print(f"[LOCAL] Not addressed to Era — ignored: {text!r}")
            self._attention_event("ignored", text)
            return

        decision = self.safety_gate.validate_input(
            transcript=text,
            confidence=None,
            state_snapshot=self._text_state_snapshot(),
            typed=False,
        )
        if decision.action == "BLOCK_SILENTLY":
            print(f"[LOCAL] Gate blocked: {text!r} — {decision.reason}")
            return
        self.ui.write_log(f"You: {text}")
        from core.autopilot import MAIN, classify

        autopilot = None
        if hasattr(self, "_run_autopilot"):
            autopilot = self._autopilot_confirmation(text) or classify(text)
        if autopilot is not None and autopilot.lane != MAIN:
            self._note_user_utterance(text, said_at)
            self._last_interaction_timestamp = time.time()
            self._local_turn_task = asyncio.create_task(self._run_autopilot(text, autopilot))
            return
        if decision.action == "ASK_CLARIFICATION":
            from core.input_safety_gate import CLARIFICATION_PHRASE

            await self._local_say(CLARIFICATION_PHRASE)
            return
        self._note_user_utterance(text, said_at)
        self._last_interaction_timestamp = time.time()
        self._local_turn_task = asyncio.create_task(self._local_turn(text, lang))

    def _local_addressed(self, text: str) -> bool:
        """Inside the conversation window, or a confirmation waiting, or the
        utterance starts with Era's name: it is for Era. Fail-open."""
        try:
            from core.attention import addressed_to_era

            state = self._listening_state()
            if addressed_to_era(text):
                state.note_activity(explicit=True)
                return True
            if state.window_open() or self._attention_keep_open():
                state.note_activity()
                return True
            return False
        except Exception:
            return True

    # ── a turn: brain -> voice, pipelined ───────────────────────────────────
    async def _local_turn(self, text: str, lang: str = "") -> str:
        from core.local_tts import guess_lang, synthesize_best

        self.ui.set_state("THINKING")
        queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue(maxsize=4)
        reply: list[str] = []
        fish = self._local_fish_ok()
        produced = False
        failed = False

        async def say_failure() -> None:
            msg = (
                "My brain isn't answering right now, sir. Try again in a moment."
                if self._in_cascade()
                else "My backup brain isn't responding, sir."
            )
            try:
                pcm, _ = await synthesize_best(msg, "en", allow_fish=fish)
            except Exception:
                pcm = b""  # a TTS outage must still produce a visible reply
            await queue.put((msg, pcm))

        async def produce() -> None:
            nonlocal produced, failed
            try:
                async for raw in self._local_brain.respond(text, self._local_execute_tool):
                    # Same speech-only rules as the Fish path (tts_text.py).
                    sentence = sanitize_for_tts(raw)
                    if not sentence:
                        continue
                    produced = True
                    pcm, _backend = await synthesize_best(
                        sentence, guess_lang(sentence), allow_fish=fish
                    )
                    await queue.put((sentence, pcm))
                if not produced:
                    # No exception AND no words used to be total silence: the live
                    # symptom was "hears me and ignores me". Say so instead.
                    failed = True
                    print("[LOCAL] Brain returned nothing.")
                    await say_failure()
            except Exception as e:
                failed = True
                print(f"[LOCAL] Brain failed: {type(e).__name__}: {e}")
                await say_failure()
            finally:
                if not asyncio.current_task().cancelling():
                    await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while (item := await queue.get()) is not None:
                sentence, pcm = item
                reply.append(sentence)
                self.ui.write_log(f"Era: {sentence}")
                await self._local_play(pcm)
        finally:
            self._local_brain.cancel()
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            if not self.ui.muted and not self._standby_active:
                self.ui.set_state("LISTENING")
        spoken = " ".join(reply)
        if spoken:
            self._tg_ack(spoken)
            try:
                self._note_era_question(spoken)
            except Exception:
                pass
        # Claude voice mode: two failed turns in a row end the Claude window, so a
        # brain that cannot answer is not the one left in charge. A no-op unless this
        # loop is serving Claude (the mixin is optional here).
        turn_result = getattr(self, "claude_turn_result", None)
        if turn_result is not None:
            try:
                if await turn_result(not failed):
                    await self._local_say(
                        "Claude isn't answering either, sir. Trying Gemini again."
                    )
            except Exception as e:  # never let bookkeeping break a spoken turn
                print(f"[LOCAL] claude_turn_result failed: {type(e).__name__}: {e}")
        # Flush any deferred tool failure notices (board item 19)
        try:
            self._flush_failure_notices()
        except Exception:
            pass
        return spoken

    def _local_cancel_turn(self) -> None:
        task = getattr(self, "_local_turn_task", None)
        if task is not None and not task.done():
            brain = getattr(self, "_local_brain", None)
            if brain is not None:
                brain.cancel()  # thread-safe (an Event)
            # trigger_abort may call this from the typed-input thread, and a
            # Task may only be cancelled on its own loop.
            try:
                on_loop = asyncio.get_running_loop() is task.get_loop()
            except RuntimeError:
                on_loop = False
            if on_loop:
                task.cancel()
            else:
                task.get_loop().call_soon_threadsafe(task.cancel)

    async def _local_play(self, pcm: bytes) -> None:
        """Play 24 kHz int16 PCM on Era's voice device, 100 ms at a time so a
        barge-in or stop takes effect within a slice."""
        if not pcm:  # Fish AND Kokoro failed: the reply is text only - say so once
            from core.local_tts import voice_unavailable_notice

            if notice := voice_unavailable_notice():
                self.ui.write_log(notice)
            return
        device = None
        try:
            from core import voice_output

            device = voice_output.resolve_output_index()
        except Exception:
            pass
        # Serialized: a speak() (Gmail alert, confirmation preview) racing the
        # turn's own playback used to end "speaking" while the other stream was
        # still playing, so the mic transcribed Era's own voice as the user's.
        lock = getattr(self, "_local_play_lock", None)
        if lock is None:
            lock = self._local_play_lock = asyncio.Lock()
        async with lock:
            await self._local_play_locked(pcm, device)

    async def _local_play_locked(self, pcm: bytes, device) -> None:
        det = getattr(self, "_barge_in", None)
        self._local_speaking = True
        if det is not None:
            det.note_speaking(True)
        self.ui.set_state("SPEAKING")
        # Falling back to the default device is allowed - losing the utterance is
        # not - but it must be VISIBLE. This used to be a bare `except Exception`
        # that silently reopened on the default, so a pinned device that failed
        # to open sent Era's voice to the laptop while `voice_output.status()`
        # still reported the pin and every reply read as success. Report the real
        # route, and say which device actually got the audio.
        opened_device, on_fallback = device, False
        try:
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS, dtype="int16", device=device
            )
        except Exception as open_err:
            opened_device, on_fallback = None, device is not None
            if on_fallback:
                print(
                    f"[era] [LOCAL] voice device {device!r} wouldn't open "
                    f"({open_err}); using the default output for this utterance."
                )
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS, dtype="int16"
            )
        try:
            from core import voice_output as _vo

            _vo.note_active_route(opened_device, fallback=on_fallback)
        except Exception:
            pass
        try:
            with stream:
                for i in range(0, len(pcm), _PLAY_SLICE):
                    await asyncio.to_thread(stream.write, pcm[i : i + _PLAY_SLICE])
        finally:
            self._local_speaking = False
            if det is not None:
                det.note_speaking(False)
            # She just answered: the conversation window restarts now
            # (core/attention.py). Missing here, it expired mid-conversation.
            listening = getattr(self, "_listening_state", None)
            if listening is not None:
                try:
                    listening().note_activity()
                except Exception:
                    pass
            # Drop her own voice: frames captured while she spoke (an awaited
            # "Yes, sir." holds up the frame loop) would otherwise be
            # transcribed afterwards as if the user had said them.
            self._local_listener.reset()
            frames = getattr(self, "_local_frames", None)
            while frames is not None and not frames.empty():
                frames.get_nowait()

    async def _local_say(self, text: str) -> None:
        from core.local_tts import guess_lang, synthesize_best

        text = sanitize_for_tts(text)
        if not text:
            return
        pcm, _ = await synthesize_best(text, guess_lang(text), allow_fish=self._local_fish_ok())
        await self._local_play(pcm)

    # ── tools: the same gate Gemini's calls take ────────────────────────────
    async def _local_execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        from google.genai import types

        from core.panic_stop import ABORT_BLOCK_MESSAGE, panic

        # The Gemini path checks the abort latch in _run_tool_call_batch, which
        # this bridge does not go through — so it is checked here.
        if panic.active():
            return {"success": False, "error": ABORT_BLOCK_MESSAGE, "error_type": "BlockedAction"}
        if name == "confirm_pending_confirmation" and not user_approved(self):
            return {
                "success": False,
                "error": "The user has not approved this. Ask them; do not confirm.",
                "error_type": "BlockedAction",
            }
        # Re-calling the armed tool with confirmed=true + its id is the other
        # way to approve; that is the user's to do, never the model's.
        args = {k: v for k, v in args.items() if k not in ("confirmed", "confirmation_id")}
        fc = types.FunctionCall(name=name, args=args, id=f"local-{uuid.uuid4().hex[:10]}")
        response = await self._execute_tool(fc)
        payload = getattr(response, "response", None)
        return dict(payload) if isinstance(payload, dict) else {"result": str(payload)}

    # ── typed / Telegram / speak() while local ──────────────────────────────
    def local_submit_text(self, text: str) -> bool:
        """From _on_text_command (any thread) when there is no session. True if
        the local brain took it."""
        if not self.local_mode_active() or not self._loop:
            return False
        decision = self.safety_gate.validate_input(
            transcript=text,
            confidence=None,
            state_snapshot=self._text_state_snapshot(),
            typed=True,
        )
        if decision.action == "BLOCK_SILENTLY":
            self.safety_gate.record_blocked(text)
            self._tg_ack(f"I couldn't act on that right now — {decision.reason.lower()}.")
            return True
        self._note_user_utterance(text)
        self._last_interaction_timestamp = time.time()
        self.ui.write_log(f"You: {text}")
        # A pasted path / link / blob is DATA, not an order: framed exactly as
        # _on_text_command and cm_flow frame it for Gemini, so instructions
        # inside a pasted email cannot drive the local model's tools.
        prompt = text
        kind = self.safety_gate.classify_typed_input(text, self._text_state_snapshot())
        if kind in ("PATH", "URL", "TEXT_BLOB") or decision.action == "ASK_CLARIFICATION":
            from app.text_intents import _build_paste_clarify_instruction

            prompt = _build_paste_clarify_instruction(
                kind if kind in ("PATH", "URL", "TEXT_BLOB") else "UNCLEAR", text
            )

        async def run() -> None:
            previous = self._local_turn_task
            self._local_cancel_turn()
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            self._local_turn_task = asyncio.create_task(self._local_turn(prompt))
            await asyncio.gather(self._local_turn_task, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(run(), self._loop)
        return True

    def local_speak(self, text: str) -> bool:
        """speak() while local: literal speech through Fish/Kokoro. True if taken."""
        if not self.local_mode_active() or not self._loop:
            return False
        asyncio.run_coroutine_threadsafe(self._local_say(text), self._loop)
        return True
