"""'Cancel that' and 'no, I meant X' — stopping or redirecting work in flight.

WHY THIS IS NOT core/panic_stop.py
----------------------------------
An abort order trips a latch that refuses EVERY tool for 30 seconds. That is
right when the model has gone mad, and exactly wrong here: the owner who says
"no, I meant the next track" wants the wrong task dropped and the right one run
IMMEDIATELY, which a blanket refusal makes impossible. So these orders cancel
one task and leave Era free to act on the correction in the same breath.

WHAT THIS GRAMMAR MAY NOT CLAIM
-------------------------------
Four vocabularies are already spoken for, and stealing any of them would break
something the owner relies on:

  bare "stop"              media command (input_safety_gate.SAFE_SHORT_COMMANDS)
  "stop"/"cancel"/"abort"  confirmation answers (CONFIRMATION_REPLIES)
  "cancel the shutdown"    the power countdown (safety_guard._POWER_ABORT_ACTIONS)
  "abort mission" etc.     the panic latch (panic_stop._ORDERS) -- strictly
                           stronger, and it is checked FIRST, so it wins

Hence two hard rules: a bare verb never matches (a demonstrative or a task noun
is mandatory), and anything panic_stop or the power words claim is handed back.
tools/test_cancel_intent.py loops over both of those vocabularies to prove it.

Whole-utterance anchored, like every intent in this codebase, so "why did you
cancel that task" stays conversation. lru_cache'd because these run on every
partial transcription chunk, several times a second while the owner talks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from core.panic_stop import _POWER_WORDS, is_abort_order

_PREFIX_WORDS = ("hey", "ok", "okay", "era", "era one", "please", "yo")

# What a task can be called. "one" and "thing" are in because people say
# "cancel that one" far more often than "cancel that task".
_TASK_NOUN = r"(?:task|job|one|thing|action|search|command|request)"

_CANCEL_BODY = (
    rf"(?:cancel|stop|drop|abandon|scrap|forget)\s+(?:that|this|it)(?:\s+{_TASK_NOUN})?",
    rf"(?:cancel|stop|drop|abandon)\s+(?:the|that|this)\s+{_TASK_NOUN}",
    r"(?:cancel|stop)\s+(?:(?:the|my|current|running|live|active|activity)\s+)*task",
    r"(?:cancel|stop)\s+what\s+you(?:'re|\s+are)\s+doing(?:\s+(?:right\s+)?now)?",
    r"(?:annule|arr[eê]te)\s+(?:[çc]a|cette\s+t[âa]che)",
    r"laisse\s+tomber\s+[çc]a",
)

# "no, I meant X" / "not that, X" / "no, play the next track". The leading
# negation is what separates a correction from a fresh command: without it,
# every sentence spoken during a task would look like one.
_CORRECTION = re.compile(
    r"^\W*(?:no+|nope|non|wait|hold\s+on|actually)\b\W*(?:era\s*[,;:]?\s*)?"
    r"(?:"
    r"i\s+(?:meant|said|wanted|asked\s+for)\s+(?P<a>.+)"
    r"|not\s+(?:that|this|it)\s*[,;:-]*\s*(?P<b>.+)"
    r"|(?P<c>.+)"
    r")$",
    re.IGNORECASE,
)

# "I meant the next track" with no leading "no".
_CORRECTION_BARE = re.compile(
    r"^\W*(?:era\s*[,;:]?\s*)?i\s+(?:meant|said)\s+(?P<a>.+)$",
    re.IGNORECASE,
)

# "not that, play Daft Punk" -- the negation IS "not that", so it carries no
# separate leading "no".
_CORRECTION_NOT_THAT = re.compile(
    r"^\W*(?:era\s*[,;:]?\s*)?not\s+(?:that|this|it)\s*[,;:-]*\s*(?P<a>.+)$",
    re.IGNORECASE,
)

# A refusal on its own: drop what is running, put nothing in its place.
_BARE_NEGATION = re.compile(r"^\W*(?:no+|nope|non)\W*$", re.IGNORECASE)

_MIN_TARGET_CHARS = 3


@lru_cache(maxsize=8)
def _cancel_pattern(words: tuple[str, ...]) -> re.Pattern:
    prefix = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
    body = "|".join(_CANCEL_BODY)
    return re.compile(rf"^\W*(?:(?:{prefix})\W+)*(?:{body})\W*[?.!]*\W*$", re.IGNORECASE)


def _words(wake_words: Iterable[str] = ()) -> tuple[str, ...]:
    return tuple(sorted({w.strip().lower() for w in (*_PREFIX_WORDS, *wake_words) if w.strip()}))


def _reserved(text: str, wake_words: Iterable[str]) -> bool:
    """True when a stronger, older meaning owns this utterance."""
    if is_abort_order(text, wake_words):
        return True  # the latch outranks us and must never be downgraded
    return bool(_POWER_WORDS.search(text))  # "cancel the shutdown" is not this


def is_cancel_task_order(text: str, wake_words: Iterable[str] = ()) -> bool:
    """The whole utterance is "stop the thing you are doing", and nothing else."""
    t = (text or "").strip().lower()
    if not t or _reserved(t, wake_words):
        return False
    return _cancel_pattern(_words(wake_words)).match(t) is not None


def is_spoken_cancel_task_order(text: str, wake_words: Iterable[str] = ()) -> bool:
    """Accept an explicit cancel sentence inside a growing voice transcript.

    Keep quoted examples and commands belonging to power/confirmation flows
    out of this lane. No keyword matching inside arbitrary sentences.
    """
    unquoted = re.sub(r'"[^"\n]*"|“[^”\n]*”', "", text or "")
    return any(
        is_cancel_task_order(sentence, wake_words) for sentence in re.split(r"[.!?\n]+", unquoted)
    )


def is_cancel_all_live_order(text: str) -> bool:
    unquoted = re.sub(r'"[^"\n]*"|“[^”\n]*”', "", text or "")
    return any(
        re.fullmatch(
            r"\W*(?:(?:era(?:\s+one)?|please|okay|ok)\W+)*(?:cancel|stop)\s+"
            r"(?:(?:all|every)(?:\s+(?:the|my))?\s+(?:(?:running|live|active|current)\s+)*(?:tasks?|jobs?)"
            r"|(?:the\s+)?(?:running|live|active)\s+tasks)\W*",
            sentence,
            re.I,
        )
        for sentence in re.split(r"[.!?\n]+", unquoted)
    )


def correction_target(text: str, wake_words: Iterable[str] = ()) -> str | None:
    """What the owner meant instead, "" for a bare cancel, None if not one.

    "" and None are different answers on purpose: "" means "this WAS a
    correction but names no replacement" (just drop the task), while None means
    "this is not a correction at all" and must fall through to normal routing.
    """
    t = (text or "").strip()
    if not t or _reserved(t.lower(), wake_words):
        return None
    if _BARE_NEGATION.match(t):
        return ""  # "no" / "nope" on its own
    m = _CORRECTION_NOT_THAT.match(t) or _CORRECTION.match(t) or _CORRECTION_BARE.match(t)
    if m is None:
        return None
    target = next((g for g in m.groupdict().values() if g), "").strip(" ,;:-.!?")
    if not target:
        return ""
    low = target.lower()
    # The replacement must be a command, not another way of saying stop.
    if is_cancel_task_order(low, wake_words) or is_abort_order(low, wake_words):
        return ""
    # These run BEFORE the length check, and they answer None rather than "".
    # "no, um" is a hesitation, not an order: treating it as a bare cancel
    # would let a filler syllable kill work the owner never asked to stop.
    try:
        from app.text_intents import is_backchannel, looks_unfinished

        if is_backchannel(target) or looks_unfinished(target):
            return None  # wait for the rest of the sentence
    except Exception:  # noqa: BLE001 — the filters are a courtesy, not a gate
        pass
    if len(target) < _MIN_TARGET_CHARS:
        return None  # too little to act on, and too little to cancel on
    return target
