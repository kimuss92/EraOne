"""Claude voice mode — Era answers through the Claude subscription CLI when
Gemini Live is not answering.

WHAT HAPPENS
  1. core/live_health.py watches the Gemini session. It says "deaf" (the user
     spoke, Gemini never transcribed it), "mute" (transcribed, never answered)
     or "flaky" (repeated upstream errors). Or the user says "use Claude".
  2. `enter_claude_voice()` opens a time window and sets the go-away event, which
     makes the Gemini session end the way a routine rotation does.
  3. The run loop calls `serve_claude_voice()` before every Gemini connect. While
     the window is open it runs the SAME local loop as the offline fallback
     (app/era_live/local_fallback.py): mic -> Silero VAD -> Whisper -> brain ->
     Fish/Piper, with the owner-voice gate, wake word, abort, mic-mute, barge-in
     and the tool gate all unchanged. Only the brain differs: `claude -p`
     (core/llm/adapters/claude_voice.py) — subscription, no API key, no GPU.
  4. The window ends, the loop reconnects Gemini, and `live_health` judges it
     afresh. If Gemini fails again the next window is twice as long (capped), so
     a Gemini that is down for an hour does not get re-tried every 3 minutes with
     a spoken announcement each time.

DEGRADE, NEVER RAISE
  Not enabled, CLI missing, or the local ears/voice not installed -> every method
  here returns False/None and Era behaves exactly as before. Nothing in this file
  may take the run loop down.

NOT PROVEN LIVE
  The pieces are unit-tested with fakes and the Claude brain was run against the
  real CLI, but switching mid-conversation on a real Gemini failure has not been
  observed on Karim's machine. `engine.claude_voice.*` events in
  logs/eraone.log are how to see it happen.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from core.logger import log_event, main_logger

CLAUDE = "claude"
CLAUDE_MANUAL = "claude_manual"

# "use Claude", "switch to Claude voice", "go to the backup brain", "passe sur
# Claude". Anchored to a WHOLE order so a sentence that merely mentions Claude
# ("ask Claude about this") never flips the voice.
_ORDER = re.compile(
    r"^\W*(?:era[,\s]+)?(?:please\s+)?"
    r"(?:(?:switch|change|go|use|turn|answer|reply|respond)(?:\s+over)?\s+"
    r"(?:to\s+|on\s+|with\s+)?(?:your\s+|the\s+|my\s+)?"
    r"(?:claude|backup)(?:\s+(?:brain|voice|mode))?"
    r"|(?:passe|bascule|utilise)\s+(?:sur\s+|en\s+|avec\s+)?claude)"
    r"(?:\s+please)?\W*$",
    re.IGNORECASE,
)


def claude_voice_order(text: str) -> bool:
    """True when the whole utterance is an order to use Claude as the voice brain."""
    return bool(_ORDER.match(text or ""))


class ClaudeVoiceMixin:
    # ── policy / readiness ──────────────────────────────────────────────────
    def _claude_policy(self) -> dict[str, Any]:
        try:
            from core.llm.router import claude_voice_policy
        except ImportError as exc:
            # A MISSING function is a code bug, not a config choice: it used to hide
            # behind the bare except below, which left this whole mode silently dead.
            log_event(
                main_logger,
                "engine.claude_voice.policy_missing",
                "WARNING",
                error=str(exc),
                note="claude_voice_policy is missing from core.llm.router - "
                "code bug, not config. Fallback disabled.",
            )
            return {"enabled": False}
        try:
            return claude_voice_policy()
        except Exception as exc:
            log_event(
                main_logger,
                "engine.claude_voice.policy_raised",
                "WARNING",
                error=f"{type(exc).__name__}: {exc}",
            )
            return {"enabled": False}

    def claude_voice_ready(self) -> tuple[bool, str]:
        if not self._claude_policy().get("enabled"):
            return False, "claude voice disabled (routing.claude_voice / ERA_CLAUDE_VOICE)"
        from core import claude_headless, local_stt, local_tts

        if not claude_headless.available():
            return False, "the claude CLI is not available"
        for ok, why in (local_stt.available(), local_tts.available()):
            if not ok:
                return False, why
        return True, ""

    def _in_claude(self) -> bool:
        """This local loop is serving Claude voice mode."""
        return self.local_mode_active() and getattr(self, "_local_outage_reason", "") in (
            CLAUDE,
            CLAUDE_MANUAL,
        )

    def _claude_brain(self):
        brain = getattr(self, "_claude_brain_obj", None)
        if brain is None:
            from core.llm.adapters.claude_voice import ClaudeVoiceProvider
            from core.local_brain import CASCADE_SYSTEM_PROMPT, LocalBrain

            pol = self._claude_policy()
            brain = self._claude_brain_obj = LocalBrain(
                provider=ClaudeVoiceProvider(),
                alias=str(pol.get("alias") or "era.cheap"),
                history_turns=int(pol.get("history_turns") or 6),
                max_tool_rounds=int(pol.get("max_tool_rounds") or 3),
                timeout_s=float(pol.get("timeout_s") or 45),
                system_prompt=CASCADE_SYSTEM_PROMPT,
            )
        return brain

    # ── entering the mode ───────────────────────────────────────────────────
    def enter_claude_voice(self, reason: str, detail: str = "", *, manual: bool = False) -> bool:
        """Open a Claude window and make the Gemini session wind down.

        False (and nothing changes) when the mode is unavailable — the caller
        must then carry on with Gemini exactly as it would have.
        """
        ok, why = self.claude_voice_ready()
        if not ok:
            if why != getattr(self, "_claude_last_why", None):
                self._claude_last_why = why
                print(f"[CLAUDE-VOICE] Not used: {why}")
            return False
        self._claude_last_why = ""
        pol = self._claude_policy()
        now = time.monotonic()
        if not manual and now - getattr(self, "_claude_failed_at", -1e9) < 300:
            # Claude itself just failed twice; switching back to it on a Gemini
            # hiccup would only bounce the user between two silent brains.
            print("[CLAUDE-VOICE] Not switching: Claude failed a moment ago.")
            return False
        if manual:
            stay = float(pol.get("manual_stay_s") or 3600)
        else:
            base = float(pol.get("stay_s") or 180)
            healthy = float(pol.get("healthy_after_s") or 600)
            last_at = getattr(self, "_claude_last_entry", None)
            prev = getattr(self, "_claude_stay_s", 0.0)
            # Gemini failed again soon after the last window: double it.
            stay = (
                base
                if last_at is None or now - last_at > healthy + prev
                else min(max(prev, base) * 2, float(pol.get("max_stay_s") or 1800))
            )
        self._claude_until = now + stay
        self._claude_stay_s = stay
        self._claude_last_entry = now
        self._claude_manual = manual
        try:
            self.live_health.clear_errors()  # Gemini gets a fresh start after the window
            self.live_health.reset()
        except Exception:
            pass
        log_event(
            main_logger,
            "engine.claude_voice.enter",
            "INFO",
            reason=reason,
            detail=detail,
            stay_s=round(stay),
            manual=manual,
        )
        print(f"[CLAUDE-VOICE] Switching to Claude for {stay / 60:.0f} min ({reason}: {detail})")
        # Same mechanism as Gemini's own GoAway: every session task winds down and
        # the run loop reconnects at once — and reaches serve_claude_voice first.
        self._go_away_event.set()
        # The event alone is not enough: the receive task is parked waiting for
        # Gemini's next message, so on 2026-10-03 "switching" happened ~3 minutes
        # late, after Gemini rotated the session itself, and the window was
        # nearly spent. End the tasks now.
        self._end_gemini_session()
        return True

    def _end_gemini_session(self) -> None:
        """Cancel the live session's tasks so the run loop gets control at once.

        Safe from any thread (the typed "use Claude" arrives on one). A cancelled
        child does not abort the TaskGroup: it ends normally once every child is
        done, and the run loop then goes back to the top — to serve_claude_voice.
        """
        tasks = list(getattr(self, "_session_tasks", None) or [])
        try:
            me = asyncio.current_task()
        except RuntimeError:
            me = None
        loop = getattr(self, "_loop", None)
        for task in tasks:
            if task is me or task.done():
                continue
            if loop is not None:
                loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()

    async def claude_turn_result(self, ok: bool) -> bool:
        """Called by the local loop after every turn. True = this failure ended
        the Claude window (two in a row), so the caller says so and Gemini is
        retried. A brain that cannot answer must not be the one left in charge."""
        if not self._in_claude():
            return False
        if ok:
            self._claude_fail_streak = 0
            return False
        self._claude_fail_streak = getattr(self, "_claude_fail_streak", 0) + 1
        if self._claude_fail_streak < 2:
            return False
        self._claude_fail_streak = 0
        self._claude_failed_at = time.monotonic()
        log_event(main_logger, "engine.claude_voice.gave_up", "WARNING", reason="two failed turns")
        self.release_claude_voice()
        self._local_exit.set()
        return True

    def release_claude_voice(self) -> None:
        """ "Reconnect": the user wants Gemini back, so the window closes now."""
        self._claude_until = 0.0

    # ── entry point from the run loop ───────────────────────────────────────
    async def serve_claude_voice(self) -> bool:
        """Serve Claude voice while its window is open. False = connect Gemini."""
        remaining = getattr(self, "_claude_until", 0.0) - time.monotonic()
        if remaining <= 0:
            return False
        ok, why = await asyncio.to_thread(self.claude_voice_ready)
        if not ok:
            print(f"[CLAUDE-VOICE] Cannot serve: {why}")
            self._claude_until = 0.0
            return False
        reason = CLAUDE_MANUAL if getattr(self, "_claude_manual", False) else CLAUDE
        try:
            await self._run_local_fallback(remaining, reason)
        except Exception as e:  # never take the run loop down — hand back to Gemini
            print(f"[CLAUDE-VOICE] Loop failed ({type(e).__name__}: {e}); back to Gemini.")
            self._claude_until = 0.0
            return False
        # _local_outage_reason is left set on purpose: the next successful Gemini
        # connect calls note_cloud_restored(), which logs "Back on Gemini".
        return True

    # ── the watchdog, run inside the Gemini session's task group ────────────
    async def _live_health_watch(self) -> None:
        """Ends the Gemini session when it is connected but not answering."""
        from core import live_health

        if not live_health.enabled():
            return
        while not self._shutdown_requested and not self._go_away_event.is_set():
            await asyncio.sleep(1.0)
            if self._standby_active or getattr(self, "_mic_muted", False):
                self.live_health.reset()  # nobody is expected to be answered
                continue
            verdict = self.live_health.verdict()
            if verdict is None:
                continue
            log_event(
                main_logger,
                "engine.claude_voice.verdict",
                "WARNING",
                kind=verdict.kind,
                detail=verdict.detail,
            )
            if self.enter_claude_voice(verdict.kind, verdict.detail):
                return
            # Unavailable: don't re-judge the same silence. reset() keeps the
            # upstream-error history on purpose, so a "flaky" verdict re-fired
            # every second (437 lines on 2026-10-08); nothing can act on it now.
            self.live_health.reset()
            self.live_health.clear_errors()
