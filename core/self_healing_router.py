# core/self_healing_router.py
"""Self-healing tool router for Era One."""

from __future__ import annotations

import re
from typing import Any

from core.action_registry import canonicalize_tool_call
from core.browser_grid_state import (
    get_last_user_query,
    get_scroll_context,
    load_recent_grids,
    set_last_user_query,
)
from core.intent_memory import load_state, remember_action
from core.utils import log
from core.window_awareness import get_active_title
from utils.ordinal_selection import parse_ordinal_command


def _norm(s: str | None) -> str:
    return (s or "").lower().strip()


# ── Platform domain family helpers (router-side, no Playwright required) ─────

_MEDIA_BROWSER_DOMAINS: dict[str, str] = {
    "youtube": "youtube.com",
    "netflix": "netflix.com",
}


def _is_platform_url(url: str, platform: str) -> bool:
    if not url:
        return False
    canonical = _MEDIA_BROWSER_DOMAINS.get((platform or "").lower().strip(), "")
    if not canonical:
        return False
    u = url.lower()
    return canonical in u


def _is_explicit_new_tab_request(user_text: str, platform: str) -> bool:
    """True if user explicitly said 'open a new <platform> tab' / 'open new <platform> tab'."""
    if not user_text:
        return False
    t = user_text.lower()
    patterns = [
        rf"open\s+a\s+new\s+{platform}\s+tab",
        rf"open\s+new\s+{platform}\s+tab",
        rf"open\s+another\s+{platform}\s+tab",
        rf"new\s+{platform}\s+tab",
    ]
    return any(re.search(p, t) for p in patterns)


def _is_explicit_platform_in_text(user_text: str, platform: str) -> bool:
    t = (user_text or "").lower()
    if platform == "youtube":
        return bool(re.search(r"\b(youtube|yt|youtu\.be)\b", t))
    if platform == "netflix":
        return bool(re.search(r"\b(netflix|nf)\b", t))
    return False


def _is_active_tab_override(user_text: str) -> bool:
    t = (user_text or "").lower()
    return "active tab" in t or "current tab" in t


def _build_platform_home_url(platform: str) -> str:
    p = (platform or "").lower().strip()
    if p in ("youtube", "yt"):
        return "https://www.youtube.com"
    if p in ("netflix", "nf"):
        return "https://www.netflix.com"
    return ""


def _detect_platform_home_intent(user_text: str) -> str | None:
    """Detect intent to go to YouTube/Netflix home page.

    Returns platform name ('youtube' | 'netflix') or None.
    """
    if not user_text:
        return None
    t = user_text.lower()
    if re.search(r"\byoutube\s+home\b|\bhome\s+(?:page\s+)?(?:of\s+)?youtube\b", t):
        return "youtube"
    if re.search(r"\bnetflix\s+home\b|\bhome\s+(?:page\s+)?(?:of\s+)?netflix\b", t):
        return "netflix"
    if re.search(r"\byt\s+home\b|\bhome\s+(?:page\s+)?(?:of\s+)?yt\b", t):
        return "youtube"
    if re.search(r"\bnf\s+home\b|\bhome\s+(?:page\s+)?(?:of\s+)?nf\b", t):
        return "netflix"
    return None


# ── Ordinal / positional command routing ─────────────────────────────────────


def _is_grid_question_or_analysis(text: str) -> bool:
    """True if *text* is a question/analysis (must stay in source tool, e.g. screen_process).

    Guards against diverting interrogative or descriptive phrases
    (e.g. "What is the title of the first result?", "Identify the first card.")
    to ordinal routing — which would otherwise turn them into grid_play actions.

    Real imperative orders ("Select the first result.", "Click row 2 title 5.")
    are NOT detected here and flow through to ordinal routing unchanged.
    """
    t = (text or "").lower().strip()

    question_starts = (
        "what ",
        "what's ",
        "which ",
        "who ",
        "where ",
        "tell me",
        "identify",
        "describe",
        "analyze",
        "analyse",
        "read ",
        "look at",
        "what do you see",
    )

    question_patterns = (
        "what is the title",
        "title of the first",
        "title of the second",
        "first search result shown",
        "what do you see on",
        "identify the first",
        "describe the first",
    )

    if t.endswith("?"):
        return True

    if t.startswith(question_starts):
        return True

    return any(p in t for p in question_patterns)


def _route_ordinal_command(user_text: str) -> tuple[str, dict] | None:
    """Route 'play the second video' / 'play the third Netflix title' style commands.

    Also handles V9C.3 row/column commands like 'row 2 title 5'.
    Returns (tool, params) if routable, else None.
    """
    if not user_text:
        return None

    # V9C.3: Check row/column position first (row/col always routes to netflix)
    grid_pos = parse_netflix_grid_position(user_text)
    if grid_pos:
        if "row" in grid_pos and "col" in grid_pos:
            row, col = grid_pos["row"], grid_pos["col"]
            # Row/col only applies to Netflix grid (not YouTube)
            params = {"action": "grid_play", "row": row, "col": col, "browser": "brave"}
            log(
                "Router",
                f"ordinal.routing tool=netflix_control row={row} col={col} (grid_position)",
            )
            return ("netflix_control", params)
        if "ordinal" in grid_pos:
            ordinal = grid_pos["ordinal"]
        else:
            ordinal = None
    else:
        parsed = parse_ordinal_command(user_text)
        if not parsed:
            return None
        ordinal = parsed.get("ordinal")
        platform_hint = parsed.get("platform_hint")

    if ordinal is None and grid_pos is None:
        return None

    if ordinal is not None:
        # Determine platform hint: prefer explicit hint from parse_ordinal_command.
        # Bug fix: when parse_netflix_grid_position matches an ordinal but no row/col,
        # grid_pos is non-None, but it carries no platform hint of its own.
        # We must still consult parse_ordinal_command for platform_hint, otherwise
        # 'play the first Netflix title' routes to youtube_video (the only seeded
        # grid in some contexts) instead of netflix_control.
        parsed_for_hint = parse_ordinal_command(user_text) or {}
        platform_hint = parsed_for_hint.get("platform_hint")
        if platform_hint == "ambiguous":
            return None

        if platform_hint in ("youtube", "netflix"):
            target_platform = platform_hint
        else:
            grids = load_recent_grids()
            if "youtube" in grids and "netflix" in grids:
                return None
            if "youtube" in grids:
                target_platform = "youtube"
            elif "netflix" in grids:
                target_platform = "netflix"
            else:
                return None

        if target_platform == "youtube":
            params = {"action": "grid_play", "ordinal": ordinal, "browser": "brave"}
            log(
                "Router",
                f"ordinal.routing tool=youtube_video ordinal={ordinal} platform_hint=youtube",
            )
            return ("youtube_video", params)

        params = {"action": "grid_play", "ordinal": ordinal, "browser": "brave"}
        log(
            "Router",
            f"ordinal.routing tool=netflix_control ordinal={ordinal} platform_hint=netflix",
        )
        return ("netflix_control", params)

    return None


def _route_i_mean_on_platform(user_text: str) -> tuple[str, dict] | None:
    """Route 'I mean on YouTube/Netflix/Spotify' → reuse last query, correct platform.

    Must run AFTER ordinal routing (to avoid stealing 'I mean the second title')
    but BEFORE query extraction / Spotify default.

    Returns (tool, params) if routable, else None.
    """
    if not user_text:
        return None
    t = user_text.lower().strip()
    m = re.search(r"i\s+mean\s+on\s+(youtube|yt|netflix|nf|spotify)", t)
    if not m:
        return None
    platform_raw = m.group(1)
    platform_map = {
        "youtube": "youtube",
        "yt": "youtube",
        "netflix": "netflix",
        "nf": "netflix",
        "spotify": "spotify",
    }
    target_platform = platform_map.get(platform_raw)
    if not target_platform:
        return None

    # Reuse last user query from runtime state
    last = get_last_user_query()
    last_query = last.get("query", "")

    # If no last query, try extracting from user_text after "I mean on <platform>"
    remaining = re.sub(r"i\s+mean\s+on\s+(?:youtube|yt|netflix|nf|spotify)\s*", "", t).strip()
    if remaining and remaining not in ("play", "search", "find", "watch", ""):
        query = remaining
    else:
        query = last_query

    if target_platform in ("youtube", "netflix") and not query:
        # No query to route — fall through to normal routing
        return None

    if target_platform == "youtube":
        log("Router", f"i_mean_on_platform → youtube_video play (query='{query}')")
        return "youtube_video", {"action": "play", "query": query}

    if target_platform == "netflix":
        log("Router", f"i_mean_on_platform → netflix_control search (query='{query}')")
        return "netflix_control", {"action": "search", "query": query}

    # Spotify — let the spotify bypass handle it
    return None


def _route_next_video(user_text: str) -> tuple[str, dict] | None:
    """Route 'next video' / 'next on YouTube' to youtube_video next_video.

    Returns (tool, params) if routable, else None.
    """
    if not user_text:
        return None
    t = user_text.lower().strip()
    if re.search(r"\bnext\b.*\b(?:episode|netflix|nf)\b", t):
        return None
    if re.search(r"\bnext\b.*\b(?:video|youtube|yt)\b|\b(?:video|youtube|yt)\b.*\bnext\b", t):
        log("Router", "next_video.routing → youtube_video next_video (explicit match)")
        return "youtube_video", {"action": "next_video"}
    if t in (
        "next",
        "next video",
        "skip to next",
        "skip to next video",
        "play next",
        "play next video",
    ):
        try:
            ctx = get_scroll_context()
            if ctx == "youtube":
                log("Router", "next_video.routing → youtube_video next_video (scroll context)")
                return "youtube_video", {"action": "next_video"}
        except Exception:
            pass
    return None


# ── Scroll target inference ──────────────────────────────────────────────────


def _infer_scroll_target(user_text: str, params: dict | None) -> str | None:
    """Return 'youtube' | 'netflix' | None for generic scroll commands.

    Rules:
      * Explicit platform name in text -> that platform.
      * 'active tab' / 'current tab' -> None.
      * URL in params containing youtube.com or netflix.com -> that platform.
      * Recent scroll context within 90s and no explicit platform -> context platform.
      * Otherwise -> None.
    """
    p = params or {}
    explicit = _norm(p.get("target"))
    if explicit in ("youtube", "netflix"):
        return explicit

    if _is_active_tab_override(user_text):
        return None

    if _is_explicit_platform_in_text(user_text, "youtube"):
        return "youtube"
    if _is_explicit_platform_in_text(user_text, "netflix"):
        return "netflix"

    url = (p.get("url") or "").lower()
    if "youtube.com" in url:
        return "youtube"
    if "netflix.com" in url:
        return "netflix"

    recent = get_scroll_context()
    if recent in ("youtube", "netflix"):
        return recent

    return None


