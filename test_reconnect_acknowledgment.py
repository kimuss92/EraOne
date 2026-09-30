"""Phase-1 reliability: reconnection is silent by policy for ordinary
conversation gaps (quota cooldown, an auth fix, a network blip, or Gemini's
routine ~2.5min session rotation alike).

THIS USED TO test a spoken apology naming the last topic ("Sorry sir, I lost
connection... you were asking about 'X' — want me to continue?"). Removed
2026-07-21: main.speak() delivers text to the Live session as live
conversational input, not narration, so an unwrapped string containing a
question gets genuinely ANSWERED by Gemini rather than just spoken back. A
reconnect after a misheard "skip track" caused Gemini to actually skip the
track and announce having done so, unprompted, in the new session — the
apology mechanism itself was the bug, not just its wording, so it was removed
outright rather than patched to filter more cases. See
reconnect_acknowledgment_text's docstring in main.py for the full account.

A community-management status report is a different, still-supported case —
see test_cm_reconnect_continuity.py — because it is a factual statement
("here's what finished" / "still working"), never phrased as a question, and
is delivered wrapped in main._say_exactly() so Gemini can only speak it.

Pure-logic — no network, no real Gemini calls, no EraLive instantiation.
"""

import time

from main import reconnect_acknowledgment_text


def test_rotation_stays_silent():
    recent = [(time.time(), "what's the weather like")]
    assert reconnect_acknowledgment_text("rotation", recent) is None


def test_no_prior_utterance_stays_silent():
    assert reconnect_acknowledgment_text("quota", []) is None


def test_blank_last_utterance_stays_silent():
    assert reconnect_acknowledgment_text("transient", [(time.time(), "   ")]) is None


def test_quota_recovery_stays_silent():
    recent = [(time.time(), "publish the Delta Cuisine reel")]
    assert reconnect_acknowledgment_text("quota", recent) is None


def test_auth_recovery_stays_silent():
    recent = [(time.time(), "check my calendar for tomorrow")]
    assert reconnect_acknowledgment_text("auth", recent) is None


def test_transient_recovery_stays_silent():
    recent = [(time.time(), "open Spotify")]
    assert reconnect_acknowledgment_text("transient", recent) is None


def test_routine_playback_command_stays_silent():
    # The scenario that originally exposed the bug (a misheard "skip track"
    # caused Gemini to auto-execute a stale action from the reconnect
    # message) — silence covers this trivially now, no ASR-pattern-matching
    # needed to get it right.
    recent = [(time.time(), "resume youtube")]
    assert reconnect_acknowledgment_text("quota", recent) is None


def test_long_or_substantive_utterance_also_stays_silent():
    long_topic = "explain in detail " + "why the sky is blue " * 10
    recent = [(time.time(), long_topic)]
    assert reconnect_acknowledgment_text("quota", recent) is None
