"""Auto-promotion of free-text user feedback into standing preferences.

main._maybe_record_feedback runs the real user transcription (never
model-supplied text) through `is_standing_instruction` as an accept gate and
`promote_feedback_to_prefs` to actually apply whatever it grants.
"""

from __future__ import annotations

import re

import core.user_prefs as _store
from core.user_prefs.auto_memory import set_auto_memory
from core.user_prefs.behavior_rules import add_behavior_rule
from core.user_prefs.claude_send_waiver import set_claude_send_confirmation_waiver
from core.user_prefs.close_waiver import set_close_confirmation_waiver
from core.user_prefs.minimize_rule import set_post_action_minimize

# Deliberately excludes "kill": a spoken waiver only ever covers graceful
# close — force-kill always keeps its confirmation gate (see safety_guard).
_WAIVE_CLOSE_RE = re.compile(
    r"(?:don'?t|do not|no need to|stop|never)\s+(?:ask(?:ing)?\s+)?"
    r"(?:for\s+)?(?:my\s+)?(?:confirmation|permission)[^.]*?\b(?:close|closing|quit)\w*\s+(?P<app>[\w .-]{2,40})",
    re.IGNORECASE,
)
_WAIVE_CLOSE_RE2 = re.compile(
    r"(?:close|quit)\s+(?P<app>[\w .-]{2,40}?)\s+without\s+(?:asking|my\s+)?(?:for\s+)?(?:my\s+)?(?:confirmation|permission|asking)",
    re.IGNORECASE,
)
_REQUIRE_CLOSE_RE = re.compile(
    # The app is the single word right before "needs/requires" — allowing
    # spaces here would swallow leading filler ("from now on spotify ...").
    r"(?P<app>[\w.-]{2,40})\s+(?:needs?|requires?|must have)\s+(?:my\s+)?confirmation\s+(?:before|when|for)\s+clos\w*",
    re.IGNORECASE,
)
# A standing instruction is one that describes WHEN, not just what — it tells
# Era how to behave on every future occasion rather than asking for one action
# now. Two shapes qualify:
#
#   1. An explicit recurrence marker: "from now on", "always", "every time".
#   2. A completion clause: "minimize Spotify AFTER USE", "close it once you're
#      done", "when you finish with it". These carry no recurrence word at all,
#      which is why they used to fall straight through and never persist — the
#      exact case the user reported ("minimize Spotify after use" was ignored).
_BEHAVIOR_RE = re.compile(
    r"\b(?:"
    r"from now on|always|never|whenever|every ?time|each time|by default|going forward"
    r"|after (?:use|using|you use|you'?re done|you finish|each use|every use)"
    r"|(?:once|after|when) (?:you'?re|you are) (?:done|finished)"
    r"|(?:once|when) you (?:finish|are done|'re done)"
    r"|when(?:ever)? you(?:'re| are)? done"
    r"|when done"
    r")\b",
    re.IGNORECASE,
)

# Auto-memory toggle spoken in plain language. Matched on the real user
# transcription only (see _maybe_record_feedback), never on model-supplied text.
_MEMORY_ASK_FIRST_RE = re.compile(
    r"(?:(?:ask|check with)\s+me\s+(?:first\s+)?before\s+(?:you\s+)?"
    r"(?:sav\w+|stor\w+|rememb\w+|writ\w+|add\w*)[^.]*?\b(?:memor\w+|it|that|anything|something)?"
    r"|(?:don'?t|do not|never|stop)\s+(?:sav\w+|stor\w+|rememb\w+|writ\w+|add\w*)[^.]{0,40}?"
    r"\b(?:memor\w+|anything|things?|stuff|it|that)\b[^.]{0,40}?"
    r"(?:without\s+(?:my\s+)?(?:confirmation|permission|asking)|on your own|by yourself|automatically))",
    re.IGNORECASE,
)
_MEMORY_AUTO_RE = re.compile(
    r"(?:(?:sav\w+|stor\w+|rememb\w+|memoriz\w+|memoris\w+)[^.]{0,40}?"
    r"(?:without\s+(?:asking|my\s+(?:confirmation|permission))|on your own|by yourself|automatically|"
    r"whenever you (?:think|see|judge)|when you think (?:it'?s|its) useful)"
    r"|(?:no need|you don'?t need|don'?t have)\s+to\s+ask[^.]{0,40}?"
    r"\b(?:sav\w+|stor\w+|rememb\w+|memor\w+)\b)",
    re.IGNORECASE,
)

# "minimize Spotify after use" / "always minimize spotify when you're done" /
# "after using Spotify, minimize it". Both orders (verb-first and clause-first)
# are matched, because both are natural and the user used the first one.
_MINIMIZE_AFTER_RE = re.compile(
    r"minimi[sz]e\s+(?P<app>[\w .-]{2,40}?)\s*(?:,\s*)?"
    r"(?:after|once|when)\b[^.]{0,40}?"
    r"\b(?:use|using|used|done|finish\w*|play\w*|it)\b",
    re.IGNORECASE,
)
_MINIMIZE_AFTER_RE2 = re.compile(
    r"(?:after|once|when)\s+(?:you\s+)?(?:are\s+|'?re\s+)?"
    r"(?:us\w+|done\s+with|finish\w*\s+with|play\w*)\s+(?P<app>[\w .-]{2,40}?)\s*[,.]?\s*"
    r"[^.]{0,30}?minimi[sz]e",
    re.IGNORECASE,
)
# The reverse — "stop minimizing Spotify" / "don't minimize Spotify after use".
_MINIMIZE_STOP_RE = re.compile(
    r"(?:don'?t|do not|stop|never|no longer)\s+minimi[sz]\w*\s+(?P<app>[\w .-]{2,40})",
    re.IGNORECASE,
)