def _media_target_from_text(text: str) -> str | None:
    t = _norm(text)
    if "youtube" in t or "video" in t or "clip" in t:
        return "youtube"
    if "netflix" in t or "movie" in t or "show" in t or "series" in t:
        return "netflix"
    if "spotify" in t or "music" in t or "song" in t or "track" in t:
        return "spotify"
    return None


_CLEAN_NETFLIX_QUERY_PREFIXES = frozenset(
    {
        "série",
        "serie",
        "film",
        "movie",
        "regarder",
        "regarde",
        "chercher",
        "cherche",
        "lance",
        "lancer",
    }
)

_FILLER_PREFIXES = frozenset(
    {
        "c'est",
        "c est",
        "c’est",
        "c'est le",
        "c'est la",
        "c'est où",
        "regarde le",
        "regarde la",
        "regarder le",
        "regarder la",
        "mets",
        "mets le",
        "mets la",
        "mets moi",
        "play the",
        "play a",
        "play",
        "open the",
        "open",
        "search for",
        "search",
        "find the",
        "find",
        "watch the",
        "watch",
    }
)

_PLATFORM_SUFFIXES = frozenset(
    {
        "sur netflix",
        "en netflix",
        "on netflix",
        "in netflix",
        "sur youtube",
        "en youtube",
        "on youtube",
        "in youtube",
    }
)


def _normalize_search_query(query: str | None) -> str | None:
    """Strip French/English filler prefixes and platform suffixes from a search query.

    Examples:
      "c'est le one piece" → "One Piece"
      "c'est où One Piece en Netflix ?" → "One Piece"
      "regarde le one piece sur Netflix" → "One Piece"
      "play the one piece on Netflix" → "One Piece"
      "One Piece" → "One Piece" (unchanged)
    """
    if not query or not query.strip():
        return query
    q = query.strip()

    # Strip platform suffixes
    lower = q.lower()
    for suffix in _PLATFORM_SUFFIXES:
        if suffix in lower:
            idx = lower.index(suffix)
            q = q[:idx].strip()
            lower = q.lower()
            break

    # Strip trailing question marks, punctuation
    q = q.rstrip("?¿!")

    # Strip filler prefixes (case-insensitive, whole-word; longest first to avoid partial match)
    lower = q.lower()
    for prefix in sorted(_FILLER_PREFIXES, key=len, reverse=True):
        if lower.startswith(prefix):
            rest = q[len(prefix) :].strip()
            if rest and len(rest) >= 2:
                q = rest
                lower = q.lower()
                # Re-check after shrinking (e.g. "c'est le one piece" -> "one piece" after "c'est le" is stripped)
                break

    # Final cleanup: remove extra whitespace
    import re

    q = re.sub(r"\s+", " ", q).strip()
    return q if q else query


def _clean_extracted_query(query: str | None) -> str | None:
    """Strip leaked media-type prefixes from an extracted query.

    When French patterns like "Série Champis sur Netflix" are parsed,
    the regex captures "Champis" directly — but edge cases or alternate
    phrasings may leave "série" or "film" at the start.  This removes
    any known prefix that leaked through. Also normalizes filler text.
    """
    if not query or not query.strip():
        return query
    q = query.strip()
    lower = q.lower()
    for prefix in _CLEAN_NETFLIX_QUERY_PREFIXES:
        if lower.startswith(prefix) and len(q) > len(prefix) + 1:
            rest = q[len(prefix) :].strip()
            if rest:
                q = rest
                break
    return _normalize_search_query(q)


# ── V9C.2: Netflix media query cleaner ─────────────────────────────────────

_NETFLIX_START_FILLERS = (
    "do you have",
    "have you got",
    "is there",
    "can you please",
    "could you please",
    "can you",
    "could you",
    "would you",
    "please",
    "find",
    "search for",
    "search",
    "look for",
    "look up",
    "playing the movie",
    "playing the series",
    "playing the show",
    "playing",
    "play the movie",
    "play the series",
    "play the show",
    "play",
    "watch the movie",
    "watch the series",
    "watch the show",
    "watch",
    "open",
)

_NETFLIX_MID_FILLERS = (
    " on netflix",
    " in netflix",
    " on nf",
    " the movie",
    " the series",
    " the show",
    " on netflix please",
    " in netflix please",
)

_ORDINAL_RE = re.compile(
    r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|number\s+\d+|number\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)"
    r"|title\s+number|result\s+number|item\s+number|grid\s+item"
    r"|\d+(?:st|nd|rd|th))\b",
    re.I,
)


def _clean_netflix_media_query(text: str) -> str | None:
    """Extract a clean Netflix title from a natural-language command.

    Strips wake words, polite filler, action verbs, platform suffixes,
    and media-type prefixes.  Returns None when the input is an ordinal
    command (e.g. 'select title number two') so the ordinal router can
    handle it instead.
    """
    if not text or not text.strip():
        return None
    t = text.strip()

    # V9C.3: Row/column guard — do NOT clean grid position commands into queries.
    grid_pos = parse_netflix_grid_position(text)
    if grid_pos and ("row" in grid_pos or "ordinal" in grid_pos):
        return None

    # Ordinal guard: do NOT clean ordinal commands into queries.
    if _ORDINAL_RE.search(t):
        t_lower = t.lower()
        has_action = any(w in t_lower for w in ("play", "watch", "search", "find", "open", "look"))
        if not has_action:
            return None

    # Strip trailing punctuation / question marks
    t = t.rstrip("?¿!.,;:")

    # Strip question forms at start: "is X on Netflix?" / "is there X on Netflix?"
    m = re.match(r"^(?:is|est[áa])\s+(?:there\s+)?", t, re.I)
    if m:
        t = t[m.end() :]
        t = re.sub(r"\s+(?:on|en|sur)\s+(?:netflix|nf|youtube|yt)\s*$", "", t, flags=re.I).strip()

    # Loop filler stripping — fillers may stack (e.g. "do you have playing X")
    for _pass in range(5):
        prev = t
        lower = t.lower()
        for filler in _NETFLIX_START_FILLERS:
            if lower.startswith(filler):
                rest = t[len(filler) :].strip()
                if rest:
                    t = rest
                    lower = t.lower()
                    break
        for mid in _NETFLIX_MID_FILLERS:
            if mid in lower:
                idx = lower.index(mid)
                t = (t[:idx] + t[idx + len(mid) :]).strip()
                lower = t.lower()
        t = re.sub(r"\s+(?:on|en|sur)\s+(?:netflix|nf|youtube|yt)\s*$", "", t, flags=re.I).strip()
        t = t.rstrip("?¿!.,;:")
        if t == prev:
            break

    # Guard: prevent over-stripping "playing" from titles like "Playing with Fire".
    # If the original text started with "playing" and the loop stripped it away,
    # leaving a very short result (≤ 2 words), re-run skipping "playing"
    # and all fillers that are prefixes of "playing" (e.g. "play").
    orig_lower = text.strip().lower()
    if orig_lower.startswith("playing") and "playing" not in t.lower():
        t_words = len(t.split())
        if t_words <= 2:
            t = text.strip()
            for _pass2 in range(5):
                prev2 = t
                lower2 = t.lower()
                for filler in _NETFLIX_START_FILLERS:
                    if "playing".startswith(filler):
                        continue
                    if lower2.startswith(filler):
                        rest = t[len(filler) :].strip()
                        if rest:
                            t = rest
                            lower2 = t.lower()
                            break
                for mid in _NETFLIX_MID_FILLERS:
                    if mid in lower2:
                        idx = lower2.index(mid)
                        t = (t[:idx] + t[idx + len(mid) :]).strip()
                        lower2 = t.lower()
                t = re.sub(
                    r"\s+(?:on|en|sur)\s+(?:netflix|nf|youtube|yt)\s*$", "", t, flags=re.I
                ).strip()
                t = t.rstrip("?¿!.,;:")
                if t == prev2:
                    break

    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()

    if not t:
        return None

    # Title-case if original casing was lost
    if t == t.lower() and len(t) > 2:
        t = t.title()

    return t


# ── V9C.3: Netflix visible row/column parser ───────────────────────────────

_WORD_TO_NUM: dict[str, int] = {
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
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
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
    "premier": 1,
    "première": 1,
    "premiere": 1,
    "deuxième": 2,
    "deuxieme": 2,
    "seconde": 2,
    "troisième": 3,
    "troisieme": 3,
    "quatrième": 4,
    "quatrieme": 4,
    "cinquième": 5,
    "cinquieme": 5,
    "onzième": 11,
    "onzieme": 11,
    "douzième": 12,
    "douzieme": 12,
    "treizième": 13,
    "treizieme": 13,
    "quatorzième": 14,
    "quatorzieme": 14,
    "quinzième": 15,
    "quinzieme": 15,
}


def _parse_word_or_digit(text: str) -> int | None:
    """Parse a word ('two') or digit ('2') or ordinal ('2nd', '5th') into an int."""
    t = text.strip().lower()
    if t in _WORD_TO_NUM:
        return _WORD_TO_NUM[t]
    m = re.match(r"^(\d+)(?:st|nd|rd|th)?$", t)
    if m:
        return int(m.group(1))
    return None


