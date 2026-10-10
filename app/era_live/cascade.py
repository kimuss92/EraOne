"""Cascade voice mode — Era WITHOUT the Gemini Live session.

WHY
  Gemini Live is one long websocket that the provider rotates, drops and
  quota-cuts. This mode has no session at all:

      mic -> Silero VAD -> Whisper-small int8 (CPU)       core/local_stt.py
          -> CodeCraft, streamed, with Era's gated tools   core/llm/adapters/openai_compat.py
          -> Fish (cloned voice), or Kokoro (local GPU)     core/local_tts.py
          -> speaker

  Each turn is one short HTTP request, so a failure costs a turn, never the
  conversation. It RUNS ON the local-fallback loop (app/era_live/local_fallback.py):
  same owner-voice gate, wake word, abort, mic-mute, barge-in and tool gate —
  only the brain and a few words differ. Nothing here runs on the GPU.

WHEN
  The run loop (main.py) calls serve_cascade() before every Gemini connect. It
  is taken only when routing.cascade is enabled AND CodeCraft is configured AND
  the local ears/voice models are present; otherwise it returns False and Gemini
  Live connects exactly as before. "Reconnect" said in this mode pauses it for
  the rest of the run, which hands Era back to Gemini Live.
"""

from __future__ import annotations

import os
import re
from typing import Any

CASCADE = "cascade"

# Spoken voice switch, checked before the brain sees the words. Local, instant,
# no model call. Anchored to a whole order so a passing mention never fires.
_VOICE_ORDER = re.compile(
    r"^\W*(?:era[,\s]+)?(?:please\s+)?(?:switch|change|go|use|turn)\s+"
    r"(?:back\s+)?(?:to\s+|on\s+)?(?:your\s+|my\s+|the\s+)?"
    r"(?P<which>fish(?:\s+audio)?|cloned?|own|primary|"
    r"premium|natural|default|normal|local|offline|kokoro)?"
    r"\s*voice\W*$|"
    r"^\W*(?:era[,\s]+)?(?:please\s+)?(?:switch|change|go|use)\s+(?:back\s+)?(?:to\s+)?"
    r"(?P<bare>fish(?:\s+audio)?|kokoro)\W*$",
    re.IGNORECASE,
)
_FISH_WORDS = {"fish", "fish audio", "clone", "cloned", "own", "primary", ""}


def voice_order(text: str) -> str | None:
    """'fish' | 'local' for a standalone voice-switch order, else None.

    "switch to my voice" / "use my cloned voice" / "switch to fish"  -> fish
    "use the premium voice" / "use default voice" / "switch to kokoro" -> local
    """
    m = _VOICE_ORDER.match(text or "")
    if not m:
        return None
    which = " ".join((m.group("which") or m.group("bare") or "").lower().split())
    # "switch to my voice": no qualifier, but "my" — the user's cloned voice.
    if not which and not re.search(r"\bmy\s+voice\b", text, re.I):
        return None
    return "fish" if which in _FISH_WORDS else "local"


def build_brain(pol: dict[str, Any]):
    """LocalBrain on CodeCraft, optionally backed by an Ollama alias."""
    from core.llm.adapters.openai_compat import FirstThatAnswers, OpenAICompatProvider
    from core.local_brain import CASCADE_SYSTEM_PROMPT, LocalBrain

    provider: Any = OpenAICompatProvider()
    backup = str(pol.get("fallback_alias") or "").strip()
    if backup:
        from core.llm.adapters.ollama_chat import OllamaChatProvider

        provider = FirstThatAnswers(provider, OllamaChatProvider(), label=backup)
    return LocalBrain(
        provider=provider,
        alias=backup or "era.codecraft",
        history_turns=int(pol.get("history_turns") or 5),
        max_tool_rounds=int(pol.get("max_tool_rounds") or 4),
        timeout_s=float(pol.get("timeout_s") or 45),
        system_prompt=CASCADE_SYSTEM_PROMPT,
    )


class CascadeMixin:
    # ── readiness ───────────────────────────────────────────────────────────
    def _cascade_policy(self) -> dict[str, Any]:
        try:
            from core.llm.router import cascade_policy

            return cascade_policy()
        except Exception:
            return {"enabled": False}

    def cascade_ready(self) -> tuple[bool, str]:
        if getattr(self, "_cascade_paused", False):
            return False, "cascade paused (reconnect asked for Gemini Live)"
        if not self._cascade_policy().get("enabled"):
            return False, "cascade disabled (routing.cascade / ERA_CASCADE)"
        from core import local_stt, local_tts
        from core.llm.adapters.openai_compat import is_configured

        for ok, why in (is_configured(), local_stt.available(), local_tts.available()):
            if not ok:
                return False, why
        return True, ""

    def cascade_active(self) -> bool:
        return self._in_cascade()

    # ── entry point from the run loop ───────────────────────────────────────
    async def serve_cascade(self) -> bool:
        """Serve one cascade cycle. False = not available; connect Gemini instead."""
        import asyncio

        ok, why = await asyncio.to_thread(self.cascade_ready)
        if not ok:
            if why != getattr(self, "_cascade_last_why", None):
                self._cascade_last_why = why
                print(f"[CASCADE] Not used: {why}")
            return False
        self._cascade_last_why = ""
        pol = self._cascade_policy()
        self._cascade_resources(pol)
        try:
            await self._run_local_fallback(float(pol.get("cycle_s") or 3600), CASCADE)
        except Exception as e:  # never take the run loop down — hand over to Gemini
            print(f"[CASCADE] Loop failed ({type(e).__name__}: {e}); falling back to Gemini Live.")
            self._cascade_paused = True
            return False
        finally:
            self._local_outage_reason = ""
        return True

    def _cascade_resources(self, pol: dict[str, Any]) -> None:
        """CPU only, capped threads, below-normal priority. Once per run."""
        if getattr(self, "_cascade_resources_set", False):
            return
        self._cascade_resources_set = True
        threads = str(int(pol.get("stt_threads") or 4))
        os.environ.setdefault("ERA_LOCAL_STT_THREADS", threads)
        os.environ.setdefault("OMP_NUM_THREADS", threads)
        if pol.get("low_priority", True):
            try:
                import psutil

                cls = getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 10)
                psutil.Process().nice(cls)
                print("[CASCADE] Process priority set to below normal.")
            except Exception as e:
                print(f"[CASCADE] Could not lower priority: {e}")

    # ── hooks the local loop calls ──────────────────────────────────────────
    def _cascade_brain(self):
        brain = getattr(self, "_cascade_brain_obj", None)
        if brain is None:
            brain = self._cascade_brain_obj = build_brain(self._cascade_policy())
        return brain

    async def _cascade_voice_order(self, text: str) -> bool:
        """Apply a spoken voice switch. True = handled (the brain never sees it)."""
        target = voice_order(text)
        if target is None:
            return False
        from core.tts_fish import is_configured
        from core.voice_mode import VOICE_CFG

        self.ui.write_log(f"You: {text}")
        if target == "fish" and not is_configured():
            await self._local_say("My cloned voice has no key configured, sir.")
            return True
        VOICE_CFG.set_mode("fish" if target == "fish" else "gemini")
        print(f"[CASCADE] Voice -> {target}")
        self.ui.write_log(f"SYS: Voice switched to {'Fish' if target == 'fish' else 'local'}.")
        await self._local_say(
            "Switched to your cloned voice." if target == "fish" else "Switched to premium voice."
        )
        return True

    def _cascade_pause(self) -> None:
        self._cascade_paused = True
        self.ui.write_log("SYS: Cascade mode paused — back on Gemini Live until the next launch.")
