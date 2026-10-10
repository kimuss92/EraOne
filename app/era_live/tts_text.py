"""What a TTS voice may say — the text rules for every synthesized sentence.

WHY THIS EXISTS (2026-10-09)
  In fish_audio mode Era's voice is synthesized from TEXT, so anything in that
  text is read aloud: Fish was speaking "[System note: Say aloud exactly this ...
  do not use any tool.]" wrappers, raw error strings, paths and log tags. Gemini's
  own voice never had this problem — it reacted to the notes instead of reading
  them — so the rules belong to the text-to-speech path, not to the callers.

  This is also the single home for speech-text rules as Fish grows into Era's
  main voice (Gemini as the brain, Fish as the voice): language hints,
  pronunciation fixes and number reading belong here, never at call sites.

THREE ENTRY POINTS, used by app/era_live/speech_out.py
  strip_notes(text)      before sentence splitting: a note carries its own full
                         stops, so splitting first would cut it in half where no
                         per-sentence filter could recognise it any more.
  open_note_at(buf)      streaming: where an unclosed "[" starts, so the rest is
                         held back until its "]" arrives.
  sanitize_for_tts(s)    per sentence, just before synthesis. "" = say nothing.

Non-ASCII text (French accents, Arabic) passes through untouched: every rule
below matches ASCII markup only.
"""

from __future__ import annotations

import re

# "[System note: ... exactly this ...: "PAYLOAD" ...]" in any wording — the
# standard _say_exactly() wrapper and the hand-written CM "Ask the user aloud,
# exactly this" note alike. The payload is what Era means to say.
_SAY_PAYLOAD = re.compile(r'\[System note:[^\]"]*?exactly this[^\]"]*"([^"]*)"[^\]]*\]', re.I)
# Any other bracketed note addressed to the model, not the listener.
_NOTE_SPAN = re.compile(r"\[\s*(?:system note|system|note|user|era|context)\b[^\]]*\]", re.I)
# How far a streamed "[" may run before it is treated as ordinary text.
_MAX_OPEN_NOTE = 1200

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LEAD_TAGS = re.compile(
    r"^\s*(?:\[[A-Z][A-Z0-9_\- ]{1,20}\]\s*|(?:SYS|ERR|INFO|WARN|WARNING|DEBUG|ERROR)\s*[:\-—]\s*)+"
)
_URL = re.compile(r"https?://(?:www\.)?([^/\s]+)\S*", re.I)
_WIN_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*([^\\\s]+)")
_POSIX_PATH = re.compile(r"(?<![\w.])(?:/[\w.\-]+){2,}")
_JSON_SPAN = re.compile(r"\{[^{}]*\}")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_HEADING = re.compile(r"(^|\s)#{1,6}\s+")
_MARKUP = re.compile(r"[*`>~]+")
_BULLET = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+")
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿️‍]")
_SPACES = re.compile(r"\s+")
_HAS_WORD = re.compile(r"\w", re.UNICODE)
# The prompt's "TIER 1 organic noise: say NOTHING" sometimes comes back as the
# model NARRATING its silence; live 2026-10-09 it spoke "Organic noise detected."
# and "Say nothing. Silence maintained." aloud. A sentence that is only that is
# dropped; real speech that merely mentions noise is untouched.
_SILENCE_NARRATION = re.compile(
    r"^\W*(?:(?:organic|background|ambient)\s+noise(?:\s+(?:detected|ignored))?|"
    r"silence(?:\s+maintained)?|(?:i\s+)?say\s+nothing|no\s+response(?:\s+needed)?|"
    r"staying\s+silent|ignoring(?:\s+(?:that|it|noise))?|noise\s+ignored|"
    r"not\s+addressed\s+to\s+me|\(?\s*silence\s*\)?)\W*$",
    re.IGNORECASE,
)


def strip_notes(text: str) -> str:
    """Replace say-exactly notes with their payload; drop every other model note."""
    if "[" not in (text or ""):
        return text or ""
    text = _SAY_PAYLOAD.sub(lambda m: m.group(1), text)
    return _NOTE_SPAN.sub(" ", text)


def open_note_at(buf: str) -> int:
    """Index of an unclosed "[" near the end of a streamed buffer, else -1."""
    i = (buf or "").rfind("[")
    if i < 0 or "]" in buf[i:] or len(buf) - i > _MAX_OPEN_NOTE:
        return -1
    return i


def sanitize_for_tts(sentence: str) -> str:
    """One sentence as it should sound. "" when nothing speakable is left."""
    t = strip_notes(sentence or "")
    t = _CODE_BLOCK.sub(" ", t)
    t = _LEAD_TAGS.sub("", t)
    t = _URL.sub(lambda m: m.group(1), t)
    t = _WIN_PATH.sub(lambda m: m.group(1), t)
    t = _POSIX_PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], t)
    t = _JSON_SPAN.sub(" ", t)
    t = _SNAKE.sub(lambda m: m.group(0).replace("_", " "), t)
    t = _HEADING.sub(r"\1", t)
    t = _BULLET.sub("", t)
    t = _MARKUP.sub("", t)
    t = _EMOJI.sub("", t)
    t = _SPACES.sub(" ", t).strip()
    if _SILENCE_NARRATION.match(t):
        return ""
    return t if _HAS_WORD.search(t) else ""
