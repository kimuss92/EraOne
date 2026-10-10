"""Reconnect / standby / status-reporting mixin for EraLive (extracted from main.py)."""

import asyncio
import json
import os
import random
import socket
import sys
import time

from app.reconnect import reconnect_acknowledgment_text
from app.text_intents import build_disconnect_resume_note
from core.era_state import EraState
from core.live_turn_state import decide_recovery, recovery_context
from core.logger import log_event, main_logger


class ReconnectMixin:
    def _compute_reconnect_delay(self, attempt_number: int) -> float:
        base_delay: float = 3.0
        exponential: float = base_delay * (2**attempt_number)
        capped: float = min(exponential, 60.0)
        jitter: float = random.uniform(0.1, 1.5)
        return capped + jitter

    async def _interruptible_sleep(self, total_seconds: float) -> None:
        """Sleep up to ``total_seconds`` but wake early on shutdown or a GoAway.

        Used for the long quota cooldown so a stop request or a fresh rotation
        signal doesn't have to wait out the whole (possibly 2-hour) delay.
        """
        deadline = time.time() + total_seconds
        while time.time() < deadline:
            if self._shutdown_requested or self._go_away_event.is_set():
                return
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.time())))

    async def _monitor_idle_state(self) -> None:
        while True:
            # Sleep in small slices so a GoAway teardown is observed promptly
            # and this task exits with the rest of the TaskGroup.
            for _ in range(50):  # 50 * 0.1s = 5s
                if self._go_away_event.is_set():
                    return
                await asyncio.sleep(0.1)
            if self._standby_active:
                continue
            # A voice-muted mic is the user's deliberate quiet mode, not idleness:
            # idling into standby would speak "System offline." out of nowhere.
            if getattr(self, "_mic_muted", False):
                self._last_interaction_timestamp = time.time()
                continue
            # Never treat an in-flight turn as idle, even if a single tool
            # call runs longer than the idle timeout.
            current_state = self.session_manager.get_state_snapshot().get("current_state")
            if current_state in (EraState.PROCESSING, EraState.SPEAKING) or self._is_speaking:
                self._last_interaction_timestamp = time.time()
                continue
            idle_seconds: float = time.time() - self._last_interaction_timestamp
            if idle_seconds > self._idle_timeout_seconds:
                print(f"[IDLE] No interaction for {idle_seconds:.0f}s. Entering standby.")
                self.ui.write_log(f"SYS: Idle for {idle_seconds:.0f}s. Entering standby.")
                self.enter_standby(network_failure=False)
                # Block until user wakes the system
                while self._standby_active:
                    if self._go_away_event.is_set():
                        return
                    await asyncio.sleep(1.0)

    async def _resume_from_standby(self, reason: str, speak_ack: bool = True) -> None:
        """Centralized resume from standby. Called by Vosk wake, F4, remote wake, and UI unmute."""
        if not self._standby_active:
            return
        print(f"[WAKE] Resume started: {reason}")

        # --- Reset wake arming state ---
        self._wake_armed = False
        self._wake_ignore_until = 0.0
        self._last_wake_partial_text = ""
        self._last_wake_partial_count = 0

        self._standby_active = False
        self._reconnect_attempts = 0
        self._last_interaction_timestamp = time.time()
        # A wake opens the conversation window (core/attention.py): the user's
        # next words need no name.
        listening = getattr(self, "_listening_state", None)
        if listening is not None:
            listening().note_activity(explicit=True)  # a wake is deliberate
        self._stop_wake_word_listener()
        # Discard any stale audio (e.g. a delayed pre-standby turn) so the
        # first thing the user hears on wake is the resume ack, not an echo.
        flushed = self._flush_audio_in_queue()
        if flushed:
            print(f"[WAKE] Discarded {flushed} stale audio chunks before resume ack")

        self.ui.set_mute(False)
        self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Resumed from standby ({reason}).")
        if getattr(self, "_mic_muted", False):
            # Waking never reopens a voice-muted mic — only a mic-on order does.
            self.ui.write_log(
                "SYS: Microphone is still off. Say 'start listening' to turn it back on."
            )

        self.session_manager.wake_word_active = reason.startswith("wake_word")
        # Mic must always be active on standby exit so voice wake works.
        if hasattr(self, "_handle_set_microphone"):
            try:
                self._handle_set_microphone({"state": "on"})
            except Exception:
                pass
        # Force microphone on regardless of prior mute state
        self._mic_muted = False
        try:
            from core.user_prefs import set_mic_muted

            set_mic_muted(False)
        except Exception:
            pass
        if reason.startswith("wake_word"):
            self.session_manager.last_wake_word_timestamp = time.time()
        self.session_manager.transition_to(EraState.LISTENING, reason=reason)

        # --- Safe speak: skip if session is unhealthy ---
        should_speak = (
            speak_ack
            and self.session is not None
            and not self._go_away_event.is_set()
            and not self.session_manager.get_state_snapshot().get("is_reconnecting", False)
        )
        if should_speak:
            try:
                # Her own brief words (Karim, 2026-10-10), not the pinned
                # "Era One online." line: one short sentence, never the wake word.
                # The silent-reconnect-on-network-blip path stays silent on purpose.
                self.speak_brief(
                    "You have just woken up from standby and are listening again.",
                    fallback="I'm here, sir.",
                )
                for _ in range(30):
                    if self._is_speaking:
                        break
                    await asyncio.sleep(0.1)
                while self._is_speaking:
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[WAKE] Resume speak error: {e}")

        # A CM status report can finish while the user was in standby, same as
        # across a network outage — deliver it right after the resume ack
        # instead of leaving it queued until some unrelated future reconnect.
        # Kept OUT of the pinned "_RESUME_ACK, nothing else" system-note above:
        # that note is verbatim-enforced, so the report goes out as its own
        # follow-up turn instead of risking Gemini folding it into the ack.
        if self._pending_cm_announcement:
            note = self._pending_cm_announcement
            self._pending_cm_announcement = None
            self._deliver_or_queue_cm_report(f"While you were away, sir: {note}", verbatim=True)

        print(f"[WAKE] Resume complete: {reason}")

    def _exit_standby(self):
        if not self._standby_active:
            return
        print("[STANDBY] Exiting standby via F4 toggle.")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._resume_from_standby("f4_wake", speak_ack=True), self._loop
            )

    @staticmethod
    def _flatten_exc(exc: BaseException) -> list[BaseException]:
        """Flatten an ExceptionGroup into its concrete leaf exceptions.

        The session lives in an asyncio.TaskGroup, so a websocket close surfaces
        here wrapped in an ExceptionGroup whose str() is only "unhandled errors
        in a TaskGroup (N sub-exceptions)" — the close code is in the leaves.
        Matching on str(group) therefore never sees it.
        """
        leaves: list[BaseException] = []
        stack = [exc]
        while stack:
            e = stack.pop()
            subs = getattr(e, "exceptions", None)
            if subs:
                stack.extend(subs)
            else:
                leaves.append(e)
                if e.__cause__ is not None:
                    stack.append(e.__cause__)
        return leaves

    def set_status(self, status: str) -> None:
        """Publish a simple, human-readable connection status.

        Shows it in the on-screen log and mirrors it to ~/.era/era_connection.json
        so external tools/UI can read the current state. Deliberately a SEPARATE
        file from era_status.json (which the CM dispatch layer owns for job state).
        Best-effort: never raises.
        """
        if status == self._connection_status:
            # Still refresh the timestamp on disk, but skip the noisy log line.
            self._write_status_file(status)
            return
        self._connection_status = status
        try:
            from app.hud.events import hud

            hud.connection(status)
        except Exception:
            pass
        try:
            self.ui.write_log(f"NET: {status}")
        except Exception:
            pass
        self._write_status_file(status)

    def _write_status_file(self, status: str) -> None:
        try:
            self._connection_status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._connection_status_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"status": status, "updated_at": time.time()}),
                encoding="utf-8",
            )
            os.replace(tmp, self._connection_status_path)
        except OSError:
            pass

    @staticmethod
    def _is_config_rejection(text: str) -> bool:
        """True if `text` (already lowercased) is the server refusing our CONFIG.

        Shared by _classify_disconnect and _is_expected_rotation so the two can
        never disagree about whether a 1008/1007 is routine or fatal.
        """
        return (
            "not supported by the model" in text
            or "response modalities" in text
            or "invalid_argument" in text
            or "is not found for api version" in text
            or "not supported for bidigeneratecontent" in text
        )

    @staticmethod
    def _classify_disconnect(exc: BaseException) -> str:
        """Classify why the Live session dropped, so run() can react correctly.

        Returns one of:
          - "rotation"  : Gemini's routine ~2.5min 1008 session rotation (not a failure)
          - "quota"     : 1011 / RESOURCE_EXHAUSTED quota exhaustion — needs a long cooldown
          - "config"    : the connect CONFIG was rejected — retrying is futile
          - "auth"      : invalid API key — retrying fast won't help
          - "transient" : anything else (network blip, timeout, service unavailable)

        The close reason can be nested inside a TaskGroup ExceptionGroup, so we
        inspect every leaf (reusing _flatten_exc).

        1007 is the WebSocket *invalid frame payload* close code, and Gemini
        uses it for CONFIG rejections, not just bad credentials — e.g.
        "The requested combination of response modalities (TEXT) is not
        supported by the model". Treating a bare 1007 as "auth" told the user to
        go fix a perfectly valid gemini_api_key while the real fault was the
        requested modality, so the config case is matched FIRST and only genuine
        credential wording still classifies as auth.
        """
        for leaf in ReconnectMixin._flatten_exc(exc):
            text = str(leaf).lower()
            # Checked BEFORE rotation: Gemini reports an unknown/retired model id
            # as 1008 too, and the bare close code is indistinguishable from a
            # routine rotation. Classifying that as "rotation" would reconnect
            # forever in silence instead of reporting a model that no longer
            # exists — the same silent-rot failure mode as a backend that keeps
            # answering while doing nothing useful.
            if ReconnectMixin._is_config_rejection(text):
                return "config"
            if "1008" in text or "policy violation" in text or "operation was aborted" in text:
                return "rotation"
            if (
                "resource has been exhausted" in text
                or "resource_exhausted" in text
                or "exceeded your current quota" in text
                or "quota" in text
                or "429" in text
            ):
                return "quota"
            if (
                "api key not valid" in text
                or "api_key_invalid" in text
                or "invalid api key" in text
                or "permission_denied" in text
                or "unauthenticated" in text
            ):
                return "auth"
        return "transient"

    @classmethod
    def _is_expected_rotation(cls, exc: BaseException) -> bool:
        """True if `exc` is Gemini's periodic Live-session rotation.

        Gemini terminates a Live session roughly every ~2.5 min with a 1008
        (policy violation) "The operation was aborted" close. It is routine, not
        a failure: run() reconnects with the resumption token and the
        conversation continues. Logging it as a crash produced hundreds of false
        ERRORs that in turn poisoned Era's own self-diagnosis.

        A 1008 that carries CONFIG wording is NOT a rotation, though: an unknown
        or retired model id reports the same close code, and treating it as
        routine would reconnect silently forever instead of surfacing a model
        that no longer exists. This runs ahead of _classify_disconnect in the
        connect loop, so the exclusion has to live here too.
        """
        for leaf in cls._flatten_exc(exc):
            text = str(leaf).lower()
            if cls._is_config_rejection(text):
                return False
            if "1008" in text or "policy violation" in text or "operation was aborted" in text:
                return True
        return False

    @classmethod
    def _is_interpreter_shutdown(cls, exc: BaseException) -> bool:
        """True if `exc` means the interpreter is tearing down, not that a
        connect failed.

        The engine runs in a DAEMON thread, so when the Tk mainloop returns the
        main thread runs threading._shutdown() -> concurrent.futures' atexit
        hook, which sets a global "no new futures" flag and then joins every
        executor worker. If one of those joins blocks, the interpreter never
        reaches Py_Finalize -- and a daemon thread is only killed at
        Py_Finalize, so this loop keeps running inside a half-dead process.

        Every connect then dies instantly and identically: the websocket needs
        a DNS lookup, getaddrinfo goes through loop.run_in_executor(None, ...),
        and that submit() raises `RuntimeError: cannot schedule new futures
        after interpreter shutdown`. It can never recover, because nothing
        clears that flag.

        On 2026-09-21 this produced 177 ERROR-level engine.reconnect_error
        entries with full tracebacks over 2.5 hours (12:03 -> 14:31) from a
        process whose UI had already closed at 12:02 -- while a SECOND Era was
        started on top of it at 12:12. It was the single largest signature in
        the DEGRADED verdict, and not one of them was a connection fault.
        """
        if sys.is_finalizing():
            return True
        for leaf in cls._flatten_exc(exc):
            if isinstance(leaf, RuntimeError) and "interpreter shutdown" in str(leaf).lower():
                return True
        return False

    @classmethod
    def _is_network_blip(cls, exc: BaseException) -> bool:
        """True if `exc` is the network dropping under us, not Era malfunctioning.

        Severity only — this deliberately does NOT change how the disconnect is
        classified or recovered (_classify_disconnect still calls these
        "transient" and the reconnect loop handles them exactly as before).

        A Wi-Fi drop or DNS hiccup kills the Live websocket mid-`receive()`, and
        google-genai reports it as `APIError: 1006 ... abnormal closure`,
        underneath which sits `ConnectionResetError [WinError 64]`. That was
        logged as a CRITICAL `engine.crash` with a full traceback, so a flaky
        connection produced a stream of fake crashes — which then drove Era's
        own self-diagnosis to a "Degraded" verdict with no real defect behind
        it. The same reasoning already applies to rotation and quota: expected,
        recoverable conditions get logged quietly.
        """
        for leaf in cls._flatten_exc(exc):
            text = str(leaf).lower()
            if any(
                marker in text
                for marker in (
                    "1006",
                    "abnormal closure",
                    "no close frame received or sent",
                    "connection reset",
                    "winerror 64",
                    # WinError 1231/1232 and WSA 10051/10065: "the network
                    # location cannot be reached" when Wi-Fi is down. Logged as
                    # engine.reconnect_error until 2026-09-16.
                    "winerror 1231",
                    "winerror 1232",
                    "winerror 10051",
                    "winerror 10065",
                    "network location cannot be reached",
                    "network name is no longer available",
                    "getaddrinfo failed",
                    "temporary failure in name resolution",
                    "name or service not known",
                    "connection closed",
                    "server disconnected",
                )
            ):
                return True
            if isinstance(
                leaf, (ConnectionError, socket.gaierror, TimeoutError, asyncio.TimeoutError)
            ):
                return True
        return False

    # ── Continuity across a drop ──────────────────────────────────────────
    #
    # THE PROBLEM THESE THREE SOLVE
    #   The resumption handle restores what the MODEL knows. It does nothing
    #   about the reply that was half-spoken when the socket died: the audio
    #   stops mid-word, the new session comes up holding the full conversation,
    #   and then sits there. The user has to notice the silence and say "carry
    #   on" — every single time. That is the whole of "it reconnects and doesn't
    #   continue speaking unless I ask it to".
    #
    #   So the drop is treated like a barge-in that the user did not choose:
    #   stash the unfinished thought on the way down (_capture_turn_cut_by_drop)
    #   and hand it straight back on the way up (_resume_after_disconnect).

    @classmethod
    def _is_resumption_rejection(cls, exc: BaseException) -> bool:
        """True if the server refused our RESUMPTION HANDLE specifically.

        A handle expires, or belongs to a session the server has since dropped.
        Replaying it then fails identically on every retry — and because a
        rejected handle reports INVALID_ARGUMENT, _classify_disconnect calls it
        "config" and parks Era on a 15-minute re-check loop with the same dead
        handle in hand. The feature meant to survive a reconnect becomes the one
        thing preventing one. Detected here, the handle is dropped once and the
        next attempt starts clean.

        Only ever consulted when we actually connected WITH a handle, so the
        broad INVALID_ARGUMENT match cannot swallow a genuine config fault on a
        cold start.
        """
        for leaf in cls._flatten_exc(exc):
            text = str(leaf).lower()
            if (
                "resum" in text
                or "handle" in text
                or "invalid_argument" in text
                or "not_found" in text
                or "not found" in text
            ):
                return True
        return False

    # How close to the server's stated deadline we are willing to cut it. The
    # reconnect itself takes a moment (DNS + TLS + setup), so taking the renewal
    # right on the deadline would mean being closed mid-handshake and falling
    # into the error path anyway — which is the abrupt outcome the planned
    # renewal exists to avoid.
    RENEWAL_MARGIN_S = 2.0

    def _tool_is_running(self) -> bool:
        """True while any tool call is mid-execution. See _execute_tool."""
        return getattr(self, "_tools_in_flight", 0) > 0

    @staticmethod
    def _go_away_seconds(time_left) -> float | None:
        """GoAway.timeLeft -> seconds. None when it cannot be read.

        The SDK surfaces a protobuf Duration, which arrives variously as a
        timedelta, as a number, or as a "10s" string depending on version and
        transport. None means "the server did not tell us how long", and the
        caller then treats the renewal as immediate rather than inventing a
        deadline it cannot support.
        """
        if time_left is None:
            return None
        total = getattr(time_left, "total_seconds", None)
        if callable(total):
            try:
                return float(total())
            except Exception:
                return None
        if isinstance(time_left, (int, float)):
            return float(time_left)
        text = str(time_left).strip()
        if text.endswith("s"):
            text = text[:-1]
        try:
            return float(text)
        except ValueError:
            return None

    def _should_take_renewal_now(self, tool_running: bool = False) -> tuple[bool, str]:
        """Is now the moment to close and reconnect on the server's warning?

        Preference order, per
        https://ai.google.dev/gemini-api/docs/live-api/session-management:
          * idle (generation finished, speaker drained, no tool) -> take it now;
            nothing audible is lost and the next connect starts clean;
          * out of time -> take it anyway and let the recovery path finish the
            answer, which is strictly better than being cut off mid-handshake;
          * otherwise -> stay on this connection a little longer.

        Deliberately NOT driven by a fixed interval: the server states its own
        deadline and the model states its own idleness, so both come from the
        protocol rather than from a constant that drifts out of date.
        """
        deadline = getattr(self, "_renewal_deadline", None)
        if deadline is None:
            return False, "no renewal pending"
        if self.live_turn.is_idle_for_renewal(tool_running=tool_running):
            return True, "idle between turns"
        remaining = deadline - time.time()
        if remaining <= self.RENEWAL_MARGIN_S:
            return True, f"deadline in {remaining:.1f}s — taking it mid-turn"
        return False, f"busy; {remaining:.1f}s of grace left"

    def _capture_turn_cut_by_drop(self) -> bool:
        """Snapshot what was in flight when the transport died.

        Returns True if something recoverable was captured. Never raises: this
        runs on the failure path and a bookkeeping error there must not cost the
        reconnect itself.

        Captures the FULL turn state — generation_complete, interrupted, bytes
        received vs bytes played — not just the transcript, because the reconnect
        has to tell four different situations apart and the transcript alone
        cannot (see core/live_turn_state.decide_recovery). The legacy
        `turn_memory` stash is still written so the explicit "go on" path keeps
        working exactly as before.
        """
        try:
            if self._cm_handoff_active():
                # That speech belongs to the headless CM lane; it reports itself
                # through _deliver_or_queue_cm_report and must not be resumed
                # into the user's conversation.
                return False

            pending_user = (getattr(self, "_recap_pending_user", "") or "").strip()
            snap = self.live_turn.snapshot(pending_user=pending_user, now=time.time())
            self._disconnect_snapshot = snap
            self._user_spoke_since_drop = False

            log_event(
                main_logger,
                "engine.continuity.captured",
                "INFO",
                generated=snap.generated,
                interrupted=snap.interrupted,
                completed=snap.completed,
                audio_received=snap.audio_received,
                audio_played=snap.audio_played,
                outstanding_bytes=snap.playback_outstanding,
                had_question=bool(snap.pending_user),
                # Length only — never the conversation itself.
                spoken_chars=len(snap.spoken_text),
                conn_generation=self._conn_generation,
                conn_age_s=round(time.time() - (self._conn_started_at or time.time()), 1),
            )

            # Reset the LIVE state now that the facts are safely in the snapshot.
            # The turn belonged to a socket that no longer exists, and leaving it
            # set is not harmless: `is_generating` would still be True on the next
            # connect, decide_recovery would read that as "the session is already
            # generating", and it would decline to recover — reproducing the exact
            # silence this feature exists to remove. Caught by
            # test_the_stash_is_consumed_so_a_rotation_storm_cannot_repeat_it.
            self.live_turn.reset()

            if not snap.had_content:
                return False

            # Legacy stash, for the explicit "continue" command.
            spoken = snap.spoken_text
            try:
                remainder = self.speech_out.pending_text()
            except Exception:
                remainder = ""
            self.turn_memory.capture(
                spoken=spoken,
                remainder=remainder,
                cut_by="disconnect",
                pending_user=snap.pending_user,
            )
            self._live_turn_text = ""
            return True
        except Exception as err:  # noqa: BLE001
            print(f"[era] [CONTINUITY] capture skipped: {err}")
            return False

    async def _resume_after_disconnect(self) -> bool:
        """Decide what the reconnect owes the user, and do exactly that.

        Returns True only when a continuation was actually sent. The snapshot is
        consumed either way, so at most ONE continuation can ever follow a given
        drop — a rotation storm cannot make Era restate the same half-sentence
        on every reconnect.

        The four outcomes and why each exists are documented on
        core/live_turn_state.decide_recovery; this method is only the wiring.
        The one that matters for the reported bug is RESUME, and it is
        deliberately the LAST branch: every cheaper explanation for the silence
        is ruled out first.

        WHY THIS IS NOT THE 2026-07-21 APOLOGY RETURNING
          That spoke a QUESTION into the session and Gemini answered it, once
          acting on the topic it named. The note sent here contains no question,
          names no action and forbids tools — see build_disconnect_resume_note.
        """
        snap = self._disconnect_snapshot
        self._disconnect_snapshot = None
        try:
            decision = decide_recovery(
                snap,
                now=time.time(),
                # The user's own voice outranks any pending continuation.
                user_spoke_since=bool(self._user_spoke_since_drop)
                or self.live_turn.user_is_speaking,
                # Never start a second response on top of one already running.
                generation_active=self.live_turn.is_generating,
            )
            log_event(
                main_logger,
                "engine.continuity.decision",
                "INFO",
                action=decision.action,
                reason=decision.reason,
                conn_generation=self._conn_generation,
            )

            if not decision.should_send_continuation:
                print(f"[era] [CONTINUITY] {decision.action}: {decision.reason}")
                return False

            if self._standby_active or self.session is None or self._cm_handoff_active():
                return False

            said, unanswered = recovery_context(snap)
            note = build_disconnect_resume_note(said, "", unanswered)
            generation_at_send = self._conn_generation
            await self.session.send_realtime_input(text=note)
            # A connection that died between the decision and the send would make
            # this a no-op; recording the generation makes that visible in the log
            # rather than silent.
            self.turn_memory.clear()
            self.ui.write_log("SYS: Picking that back up, sir.")
            log_event(
                main_logger,
                "engine.continuity.resumed",
                "INFO",
                from_speech=bool(said),
                conn_generation=generation_at_send,
                age_s=round(time.time() - snap.ts, 1) if snap and snap.ts else None,
            )
            print("[era] [CONTINUITY] Resumed the interrupted reply automatically.")
            return True
        except Exception as err:  # noqa: BLE001
            print(f"[era] [CONTINUITY] auto-resume skipped: {err}")
            return False

    def _build_reconnect_acknowledgment(self, disconnect_kind: str) -> str | None:
        """What Era says on reconnect, in priority order. Silence is the
        default (see reconnect_acknowledgment_text) — the only two things
        that ever override it are a community-management job's own status,
        because that is real, factual information the user is owed, never a
        rhetorical question:

        1. A CM report that finished with no live session to speak it into —
           deliver it now as what-you-missed, then clear it. Unconditional:
           this is one-shot information that must reach the user the moment
           any session comes up, rotation or not.
        2. A CM job still running right now — say so honestly. Gated to
           non-rotation disconnects only: rotation reconnects every ~2.5 min,
           and a 30-minute job would otherwise repeat "still working" a
           dozen times before it's done — the exact chatter the
           silent-rotation policy exists to prevent.
        3. Otherwise, silence — reconnect_acknowledgment_text always returns
           None now (see its docstring for why the old "you were asking
           about X — want me to continue?" apology was removed entirely,
           not just filtered).

        The caller (run()) wraps whatever this returns in _say_exactly()
        before speaking it — never return a bare question or anything phrased
        so that Gemini could "answer" it instead of just saying it.
        """
        if self._pending_cm_announcement:
            note = self._pending_cm_announcement
            self._pending_cm_announcement = None
            return f"While you were disconnected, sir: {note}"

        if disconnect_kind != "rotation":
            try:
                import era_dispatch as ed

                busy = ed.current_job()
            except Exception:
                busy = None
            if busy:
                nice_brand = str(busy.get("brand") or "that brand").replace("_", " ")
                return (
                    f"Still working on {nice_brand}'s {busy.get('task_type', 'job')}, sir — "
                    "I'll let you know the moment it's done."
                )

        return reconnect_acknowledgment_text(disconnect_kind, self._recent_user_texts)