def parse_netflix_grid_position(user_text: str) -> dict | None:
    """Parse visible row/column or ordinal position from Netflix grid commands.

    Returns dict with keys:
      {"row": int, "col": int}       — row/column selection
      {"ordinal": int}                — simple ordinal (e.g. "first result")
      {"row": 1, "col": 1}           — positional alias (e.g. "top left")
    Returns None if no grid position detected.

    Priority: row/col wins over ordinal.  Top-left → row=1, col=1.
    """
    if not user_text:
        return None
    t = user_text.lower().strip()

    # ── Top-left / top-right / bottom-left / bottom-right → row/col ──
    corner_map = {
        "top left": (1, 1),
        "top-left": (1, 1),
        "upper left": (1, 1),
        "top right": (1, 99),
        "top-right": (1, 99),
        "upper right": (1, 99),
        "bottom left": (99, 1),
        "bottom-left": (99, 1),
        "lower left": (99, 1),
        "bottom right": (99, 99),
        "bottom-right": (99, 99),
        "lower right": (99, 99),
    }
    for phrase, (r, c) in corner_map.items():
        if phrase in t:
            return {"row": r, "col": c}

    # ── Explicit row/col patterns ──
    # "row 2 title 5", "row two column five", "second row fifth title"
    # "title 5 in row 2", "5th title in row 2"
    # Patterns checked in priority order (row/col wins over ordinal)

    # Pattern A: "row <N> ... (title|column) <N>"
    m = re.search(
        r"(?:row|fila)\s+(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)\s+"
        r"(?:title|titre|col(?:umn)?|résultat|result)?\s*"
        r"(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)",
        t,
    )
    if m:
        row = _parse_word_or_digit(m.group(1))
        col = _parse_word_or_digit(m.group(2))
        if row and col:
            return {"row": row, "col": col}

    # Pattern B: "<N>th title in row <N>"
    m = re.search(
        r"(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)\s+"
        r"(?:title|titre|result|résultat|item|thumbnail)\s+"
        r"(?:in|dans|del?|en)\s+"
        r"(?:row|fila|la\s+fila)\s+"
        r"(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)",
        t,
    )
    if m:
        col = _parse_word_or_digit(m.group(1))
        row = _parse_word_or_digit(m.group(2))
        if row and col:
            return {"row": row, "col": col}

    # Pattern C: "row <N>" without explicit col (col defaults to 1)
    m = re.search(r"(?:row|fila)\s+(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)", t)
    if m:
        row = _parse_word_or_digit(m.group(1))
        if row:
            # Check if there's also a column reference nearby
            m_col = re.search(
                r"(?:title|titre|col(?:umn)?|résultat|result)?\s*"
                r"(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)",
                t[m.end() :],
            )
            if m_col:
                col = _parse_word_or_digit(m_col.group(1))
                if col:
                    return {"row": row, "col": col}
            return {"row": row, "col": 1}

    # ── Simple ordinal patterns (no row/col) ──
    # "first result", "select thumbnail number one", "play first"
    ordinal_result = parse_ordinal_command(user_text)
    if ordinal_result and ordinal_result.get("ordinal") is not None:
        return {"ordinal": ordinal_result["ordinal"]}

    # Standalone ordinal words without parse_ordinal_command detection
    # Guard: after matching the ordinal word, the next word (if any)
    # must be a suffix word or platform preposition — otherwise it's
    # likely a title component (e.g. "One" in "One Piece").
    m = re.match(
        r"^(?:select|choose|play|pick|click)\s+(?:the\s+)?"
        r"(" + "|".join(_WORD_TO_NUM.keys()) + r"|\d+(?:st|nd|rd|th)?)"
        r"(?:\s+(.+))?$",
        t,
    )
    if m:
        ordinal = _parse_word_or_digit(m.group(1))
        if ordinal:
            rest = (m.group(2) or "").strip().lower()
            if rest:
                _ORDINAL_SUFFIX_WORDS = frozenset(
                    {
                        "result",
                        "résultat",
                        "title",
                        "titre",
                        "card",
                        "thumbnail",
                        "one",
                        "video",
                        "item",
                        "selection",
                    }
                )
                _PLATFORM_PREPOS = frozenset(
                    {
                        "on",
                        "in",
                        "sur",
                        "en",
                        "à",
                        "a",
                        "dans",
                    }
                )
                first_word = rest.split(maxsplit=1)[0]
                if first_word not in _ORDINAL_SUFFIX_WORDS and first_word not in _PLATFORM_PREPOS:
                    return None
            return {"ordinal": ordinal}

    return None


def _is_netflix_availability_or_list_intent(user_text: str) -> bool:
    """Detect availability / list / search-only intent.

    Returns True when the user wants to CHECK availability or SEE results,
    rather than explicitly play a title.
    """
    t = (user_text or "").lower()
    return bool(
        re.search(
            r"\b(do you have|have you got|is .+ on netflix|available|availability"
            r"|search for|search|find|look for|look up|show results|list|show me"
            r"|what .+ (?:is|are) on|anything .+ on"
            r")\b",
            t,
        )
    )


def _extract_media_query(
    params: dict | None, user_text: str, target: str | None, action: str | None
) -> str | None:
    """Extract a media search query from Gemini params or user_text.

    Priority:
    1. Gemini-provided params keys (query, title, value, text, media_title, search, episode)
    2. user_text parsing for "play/search/find/watch <title> on <platform>" patterns
    3. user_text parsing for "open <platform> and play/search/find/watch <query>" patterns
    4. user_text parsing for standalone "<query> on <platform>" patterns
    5. user_text parsing for standalone "search <query>" / "find <query>" patterns

    Returns None for plain play/resume/open/focus (no content command).
    Preserves original case from params or user_text.
    """
    p = params or {}
    for key in ("query", "title", "value", "text", "media_title", "search", "episode"):
        val = p.get(key)
        if val and isinstance(val, str) and val.strip():
            return _clean_extracted_query(val.strip())
    raw = user_text.strip()
    # "play/search/find/watch <query> on/sur <platform>" (English + French verbs)
    m = re.search(
        r"(?:play|search|find|watch|r[ée]garde|cherche|lance)\s+(.+?)\s+(?:on|sur)\s+(?:youtube|netflix|spotify)",
        raw,
        re.I,
    )
    if m:
        return _clean_extracted_query(m.group(1).strip())
    # French: "série/serie/film <query> sur <platform>" — strip the media prefix
    m = re.search(r"(?:s[eé]rie|serie|film)\s+(.+?)\s+sur\s+(?:netflix|nf)\b", raw, re.I)
    if m:
        return _clean_extracted_query(m.group(1).strip())
    # "open <platform> and <action> <query>"
    m = re.search(
        r"open\s+(?:youtube|netflix|yt|nf)\s+and\s+(?:play|search|find|watch)\s+(.+)", raw, re.I
    )
    if m:
        return _clean_extracted_query(m.group(1).strip())
    # "watch <query>"
    m = re.search(r"watch\s+(.+)", raw, re.I)
    if m:
        candidate = m.group(1).strip().lower()
        if candidate not in ("youtube", "netflix", "yt", "nf", "the", "a video", "a movie"):
            return _clean_extracted_query(m.group(1).strip())
    # Question forms: "Is/Está X on Netflix?" → extract X (before general <query> on <platform>)
    m = re.search(r"(?:is|est[áa])\s+(.+?)\s+(?:on|en)\s+(?:netflix|nf|youtube|yt)\b", raw, re.I)
    if m:
        return _clean_extracted_query(m.group(1).strip())
    # "<query> on/sur <platform>" — but reject bare command / scroll words
    _QUERY_EXCLUDES = frozenset(
        {
            "play",
            "search",
            "find",
            "watch",
            "open",
            "focus",
            "resume",
            "scroll",
            "scroll up",
            "scroll down",
            "i mean",
            "",
        }
    )
    m = re.search(r"^(.+?)\s+(?:on|sur)\s+(?:youtube|netflix|yt|nf)\s*$", raw, re.I)
    if m:
        candidate = m.group(1).strip().lower()
        if candidate not in _QUERY_EXCLUDES:
            return _clean_extracted_query(m.group(1).strip())
    m = re.search(r"\b(.+?)\s+(?:on|sur)\s+(?:youtube|netflix)\b", raw, re.I)
    if m:
        candidate = m.group(1).strip().lower()
        if candidate not in _QUERY_EXCLUDES:
            return _clean_extracted_query(candidate)
    act = _norm(action)
    if act in ("search", "find"):
        m = re.search(r"(?:search|find)\s+(.+)", raw, re.I)
        if m:
            return _clean_extracted_query(m.group(1).strip())
    return None


def _detect_scroll_intent(user_text: str, params: dict | None) -> tuple[bool, str]:
    """Detect scroll intent and direction from user_text and/or params.

    Returns (is_scroll: bool, direction: str).
    Direction is one of: "up", "down", "left", "right".
    """
    p = params or {}
    action = _norm(p.get("action"))
    direction = _norm(p.get("direction"))

    if action == "scroll":
        if direction in ("up", "down", "left", "right"):
            return True, direction
        return True, "down"

    text = _norm(user_text)
    m = re.search(r"scroll\s+(up|down|left|right)\b", text)
    if m:
        return True, m.group(1)

    return False, ""


def _route_browser_intent(user_text: str, params: dict | None) -> tuple[str, dict] | None:
    """Route open/focus/list-tabs/close-tab commands to browser_control.

    Returns (tool, params) tuple if a match is found, None otherwise.
    Preserves original case from user_text for URL fragments.
    """
    p = params or {}
    action = _norm(p.get("action"))
    raw = user_text.strip()
    text = _norm(raw)

    _DOMAIN_MAP = {
        "youtube": "https://www.youtube.com",
        "netflix": "https://www.netflix.com",
        "spotify": "https://open.spotify.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "github": "https://github.com",
    }
    _ALIASES = {
        "yt": "youtube",
        "gmail": "gmail",
    }

    # Apps with a real desktop client: "open/focus spotify" must drive the
    # installed app, never the web player. Only an explicit "spotify web"/
    # "in the browser" sends these to browser_control.
    _DESKTOP_FIRST = {"spotify"}
    _WEB_MARKERS = ("web", "browser", "brave", "chrome", "edge", "firefox", "tab", "online")

    # 1. Open / focus known site → go_to (reuses existing media tab)
    if action in ("open", "focus") or text.startswith(("open ", "focus ")):
        m = re.search(r"(?:open|focus(?:\s+on)?)\s+(.+)", raw, re.I)
        if m:
            site = m.group(1).strip().lower()
            wants_web = any(w in text for w in _WEB_MARKERS)
            # Strip web qualifiers so "spotify web" / "spotify in brave" still
            # resolve to the spotify domain once web intent is established.
            bare = re.sub(
                r"\b(?:web|online|in|on|the|browser|brave|chrome|edge|firefox|tab)\b",
                " ",
                site,
            ).strip()
            key = _ALIASES.get(bare, bare) or _ALIASES.get(site, site)
            if key in _DESKTOP_FIRST and not wants_web:
                return None
            url = _DOMAIN_MAP.get(key)
            if url:
                return ("browser_control", {"action": "go_to", "url": url})

    # 2. List tabs
    if action == "list_tabs" or (action in ("list", "show") and "tab" in text):
        return ("browser_control", {"action": "list_tabs"})

    # 3. Close tab — also matches "close youtube", "close netflix" without requiring "tab" keyword
    # IMPORTANT: a site name (youtube/netflix/spotify/etc.) ALWAYS means "close that tab",
    # even if the browser name (brave/chrome) also appears in the sentence.
    # e.g. "close youtube in brave" → close the YouTube tab, NOT the whole Brave browser.
    if action in ("close_tab", "close"):
        has_tab_keyword = "tab" in text

        # Known site names that map to tabs, not the browser process itself.
        _SITE_NAME_TOKENS = (
            "youtube",
            "yt",
            "netflix",
            "nf",
            "spotify",
            "twitch",
            "disney",
            "hulu",
            "prime",
            "twitch",
            "vimeo",
            "github",
            "gmail",
            "google",
            "twitter",
            "reddit",
            "facebook",
            "amazon",
            "wikipedia",
        )
        # Detect whole-browser close intent: "close brave", "close browser", "close chrome"
        # but ONLY when no site name is present.
        text_lower = text.lower()
        has_site_name = any(
            re.search(r"\b" + re.escape(tok) + r"\b", text_lower) for tok in _SITE_NAME_TOKENS
        )
        is_whole_browser = (
            bool(re.search(r"\b(browser|brave|chrome|window|app)\b", text_lower))
            and not has_site_name
        )

        m = re.search(r"close\s+(?:the\s+)?(?:tab\s+)?(?:for\s+|named?\s+)?(.+)", raw, re.I)
        if m:
            fragment = m.group(1).strip()
            # Strip trailing "tab" from fragment: "youtube tab" → "youtube"
            fragment = re.sub(r"\s+tab\s*$", "", fragment, flags=re.I).strip()
            # Strip trailing browser-name qualifier: "youtube in brave" → "youtube",
            # "youtube on chrome" → "youtube". This is the key fix for the
            # "close youtube in brave → kills whole Brave" bug.
            fragment = re.sub(
                r"\s+(?:in|on|inside|with)\s+(?:the\s+)?(?:brave|chrome|browser|edge|firefox|opera|safari)\s*$",
                "",
                fragment,
                flags=re.I,
            ).strip()
            # If fragment is just "tab" or empty, close active tab
            if fragment.lower() in ("tab", ""):
                return ("browser_control", {"action": "close_tab", "url": ""})
            # If the fragment is a whole-browser keyword and no explicit "tab", don't intercept
            if is_whole_browser and not has_tab_keyword:
                return None
            # Only hijack a close when there is positive browser evidence — a
            # known site name or an explicit "tab" keyword. Any other fragment
            # ("close photoshop", "close calculator") is a desktop app and must
            # stay with window_control instead of forcing a CDP attach.
            if not (has_site_name or has_tab_keyword):
                return None
            # Route to close_tab for named sites/fragments (youtube, netflix, etc.)
            return ("browser_control", {"action": "close_tab", "url": fragment})
        elif has_tab_keyword:
            return ("browser_control", {"action": "close_tab", "url": ""})

    return None


