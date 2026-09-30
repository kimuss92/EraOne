# utils/ordinal_selection.py
"""
Pure ordinal / positional command parser.

No Playwright, no I/O, no side effects. Used by the router to detect
"play the second video" / "play the third Netflix title" style commands
and to extract an ordinal + platform hint.

Public API:
  ordinal_to_index(text)        -> int | None
  parse_ordinal_command(text)   -> dict | None

Returned dict shape:
  {
    "ordinal": int,                    # 1..10, or -1 for "last"
    "platform_hint": "youtube"|"netflix"|None,
    "target_type": "video"|"title"|"result"|"item"|"one"|None,
    "raw": str,
  }

Returns None if user_text does not contain an ordinal selector.
Explicitly returns None for plain "play it", "play Despacito", etc.
"""

from __future__ import annotations

import re

_WORD_ORDINALS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "last": -1,
    # French
    "premier": 1,
    "première": 1,
    "premiere": 1,
    "deuxième": 2,
    "deuxieme": 2,
    # "second" is spelled identically in English and French and is already
    # mapped above — repeating it here only shadowed the earlier entry.
    "seconde": 2,
    "troisième": 3,
    "troisieme": 3,
    "quatrième": 4,
    "quatrieme": 4,
    "cinquième": 5,
    "cinquieme": 5,
    "sixième": 6,
    "sixieme": 6,
    "septième": 7,
    "septieme": 7,
    "huitième": 8,
    "huitieme": 8,
    "neuvième": 9,
    "neuvieme": 9,
    "dixième": 10,
    "dixieme": 10,
}

_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    # French
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    # "six" is shared with English above — repeating it shadowed that entry.
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
}

_TARGET_TYPES: tuple[str, ...] = (
    "video",
    "title",
    "result",
    "item",
    "number",
    "one",
    "grid",
    "titre",
    "vidéo",
    "video",
    "résultat",
    "resultat",
)


