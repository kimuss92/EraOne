"""Reconnect backoff + acknowledgment helpers (extracted from main.py).

Pure functions. Re-imported by main.py (`from main import reconnect_acknowledgment_text`).
"""


def grow_cooldown(current: float, cap: float) -> float:
    """Next quota-cooldown length: double the current wait, capped at ``cap``.

    Kept as a tiny pure function so the cooldown-growth policy is unit-testable
    without constructing the full EraLive engine.
    """
    return min(current * 2.0, cap)


def reconnect_acknowledgment_text(
    disconnect_kind: str, recent_user_texts: list[tuple[float, str]]
) -> str | None:
    """Always None — reconnection is silent by policy for ordinary conversation
    gaps (quota cooldown, an auth fix, a network blip, or Gemini's routine
    ~2.5min rotation alike). The Gemini resumption token already means the
    model itself still holds the full conversation, so nothing needs to be
    re-stated for the user to "remember what was happening" — that happens
    on its own, silently.

    THIS USED TO speak an apology naming the last topic ("Sorry sir, I lost
    connection... you were asking about 'X' — want me to continue?") and ask
    whether to pick it back up. Removed 2026-07-21: speak() delivers text to
    the Live session as live conversational input, not narration — Gemini
    does not just read an unwrapped string back verbatim, it RESPONDS to it.
    A rhetorical "want me to continue?" got genuinely answered: a reconnect
    after a misheard "skip track" caused Gemini to actually skip the track
    and announce having done so, unprompted, in the new session. The failure
    mode was structural (any bare question in spoken text invites an answer),
    not fixable by better-matching which utterances deserve the apology, so
    the whole mechanism is gone rather than patched again. A community-
    management status report is a different case — a factual "here's what
    finished" or "still working", never phrased as a question — and is still
    delivered; see EraLive._build_reconnect_acknowledgment /
    _deliver_or_queue_cm_report, both wrapped in _say_exactly() so Gemini can
    only speak them, never answer them.

    Kept as a function (mirrors grow_cooldown above, and existing call sites /
    tests reference it) purely so the "no apology" policy has one documented,
    unit-testable home instead of being an inline `None` scattered around.
    """
    return None