def _log_media_routing(
    tool: str, params: dict, user_text: str, target: str | None, media_query: str | None
) -> None:
    """Log media routing decisions for debugging.

    Safe: no secrets, truncated previews only.
    """
    ut_present = bool(user_text.strip())
    ut_preview = user_text.strip()[:80] if ut_present else ""
    log(
        "Router",
        (
            f"media.routing tool={tool} "
            f"action={params.get('action', '')} "
            f"target={target or 'None'} "
            f"has_query={bool(media_query)} "
            f"user_text_present={ut_present} "
            f"user_text_preview={ut_preview!r}"
        ),
    )


def _has_netflix_intent(user_text: str) -> bool:
    """True if user command clearly mentions Netflix as the target platform."""
    if not user_text:
        return False
    t = user_text.lower()
    # Explicit platform mention
    if re.search(r"\b(netflix|nf)\b", t):
        return True
    return False


def _is_netflix_google_search(url: str) -> bool:
    """True if browser_control URL is a Google search with site:netflix.com."""
    if not url:
        return False
    u = url.lower()
    return "google.com/search" in u and "site:netflix.com" in u


def _extract_query_from_netflix_google_search(url: str) -> str:
    """Extract the real query from a Google site:netflix.com search URL."""
    if not url:
        return ""
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        # Remove the site:netflix.com prefix
        q = re.sub(
            r"\bsite%3Anetflix\.com\b|site:netflix\.com\s*", "", q, flags=re.IGNORECASE
        ).strip()
        q = q.replace("+", " ").replace("%20", " ").strip()
        return q
    except Exception:
        return ""


def _is_netflix_screen_process_prompt(text: str) -> bool:
    """True if screen_process prompt asks about Netflix-specific content."""
    if not text:
        return False
    t = text.lower().strip()
    netflix_prompt_patterns = [
        "netflix search page",
        "analyze netflix",
        "netflix screen",
        "netflix grid",
        "visible netflix",
        "titles are visible",
        "netflix titles",
        "netflix title card",
        "netflix title",
        "what is on netflix",
        "what titles are visible",
        "grid layout",
        "search page on netflix",
        "on netflix",
    ]
    return any(p in t for p in netflix_prompt_patterns)


def _user_explicitly_requests_spotify(user_text: str, parameters: dict | None) -> bool:
    """True if user explicitly asked for Spotify during this turn.

    Guardrail: only used to override the "no spotify resume after netflix failure".
    """
    t = (user_text or "").lower()
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    # Explicit utterances win.
    if "play spotify" in t or "resume spotify" in t:
        return True
    if "spotify" in t and any(w in t for w in ["play", "resume"]):
        return True

    # Explicit tool intent via parameters wins.
    if action in ("play", "resume", "open"):
        # If the command is clearly spotify and not coming from a generic "play",
        # require spotify mention or spotify action.
        if "spotify" in t:
            return True
    return False


def _active_netflix_page_context(user_text: str, parameters: dict | None) -> bool:
    """True if the current/target context strongly indicates an active Netflix page.

    Works without Playwright by using router-provided url parameters.
    """
    p = parameters or {}
    url = (p.get("url") or p.get("current_url") or p.get("target_url") or "").lower().strip()

    # If user explicitly mentions Netflix, treat as Netflix context.
    if re.search(r"\b(netflix|nf)\b", (user_text or "").lower()):
        return True

    # Otherwise rely on url hints if present.
    netflix_markers = (
        "netflix.com/",
        "netflix.com/search",
        "netflix.com/browse",
        "netflix.com/title",
        "netflix.com/watch",
    )
    return any(m in url for m in netflix_markers)


def _is_vague_grid_play_command(user_text: str) -> bool:
    """Detect vague grid/ordinal phrases like 'top left', 'first one', etc."""
    if not user_text:
        return False
    t = user_text.lower()

    # English
    if any(
        x in t
        for x in [
            "top left",
            "top-left",
            "upper left",
            "left upper",
            "first one",
            "first result",
            "first card",
            "first title",
            "the first",
            "upper-left",
            "select the first title",
            "choose the first title",
            "play first title",
            "i'll select the first title",
            "berlin top left",
            "try again top left",
            "i select the first title",
            "pick the first title",
        ]
    ):
        return True

    # French
    if any(
        x in t
        for x in [
            "la première",
            "la premiere",
            "la deuxieme",
            "la deuxième",
            "la 2e",
            "la 2eme",
            "deuxième",
            "première",
        ]
    ):
        return True

    # Generic ordinal-ish without explicit platform keywords
    if any(x in t for x in ["second title", "second", "third title"]):
        # Don't classify "second" alone if it looks like a different structure;
        # keep routing conservative.
        return any(x in t for x in ["second", "third", "first"])

    return False


def _infer_netflix_ordinal_from_vague_position(user_text: str) -> int | None:
    """Map vague positional/ordinal language to a Netflix ordinal.
    Only handles the required phrases for the regression fixes.
    """
    if not user_text:
        return None
    t = user_text.lower()

    # Required mapping: top-left / first / upper-left -> 1
    if any(
        x in t
        for x in [
            "top left",
            "top-left",
            "upper left",
            "upper-left",
            "first one",
            "first result",
            "first card",
            "first title",
            "the first",
            "select the first title",
            "choose the first title",
            "play first title",
            "i'll select the first title",
            "berlin top left",
            "try again top left",
            "i select the first title",
            "pick the first title",
        ]
    ):
        return 1

    # French: la deuxième -> 2
    if any(
        x in t
        for x in [
            "la deuxième",
            "la deuxieme",
            "deuxième",
            "deuxieme",
            "second title",
            "the second",
            "second one",
        ]
    ):
        return 2

    # Fallback for first/second/third words
    if "second" in t:
        return 2
    if "third" in t:
        return 3
    if "first" in t:
        return 1
    return None


def _netflix_user_wants_autoplay(user_text: str) -> bool:
    """True if user_text expresses play/watch intent rather than search/find.

    Returns True for action verbs implying direct playback:
      play, watch, start, lance, joue, regarde
    Returns False for search/discovery verbs:
      search, find, look for, cherche, recherche

    Search/find intent overrides play/watch when both appear (e.g. "search
    for a movie to play" is still a search).
    """
    if not user_text:
        return False
    t = user_text.lower().strip()

    # Search/find override (checked first so it wins if both present)
    if re.search(r"\b(search|find|cherche|recherche)\b|\blook for\b", t):
        return False

    # Play intent
    if re.search(r"\b(play|watch|start|lance|joue|regarde)\b", t):
        return True

    return False


def _is_netflix_enter_fallback_blocked(tool: str, params: dict, user_text: str) -> bool:
    """Block bare browser_control/computer_control press Enter when Netflix
    failure or cooldown is active, unless the user explicitly says so.

    Returns True when the press should be suppressed.
    """
    p = params or {}
    action = str(p.get("action", "")).lower().strip()
    key = str(p.get("key", "") or (p.get("keys", []) or [""])[0]).lower().strip()
    # Block all non-explicit Enter variants: press, key, keyboard, type_key
    enter_actions = {"press", "key", "keyboard", "type_key"}
    is_enter = (
        tool in ("browser_control", "computer_control")
        and action in enter_actions
        and key in ("enter", "return")
    )
    if not is_enter:
        return False

    # Explicit user permission — never block when user says "press enter" etc.
    t = (user_text or "").lower()
    explicit_patterns = {
        "press enter",
        "hit enter",
        "appuie sur entrée",
        "appuie sur enter",
        "appuyer sur entrée",
        "appuyer sur enter",
    }
    if any(pat in t for pat in explicit_patterns):
        return False

    try:
        from actions.netflix_control import (
            is_netflix_failure_context_active,
            is_netflix_vision_blocked,
        )

        if is_netflix_failure_context_active() or is_netflix_vision_blocked():
            log(
                "Router",
                "netflix_enter_fallback_blocked: suppressing bare Enter press during Netflix failure/cooldown",
            )
            return True
    except Exception:
        pass
    return False


# Words that mean "the machine", not "you, Era". Kept narrow on purpose: only
# an unambiguous machine noun re-routes a shutdown_era call.
_MACHINE_NOUNS_RE = re.compile(
    r"\b(computer|pc|laptop|machine|system|windows|desktop|notebook|"
    # common ASR/accent variants and other languages the user speaks
    r"ordinateur|pc portable|el pc|kompyuter)\b",
    re.IGNORECASE,
)
# An explicit self-target beats the machine noun, so "close Era, I'm leaving the
# computer" still shuts down Era and not the PC. The self word must be the
# OBJECT of the verb — a bare "era" is just the wake word and appears in nearly
# every utterance ("Era, shut down the computer"), so it must NOT match here.
_SELF_TARGET_RE = re.compile(
    r"\b(?:shut ?down|shut|close|quit|exit|kill|stop|turn off|terminate)\s+"
    r"(?:the\s+)?(?:yourself|your ?self|era(?: ?one)?|app|program|assistant)\b",
    re.IGNORECASE,
)
_RESTART_RE = re.compile(r"\b(restart|reboot|reset|relaunch)\b", re.IGNORECASE)
_SHUTDOWN_RE = re.compile(
    r"\b(shut ?down|shut ?off|turn (?:it )?off|power (?:it )?off|switch off)\b",
    re.IGNORECASE,
)