def ordinal_to_index(text: str) -> int | None:
    """Extract a 1..10 ordinal (or -1 for "last") from *text*.

    Accepts forms like:
      "second", "third", "fourth item", "2nd", "3rd", "10th",
      "number 4", "result number 3", "video 2", "title 5", "last",
      "number one", "top video", "video number one", "first one".
    Returns None when no ordinal is present.
    """
    if not text or not text.strip():
        return None
    t = text.strip().lower()

    # Build dynamic ordinal pattern from _WORD_ORDINALS keys sorted by length desc
    # to ensure longer matches (e.g. "deuxième") are tried before shorter ("deux")
    ordinal_keys = sorted(_WORD_ORDINALS.keys(), key=len, reverse=True)
    ordinal_alt = "|".join(re.escape(k) for k in ordinal_keys)

    # French: "le premier", "la première", etc.
    m = re.match(r"^(?:le|la)\s+(" + ordinal_alt + r")\b", t)
    if m:
        return _WORD_ORDINALS.get(m.group(1))

    # French number words: "deux", "trois", etc. (ordinal context: "le deux", "la trois")
    number_keys = sorted(_NUMBER_WORDS.keys(), key=len, reverse=True)
    number_alt = "|".join(re.escape(k) for k in number_keys)

    m = re.match(r"^(?:le|la)\s+(" + number_alt + r")\b", t)
    if m:
        return _NUMBER_WORDS.get(m.group(1))

    # "number one" / "video number one" / "title number two" / "grid number 2"
    m = re.match(
        r"^(?:the\s+)?(?:video|title|result|item|grid)\s+number\s+(" + number_alt + r")\b", t
    )
    if m:
        return _NUMBER_WORDS.get(m.group(1))

    # "number one" (standalone)
    m = re.match(r"^(?:the\s+)?number\s+(" + number_alt + r")\b", t)
    if m:
        return _NUMBER_WORDS.get(m.group(1))

    # "title two" / "grid two" / "result three" (target + number word, no "number" token)
    m = re.match(r"^(?:the\s+)?(?:video|title|result|item|grid)\s+(" + number_alt + r")\b", t)
    if m:
        return _NUMBER_WORDS.get(m.group(1))

    # French ordinal + target type: "premier titre", "seconde vidéo"
    m = re.match(
        r"^("
        + ordinal_alt
        + r")\s+(?:titre|vid(?:é|e)o|r(?:é|e)sultat|title|video|result|item|one)\b",
        t,
    )
    if m:
        return _WORD_ORDINALS.get(m.group(1))

    m = re.match(r"^(?:the\s+)?top(?:\s+(?:video|result|item|title|one))?\s*$", t)
    if m:
        return 1

    # Standalone ordinal word (exact match) — covers both English and French
    m = re.match(r"^(?:the\s+)?(" + ordinal_alt + r")$", t)
    if m:
        return _WORD_ORDINALS.get(m.group(1))

    # Standalone ordinal word (word boundary) — for phrases like "second title"
    m = re.match(r"^(?:the\s+)?(" + ordinal_alt + r")\b", t)
    if m:
        return _WORD_ORDINALS.get(m.group(1))

    m = re.match(r"^(?:the\s+)?(\d+)\s*(?:st|nd|rd|th|er|ère|eme|ème)\b", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10:
            return n

    m = re.match(
        r"^(?:the\s+)?(?:video|number|item|result|title|titre|grid)\s+(?:item\s+)?(?:number\s+)?(\d+)\b",
        t,
    )
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10:
            return n

    return None


def _detect_platform_hint(text: str) -> str | None:
    t = (text or "").lower()
    has_youtube = bool(re.search(r"\byoutube\b|\byt\b|\byoutu\.be\b", t))
    has_netflix = bool(re.search(r"\bnetflix\b|\bnf\b", t))
    if has_youtube and not has_netflix:
        return "youtube"
    if has_netflix and not has_youtube:
        return "netflix"
    if has_youtube and has_netflix:
        return "ambiguous"
    return None


def _detect_target_type(text: str) -> str | None:
    t = (text or "").lower()
    for word in _TARGET_TYPES:
        if re.search(rf"\b{word}\b", t):
            return word
    return None


def _normalize_phrase(phrase: str) -> str:
    p = phrase.strip().lower()
    p = re.sub(r"^(?:please|can you|could you|would you|just)\s+", "", p)
    p = re.sub(r"^(?:play|open|select|click|show|start|launch|choose|pick)\s+", "", p)
    p = re.sub(r"^(?:i\s+)?mean\s+", "", p)
    # French "et de <ordinal>" → "<ordinal>" (e.g. "et de seconde" → "seconde")
    p = re.sub(r"\bet\s+de\s+", "", p)
    p = re.sub(r"\s+", " ", p).strip()
    return p


def parse_ordinal_command(user_text: str) -> dict | None:
    """Return a parsed ordinal command dict, or None.

    Recognized forms (case-insensitive):
      "play the second video" / "open the 3rd result"
      "play first item" / "play the fourth title"
      "video number 3" / "result number 4" / "title number 2"
      "1st", "2nd", "3rd", "4th", ...
      "play the first Netflix title" / "play the third YouTube video"
      "open the last one"

    Returns None for:
      "play it", "play Despacito", "play music", "open YouTube", ...
    """
    if not user_text or not user_text.strip():
        return None

    raw = user_text.strip()
    normalized = _normalize_phrase(raw)
    if not normalized:
        return None

    if not re.search(
        r"\b(top|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|one|two|three|four|five|six|seven|eight|nine|ten|premier|première|premiere|deuxième|deuxieme|second|seconde|troisième|troisieme|quatrième|quatrieme|cinquième|cinquieme|sixième|sixieme|septième|septieme|huitième|huitieme|neuvième|neuvieme|dixième|dixieme|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|\d+(?:st|nd|rd|th|er|ère|eme|ème)?)\b",
        normalized,
    ):
        return None

    ordinal = ordinal_to_index(normalized)
    if ordinal is None:
        return None

    if not any(tok in normalized for tok in _TARGET_TYPES) and ordinal not in (-1,):
        if "number" in normalized or "top" in normalized:
            pass
        else:
            ordinal_keys = sorted(_WORD_ORDINALS.keys(), key=len, reverse=True)
            ordinal_alt = "|".join(re.escape(k) for k in ordinal_keys)
            m = re.match(r"^(?:the\s+)?(" + ordinal_alt + r")$", normalized)
            if not m:
                # Also accept "le/la <ordinal>" pattern for French
                m = re.match(r"^(?:le|la)\s+(" + ordinal_alt + r")$", normalized)
                if not m:
                    return None

    if ordinal == -1 and "last" not in normalized:
        return None

    platform_hint_raw = _detect_platform_hint(normalized)
    if platform_hint_raw == "ambiguous":
        platform_hint = "ambiguous"
    else:
        platform_hint = platform_hint_raw

    target_type = _detect_target_type(normalized)
    if ordinal == -1:
        target_type = target_type or "one"

    return {
        "ordinal": ordinal,
        "platform_hint": platform_hint,
        "target_type": target_type,
        "raw": raw,
    }
