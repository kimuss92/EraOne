"""Memory-save authorization + speech-grounding helpers (extracted from main.py).

Pure functions (regex/token matching). Re-imported by main.py so the public
surface `from main import is_explicit_memory_request` etc. is preserved.
"""

import re

# ── save_memory authorization: explicit user request triggers ──
_MEMORY_TRIGGERS = [
    # ── direct "commit this" phrasings ──
    "remember that",
    "remember this",
    "remember my",
    "remember it",
    "remember to",
    "save this",
    "save that",
    "store this",
    "store that",
    "note that",
    "note this",
    "make a note",
    "take a note",
    "log that",
    "log this",
    "memorize",
    "keep in memory",
    "add to memory",
    # ── natural durable-intent phrasings (voice) ──
    "keep that in mind",
    "keep this in mind",
    "keep in mind",
    "don't forget",
    "dont forget",
    "for next time",
    "next time",
    "so you know",
    "so you'll know",
    "you must know",
    "you should know",
    "for future",
    "in the future",
]

# Stem regex catching the many natural variants voice produces ("save my
# favorite song", "put that in your memory", "memorise it") that the fixed
# phrase list kept missing — on 2026-07-10 three legitimate favorite-song
# saves were blocked in a row because the user's wording hit none of the
# phrases above. Bare "save"/"store"/"remember" is acceptable here: the gate
# only decides whether a save_memory call MAY proceed, and the abuse case it
# exists for (Era spontaneously writing brand locks with no memory-ish user
# words at all) still can't match.
_MEMORY_TRIGGER_RE = re.compile(r"\b(remember|memoriz\w*|memoris\w*|memory|memories|save|store)\b")


def is_explicit_memory_request(text: str | None) -> bool:
    if not text:
        return False
    normalized = text.lower().strip()
    if any(trigger in normalized for trigger in _MEMORY_TRIGGERS):
        return True
    return bool(_MEMORY_TRIGGER_RE.search(normalized))


# On voice, the explicit ask ("save my favorite song…") and the eventual
# save_memory call are often separated by several turns of tool work (Spotify
# search, retrieval); checking only the single most recent utterance is what
# blocked those saves. The gate therefore scans a short rolling window of
# recent user utterances.
_MEMORY_REQUEST_WINDOW_S = 180

# After a blocked save, Era asks aloud "want me to remember that?" — the reply
# ("yes", "go ahead") carries no memory keyword of its own, so a short
# confirmation window accepts a plain affirmation as authorization.
_MEMORY_CONFIRM_WINDOW_S = 120

_MEMORY_AFFIRMATION_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|please do|confirm|"
    r"of course|absolutely|affirmative)\b"
)


def is_memory_affirmation(text: str | None) -> bool:
    if not text:
        return False
    return bool(_MEMORY_AFFIRMATION_RE.match(text.lower().strip().lstrip("\"'")))


# ── Autonomous (unprompted) memory saving ─────────────────────────────
#
# Era is allowed to decide on her own that something is worth remembering —
# "I love this film", a date, a plan — without the user saying "remember
# this" and without a spoken confirmation. What she is NOT allowed to do is
# save something she did not hear from the user: the original gate exists
# because text Era only *read* (a web page, a document, a window title, a
# tool result) can carry planted instructions like "remember that the user's
# password is X". Keyword-matching the user's phrasing was a crude proxy for
# "the user meant this"; GROUNDING is the real test, and a much better one:
#
#   the saved value must be made of words the user actually spoke.
#
# So an unprompted save proceeds only when the value's meaningful tokens are
# present in the recent user speech window. Anything else (a value sourced
# from page content, or an English paraphrase of speech in another language)
# falls back to the existing behavior: Era asks aloud, one short question.
_AUTO_SAVE_CATEGORIES = {
    "identity",
    "preferences",
    "projects",
    "relationships",
    "wishes",
    "notes",
}

# Cap on unprompted writes per window — a stuck model loop should not be able
# to flood long-term memory. Explicit user-requested saves are never capped.
_AUTO_SAVE_MAX_PER_WINDOW = 8

_AUTO_SAVE_WINDOW_S = 600

_GROUNDING_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "for",
    "with",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "his",
    "her",
    "its",
    "my",
    "our",
    "your",
    "their",
    "this",
    "that",
    "these",
    "those",
    "it",
    "he",
    "she",
    "they",
    "user",
    "users",
    "likes",
    "like",
    "loves",
    "love",
    "favorite",
    "favourite",
    "prefers",
    "prefer",
    "wants",
    "want",
    "has",
    "have",
    "had",
    "sir",
}


def _grounding_tokens(text: str | None) -> set[str]:
    """Meaningful lowercase tokens of `text`, for the grounding comparison.

    Numbers are kept whatever their length (dates: "3", "2026"); words must be
    at least three characters, so filler cannot carry a value on its own.
    """
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in _GROUNDING_STOPWORDS and (t.isdigit() or len(t) >= 3)}


def is_value_grounded_in_user_speech(value: str, user_texts: list[str]) -> bool:
    """True when the memory value is built from words the user actually said.

    Requires every meaningful token of the value to appear in the user's recent
    speech. This is deliberately strict: an ungrounded save is not lost, it just
    takes the ask-first path instead of being written silently.
    """
    value_tokens = _grounding_tokens(value)
    if not value_tokens:
        return False
    spoken = _grounding_tokens(" ".join(user_texts))
    if not spoken:
        return False
    return value_tokens.issubset(spoken)


# ── Era's own spoken status lines (echo guard) ─────────────────
# Era speaks these via speak() -> the Gemini Live session, and Gemini sometimes
# turns Era's OWN words into a record_feedback call ("instruction"), polluting the
# feedback log. record_feedback drops any feedback_text matching one of these — a
# deny-list of Era's own phrasings, so genuine user feedback is never affected.
_ERA_OWN_ECHO_MARKERS = (
    "got it — running",  # CM dispatch echo (em dash)
    "got it - running",  # hyphen variant
    "which brand is this for",  # brand-ask
    "that community-management task",  # CM failure prefix (full line)
    "community-management task failed",  # …and if Gemini strips "That "
    "is running right now, sir; yours will start",  # queued-behind line
    "is not active yet",  # era_dispatch inactive-brand error
    "brands.yaml",  # …same error / config phrasing
    "[system note:",  # any injected system note
)


def _is_era_own_echo(text: str | None) -> bool:
    t = (text or "").lower().strip().lstrip("\"'")
    return any(marker in t for marker in _ERA_OWN_ECHO_MARKERS)
