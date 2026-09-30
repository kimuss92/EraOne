"""Security gates for the local fallback loop (app/era_live/local_fallback.py).

Split out so each is testable on its own. Both come from the 2026-09-23
security review of the local path, and both mirror a gate the Gemini path
already enforces:

  owner_voice    voice lock — only the enrolled owner's speech is acted on
                 (the session path: core/voice_gate.UtteranceGate)
  user_approved  a pending HIGH-risk action is confirmed only by the USER's
                 own "yes", said after it was armed — never by the model
"""

from __future__ import annotations

import time

import numpy as np


def owner_voice(era, segment) -> bool:
    """True when this VAD segment may be acted on. Fail-closed.

    Without it, any voice (a guest, a video playing) could say "turn off voice
    recognition" and the change would persist after Gemini came back. Purposes
    mirror the session gate: "wake" in standby; a short "followup" shortly
    after a verified command, but never while a confirmation is pending — that
    "yes" must be long enough to verify.
    """
    try:
        from core.speaker_recognition import get_verifier

        verifier = get_verifier()
        if not verifier.enabled:
            return True
        store = getattr(era, "_pending_store", None)
        pending = store is not None and store.is_pending()
        window = float((getattr(verifier, "settings", None) or {}).get("followup_window_s", 8.0))
        recent = time.monotonic() - getattr(era, "_local_owner_at", -1e9) < window
        if era._standby_active:
            purpose = "wake"
        elif recent and not pending:
            purpose = "followup"
        else:
            purpose = "command"
        pcm = (np.clip(segment, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        result = verifier.verify(pcm, purpose)
    except Exception as e:
        print(f"[LOCAL] Voice check unavailable ({e}) — refused.")
        return False
    if not result.accepted:
        print(f"[LOCAL] Voice refused ({purpose}): {result.reason}")
        return False
    if purpose != "followup":
        era._local_owner_at = time.monotonic()
    return True


def user_approved(era) -> bool:
    """The user said yes AFTER the pending action was armed (CRITICAL 1).

    The rule itself lives in tool_dispatch._user_approved_pending, which the
    session path's confirm handler enforces too: only the LATEST utterance
    counts ("yes ... no, don't" is a no), and the WHOLE reply must be a yes — an
    approval word inside a new order ("okay, set a timer") moved on from the
    question; it did not answer it (security re-check 2026-09-23)."""
    from app.era_live.tool_dispatch import _user_approved_pending

    store = getattr(era, "_pending_store", None)
    return _user_approved_pending(era, store.peek() if store is not None else None)