# "don't ask (for) confirmation when I tell you to send a message/prompt to
# Claude - just send it". This waives only the extra CONFIRMATION step; the
# user-words gate (Era must have heard the user name Claude) is enforced
# separately in the dispatch and is never touched by this flag. The revoke
# shape ("ask me before messaging Claude") is matched first so it can't read
# as a grant.
_WAIVE_CLAUDE_SEND_RE = re.compile(
    r"(?:don'?t|do not|no need to|stop|never)\s+(?:ask(?:ing)?\s+)?(?:me\s+)?"
    r"(?:for\s+)?(?:my\s+)?(?:confirmation|permission)[^.]*?"
    r"\b(?:send|sending|message|messaging|prompt|tell|telling|ask|asking)\b"
    r"[^.]*?\bclaude\b",
    re.IGNORECASE,
)
_REQUIRE_CLAUDE_SEND_RE = re.compile(
    r"\b(?:ask|confirm|check)\b[^.]*?\bbefore\b[^.]*?"
    r"\b(?:send|sending|message|messaging|prompt|tell|telling|ask|asking)\b"
    r"[^.]*?\bclaude\b",
    re.IGNORECASE,
)

_STOPWORDS = {"the", "a", "an", "app", "application", "it", "this", "that"}


def _clean_app_capture(raw: str) -> str:
    words = [w for w in _store._normalize_app(raw).split() if w not in _STOPWORDS]
    return words[0] if words else ""


def is_standing_instruction(text: str) -> bool:
    """True if `text` reads as a durable behavior rule rather than a one-off order.

    main._maybe_record_feedback uses this as an accept gate so that anything
    this module can PERSIST is also something main will hand it. The two used to
    disagree: main's keyword list decided what reached the prefs layer, so a
    rule like "minimize Spotify after use" was dropped before promotion ever ran.
    """
    t = (text or "").strip()
    if not t or len(t) >= 300:
        return False
    return bool(
        _BEHAVIOR_RE.search(t)
        or _WAIVE_CLOSE_RE.search(t)
        or _WAIVE_CLOSE_RE2.search(t)
        or _REQUIRE_CLOSE_RE.search(t)
        or _MEMORY_ASK_FIRST_RE.search(t)
        or _MEMORY_AUTO_RE.search(t)
        or _WAIVE_CLAUDE_SEND_RE.search(t)
        or _REQUIRE_CLAUDE_SEND_RE.search(t)
    )


def promote_feedback_to_prefs(text: str) -> str | None:
    """Inspect a feedback utterance and persist any standing preference it grants.

    Returns a human-readable summary of what was applied, or None if the text
    carries no persistable preference.
    """
    t = (text or "").strip()
    if not t:
        return None

    applied: list[str] = []

    # Auto-memory toggle. "ask me first" is checked BEFORE the enable pattern:
    # both fire on "don't save memories without asking me", and the restrictive
    # reading is the safe one.
    if _MEMORY_ASK_FIRST_RE.search(t):
        applied.append(set_auto_memory(False))
    elif _MEMORY_AUTO_RE.search(t):
        applied.append(set_auto_memory(True))

    # "minimize Spotify after use" — promoted to the STRUCTURED rule the action
    # layer enforces, not just to advisory prompt text. Revocation is checked
    # first so "don't minimize Spotify" cannot be read as a grant.
    mstop = _MINIMIZE_STOP_RE.search(t)
    if mstop:
        app = _clean_app_capture(mstop.group("app"))
        if app:
            applied.append(set_post_action_minimize(app, False))
    else:
        for rx in (_MINIMIZE_AFTER_RE, _MINIMIZE_AFTER_RE2):
            mmin = rx.search(t)
            if mmin:
                app = _clean_app_capture(mmin.group("app"))
                if app:
                    applied.append(set_post_action_minimize(app, True))
                break

    m = _REQUIRE_CLOSE_RE.search(t)
    if m:
        app = _clean_app_capture(m.group("app"))
        if app:
            applied.append(set_close_confirmation_waiver(app, False))

    if not m:
        for rx in (_WAIVE_CLOSE_RE, _WAIVE_CLOSE_RE2):
            m2 = rx.search(t)
            if m2:
                app = _clean_app_capture(m2.group("app"))
                if app:
                    applied.append(set_close_confirmation_waiver(app, True))
                break

    # "just send it to Claude without asking" — waives only the extra
    # confirmation step (the user-words gate is enforced elsewhere and is never
    # opened by this). Revoke is checked first so it cannot read as a grant.
    if _REQUIRE_CLAUDE_SEND_RE.search(t):
        applied.append(set_claude_send_confirmation_waiver(False))
    elif _WAIVE_CLAUDE_SEND_RE.search(t):
        applied.append(set_claude_send_confirmation_waiver(True))

    # Standing behavior instructions ("minimize spotify when you're done",
    # "from now on ...") that aren't confirmation-related.
    if not applied and _BEHAVIOR_RE.search(t) and len(t) < 300:
        applied.append(add_behavior_rule(t))

    return " ".join(applied) if applied else None