def _reroute_machine_power(user_text: str) -> str | None:
    """Return the computer_settings action when the user asked to power the
    MACHINE down/restart, or None when the target is Era herself.

    Returning None is the safe default: it leaves shutdown_era alone, which
    only closes the assistant.
    """
    text = (user_text or "").strip()
    if not text:
        return None
    if not _MACHINE_NOUNS_RE.search(text):
        return None
    # An explicit "shut down Era / yourself / the app" wins over the machine
    # noun — the user named the target, and it isn't the PC.
    if _SELF_TARGET_RE.search(text):
        return None
    if _RESTART_RE.search(text):
        return "restart"
    if _SHUTDOWN_RE.search(text):
        return "shutdown"
    return None


# "reboot Era One" / "restart yourself": the target is Era, the verb is a restart.
_ERA_RESTART_RE = re.compile(
    r"\b(?:re-?boot|restart|relaunch)\s+(?:yourself|your ?self|era(?:[\s-]*(?:one|1))?)\b",
    re.IGNORECASE,
)
_POWER_RESTART_ACTIONS = {"restart", "reboot", "shutdown", "shut_down", "power_off"}


def divert_era_reboot(tool: str, parameters: dict | None, user_text: str) -> tuple[str, dict]:
    """Keep "reboot Era One" from rebooting the COMPUTER or stranding Era.

    The model hears "reboot" and may reach for computer_settings(restart) — with a
    restart waiver on, that reboots the machine unasked — or for a plain
    shutdown_era, which writes era.stop and leaves Era down. When the sentence
    restarts ERA and names no machine, both become shutdown_era(relaunch=True).
    """
    params = dict(parameters or {})
    text = user_text or ""
    if not _ERA_RESTART_RE.search(text) or _MACHINE_NOUNS_RE.search(text):
        return tool, params
    if tool in {"shutdown_era", "close_era", "exit_era"}:
        return "shutdown_era", {**params, "relaunch": True}
    action = str(params.get("action", "")).lower().strip().replace(" ", "_").replace("-", "_")
    if tool == "computer_settings" and action in _POWER_RESTART_ACTIONS:
        log("Router", f"era_reboot_guard: computer_settings({action}) -> shutdown_era(relaunch)")
        return "shutdown_era", {"relaunch": True}
    return tool, params


def route_tool_call(tool: str, parameters: dict | None, user_text: str = "") -> tuple[str, dict]:
    # The tool the caller actually asked for, before any rewriting below. An
    # explicit window_control/spotify_control call is a deliberate desktop-window
    # operation and must never be re-routed into a browser navigation.
    _requested_tool = tool

    # ── POWER-TARGET GUARD ───────────────────────────────────────────────
    # "shut down the computer" used to kill Era instead: the word "shutdown"
    # matched shutdown_era's description and Era closed herself, leaving the
    # machine running. The tool declarations now spell out the difference, but
    # the model is not the last line of defense — this is. If shutdown_era is
    # called while the user's sentence names the MACHINE, re-route it to the
    # real power action (which is HIGH-risk and confirmation-gated).
    if tool in {"shutdown_era", "close_era", "exit_era"}:
        rerouted = _reroute_machine_power(user_text)
        if rerouted is not None:
            log("Router", f"power_target_guard: {tool} -> computer_settings({rerouted})")
            return "computer_settings", {"action": rerouted}

    # NOTE: "open Netflix" is already hard-blocked away from open_app further
    # down (search "Netflix/YouTube must NEVER use open_app"), which routes it
    # to netflix_control browse / search / play depending on intent. An early
    # guard was briefly added here and removed again: returning a flat
    # action="open" preempted that logic and threw away the media-query and
    # search-intent handling it does. The profile gate is cleared inside the
    # browse branch of netflix_control instead, so every route through it lands
    # on the right profile without touching this routing at all.

    # Spotify must be guarded ONLY here (router/media-safety), never in spotify_control.py.
    if tool == "spotify_control":
        params = parameters or {}
        action = str(params.get("action", "")).lower().strip()

        # If Netflix failure context is active, block spotify play/resume unless explicitly requested.
        try:
            from actions.netflix_control import (
                _LAST_NETFLIX_FAILURE_AT,  # noqa: F401
                is_netflix_failure_context_active,
            )

            nf_fail = is_netflix_failure_context_active()
        except Exception:
            nf_fail = False

        if nf_fail and action in ("play", "resume"):
            if _user_explicitly_requests_spotify(user_text, params):
                return tool, params
            log(
                "Router",
                "spotify_block_after_netflix_failure: blocking spotify_control play/resume",
            )
            return "spotify_control", {
                "action": "play",
                "suppress_reason": "netflix_failure_guard_blocked",
            }

        return tool, params

    params = dict(parameters or {})

    # ── SCREEN_PROCESS VISION GUARD ──────────────────────────────────────
    # Explicit screen_process calls must NEVER be hijacked by Netflix, window,
    # or browser routing.  This MUST run BEFORE canonicalize_tool_call,
    # because canonicalize maps actions like "list" → "window_control" which
    # would change the tool before the guard can see it.
    #
    # Management actions and async calls bypass all routing unconditionally.
    # Bare calls with no action and no text also bypass (basic capture).
    # Generic vision actions (screen, analyze, look, etc.) and calls with
    # text flow through to the Netflix Vision Guard (line ~1005) which knows
    # whether to suppress or pass through.
    if tool == "screen_process":
        sp_action = _norm(params.get("action"))
        sp_has_text = bool((params.get("text") or params.get("user_text") or "").strip())
        sp_is_async = bool(params.get("async"))
        _SCREEN_PROCESS_MGMT_ACTIONS = {
            "result",
            "get_result",
            "status",
            "check_status",
            "list",
            "requests",
            "clear",
            "cleanup",
        }
        if sp_action in _SCREEN_PROCESS_MGMT_ACTIONS or sp_is_async:
            return tool, params

        if sp_action == "" and not sp_has_text:
            return tool, params

    # ── PART A.7: V8B Live Netflix Browser-Control Click Guard ──────────────
    # Prevents Gemini from using generic browser clicks/types on Netflix
    # pages when Netflix is the active runtime context (scroll context or
    # intent memory), even without explicit Netflix mention in user_text or
    # params.  Runs BEFORE canonicalize_tool_call so it catches raw
    # browser_control press/hotkey before they become computer_control.
    if tool == "browser_control":
        _v8b_action = _norm(params.get("action"))
        if _v8b_action in ("click", "type", "press", "hotkey", "smart_click"):
            _v8b_skip = False
            if _v8b_action == "press":
                _v8b_key = _norm(params.get("key") or (params.get("keys") or [""])[0])
                if _v8b_key in ("enter", "return"):
                    _v8b_skip = True
            if not _v8b_skip:
                _v8b_nf = False
                try:
                    if get_scroll_context() == "netflix":
                        _v8b_nf = True
                except Exception:
                    pass
                if not _v8b_nf:
                    try:
                        _v8b_state = load_state()
                        if _v8b_state and _v8b_state.get("last_media_app") == "netflix":
                            _v8b_nf = True
                    except Exception:
                        pass
                if not _v8b_nf:
                    try:
                        _active_ctx = _active_netflix_page_context(user_text, parameters)
                        if _active_ctx:
                            _v8b_nf = True
                    except Exception:
                        pass
                if _v8b_nf:
                    _v8b_q = (
                        params.get("text")
                        or params.get("value")
                        or params.get("query")
                        or params.get("description")
                        or ""
                    ).strip()
                    log(
                        "Router",
                        f"netflix_v8b_guard: browser_control {_v8b_action} suppressed (Netflix context active)",
                    )
                    if _v8b_action in ("click", "smart_click") and _v8b_q:
                        from utils.ordinal_selection import ordinal_to_index

                        _v8b_ord = ordinal_to_index(_v8b_q)
                        if _v8b_ord is not None and _v8b_ord > 0:
                            return "netflix_control", {"action": "grid_play", "ordinal": _v8b_ord}
                        _lower_q = _v8b_q.lower()
                        vague = {
                            "play",
                            "first title",
                            "second title",
                            "third title",
                            "top left",
                            "top right",
                            "bottom left",
                            "bottom right",
                            "that one",
                        }
                        if (
                            _lower_q in vague
                            or _lower_q.startswith("first ")
                            or _lower_q.startswith("second ")
                            or _lower_q.startswith("third ")
                        ):
                            return "netflix_control", {"action": "grid_play"}
                        return "netflix_control", {"action": "search", "query": _v8b_q}
                    if _v8b_action == "type" and _v8b_q:
                        return "netflix_control", {"action": "search", "query": _v8b_q}
                    _v8b_fb = _extract_media_query(parameters, user_text, "netflix", None)
                    if _v8b_fb:
                        _v8b_ap = _netflix_user_wants_autoplay(user_text)
                        return "netflix_control", {
                            "action": "search",
                            "query": _v8b_fb,
                            "_auto_play": _v8b_ap,
                        }
                    return "netflix_control", {"action": "status"}

    # ── V8C: computer_control Netflix guard ──────────────────────────────────
    # When Netflix context is active, reroute generic computer_control actions
    # (type, click, press, hotkey, smart_click) to netflix_control instead of
    # allowing them to execute directly on the Netflix page.
    if tool == "computer_control":
        _v8c_action = _norm(params.get("action"))
        if _v8c_action in ("type", "click", "press", "hotkey", "smart_click"):
            _v8c_nf = False
            try:
                if get_scroll_context() == "netflix":
                    _v8c_nf = True
            except Exception:
                pass
            if not _v8c_nf:
                try:
                    _v8c_state = load_state()
                    if _v8c_state and _v8c_state.get("last_media_app") == "netflix":
                        _v8c_nf = True
                except Exception:
                    pass
            if not _v8c_nf:
                try:
                    _active_ctx = _active_netflix_page_context(user_text, parameters)
                    if _active_ctx:
                        _v8c_nf = True
                except Exception:
                    pass
            if _v8c_nf:
                _v8c_q = (params.get("text") or params.get("value") or "").strip()
                log(
                    "Router",
                    f"netflix_v8c_guard: computer_control {_v8c_action} suppressed (Netflix context active)",
                )
                if _v8c_action in ("type", "click", "smart_click") and _v8c_q:
                    return "netflix_control", {
                        "action": "search",
                        "query": _normalize_search_query(_v8c_q),
                    }
                if _v8c_action in ("press", "hotkey"):
                    # A fullscreen key (F11, or Netflix's "f") is a legitimate
                    # request — route it to the real fullscreen handler instead
                    # of swallowing it into a no-op status. This is what let
                    # "press f11 to fullscreen" silently do nothing.
                    _v8c_key = _norm(
                        params.get("key") or params.get("keys") or params.get("hotkey") or ""
                    )
                    if _v8c_key in ("f11", "f", "fullscreen") or "f11" in _v8c_key:
                        log(
                            "Router",
                            "netflix_v8c_guard: fullscreen key routed to netflix fullscreen",
                        )
                        return "netflix_control", {"action": "fullscreen"}
                    _v8c_fb = _extract_media_query(parameters, user_text, "netflix", None)
                    if _v8c_fb:
                        _v8c_ap = _netflix_user_wants_autoplay(user_text)
                        return "netflix_control", {
                            "action": "search",
                            "query": _v8c_fb,
                            "_auto_play": _v8c_ap,
                        }
                    return "netflix_control", {"action": "status"}
                _v8c_fb2 = _extract_media_query(parameters, user_text, "netflix", None)
                if _v8c_fb2:
                    _v8c_ap2 = _netflix_user_wants_autoplay(user_text)
                    return "netflix_control", {
                        "action": "search",
                        "query": _v8c_fb2,
                        "_auto_play": _v8c_ap2,
                    }
                return "netflix_control", {"action": "status"}

    # ── Netflix fullscreen early-return guard ──────────────────────────────────
    # Must run BEFORE canonicalize_tool_call.  The action_registry also has this
    # guard, but catching it here prevents the router from even entering
    # canonicalize logic at all for netflix_control fullscreen — double-lock.
    if tool == "netflix_control" and _norm(params.get("action")) == "fullscreen":
        log("Router", "netflix_fullscreen_early_return: bypassed canonicalize_tool_call rewrite")
        return tool, params

    tool, params = canonicalize_tool_call(tool, params)

    # ── PART A: Hard-lock Netflix intent away from browser_control ──────────
    has_netflix_text = _has_netflix_intent(user_text)
    # str(): a numeric arg ("set_volume" value=100) crashed .lower() here, and the
    # caller's except then skipped the WHOLE routing layer for that call (2026-09-27).
    has_netflix_url = "netflix.com" in str(params.get("url") or "").lower()
    has_netflix_desc = (
        "netflix"
        in str(params.get("description") or params.get("text") or params.get("value") or "").lower()
    )
    has_netflix = has_netflix_text or has_netflix_url or has_netflix_desc
    if has_netflix and tool == "browser_control":
        bc_action = _norm(params.get("action"))
        bc_url = params.get("url", "") or ""

        # 1. Google search with site:netflix.com → route to netflix_control
        if _is_netflix_google_search(bc_url):
            q = _extract_query_from_netflix_google_search(bc_url)
            log(
                "Router",
                f"netflix_lock: google site:netflix.com → netflix_control search query={bool(q)}",
            )
            return "netflix_control", {"action": "search", "query": q}

        # 2. browser_control type/click/press/smart_click with Netflix intent → reroute to netflix_control
        if bc_action in ("type", "click", "press", "fill_form", "smart_click"):
            # Extract query from params value/text/description
            q = (
                params.get("value")
                or params.get("text")
                or params.get("query")
                or params.get("description")
                or ""
            ).strip()
            if q:
                # Check for ordinal patterns in the description (e.g. "fourth video title card")
                from utils.ordinal_selection import ordinal_to_index

                ordinal = ordinal_to_index(q)
                if ordinal is not None and ordinal > 0:
                    log(
                        "Router",
                        f"netflix_lock: browser_control {bc_action} → netflix_control grid_play ordinal={ordinal}",
                    )
                    return "netflix_control", {
                        "action": "grid_play",
                        "ordinal": ordinal,
                        "browser": "brave",
                    }
                log(
                    "Router",
                    f"netflix_lock: browser_control {bc_action} → netflix_control search (query={q[:40]})",
                )
                return "netflix_control", {"action": "search", "query": q}
            # No query → return safe no-op
            log(
                "Router",
                f"netflix_lock: browser_control {bc_action} suppressed (Netflix intent, no query)",
            )
            return "netflix_control", {"action": "search", "query": ""}

        # 3. browser_control generic search helper → reroute
        if bc_action in ("search", "search_google"):
            q = (params.get("query") or params.get("value") or "").strip()
            if q:
                log(
                    "Router",
                    f"netflix_lock: browser_control {bc_action} → netflix_control search (query={q[:40]})",
                )
                return "netflix_control", {"action": "search", "query": q}

    # ── PART A.5: Catch-all Netflix URL hard-lock ──────────────────────────────
    # Any browser_control go_to whose URL contains netflix.com is hard-locked
    # to netflix_control (browse if no query, search otherwise).
    # /title/ and /watch/ URLs get special treatment.
    if tool == "browser_control" and _norm(params.get("action")) == "go_to":
        go_url = (params.get("url") or "").lower()
        if "netflix.com" in go_url:
            # Detect /title/ or /watch/ URL patterns for targeted routing
            if "/title/" in go_url:
                log(
                    "Router",
                    "netflix_url_lock: browser_control go_to /title/ → netflix_control play",
                )
                return "netflix_control", {"action": "play", "browser": "brave"}
            if "/watch/" in go_url:
                log(
                    "Router",
                    "netflix_url_lock: browser_control go_to /watch/ → netflix_control status",
                )
                return "netflix_control", {"action": "status"}
            q = params.get("value") or params.get("query") or ""
            if not q:
                try:
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(go_url)
                    q = parse_qs(parsed.query).get("q", [""])[0]
                except Exception:
                    q = ""
            nf_action = "search" if q else "browse"
            log(
                "Router", f"netflix_url_lock: browser_control go_to → netflix_control ({nf_action})"
            )
            return "netflix_control", {"action": nf_action, "query": q, "value": q}

    # ── PART A.6: Netflix Enter-fallback guard ────────────────────────────────────
    # When Netflix failure or cooldown is active, suppress bare browser_control/
    # computer_control press Enter. Only allow when the user explicitly says
    # "press enter" / "hit enter" / "appuie sur entrée".
    if _is_netflix_enter_fallback_blocked(tool, params, user_text):
        return "netflix_control", {"action": "status"}

    # ── Netflix/YouTube must NEVER use open_app. Hard-block + always reroute. ──
    if tool == "open_app":
        app_name = _norm(params.get("app_name"))
        if app_name in ("netflix", "youtube", "yt", "nf"):
            canonical = "netflix" if app_name in ("netflix", "nf") else "youtube"
            home_url = _build_platform_home_url(canonical)
            media_query = _extract_media_query(
                parameters, user_text, canonical, _norm(params.get("action"))
            )
            # V9C.2: Deep-clean Netflix queries
            if canonical == "netflix" and media_query:
                _cleaned = _clean_netflix_media_query(media_query)
                if _cleaned:
                    media_query = _cleaned
            _open_nf_avail = (
                _is_netflix_availability_or_list_intent(user_text)
                if canonical == "netflix"
                else False
            )
            if canonical == "netflix" and any(
                w in user_text.lower() for w in ("play ", "playing ", "watch ")
            ):
                _open_nf_avail = False
            user_text_lower = user_text.lower()
            has_search_intent = any(
                w in user_text_lower for w in ["play", "search", "find", "watch"]
            )
            force_new = _is_explicit_new_tab_request(user_text, canonical)

            if media_query or has_search_intent:
                target_tool = "netflix_control" if canonical == "netflix" else "youtube_video"
                action = "search" if media_query else "play"
                log("Router", f"open_app.{canonical} hard-block → {target_tool} ({action})")
                if media_query:
                    out_params = {"action": action, "query": media_query}
                else:
                    out_params = {"action": action}
                if canonical == "netflix":
                    out_params["value"] = media_query or ""
                    out_params["_auto_play"] = not _open_nf_avail and bool(media_query)
                return target_tool, out_params

            # No media intent, no query: Netflix uses netflix_control browse per hard-lock policy.
            # YouTube goes to browser_control to open home page.
            if canonical == "netflix":
                log(
                    "Router",
                    "open_app.netflix hard-block → netflix_control browse (no media intent)",
                )
                return "netflix_control", {"action": "browse", "browser": "brave"}
            else:
                log(
                    "Router",
                    f"open_app.{canonical} hard-block → browser_control go_to home (no media intent)",
                )
                return "browser_control", {
                    "action": "go_to",
                    "url": home_url,
                    "force_new": force_new,
                }

    # ── Platform home routing: 'home page of YouTube' / 'YouTube home' / 'Netflix home' ──
    platform_home = _detect_platform_home_intent(user_text)
    if platform_home:
        home_url = _build_platform_home_url(platform_home)
        log(
            "Router",
            f"platform_home_routing detected={platform_home} → browser_control go_to {home_url}",
        )
        return "browser_control", {"action": "go_to", "url": home_url}

    # ── YouTube next-video routing (before query extraction) ──
    next_route = _route_next_video(user_text)
    if next_route:
        return next_route

    # ── A/B: Active Netflix page must override vague grid/position commands ──
    if tool in ("media_coordinator", "youtube_video", "netflix_control"):
        # Exception: do NOT override if user explicitly requests YouTube.
        has_explicit_youtube = bool(
            re.search(r"\b(youtube|yt|youtu\.be)\b", (user_text or "").lower())
        )
        netflix_ctx = _active_netflix_page_context(user_text, params)

        if netflix_ctx and not has_explicit_youtube:
            # V9C.3: Check row/column position first
            grid_pos = parse_netflix_grid_position(user_text)
            if grid_pos and "row" in grid_pos and "col" in grid_pos:
                log(
                    "Router",
                    f"netflix_active_override: row/col → netflix_control grid_play row={grid_pos['row']} col={grid_pos['col']}",
                )
                return "netflix_control", {
                    "action": "grid_play",
                    "row": grid_pos["row"],
                    "col": grid_pos["col"],
                    "browser": "brave",
                }
            # Fallback: ordinal from vague grid command
            if _is_vague_grid_play_command(user_text):
                nf_ord = _infer_netflix_ordinal_from_vague_position(user_text)
                if nf_ord is not None:
                    log(
                        "Router",
                        f"netflix_active_override: vague grid → netflix_control grid_play ordinal={nf_ord}",
                    )
                    return "netflix_control", {
                        "action": "grid_play",
                        "ordinal": nf_ord,
                        "browser": "brave",
                    }

    # ── ORDINAL ROUTING: 'play the second video' / 'third Netflix title' ──
    # Must run before query extraction so "i mean the second title" is not
    # extracted as a search query.
    # Question-vs-action guard: questions/analyses (e.g. "What is the title of
    # the first result?") must NOT be diverted to grid_play — they stay in
    # their source tool (typically screen_process). Imperative orders still
    # route normally.
    if _is_grid_question_or_analysis(user_text):
        ordinal_route = None
    else:
        ordinal_route = _route_ordinal_command(user_text)
    if ordinal_route:
        return ordinal_route

    # ── I-MEAN-ON-PLATFORM correction ──
    # "I mean on YouTube" / "I mean on Netflix" — placed after ordinal to avoid
    # stealing "I mean the second title", but before query extraction.
    i_mean_route = _route_i_mean_on_platform(user_text)
    if i_mean_route:
        return i_mean_route

    # ── V8C: Vague follow-up routing for Netflix context ──────────────────────
    # Phrases like "play it", "yes", "that one", "ok play it" on Netflix should
    # not loop through media_coordinator play → netflix_control play (which
    # produces "I see search results" again).  Route them to netflix_control
    # status or grid_play depending on context.
    if tool in ("media_coordinator", "netflix_control") and _norm(params.get("action")) in (
        "play",
        "",
    ):
        _v8c_followup_text = (user_text or "").lower().strip()
        _v8c_is_vague = _v8c_followup_text in (
            "play it",
            "yes",
            "that one",
            "ok",
            "play that",
            "play this",
            "go",
        )
        _v8c_is_vague = (
            _v8c_is_vague
            or _v8c_followup_text.startswith("ok play")
            or _v8c_followup_text.startswith("yes play")
        )
        # Check Netflix context
        _v8c_nf_followup = False
        try:
            if get_scroll_context() == "netflix":
                _v8c_nf_followup = True
        except Exception:
            pass
        if not _v8c_nf_followup:
            try:
                _v8c_state = load_state()
                if _v8c_state and _v8c_state.get("last_media_app") == "netflix":
                    _v8c_nf_followup = True
            except Exception:
                pass
        if _v8c_is_vague and _v8c_nf_followup:
            log(
                "Router",
                f"netflix_followup_routing: vague follow-up '{_v8c_followup_text}' → netflix_control grid_play",
            )
            return "netflix_control", {"action": "grid_play", "browser": "brave"}

    action = _norm(params.get("action"))
    desc = " ".join(
        str(params.get(k, "")) for k in ("description", "query", "text", "value", "url")
    )
    text = f"{user_text} {desc}".strip()
    target = _media_target_from_text(text)
    state = load_state()

    # ── Pre-extract media query for all paths (before params get overwritten) ──
    media_query = _extract_media_query(parameters, user_text, target, action)

    # ── V9C.2: Deep-clean Netflix queries and detect availability intent ──
    _nf_availability_intent = False
    if target == "netflix" and media_query:
        cleaned = _clean_netflix_media_query(media_query)
        if cleaned:
            media_query = cleaned
    _nf_availability_intent = _is_netflix_availability_or_list_intent(user_text)
    # If user text has explicit play/watch action words, override availability intent
    _user_text_lower_nf = (user_text or "").lower()
    if any(w in _user_text_lower_nf for w in ("play ", "playing ", "watch ")):
        _nf_availability_intent = False

    # ── Track last user query (runtime only, no memory write) ──
    if media_query:
        platform_hint = target or _media_target_from_text(user_text) or ""
        set_last_user_query(media_query, platform_hint)

    # ── Fix 2: Netflix/YouTube title commands must not become browser_control go_to ──
    if tool == "browser_control" and _norm(params.get("action")) == "go_to":
        go_url = params.get("url", "").lower()
        is_netflix_search = "netflix.com/search" in go_url
        is_youtube_search = "youtube.com/results" in go_url
        if is_netflix_search or is_youtube_search:
            user_text_lower = user_text.lower()
            has_media_intent = any(
                w in user_text_lower for w in ["play", "search", "find", "watch"]
            )
            if has_media_intent or media_query:
                if is_netflix_search:
                    raw_url = params.get("url", "")
                    q = media_query or (
                        raw_url.split("?q=")[-1].split("&")[0].replace("%20", " ")
                        if "?q=" in raw_url
                        else ""
                    )
                    log(
                        "Router",
                        f"browser_control go_to netflix search re-routed to netflix_control query={bool(q)}",
                    )
                    return "netflix_control", {"action": "search", "value": q}
                elif is_youtube_search:
                    raw_url = params.get("url", "")
                    q = media_query or (
                        raw_url.split("?search_query=")[-1].split("&")[0].replace("+", " ")
                        if "?search_query=" in raw_url
                        else ""
                    )
                    log(
                        "Router",
                        f"browser_control go_to youtube search re-routed to youtube_video query={bool(q)}",
                    )
                    return "youtube_video", {"action": "play", "query": q}

    # ── Issue 14B.2: browser_control new_tab for YouTube/Netflix → go_to (reuse tab) ──
    # Only allow new_tab when user explicitly says 'open a new <platform> tab'.
    if tool == "browser_control" and _norm(params.get("action")) == "new_tab":
        new_url = (params.get("url") or "").lower()
        for canonical in ("youtube", "netflix"):
            if canonical not in _MEDIA_BROWSER_DOMAINS:
                continue
            domain = _MEDIA_BROWSER_DOMAINS[canonical]
            if domain in new_url and not _is_explicit_new_tab_request(user_text, canonical):
                home_url = _build_platform_home_url(canonical)
                final_url = new_url or home_url
                log("Router", f"browser_control new_tab {canonical} → go_to (reuse existing tab)")
                return "browser_control", {"action": "go_to", "url": final_url, "force_new": False}

    # ── QUERY ROUTING: If a real search query was extracted, route to platform tool with query ──
    # This prevents "One Piece on Netflix" / "Open Netflix and play One Piece" from
    # becoming a bare media_coordinator play (which plays featured content instead of searching).
    if media_query and tool == "media_coordinator" and action == "play":
        platform_target = target or _media_target_from_text(user_text)
        if platform_target == "netflix":
            _nf_ap = not _nf_availability_intent
            log(
                "Router",
                f"query_routing: netflix_control search → {media_query} (_auto_play={_nf_ap})",
            )
            return "netflix_control", {
                "action": "search",
                "query": media_query,
                "_auto_play": _nf_ap,
            }
        if platform_target == "youtube":
            log("Router", f"query_routing: youtube_video play → {media_query}")
            return "youtube_video", {"action": "play", "query": media_query}

    # ── ORCHESTRATOR INTERCEPT: ONLY Generic Transport Commands ──
    # Specific commands (liked_songs, next, etc) bypass this and go to their app.
    GENERIC_PLAYBACK_ACTIONS = {"play", "pause", "resume", "stop", "play_pause", "toggle_playback"}

    # ── Issue 2: Explicit target lock — if params already has explicit target, do not override ──
    _EXPLICIT_TARGETS = {"youtube", "netflix", "spotify"}
    explicit_target = params.get("target", "").lower().strip()
    target_locked = explicit_target in _EXPLICIT_TARGETS
    if target_locked:
        target = explicit_target

    # For YouTube/Netflix play with a query — keep platform tool, don't route to media_coordinator
    if tool == "youtube_video" and action == "play" and (params.get("query") or media_query):
        if not params.get("query") and media_query:
            params["query"] = media_query
        pass
    elif tool == "netflix_control" and action == "play" and (params.get("query") or media_query):
        search_query = params.get("query") or media_query
        # V9C.2: Deep-clean Netflix queries
        if search_query:
            _cleaned2 = _clean_netflix_media_query(search_query)
            if _cleaned2:
                search_query = _cleaned2
        _nf_ap2 = not _nf_availability_intent
        log(
            "Router", f"netflix_control query_routing: play → {search_query} (_auto_play={_nf_ap2})"
        )
        return "netflix_control", {"action": "play", "query": search_query, "_auto_play": _nf_ap2}
    elif (
        tool in ("youtube_video", "netflix_control", "media_coordinator")
        and action in GENERIC_PLAYBACK_ACTIONS
    ):
        if any(x in action for x in ("pause", "stop")):
            canonical_action = "pause"
        else:
            canonical_action = "play"

        if not target_locked:
            if tool == "youtube_video" or "youtube" in action:
                target = "youtube"
            elif tool == "netflix_control" or "netflix" in action:
                target = "netflix"
            elif tool == "spotify_control" or "spotify" in action:
                target = "spotify"
            elif not target:
                recent = get_scroll_context()
                if recent in ("youtube", "netflix"):
                    target = recent
                    log("Router", f"platform.default recent_scroll_context={recent}")
                else:
                    target = "spotify"

        tool = "media_coordinator"
        params = {"target": target, "action": canonical_action}
        if target_locked:
            log("Router", f"media.routing.target_locked=True target={target}")

    # ── Inject extracted media query back into params ──
    if media_query and not params.get("query"):
        if tool in ("media_coordinator", "youtube_video", "netflix_control"):
            params["query"] = media_query

    # ── Issue 1: Debug logging for media routing ──
    if tool in ("media_coordinator", "youtube_video", "netflix_control"):
        _log_media_routing(tool, params, user_text, target, media_query)

    # ── SCROLL ROUTING: generic scroll → computer_control (JS-first, pyautogui fallback) ──
    is_scroll, direction = _detect_scroll_intent(user_text, params)
    if is_scroll:
        # Preserve explicit YouTube/Netflix scroll handlers
        if tool in ("youtube_video", "netflix_control") and _norm(params.get("action")) in (
            "scroll",
            "scroll_down",
            "scroll_up",
        ):
            pass  # Let the native handler manage it
        else:
            if tool == "computer_control":
                # Inject target into existing computer_control params
                scroll_target = _infer_scroll_target(user_text, params)
                if scroll_target:
                    params["target"] = scroll_target
                    log(
                        "Router",
                        f"scroll.target_injected={scroll_target} into computer_control params",
                    )
                return tool, params
            # ── Issue 6: Normalize small Gemini-generated scroll amounts ──
            orig_amount = params.get("amount")
            if (
                orig_amount is not None
                and "px" not in user_text.lower()
                and "pixel" not in user_text.lower()
            ):
                try:
                    if int(orig_amount) < 100:
                        log(
                            "Router",
                            f"scroll.amount_normalized from={orig_amount} to=normal(700px)",
                        )
                        scroll_params = {"action": "scroll", "direction": direction}
                    else:
                        scroll_params = {
                            "action": "scroll",
                            "direction": direction,
                            "amount": orig_amount,
                        }
                except (ValueError, TypeError):
                    scroll_params = {"action": "scroll", "direction": direction}
            else:
                scroll_params = {"action": "scroll", "direction": direction}
            # ── Issue 14B.2: Scroll target inference (YouTube/Netflix owned tab) ──
            scroll_target = _infer_scroll_target(user_text, params)
            if scroll_target:
                scroll_params["target"] = scroll_target
                log("Router", f"scroll.target={scroll_target} (recent platform context)")
            return "computer_control", scroll_params

    # ── BROWSER INTENT ROUTING: open/focus/list-tabs/close-tab → browser_control ──
    # An explicitly requested window_control/spotify_control call is exempt:
    # "focus/minimize/maximize spotify" operates on the desktop window and must
    # never become a browser navigation to the web player. (Checked against the
    # ORIGINAL tool — tools rewritten into window_control above still route.)
    _explicit_desktop_call = _requested_tool in ("window_control", "spotify_control")
    if tool not in ("media_coordinator", "computer_control") and not _explicit_desktop_call:
        browser_route = _route_browser_intent(user_text, params)
        if browser_route:
            tool, params = browser_route

    # "close this" / "minimize this" get active title if title missing.
    if tool == "window_control" and params.get("action") in (
        "close",
        "minimize",
        "maximize",
        "restore",
        "focus",
    ):
        if not params.get("title"):
            params["title"] = get_active_title("")

    # Browser defaults.
    if tool in ("browser_control", "youtube_video", "netflix_control") and not params.get(
        "browser"
    ):
        last_browser = state.get("last_browser") or "brave"
        if tool == "browser_control":
            params.setdefault("browser", last_browser)

    if tool != "media_coordinator":
        tool, params = canonicalize_tool_call(tool, params)

    # ── Netflix Vision Guard: suppress screen_process after Netflix failures ──
    if tool == "screen_process":
        text_param = params.get("text") or params.get("user_text") or ""

        # Check 1: is the prompt asking about Netflix content?
        is_netflix_prompt = _is_netflix_screen_process_prompt(
            text_param
        ) or _is_netflix_screen_process_prompt(user_text)

        # Check 2: is failure context active (previous Netflix/browser_control failure)?
        nf_failure = False
        try:
            from actions.netflix_control import is_netflix_failure_context_active

            nf_failure = is_netflix_failure_context_active()
        except Exception:
            pass

        # Check 3: is Vision cooldown active?
        nf_cooldown = False
        try:
            from actions.netflix_control import is_netflix_vision_blocked

            nf_cooldown = is_netflix_vision_blocked()
        except Exception:
            pass

        if is_netflix_prompt and (nf_failure or nf_cooldown):
            log(
                "Router",
                "netflix_screen_process_suppress: "
                f"prompt={is_netflix_prompt} failure={nf_failure} cooldown={nf_cooldown}",
            )
            # Return controlled status which will output the suppression message
            return "netflix_control", {"action": "status"}

        # Fix G: suppress generic screen_process when Netflix active AND failure/cooldown
        if nf_failure or nf_cooldown:
            _is_netflix_active = False
            try:
                if get_scroll_context() == "netflix":
                    _is_netflix_active = True
            except Exception:
                pass
            if not _is_netflix_active:
                try:
                    _intent = load_state()
                    if _intent and _intent.get("last_media_app") == "netflix":
                        _is_netflix_active = True
                except Exception:
                    pass
            if _is_netflix_active:
                log(
                    "Router",
                    "netflix_screen_process_suppress_generic: Netflix active + failure/cooldown",
                )
                return "netflix_control", {"action": "status"}

    # ── V7: VisionPolicy guard ───────────────────────────────────────
    _VISION_MUTATION_ACTIONS = {
        "click",
        "type",
        "press",
        "fill_form",
        "smart_click",
        "hotkey",
        "scroll",
    }
    if tool in ("computer_control", "browser_control"):
        action = _norm(params.get("action"))
        if action in _VISION_MUTATION_ACTIONS:
            try:
                from core.vision_context import get_latest_vision_result

                ctx = get_latest_vision_result()
                if ctx is not None:
                    ctx_id, vision_result = ctx
                    intended = {"tool": tool, "action": action}
                    if params:
                        intended.update(params)
                    from core.vision_policy import evaluate_vision_recommendation

                    policy = evaluate_vision_recommendation(vision_result, intended)
                    should_block = False
                    if tool == "computer_control":
                        if not policy.get("allow_action", True) or policy.get(
                            "requires_confirmation", False
                        ):
                            should_block = True
                    elif tool == "browser_control":
                        risk = policy.get("risk_level", "low")
                        if risk == "high" or policy.get("requires_confirmation", False):
                            should_block = True
                        elif vision_result.needs_confirmation:
                            should_block = True
                    if should_block:
                        log(
                            "Router",
                            "vision_policy_block: "
                            f"tool={tool} action={action} "
                            f"risk={policy.get('risk_level', '?')} "
                            f"recommended={policy.get('recommended_path', '?')} "
                            f"reason={policy.get('reason', '?')}",
                        )
                        return "screen_process", {"action": "result", "request_id": "latest"}
            except Exception:
                log(
                    "Router",
                    "vision_policy_error: failing closed for mutation with recent vision result",
                )
                return "screen_process", {"action": "result", "request_id": "latest"}

    # ── Netflix search autoplay guard ──────────────────────────────────
    # Ensure _auto_play is always explicit for netflix_control search.
    # Prevents netflix_control.py from defaulting _auto_play=True for
    # search/find commands that should return a list, not autoplay.
    if tool == "netflix_control":
        nf_action = str(params.get("action") or "").lower().strip()
        if nf_action == "search" and "_auto_play" not in params:
            params["_auto_play"] = _netflix_user_wants_autoplay(user_text)

    return tool, params


def record_tool_result(
    tool: str, parameters: dict | None, result: Any = None, error: Any = None
) -> None:
    params = parameters or {}
    action = str(params.get("action", "") or "")
    media_app = None
    app = None
    browser = None

    if tool in ("computer_settings", "spotify_control") and (
        action.startswith("spotify_")
        or action in ("play", "pause", "liked_songs", "made_for_you", "next", "previous")
    ):
        media_app = app = "spotify"
    elif tool == "youtube_video":
        media_app = "youtube"
        app = "browser"
        browser = params.get("browser") or "brave"
    elif tool == "netflix_control":
        media_app = "netflix"
        app = "browser"
        browser = params.get("browser") or "brave"
    elif tool == "open_app":
        app = str(params.get("app_name", "") or "").lower() or None
    elif tool == "browser_control":
        app = "browser"
        browser = params.get("browser") or "brave"
    elif tool == "window_control":
        app = "window"

    remember_action(
        tool=tool,
        action=action or None,
        app=app,
        media_app=media_app,
        browser=browser,
        result=str(result) if result is not None else None,
        error=str(error) if error is not None else None,
    )

    # ── Detect Netflix/browser_control failures ──
    # NOTE: screen_process failures must NEVER set Netflix failure context or
    # cooldown. Vision overload / 503 / timeout are temporary API issues that
    # have nothing to do with Netflix automation state.
    _result_text = _norm(str(result or ""))
    _error_text = _norm(str(error or ""))

    # Netflix/browser_control failure keywords
    _nf_failure_patterns = [
        "could not resolve",
        "could not read",
        "modal anchor bypassed",
        "thumbnail link locator",
        "no visible netflix",
        "could not start playback",
        "could not click the play button",
        "could not navigate",
        "internal error before selecting",
        "netflix search had an internal error",
        "could not read any title cards",
        "could not read netflix title cards",
        "netflix browsing/search/card actions are handled by netflix_control only",
    ]

    _is_nf_fail = tool in ("netflix_control", "browser_control") and any(
        p in _result_text or p in _error_text for p in _nf_failure_patterns
    )
    # page_snapshot / a landed click_ref name the Netflix page because they worked on
    # it; flagging those set a 120 s "Netflix failure" after every snapshot.
    _bc_read_ok = _norm(action) == "page_snapshot" or (
        _norm(action) == "click_ref" and _result_text.startswith("clicked [")
    )
    _is_netflix_browser_collision = (
        tool == "browser_control"
        and not _bc_read_ok
        and ("netflix.com" in _result_text or "netflix" in _result_text)
    )

    if _is_nf_fail or _is_netflix_browser_collision:
        try:
            from actions.netflix_control import _set_netflix_failure

            _set_netflix_failure(tool=tool or "", error_text=_result_text or _error_text)
        except Exception:
            pass

    # ── Netflix Vision cooldown for screen_process API failures ──
    # Generic screen_process 503/UNAVAILABLE/429 must NOT set Netflix
    # cooldown.  Only Netflix-context failures do.  Netflix context is
    # determined by: params/text/user_text containing "netflix", scroll
    # context, or intent memory last_media_app.
    if tool == "screen_process":
        _sp_result = _norm(str(result or ""))
        _sp_error = _norm(str(error or ""))
        _is_api_error = any(
            p in _sp_result or p in _sp_error
            for p in ["503", "429", "unavailable", "resource_exhausted", "quota exceeded"]
        )
        if _is_api_error:
            _is_nf_ctx = False
            # Check params/text/user_text for netflix
            try:
                _params_str = _norm(str(parameters or {}))
                if "netflix" in _params_str:
                    _is_nf_ctx = True
            except Exception:
                pass
            # Check scroll context
            if not _is_nf_ctx:
                try:
                    from core.browser_grid_state import get_scroll_context

                    if get_scroll_context() == "netflix":
                        _is_nf_ctx = True
                except Exception:
                    pass
            # Check intent memory
            if not _is_nf_ctx:
                try:
                    from core.intent_memory import load_state

                    _intent = load_state()
                    if _intent and _intent.get("last_media_app") == "netflix":
                        _is_nf_ctx = True
                except Exception:
                    pass
            # Check result/error text for netflix
            if not _is_nf_ctx:
                _combined = _sp_result or _sp_error
                if "netflix" in _combined:
                    _is_nf_ctx = True
            if _is_nf_ctx:
                from actions.netflix_control import _set_netflix_vision_cooldown

                _set_netflix_vision_cooldown()


def is_failure(result: Any) -> bool:
    text = _norm(str(result))
    return any(
        x in text
        for x in (
            "failed",
            "error",
            "exception",
            "not found",
            "unknown",
            "cannot",
            "permission",
            "denied",
        )
    )


def fallback_suggestions(tool: str, parameters: dict | None, result: Any) -> list[tuple[str, dict]]:
    """Return safe alternate routes."""
    params = dict(parameters or {})
    action = _norm(params.get("action"))
    suggestions: list[tuple[str, dict]] = []

    if tool == "spotify_control":
        if action in ("play", "pause"):
            suggestions.append(("computer_control", {"action": "press", "key": "playpause"}))
        elif action == "next":
            suggestions.append(("computer_control", {"action": "press", "key": "nexttrack"}))
        elif action == "previous":
            suggestions.append(("computer_control", {"action": "press", "key": "prevtrack"}))

    if tool == "youtube_video" and action in ("youtube_pause", "youtube_resume"):
        suggestions.append(("browser_control", {"action": "press", "key": "Space"}))

    return suggestions
