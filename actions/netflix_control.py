# actions/netflix_control.py
from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from urllib.parse import quote_plus

from actions._netflix.js_snippets import (  # noqa: F401
    _FAST_SEARCH_READY_JS,
    _MARWA_PROFILE_JS,
    _NETFLIX_PLAY_BUTTON_JS,
    _NETFLIX_VISIBLE_GRID_JS,
    _SIMPLE_MODE_CANDIDATE_JS,
    _SIMPLE_MODE_TITLE_MATCH_JS,
)
from actions._netflix.player_api import PLAYER_ACTIONS
from actions.browser_control import get_browser_session
from core.intent_memory import remember_action

# ── Fullscreen settle window ────────────────────────────────────────────────────
# HTML5 fullscreen entry is not instantaneous: after the "f" key is dispatched it
# takes Chromium a beat to commit `document.fullscreenElement`. Any focus/z-order
# work in that gap (bring_to_front, SetWindowPos TOPMOST flip, the synthetic Shift
# tap in focus_window) aborts the transition — which is why both Era's fullscreen
# command AND a manual "f" would silently fail while Era was interacting with the
# tab. `_mark_fullscreen_pending` opens a short window during which
# `_focus_netflix_foreground` refuses to touch focus, so nothing races the entry.
_FS_SETTLE_UNTIL: float = 0.0
_FS_SETTLE_SECONDS: float = 2.5


def _mark_fullscreen_pending() -> None:
    """Suppress focus/z-order manipulation briefly while fullscreen is settling."""
    global _FS_SETTLE_UNTIL
    _FS_SETTLE_UNTIL = time.time() + _FS_SETTLE_SECONDS


def _fullscreen_settling() -> bool:
    return time.time() < _FS_SETTLE_UNTIL


# ── Netflix href validation ────────────────────────────────────────────────────

_NETFLIX_TITLE_RE = re.compile(r"https?://[^/]*netflix\.com/(?:title|watch)/\d+", re.I)


def _normalize_netflix_title_href(href: str) -> str:
    """Normalize a Netflix href to a /title/<id> or /watch/<id> URL.

    Handles /search?...&jbv=<id> URLs by extracting the numeric ID.
    Returns empty string for invalid, empty, or non-Netflix hrefs.
    """
    href = (href or "").strip()
    if not href:
        return ""

    bad_tokens = (
        "privacy",
        "preferences",
        "préférences",
        "confidentialité",
        "cookie",
        "legal",
        "help",
        "account",
        "footer",
    )
    low = href.lower()
    if any(token in low for token in bad_tokens):
        return ""

    if href.startswith("/"):
        href = "https://www.netflix.com" + href

    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(href)
    except Exception:
        return ""

    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if "netflix.com" not in host:
        return ""

    if "/title/" in path or "/watch/" in path:
        return href

    if path.startswith("/search"):
        qs = parse_qs(parsed.query or "")
        jbv = (qs.get("jbv") or [""])[0]
        if jbv and jbv.isdigit():
            return f"https://www.netflix.com/title/{jbv}"

    return ""


def _is_valid_netflix_href(href: str) -> bool:
    return bool(_normalize_netflix_title_href(href))


def _extract_netflix_title_id(href: str) -> str | None:
    """Extract the numeric title id from a Netflix URL.

    Handles /title/<id>, /watch/<id>, and the ?jbv=<id> form that browse,
    home and search cards use (see era-netflix-jbv-fullscreen). The jbv id is
    a real title id usable in /watch/<id>, so extracting it lets the caller
    take the reliable direct-/watch/ playback path instead of falling back to
    the flaky details-page Play button — which is why "play the top trending
    title" opened the page but never started playing.
    """
    s = str(href)
    m = re.search(r"/(?:title|watch)/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]jbv=(\d+)", s)
    return m.group(1) if m else None


AUTOPLAY_MIN_SCORE = 0.85

# ── V9C.3.1: Strict bad-element filter ──────────────────────────────────────
# Elements matching these tokens must NEVER be treated as Netflix title cards.
_BAD_NETFLIX_CARD_TOKENS = (
    "privacy",
    "confidentialit",
    "confidentialité",
    "preferences",
    "préférences",
    "pr",
    "cookie",
    "cookies",
    "legal",
    "footer",
    "help",
    "aide",
    "account",
    "profile",
    "notification",
    "navigation",
    "settings",
    "param",
    "botlink",
    "bot-link",
    "loc",
    "gpc",
    "opt-out",
    "optout",
    "consent",
    "gdpr",
    "privacy-center",
    "preference-center",
    "privacycenter",
    "preferencecenter",
    "manage-cookies",
    "cookiepolicy",
    "cookie-policy",
    "terms",
    "cgu",
    "mentions-legales",
)


def _is_bad_netflix_element(info: str) -> bool:
    """Return True if *info* (lowercased class/id/aria/text) contains a bad token.

    Short tokens ('pr', 'loc') are matched as whole words only — plain
    substring matching would reject legitimate titles like 'Sherlock' or
    'Prison Break'.
    """
    t = (info or "").lower()
    for tok in _BAD_NETFLIX_CARD_TOKENS:
        if len(tok) <= 3:
            if re.search(rf"\b{re.escape(tok)}\b", t):
                return True
        elif tok in t:
            return True
    return False


# ── V9C.2: Dirty query guard ────────────────────────────────────────────────

_DIRTY_QUERY_PHRASES = (
    "do you have",
    "can you",
    "could you",
    "would you",
    "have you got",
    "is there",
    "playing ",
    "on netflix",
    "on nf",
    "search for",
    "look for",
)


def _looks_like_dirty_media_query(query: str) -> bool:
    """Return True if the query contains filler phrases that suggest it was
    not properly cleaned before reaching the Netflix controller."""
    t = (query or "").lower().strip()
    return any(x in t for x in _DIRTY_QUERY_PHRASES)


# ── V9C.2.1: Title verification guard ──────────────────────────────────────

_NETFLIX_TITLE_FILLERS = re.compile(r"\b(netflix|movie|series|film|show|the|a|an)\b", re.I)

# ── Generic Netflix headings that must never be treated as movie/show titles ──
_NETFLIX_GENERIC_HEADINGS = frozenset(
    (
        "trouvez votre prochain coup de cœur",
        "trouvez votre prochain coup de coeur",
        "find your next favorite",
        "who's watching?",
        "who is watching?",
        "qui regarde ?",
        "qui est en train de regarder ?",
        "browse",
        "home",
    )
)


def _is_netflix_generic_heading(text: str) -> bool:
    """Return True if *text* matches a known generic Netflix page heading."""
    norm = _normalize_title_for_match(text)
    if not norm:
        return False
    for generic in _NETFLIX_GENERIC_HEADINGS:
        g_norm = _normalize_title_for_match(generic)
        if norm == g_norm:
            return True
    return False


def _normalize_title_for_match(text: str) -> str:
    """Normalize a title or query for comparison: lowercase, strip punctuation,
    remove media fillers, collapse spaces."""
    t = (text or "").lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = _NETFLIX_TITLE_FILLERS.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _is_verified_title_match(candidate: dict, query: str, min_score: float = 0.88) -> bool:
    """Return True only if the candidate title/text safely matches the requested
    Netflix query.  Used before autoplaying no-href geometry results.

    Safe matches:
      - exact match after normalization
      - candidate contains query as a phrase
      - fuzzy ratio >= min_score (default 0.88)

    Fails for empty/unknown candidate titles and low-confidence fuzzies.
    """
    cand_title = (candidate.get("title") or candidate.get("text") or "").strip()
    if not cand_title:
        return False
    q_norm = _normalize_title_for_match(query)
    c_norm = _normalize_title_for_match(cand_title)
    if not q_norm or not c_norm:
        return False
    # Exact match
    if q_norm == c_norm:
        return True
    # Candidate contains query as full phrase (e.g. "One Piece Film Red" contains "one piece")
    if q_norm in c_norm:
        return True
    # Query contains candidate as full phrase (e.g. residual filler the
    # sanitizer missed: "next to play one piece" contains "one piece")
    if len(c_norm) >= 4 and c_norm in q_norm:
        return True
    # Fuzzy ratio
    ratio = difflib.SequenceMatcher(None, q_norm, c_norm).ratio()
    return ratio >= min_score


def _read_netflix_visible_title(page, sess) -> str | None:
    """Try to extract the visible Netflix title text from the current page
    (modal, details panel, player overlay, or full /title/ page).
    Returns None if unavailable."""
    if page is None:
        return None
    js = """(() => {
        var GENERIC_HEADINGS = [
            'trouvez votre prochain coup de coeur',
            'trouvez votre prochain coup de c\\u0153ur',
            'find your next favorite',
            "who's watching?",
            'who is watching?',
            'qui regarde ?',
            'qui est en train de regarder ?',
            'browse', 'home'
        ];
        function isGeneric(txt) {
            var t = txt.toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
            for (var gi = 0; gi < GENERIC_HEADINGS.length; gi++) {
                var g = GENERIC_HEADINGS[gi].toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
                if (t === g) return true;
            }
            return false;
        }
        var selectors = [
            '[data-uia="title-text"]',
            '[data-uia="title"] h1', '[data-uia="title"] h2',
            '[data-uia="title-info"] h1', '[data-uia="title-info"] h2',
            '[class*="title-info"] h1', '[class*="title-info"] h2',
            '.DetailContainer h1', '.DetailContainer h2',
            '.preview-modal h1', '.preview-modal h2',
            '.detail-modal h1', '.detail-modal h2',
            '[class*="detail"] h1', '[class*="detail"] h2',
            '.video-title h1', '.video-title h2',
            '.title-info-container h1', '.title-info-container h2',
            '.title-info h1', '.title-info h2',
            '.jawbone-main h1', '.jawbone-main h2'
        ];
        for (var i = 0; i < selectors.length; i++) {
            var el = document.querySelector(selectors[i]);
            if (el) {
                var txt = (el.textContent || '').trim();
                if (txt.length >= 2 && txt.length < 200) {
                    if (!isGeneric(txt)) { return txt; }
                }
            }
        }
        // Netflix often renders the title as a logo image (billboard-title,
        // preview modal story art) instead of text, leaving the containers
        // above textContent-empty — check alt text before falling back to a
        // bare h1/h2, which may belong to an unrelated background row.
        var imgContainerSelectors = [
            '[data-uia="billboard-title"]',
            '[data-uia="previewModal--player_container"]',
            '[data-uia*="previewModal"]'
        ];
        for (var j = 0; j < imgContainerSelectors.length; j++) {
            var container = document.querySelector(imgContainerSelectors[j]);
            if (container) {
                // querySelector('img[alt]') matches alt="" too, so scan all
                // images in the container for the first non-empty alt.
                var imgs = container.querySelectorAll('img[alt]');
                for (var k = 0; k < imgs.length; k++) {
                    var alt = (imgs[k].getAttribute('alt') || '').trim();
                    if (alt.length >= 2 && alt.length < 200 && !isGeneric(alt)) { return alt; }
                }
            }
        }
        // Last resort: a bare h1/h2 — only reliable on plain full-page
        // layouts with no modal/billboard overlay present.
        var fallback = document.querySelector('h1, h2');
        if (fallback) {
            var fbTxt = (fallback.textContent || '').trim();
            if (fbTxt.length >= 2 && fbTxt.length < 200 && !isGeneric(fbTxt)) { return fbTxt; }
        }
        return null;
    })()"""
    try:
        result = sess.run(page.evaluate(js))
        if result and isinstance(result, str) and len(result.strip()) >= 2:
            return result.strip()
    except Exception:
        pass
    return None


# ── Simple mode toggle ────────────────────────────────────────────────────────
# When True, Netflix search uses direct URL + DOM text matching instead of the
# smart card scanner.  Flip to False to fall back to the legacy scanner path.
NETFLIX_SIMPLE_MODE: bool = True

# ── Fast search mode toggle ───────────────────────────────────────────────────
# When True, Netflix search uses an aggressive early-exit polling loop before
# falling back to the stable search path. Set to False to disable the fast path.
NETFLIX_FAST_SEARCH_ENABLED: bool = True


def _save_netflix_grid(cards: list, query: str) -> None:
    """Save Netflix grid snapshot for ordinal selection."""
    try:
        import time

        from core.browser_grid_state import GridCandidate, GridSnapshot, save_grid

        candidates = []
        for i, card in enumerate(cards):
            raw_title = getattr(card, "title", "") or ""
            if not raw_title and isinstance(card, dict):
                raw_title = card.get("title", "")
            title = _clean_netflix_title(raw_title)
            if not title:
                continue
            url = getattr(card, "href", "") or ""
            if not url and isinstance(card, dict):
                url = card.get("url", "") or card.get("href", "")
            candidates.append(
                GridCandidate(
                    visible_order=i + 1,
                    title=title,
                    url=url,
                    kind="title",
                    safe=True,
                    source="netflix",
                )
            )
        snap = GridSnapshot(
            platform="netflix",
            query=query or "",
            timestamp=time.time(),
            candidates=candidates,
        )
        save_grid(snap)
        print(f"[Netflix] Grid saved: {len(candidates)} candidates (query='{query}')")
    except Exception as e:
        print(f"[Netflix] Failed to save grid: {e}")


# ── V9C.3: Visible grid model + fast cache ──────────────────────────────────
# Rows are viewport-relative (~6 titles per row).  The JS scanner groups
# visible elements by Y-position proximity (≤ 40 px gap = same row), sorts
# row-major, and assigns (row, col) indices.  A JSON cache at
# runtime/netflix_grid_cache.json keeps the last scan for 300 s so that
# follow-up row/col commands ("row 2, title 5") resolve instantly.

import os as _os  # noqa: E402  (kept beside the cache constant it builds)
import pathlib as _pathlib  # noqa: E402

_NETFLIX_GRID_CACHE_PATH = str(
    _pathlib.Path(__file__).resolve().parent.parent / "runtime" / "netflix_grid_cache.json"
)
_NETFLIX_GRID_CACHE_TTL = 300  # seconds


def scan_netflix_visible_grid(page, sess):
    """Scan visible Netflix cards and return a grid with row/col coordinates.

    Returns dict with key 'rows' (list of dicts, each with row, col, title, href, x, y, w, h)
    and 'count' (total cards).  Returns None on failure.
    """
    if page is None:
        return None
    try:
        raw = sess.run(page.evaluate(_NETFLIX_VISIBLE_GRID_JS))
        raw_str = str(raw) if not isinstance(raw, str) else raw
        import json as _json

        data = _json.loads(raw_str) if raw_str.strip().startswith("{") else {}
        rows = data.get("rows", [])
        if not rows:
            return None
        return data
    except Exception as exc:
        print(f"[Netflix] Visible grid scan failed: {exc}")
        return None


def _save_netflix_grid_cache(grid_data: dict) -> None:
    """Persist the visible grid cache to disk."""
    try:
        _os.makedirs(_os.path.dirname(_NETFLIX_GRID_CACHE_PATH), exist_ok=True)
        import json as _json

        grid_data["_cached_at"] = time.time()
        with open(_NETFLIX_GRID_CACHE_PATH, "w", encoding="utf-8") as f:
            _json.dump(grid_data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[Netflix] Failed to save grid cache: {exc}")


def _load_netflix_grid_cache() -> dict | None:
    """Load the grid cache if valid (not expired).  Returns None if missing/expired."""
    try:
        if not _os.path.exists(_NETFLIX_GRID_CACHE_PATH):
            return None
        import json as _json

        with open(_NETFLIX_GRID_CACHE_PATH, encoding="utf-8") as f:
            data = _json.load(f)
        cached_at = data.get("_cached_at", 0)
        if time.time() - cached_at > _NETFLIX_GRID_CACHE_TTL:
            return None
        if not data.get("rows"):
            return None
        return data
    except Exception:
        return None


def _find_card_at_row_col(grid_data: dict, row: int, col: int) -> dict | None:
    """Look up a card by row/col in the grid data.  Returns the card dict or None."""
    for card in grid_data.get("rows", []):
        if card.get("row") == row and card.get("col") == col:
            return card
    return None


def _click_cached_netflix_card(row: int, col: int, page, sess) -> dict | None:
    """Click a Netflix card at (row, col) using the fast cache.

    Validates the element at (x, y) is not a bad element before clicking.
    Returns the card dict on success, None on failure.
    """
    if page is None or row < 1 or col < 1:
        return None
    # Try live scan first
    grid = scan_netflix_visible_grid(page, sess)
    if grid:
        _save_netflix_grid_cache(grid)
    else:
        grid = _load_netflix_grid_cache()
    if not grid:
        return None
    card = _find_card_at_row_col(grid, row, col)
    if not card:
        return None
    x, y = card.get("x", 0), card.get("y", 0)
    if x <= 0 or y <= 0:
        return None
    # Validate element at point is not a bad element, then click
    click_js = (
        "() => {"
        "  var BAD=['privacy','confidentialit','confidentialité','preferences','préférences','cookie','legal',"
        "    'footer','help','aide','account','profile','notification',"
        "    'navigation','settings','param','botlink','gpc','opt-out',"
        "    'optout','consent','gdpr','terms','cgu','mentions-legales'];"
        "  function hasBad(el){"
        "    var p=(el.id+' '+el.className+' '+(el.getAttribute('data-uia')||'')+' '+(el.getAttribute('aria-label')||'')+' '+(el.getAttribute('href')||'')).toLowerCase();"
        "    var t=(el.textContent||'').trim().toLowerCase();if(t.length<120)p+=' '+t;"
        "    for(var bi=0;bi<BAD.length;bi++){if(p.indexOf(BAD[bi])>=0)return true;}return false;"
        "  }"
        "  var el = document.elementFromPoint(" + str(x) + ", " + str(y) + ");"
        "  if (!el) return JSON.stringify({ok:false, reason:'no_element'});"
        "  if (hasBad(el)) return JSON.stringify({ok:false, reason:'bad_element', text:(el.textContent||'').trim().slice(0,60)});"
        "  var cur = el;"
        "  while (cur && cur !== document.body) {"
        "    if (hasBad(cur)) return JSON.stringify({ok:false, reason:'bad_ancestor', text:(cur.textContent||'').trim().slice(0,60)});"
        "    cur = cur.parentElement;"
        "  }"
        "  el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, clientX:"
        + str(x)
        + ", clientY:"
        + str(y)
        + "}));"
        "  el.dispatchEvent(new MouseEvent('mousemove', {bubbles:true, clientX:"
        + str(x)
        + ", clientY:"
        + str(y)
        + "}));"
        "  el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:"
        + str(x)
        + ", clientY:"
        + str(y)
        + "}));"
        "  el.click();"
        "  el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:"
        + str(x)
        + ", clientY:"
        + str(y)
        + "}));"
        "  return JSON.stringify({ok:true});"
        "}"
    )
    try:
        before_url = ""
        try:
            from utils.human_click import human_like_click, page_url, wait_for_url_change

            before_url = page_url(sess, page)
        except Exception:
            human_like_click = None  # type: ignore[assignment]
        result = sess.run(page.evaluate(click_js))
        result_str = str(result) if result else ""
        if '"ok": true' in result_str or "'ok': true" in result_str:
            print(
                f"[Netflix] Clicked cached card at row={row} col={col} ({card.get('title', '')[:40]})"
            )
            # Netflix can silently ignore untrusted el.click(). Verify the page
            # reacted (card clicks navigate or add ?jbv=); if not, retry with a
            # trusted human-like CDP mouse click at the same coordinates.
            try:
                if (
                    human_like_click
                    and before_url
                    and not wait_for_url_change(sess, page, before_url, timeout_s=2.0)
                ):
                    print(
                        f"[Netflix] Synthetic click had no effect — human-like fallback at ({x}, {y})"
                    )
                    human_like_click(sess, page, float(x), float(y), label=f"netflix r{row}c{col}")
            except Exception as exc:
                print(f"[Netflix] Human-click fallback failed: {exc}")
            return card
        print(f"[Netflix] Cached click rejected: {result_str[:200]}")
        # Clear stale cache on bad-element rejection
        try:
            _os.remove(_NETFLIX_GRID_CACHE_PATH)
        except Exception:
            pass
    except Exception as exc:
        print(f"[Netflix] Cached card click failed: {exc}")
    return None


def _handle_grid_play_row_col(
    row: int, col: int, state: str, params: dict, sess, page, player=None
) -> str | None:
    """Handle grid_play with explicit row/col parameters.  Returns a response string or None to fall through."""
    if page is None:
        return None
    # V9C.3.1: Close any open modal before grid selection
    _close_netflix_modal_for_grid_selection(sess, page)
    card = _click_cached_netflix_card(row, col, page, sess)
    if not card:
        return f"I could not find a card at row {row}, column {col} on the visible Netflix grid."
    time.sleep(1.5)
    href = card.get("href", "")
    if href:
        # Navigated via click, now try to play
        if _verify_playback_started(sess, page):
            _set_playing(card.get("title", f"row {row} col {col}"))
            return f"Playing: {card.get('title', f'row {row} col {col}')}"
        play_err = click_netflix_play_button_for_current_title(
            sess, page, expected_title=card.get("title", "")
        )
        if play_err is None:
            _set_playing(card.get("title", f"row {row} col {col}"))
            return f"Playing: {card.get('title', f'row {row} col {col}')}"
        # Do NOT use generic non-scoped fallback when expected_title is known.
        return f"I opened '{card.get('title', '')}' at row {row} col {col}, but could not start playback."
    else:
        # No href — card was clicked via elementFromPoint; check if modal/play appeared
        if _verify_playback_started(sess, page):
            _set_playing(card.get("title", f"row {row} col {col}"))
            return f"Playing: {card.get('title', f'row {row} col {col}')}"
        play_err = click_netflix_play_button_for_current_title(
            sess, page, expected_title=card.get("title", "")
        )
        if play_err is None:
            _set_playing(card.get("title", f"row {row} col {col}"))
            return f"Playing: {card.get('title', f'row {row} col {col}')}"
        # Do NOT use generic non-scoped fallback when expected_title is known.
        return (
            f"I selected '{card.get('title', f'row {row} col {col}')}' on Netflix, "
            f"but could not start playback automatically."
        )


def _deduplicate_query(query: str) -> str:
    """Remove consecutive duplicate words from query."""
    import re

    return re.sub(r"\b(\w+)\s+\1\b", r"\1", query.strip(), flags=re.IGNORECASE)


# Spoken-intent phrases that must never reach the title matcher: the scorer
# requires the candidate title to start with the full query, so a query like
# "next to play One Piece" can never match the card "One Piece".
_INTENT_PREFIX_RE = re.compile(
    r"""^(?:
        (?:next\s+to\s+play|to\s+play|play|watch|open|start|launch|resume|
           put\s+on|i\s+(?:want|would\s+like)\s+to\s+(?:watch|play|see)|
           can\s+you\s+(?:play|put\s+on|open)|
           lance|joue|mets?|regarde[rz]?|ouvre|d[ée]marre|
           please|s'il\s+te\s+pla[iî]t)
        \s+
    )+""",
    re.IGNORECASE | re.VERBOSE,
)
_INTENT_SUFFIX_RE = re.compile(
    r"""(?:\s+(?:on\s+netflix|netflix|please|for\s+me|now|
                sur\s+netflix|s'il\s+te\s+pla[iî]t|maintenant))+$""",
    re.IGNORECASE | re.VERBOSE,
)

# Availability questions: "is The Creator on Netflix" must search "The Creator".
# Only applied when the query is provably a QUESTION (it named the platform or
# ended with "?") — a bare leading "is" is otherwise part of real titles
# ("Is It Cake?"), so stripping it unconditionally would corrupt them.
_AVAILABILITY_PREFIX_RE = re.compile(
    r"""^(?:
        do\s+you\s+have|have\s+you\s+got|is\s+there|
        y\s+a-t-il|avez-vous|as-tu|est-ce\s+qu[e']\s*(?:il\s+y\s+a)?|
        is|are|was
    )\s+""",
    re.IGNORECASE | re.VERBOSE,
)
_QUESTION_FORM_RE = re.compile(r"\b(?:on|sur|en)\s+netflix\b", re.IGNORECASE)


# Every key Gemini might place a Netflix title under. The tool schema declares
# `query`, but models are inconsistent and also emit `title`, `value`,
# `media_title`, `name`, `search`. Reading only `query`/`value` was why an
# explicitly named title still hit "Netflix search requires a title" — the
# title arrived under a key the handler never looked at.
_MEDIA_QUERY_KEYS = ("query", "value", "title", "media_title", "search", "name", "text")


def _media_query_from_params(params: dict | None) -> str:
    """First non-empty title-bearing value across the keys models actually use."""
    p = params or {}
    for key in _MEDIA_QUERY_KEYS:
        val = p.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _sanitize_media_query(query: str) -> str:
    """Strip spoken command filler from a media query, keeping the bare title.

    'next to play One Piece' -> 'One Piece'; 'play war machine on netflix'
    -> 'war machine'; 'is The Creator on Netflix?' -> 'The Creator'. Returns
    the original query if stripping would leave nothing (e.g. the title
    itself is a filler word).
    """
    q = _deduplicate_query(query or "")
    if not q:
        return q
    # Question detection BEFORE any stripping: the platform suffix and the
    # trailing "?" are the evidence, and both get removed below.
    question_form = bool(_QUESTION_FORM_RE.search(q)) or q.rstrip().endswith("?")
    stripped = q.rstrip(" ?¿!.").strip()
    stripped = _INTENT_PREFIX_RE.sub("", stripped).strip()
    stripped = _INTENT_SUFFIX_RE.sub("", stripped).strip()
    if question_form:
        stripped = _AVAILABILITY_PREFIX_RE.sub("", stripped).strip()
    if not stripped:
        return q
    if stripped != q:
        print(f"[Netflix] query sanitized: {q!r} -> {stripped!r}")
    return stripped


def _infer_netflix_query_from_page(page, sess, params: dict | None) -> str:
    """Infer Netflix search query for grid context safety.

    Priority:
      1) params keys query/title/value
      2) current page URL ?q=
      3) recent grid query stored in core.browser_grid_state (if any)
    """
    p = params or {}
    for k in ("query", "title", "value"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return _deduplicate_query(v)

    # URL-based inference: /search?q=...
    url = ""
    try:
        if page is not None:
            url = sess.run(page.evaluate("window.location.href")) or ""
        else:
            # best-effort: no page, still try session-level evaluation
            url = ""
    except Exception:
        url = ""

    url = (url or "").strip()
    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        if q and isinstance(q, str):
            q = q.replace("+", " ").strip()
            if q:
                return _deduplicate_query(q)
    except Exception:
        pass

    # Fallback: last saved grid query (runtime only)
    try:
        from core.browser_grid_state import get_recent_grid

        snap = get_recent_grid("netflix")
        if snap and isinstance(snap.query, str) and snap.query.strip():
            return _deduplicate_query(snap.query)
    except Exception:
        pass

    return ""


def _clean_netflix_title(raw: str) -> str:
    """Clean a Netflix card title by removing metadata phrases.

    Strips known Netflix metadata fragments. Returns empty string if
    nothing meaningful survives cleaning.
    """
    if not raw or not raw.strip():
        return ""
    import re

    t = raw.strip()
    metadata_patterns = [
        r"Disponible maintenant",
        r"Nouveauté",
        r"Saison\s*\d+",
        r"il y a\s*.*",
        r"Épisode\s*\d+",
        r"Regarder",
        r"Reprendre",
        r"Lecture",
        r"Nouveaux épisodes",
        r"Classement",
    ]
    for pat in metadata_patterns:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    t = t.strip()
    if len(t) < 2:
        return ""
    return t


# ── State machine constants ──────────────────────────────────────────────────

_STATE_BROWSE = "browse"
_STATE_SEARCH_RESULTS = "search_results"
_STATE_DETAILS = "details"
_STATE_PLAYER = "player"
_STATE_UNKNOWN = "unknown"


# ── Vision cooldown state ─────────────────────────────────────────────────────
_VISION_COOLDOWN_UNTIL: float = 0.0
_VISION_ALREADY_ATTEMPTED: bool = False


def _reset_vision_attempted():
    global _VISION_ALREADY_ATTEMPTED
    _VISION_ALREADY_ATTEMPTED = False


def _reset_netflix_vision_cooldown():
    """Reset Netflix Vision cooldown state. For use in tests."""
    global _VISION_COOLDOWN_UNTIL
    _VISION_COOLDOWN_UNTIL = 0.0


def _set_netflix_vision_cooldown(duration: float = 45.0) -> None:
    """Set Netflix Vision cooldown to prevent repeated screen_process calls."""
    global _VISION_COOLDOWN_UNTIL
    _VISION_COOLDOWN_UNTIL = time.monotonic() + duration
    print(
        f"[Netflix] Vision cooldown set for {duration:.0f}s (until monotonic {_VISION_COOLDOWN_UNTIL:.0f})"
    )


def is_netflix_vision_blocked() -> bool:
    """Public check for router guard — True if Netflix Vision cooldown is active."""
    global _VISION_COOLDOWN_UNTIL
    if time.monotonic() < _VISION_COOLDOWN_UNTIL:
        return True
    return False


# ── Netflix failure context (router-level screen_process suppression) ─────────
_NETFLIX_FAILURE_TTL: float = 120.0
_LAST_NETFLIX_FAILURE_AT: float = 0.0
_LAST_NETFLIX_FAILURE_TOOL: str = ""


def _set_netflix_failure(tool: str, error_text: str = "") -> None:
    """Record a Netflix automation failure so the router can suppress
    automatic screen_process calls for a short TTL window.

    This is Netflix-specific: it only affects screen_process calls that the
    router detects as Netflix-related. Non-Netflix screen_process and
    browser_control calls are never blocked by this flag.
    """
    global _LAST_NETFLIX_FAILURE_AT, _LAST_NETFLIX_FAILURE_TOOL
    _LAST_NETFLIX_FAILURE_AT = time.monotonic()
    _LAST_NETFLIX_FAILURE_TOOL = tool or "unknown"
    print(
        f"[Netflix] Failure context set (tool={tool}, ttl={_NETFLIX_FAILURE_TTL:.0f}s) "
        f"error={error_text[:120]!r}"
    )


def is_netflix_failure_context_active() -> bool:
    """Public check for router guard — True if within the Netflix failure TTL."""
    if _LAST_NETFLIX_FAILURE_AT <= 0.0:
        return False
    return (time.monotonic() - _LAST_NETFLIX_FAILURE_AT) < _NETFLIX_FAILURE_TTL


def _clear_netflix_failure() -> None:
    """Clear Netflix failure context (e.g. after a successful action)."""
    global _LAST_NETFLIX_FAILURE_AT, _LAST_NETFLIX_FAILURE_TOOL
    _LAST_NETFLIX_FAILURE_AT = 0.0
    _LAST_NETFLIX_FAILURE_TOOL = ""


# ── State detection ───────────────────────────────────────────────────────────


def _detect_state(sess, page) -> dict:
    """Detect Netflix page state from URL and DOM signals.

    Returns dict with keys: state, signals
    """
    signals = _check_local_signals(sess, page)
    url = signals["current_url"].lower() if signals["current_url"] else ""

    if not signals["active_netflix"]:
        return {"state": _STATE_UNKNOWN, "signals": signals}
    if "/watch/" in url:
        return {"state": _STATE_PLAYER, "signals": signals}
    if "/search" in url:
        return {"state": _STATE_SEARCH_RESULTS, "signals": signals}
    if "/title/" in url:
        return {"state": _STATE_DETAILS, "signals": signals}
    if "/browse" in url:
        browse_signals = _check_local_signals(sess, page)
        if browse_signals.get("current_url", "").startswith("https://www.netflix.com/browse"):
            has_details_modal = False
            try:
                modal_js = """() => { const m = document.querySelector('.modal-container, .detail-modal, .modal-wrapper'); return m ? JSON.stringify({modal: m.id || 'found', bbox: m.getBoundingClientRect()}) : 'null'; }"""
                if page is not None:
                    modal_raw = sess.run(page.evaluate(modal_js))
                else:
                    modal_raw = sess.run(sess.evaluate_background_media("netflix.com", modal_js))

                if isinstance(modal_raw, str) and modal_raw != "null":
                    has_details_modal = True
            except Exception:
                pass
            if has_details_modal:
                return {"state": _STATE_DETAILS, "signals": browse_signals}
        return {"state": _STATE_BROWSE, "signals": browse_signals}
    if signals["has_play_button"]:
        return {"state": _STATE_DETAILS, "signals": signals}
    return {"state": _STATE_UNKNOWN, "signals": signals}


# ── Page acquisition ─────────────────────────────────────────────────────────


def _acquire_netflix_page(sess_name: str = "brave") -> tuple:
    """Acquire owned Netflix page and browser session.

    Returns (sess, page) where page may be None.
    Raises RuntimeError if browser session cannot be obtained.
    """
    sess = get_browser_session(sess_name)
    page = None
    try:
        from actions.platform_tab import get_or_open_platform_tab

        tab_result = get_or_open_platform_tab(platform="netflix", browser=sess_name)
        page = tab_result.page
    except Exception:
        pass
    return sess, page


def _focus_netflix_foreground(page, sess, browser: str = "brave") -> bool:
    """Bring Netflix browser tab to foreground and focus the browser window.

    Uses CDP bring_to_front + window.focus() and OS-level window focus via
    existing project helpers. Logs before/after URL and focus success status.
    """
    before_url = ""
    after_url = ""
    ok = False

    # Never touch window focus/z-order while the player is fullscreen —
    # bring_to_front + SetWindowPos/SetForegroundWindow kicks Chromium out of
    # fullscreen, which made fullscreen impossible (even manually) while Era
    # was polling Netflix.
    #
    # Two guards, both needed:
    #  1. Settle window — a fullscreen request was just dispatched (by Era or,
    #     conservatively, whenever we suspect one). `document.fullscreenElement`
    #     is still null mid-transition, so the committed check below can't see
    #     it yet; skip regardless so we don't abort the entry.
    #  2. Committed check — already fullscreen, steady state.
    if _fullscreen_settling():
        print("[Netflix] focus skipped: fullscreen settling")
        return True
    try:
        if page is not None and sess.run(page.evaluate("!!document.fullscreenElement")) is True:
            print("[Netflix] focus skipped: player is fullscreen")
            return True
    except Exception:
        pass

    try:
        if page is not None:
            try:
                before_url = sess.run(page.evaluate("window.location.href"))
            except Exception:
                before_url = getattr(page, "url", "") or ""
    except Exception:
        pass
    print(f"[Netflix] netflix_focus_before_url={str(before_url)[:100]!r}")

    try:
        if page is not None:
            try:
                sess.run(page.bring_to_front())
            except Exception:
                pass
            try:
                sess.run(page.evaluate("window.focus()"))
            except Exception:
                pass
        try:
            from actions.window_control import focus_window

            focus_window(browser)
        except Exception:
            pass
        ok = True
    except Exception:
        ok = False

    try:
        if page is not None:
            try:
                after_url = sess.run(page.evaluate("window.location.href"))
            except Exception:
                after_url = getattr(page, "url", "") or ""
    except Exception:
        pass
    print(f"[Netflix] netflix_focus_after_url={str(after_url)[:100]!r}")
    print(f"[Netflix] foreground_focus_ok={ok}")
    return ok


# ── Local signals ─────────────────────────────────────────────────────────────


def _check_local_signals(sess, page) -> dict:
    """Non-destructive check of local DOM signals on current Netflix page.

    Returns dict with booleans:
      has_play_button  — DOM contains visible Play/Lire/Resume/Continuer button
      has_video        — DOM contains a <video> element
      url_contains_watch — current URL includes /watch/
      has_player_controls — Netflix player controls detected
      active_netflix   — current URL is netflix.com
      page_title       — document title
      current_url      — page URL
      watch_links      — number of links containing /watch/
    """
    result = {
        "has_play_button": False,
        "has_video": False,
        "url_contains_watch": False,
        "has_player_controls": False,
        "active_netflix": False,
        "page_title": "",
        "current_url": "",
        "watch_links": 0,
    }
    js = """
    () => {
        const url = window.location.href || '';
        const title = document.title || '';
        const video = document.querySelector('video');
        const player = document.querySelector(
            '[data-uia="player"], [data-uia="player-canvas"], ' +
            '.watch-video, .nf-player-container'
        );
        const watchLinks = document.querySelectorAll('a[href*="/watch"]').length;
        const playSelectors = [
            'a[aria-label*="play" i]', 'a[aria-label*="lire" i]',
            'a[aria-label*="resume" i]', 'a[aria-label*="lecture" i]',
            'a[aria-label*="continuer" i]', 'a[aria-label*="watch" i]',
            'button[aria-label*="play" i]', 'button[aria-label*="lire" i]',
            'button[aria-label*="resume" i]', 'button[aria-label*="lecture" i]',
            'button[aria-label*="continuer" i]', 'button[aria-label*="watch" i]',
            '.play-button', '.playLink', 'a.playLink',
            'button[data-uia*="play"]', 'a[data-uia*="play"]',
            '[data-uia="action-play-pretitle"]', '[data-uia="action-play-title"]',
            '[data-uia="play"]', 'a[href*="/watch"]',
            '.btn-play', '.billboard a.playLink',
            '[data-uia="action-select-profile"] ~ a[href*="/watch"]'
        ].join(',');
        const allButtons = document.querySelectorAll(playSelectors);
        let hasPB = false;
        for (const btn of allButtons) {
            const r = btn.getBoundingClientRect();
            if (r.width > 10 && r.height > 10 && btn.offsetParent !== null) {
                hasPB = true;
                break;
            }
        }
        return JSON.stringify({
            hasPlayButton: hasPB,
            hasVideo: video !== null,
            urlContainsWatch: url.includes('/watch/'),
            hasPlayerControls: player !== null,
            activeNetflix: url.includes('netflix.com'),
            pageTitle: title,
            currentUrl: url,
            watchLinks: watchLinks
        });
    }
    """
    try:
        import json as _json

        if page is not None:
            raw = sess.run(page.evaluate(js))
        else:
            raw = sess.run(sess.evaluate_background_media("netflix.com", js))
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
        result["has_play_button"] = parsed.get("hasPlayButton", False)
        result["has_video"] = parsed.get("hasVideo", False)
        result["url_contains_watch"] = parsed.get("urlContainsWatch", False)
        result["has_player_controls"] = parsed.get("hasPlayerControls", False)
        result["active_netflix"] = parsed.get("activeNetflix", False)
        result["page_title"] = parsed.get("pageTitle", "")
        result["current_url"] = parsed.get("currentUrl", "")
        result["watch_links"] = parsed.get("watchLinks", 0)
    except Exception:
        pass
    return result


def _verify_playback_active(sess, page) -> bool:
    """Verify that Netflix playback is actually active.

    Polls up to 20 seconds for:
      - /watch/ URL
      - video element OR Netflix player container
    Also detects blocking states (profile, login, PIN, error).

    Does NOT trust player controls alone (controls can be visible
    on a paused /watch/ screen with no actual playback).
    """
    import json as _json
    import time as _time

    # JS to detect video element, player containers, and blocking states
    player_check_js = """
    () => {
        var url = window.location.href || '';
        var result = {url: url, hasVideo: false, hasPlayer: false, blocked: false, blockReason: ''};

        // Check for blocking states first
        var blockingSelectors = [
            {sel: '[data-profile-selector], .profile-management, .ProfilesBillboard', reason: 'profile_selection'},
            {sel: '[data-login], .login-form, .auth-page, #appMountPoint .login', reason: 'login_page'},
            {sel: '[data-uia="pin-input"], .pin-input, .maturity-prompt', reason: 'pin_prompt'},
            {sel: '.error-page, .error-container, [data-error]', reason: 'error_page'},
        ];
        for (var i = 0; i < blockingSelectors.length; i++) {
            var el = document.querySelector(blockingSelectors[i].sel);
            if (el && el.getBoundingClientRect().width > 50) {
                result.blocked = true;
                result.blockReason = blockingSelectors[i].reason;
                return JSON.stringify(result);
            }
        }

        // Check for video element
        var v = document.querySelector('video');
        if (v) {
            result.hasVideo = true;
            result.paused = v.paused;
            result.currentTime = v.currentTime;
            result.readyState = v.readyState;
            result.duration = v.duration;
        }

        // Check for Netflix player containers
        if (!result.hasVideo) {
            var playerSelectors = [
                '[data-uia*="player"]',
                '[data-uia*="video"]',
                '.watch-video',
                '.VideoContainer',
                '[class*="Player"]',
                '[class*="player"]',
            ];
            for (var j = 0; j < playerSelectors.length; j++) {
                var pel = document.querySelector(playerSelectors[j]);
                if (pel && pel.getBoundingClientRect().width > 100) {
                    result.hasPlayer = true;
                    break;
                }
            }
        }

        return JSON.stringify(result);
    }
    """

    # Pre-check: URL must contain /watch/
    signals = _check_local_signals(sess, page)
    if not signals.get("url_contains_watch"):
        print(
            f"[Netflix] Playback NOT verified: no /watch/ in URL ({signals.get('current_url', '')[:80]})"
        )
        return False

    # Poll up to 20 seconds
    max_attempts = 20
    for attempt in range(max_attempts):
        try:
            if page is not None:
                raw = sess.run(page.evaluate(player_check_js))
            else:
                raw = sess.run(sess.evaluate_background_media("netflix.com", player_check_js))
            state = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            _time.sleep(1.0)
            continue

        # Blocking state detected — playback will not happen
        if state.get("blocked"):
            print(f"[Netflix] Playback blocked: {state.get('blockReason', 'unknown')}")
            return False

        # Video element found — check if playing
        if state.get("hasVideo"):
            if not state.get("paused", True):
                print("[Netflix] Playback verified: video actively playing (not paused)")
                return True
            # Paused but video exists — check if currentTime advances
            ct1 = state.get("currentTime", 0)
            _time.sleep(1.0)
            try:
                if page is not None:
                    raw2 = sess.run(page.evaluate(player_check_js))
                else:
                    raw2 = sess.run(sess.evaluate_background_media("netflix.com", player_check_js))
                state2 = _json.loads(raw2) if isinstance(raw2, str) else raw2
            except Exception:
                continue
            ct2 = state2.get("currentTime", 0)
            if ct2 > ct1 + 0.1:
                print(f"[Netflix] Playback verified: currentTime advanced {ct1:.1f} -> {ct2:.1f}")
                return True
            # Video exists but paused and not advancing — might still be loading
            # Keep polling if player container is present
            if state2.get("hasPlayer") and attempt < max_attempts - 1:
                _time.sleep(1.0)
                continue
            print(
                f"[Netflix] Playback NOT verified: video paused (ct={ct1:.1f}s, dur={state.get('duration', 0):.1f}s)"
            )
            return False

        # Player container found but no video element yet — keep waiting
        if state.get("hasPlayer"):
            if attempt < max_attempts - 1:
                _time.sleep(1.0)
                continue
            print(
                f"[Netflix] Playback NOT verified: player container found but no video element after {max_attempts}s"
            )
            return False

        # Neither video nor player — wait and retry
        if attempt < max_attempts - 1:
            _time.sleep(1.0)
            continue

    print(
        f"[Netflix] Playback NOT verified: no video element or player container on /watch/ page after {max_attempts}s"
    )
    return False


def _verify_playback_started(sess, page) -> bool:
    """Compatibility wrapper — delegates to strict _verify_playback_active."""
    return _verify_playback_active(sess, page)


def _verify_title_context_matches(
    page, sess, expected_title: str, expected_title_id: str | None = None
) -> bool:
    """Verify current page belongs to the expected Netflix title.

    Checks:
      - Current URL contains expected /title/<id> if available
      - Or page og:title / document.title / visible h1 matches expected title
      - Strong normalized match required
    Returns True only if identity is proven.
    """
    if not expected_title:
        return False

    try:
        url = ""
        if page is not None:
            url = str(sess.run(page.evaluate("window.location.href")) or "")
        else:
            url = str(
                sess.run(sess.evaluate_background_media("netflix.com", "window.location.href"))
                or ""
            )
    except Exception:
        return False

    url = url.strip()

    # If we have an expected_title_id, check URL contains /title/<id> or /watch/<id>
    if expected_title_id:
        if f"/title/{expected_title_id}" in url or f"/watch/{expected_title_id}" in url:
            return True

    # Check URL for known pattern: /title/<digits> or /watch/<digits>
    known_url_id = _extract_netflix_title_id(url)
    if known_url_id and expected_title_id and known_url_id == expected_title_id:
        return True

    # Try to extract page title metadata
    try:
        title_js = """
        () => {
            var og = document.querySelector('meta[property="og:title"]');
            if (og) return og.getAttribute('content') || '';
            var h1 = document.querySelector('h1');
            if (h1) return h1.textContent || '';
            return document.title || '';
        }
        """
        if page is not None:
            raw = str(sess.run(page.evaluate(title_js)) or "")
        else:
            raw = str(sess.run(sess.evaluate_background_media("netflix.com", title_js)) or "")
    except Exception:
        raw = ""

    page_title = raw.strip()

    # Normalize both for comparison
    def _norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    norm_expected = _norm(expected_title)
    norm_page = _norm(page_title)

    if not norm_expected or not norm_page:
        return False

    # Exact normalized match
    if norm_expected == norm_page:
        return True

    # Page title starts with expected
    if norm_page.startswith(norm_expected):
        return True

    # Expected starts with page title (unlikely but safe)
    if norm_expected.startswith(norm_page):
        return True

    # Strong token overlap: all expected tokens present in page title
    exp_tokens = set(norm_expected.split())
    page_tokens = set(norm_page.split())
    if exp_tokens and len(exp_tokens) >= 2 and exp_tokens.issubset(page_tokens):
        return True

    return False


# ── Play button detection ─────────────────────────────────────────────────────


def click_netflix_play_button_for_current_title(
    sess,
    page,
    expected_title: str | None = None,
    expected_title_id: str | None = None,
    expected_href: str | None = None,
) -> str | None:
    """Click Play/Lire/Resume button scoped to the current title's details/modal/hero area.

    Avoids clicking random global /watch/ links from other titles.
    Prefers visible primary action buttons inside modal/details/hero container.
    Uses labels: Play, Lire, Lecture, Resume, Reprendre, Continuer, Watch.
    After click, verifies playback and returns None on success or error message string.

    Scoping rules:
      - Must not click the first global /watch/ link
      - Must not click billboard Play unless context matches expected_title
      - Prefers Play/Lire/Reprendre buttons near the expected title or on the expected title page
      - If it cannot prove the button/link belongs to expected title, return an error
    """
    _focus_netflix_foreground(page, sess)
    if expected_title:
        print(
            f"[Netflix] Scoped play button: expected_title={expected_title} id={expected_title_id}"
        )

    # ── Brief wait for title page to load ───────────────────────────────────
    if expected_title_id:
        for _w in range(10):  # ~3s total
            try:
                cur_url = str(sess.run(page.evaluate("window.location.href")) or "")
                if (
                    f"/title/{expected_title_id}" in cur_url
                    or f"/watch/{expected_title_id}" in cur_url
                ):
                    has_content = sess.run(
                        page.evaluate(
                            "() => { return !!document.querySelector("
                            "'.detail-modal, .modal-container, .DetailContainer, "
                            '[data-uia="title-text"], [data-uia="action-play-title"], '
                            '[data-uia="play"]'
                            "); }"
                        )
                    )
                    if has_content:
                        break
            except Exception:
                pass
            time.sleep(0.3)

    # ── Pre-verification: confirm visible title matches expected ────────────
    if expected_title:
        try:
            visible_title = _read_netflix_visible_title(page, sess)
            if visible_title and _is_netflix_generic_heading(visible_title):
                print(f"[Netflix] Visible title is generic heading ({visible_title!r}) — ignoring")
                visible_title = None
            if visible_title:
                t_norm = _normalize_title_for_match(visible_title)
                e_norm = _normalize_title_for_match(expected_title)
                if t_norm and e_norm and e_norm not in t_norm and t_norm not in e_norm:
                    print(
                        f"[Netflix] Title mismatch: visible={visible_title!r} expected={expected_title!r}"
                    )
                    return (
                        f"Title mismatch on page: visible {visible_title!r} "
                        f"does not match expected {expected_title!r}"
                    )
        except Exception:
            pass

    # ── Scoped JS: container-scoped play button detection ──────────────────
    scoped_js = """
    (args) => {
        var expTitle = args.expectedTitle || '';
        function isRelatedOrBadSection(el) {
            var bad = [
                'more-like-this', 'moreliethis',
                'related', 'recommended',
                'trailer', 'preview',
                'billboard',
                'titres similaires', 'similar titles',
                'footer', 'privacy', 'preferences', 'préférences',
                'cookie', 'account', 'help', 'legal',
                'originals', 'popularonnetflix', 'trendingnow'
            ];
            var cur = el;
            while (cur && cur !== document.body) {
                var cls = String(cur.className || '').toLowerCase();
                var id = String(cur.id || '').toLowerCase();
                var uia = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase();
                var role = String(cur.getAttribute && cur.getAttribute('role') || '').toLowerCase();
                var combined = cls + ' ' + id + ' ' + uia + ' ' + role;
                for (var t = 0; t < bad.length; t++) {
                    if (combined.indexOf(bad[t]) >= 0) return true;
                }
                cur = cur.parentElement;
            }
            return false;
        }

        function isVisible(el) {
            var r = el.getBoundingClientRect();
            if (r.width < 10 || r.height < 10) return false;
            if (el.offsetParent === null) return false;
            var cur = el;
            while (cur && cur !== document.body) {
                var s = window.getComputedStyle(cur);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                cur = cur.parentElement;
            }
            return true;
        }

        function markPlayButton(el, foundLabel) {
            var marker = 'era-netflix-primary-play-' + Date.now();
            el.setAttribute('data-era-primary-play', marker);
            el.scrollIntoView({block: "center", inline: "center"});
            var label = foundLabel || (el.textContent || '').trim().slice(0, 40);
            var rect = el.getBoundingClientRect();
            return JSON.stringify({
                found: true, marked: true, marker: marker,
                foundLabel: label,
                text: label,
                tag: el.tagName,
                rect: {
                    x: Math.round(rect.left + rect.width / 2),
                    y: Math.round(rect.top + rect.height / 2),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height)
                }
            });
        }

        // Play button candidate selector — used to validate containers contain
        // at least one plausible Play/action button before we accept them.
        var PLAY_SELECTORS = [
            'button',
            'a',
            '[role="button"]',
            '[data-uia*="play"]',
            '[data-uia*="action-play"]'
        ].join(',');

        function hasPlayCandidate(root) {
            if (!root) return false;
            return !!root.querySelector(PLAY_SELECTORS);
        }

        // Step 1: try known Netflix detail containers, prefer one that contains
        // both the expected title text AND a Play button candidate.
        var containerSelectors = [
            '.detail-modal', '.modal-container', '.previewModal--wrapper',
            '.jawbone-main', '.DetailContainer', '.title-info-container',
            '[data-uia="title-info"]', '[data-uia="title"]'
        ];
        var area = null;
        var expLow = expTitle ? expTitle.toLowerCase() : '';
        var expId = args.expectedTitleId || '';
        var hasExpectedTitleInfo = !!(expTitle || expId);

        // 1a: first pass — accept only containers with a Play candidate
        for (var ci = 0; ci < containerSelectors.length; ci++) {
            var cands = document.querySelectorAll(containerSelectors[ci]);
            for (var cj = 0; cj < cands.length; cj++) {
                var cel = cands[cj];
                if (isRelatedOrBadSection(cel)) continue;
                if (!hasPlayCandidate(cel)) continue;
                if (expLow) {
                    var celTxt = (cel.innerText || cel.textContent || '').toLowerCase();
                    if (celTxt.indexOf(expLow) === -1) continue;
                }
                area = cel;
                break;
            }
            if (area) break;
        }

        // 1b: second pass — accept any safe container (will try ancestor climb next)
        if (!area) {
            for (var ci2 = 0; ci2 < containerSelectors.length; ci2++) {
                var el2 = document.querySelector(containerSelectors[ci2]);
                if (el2 && !isRelatedOrBadSection(el2)) {
                    area = el2;
                    break;
                }
            }
        }

        // Step 2: if no container from Step 1, find title element and climb
        // ancestors looking for the nearest safe ancestor that contains both
        // the expected title text and a Play button candidate.
        if (!area && expTitle) {
            var titleEls = document.querySelectorAll('h1, h2, [data-uia="title-text"]');
            for (var i = 0; i < titleEls.length; i++) {
                var txt = (titleEls[i].textContent || '').trim().toLowerCase();
                var exp = expTitle.toLowerCase();
                if (txt.indexOf(exp) >= 0 || exp.indexOf(txt) >= 0) {
                    // Climb ancestors: find one with title text AND Play candidate
                    var cur = titleEls[i];
                    while (cur && cur !== document.body) {
                        if (!isRelatedOrBadSection(cur) && hasPlayCandidate(cur)) {
                            var curTxt = (cur.innerText || cur.textContent || '').toLowerCase();
                            if (curTxt.indexOf(exp) >= 0) {
                                area = cur;
                                break;
                            }
                        }
                        cur = cur.parentElement;
                    }
                    if (area) break;
                    // Fallback: closest section/detail container (even without Play candidate)
                    var container = titleEls[i].closest(
                        'section, [class*="detail"], [class*="title"], ' +
                        '[class*="info"], .jawbone-main, .previewModal--wrapper, ' +
                        '[role="main"], main, .DetailContainer'
                    ) || titleEls[i].parentElement;
                    if (container && !isRelatedOrBadSection(container)) {
                        area = container;
                        break;
                    }
                }
            }
        }

        // Step 2a: if on /title/ page and still no container, try safe fallback
        if (!area && window.location.href.indexOf('/title/') >= 0) {
            var mainCandidates = document.querySelectorAll(
                'main, [role="main"], .mainView, .detail--container, ' +
                '[class*="detail"], [class*="Detail"]'
            );
            for (var mi = 0; mi < mainCandidates.length; mi++) {
                var mc = mainCandidates[mi];
                if (isRelatedOrBadSection(mc)) continue;
                var mcr = mc.getBoundingClientRect();
                if (mcr.width > 100 && mcr.height > 100) {
                    area = mc;
                    break;
                }
            }
        }

        // Step 2b: if container found but no Play candidate, climb ancestors
        // to find a wider safe container that includes a Play button.
        if (area && !hasPlayCandidate(area)) {
            var climb = area;
            while (climb && climb !== document.body) {
                if (!isRelatedOrBadSection(climb) && hasPlayCandidate(climb)) {
                    area = climb;
                    break;
                }
                climb = climb.parentElement;
            }
        }

        // Step 3: NO fallback to document.body — fail if no container found
        if (!area) {
            return JSON.stringify({ found: false, reason: 'no_title_container' });
        }

        // Step 4: prefer data-uia play buttons (Netflix's primary action)
        var uiaSelectors = [
            '[data-uia="action-play-title"]',
            '[data-uia="action-play-pretitle"]',
            '[data-uia="play"]',
            'button[data-uia*="play"]',
            'a[data-uia*="play"]'
        ];
        var uiaButtons = area.querySelectorAll(uiaSelectors.join(','));
        for (var ui = 0; ui < uiaButtons.length; ui++) {
            var btn = uiaButtons[ui];
            if (!isVisible(btn)) continue;
            if (isRelatedOrBadSection(btn)) continue;
            return markPlayButton(btn);
        }

        // Step 5: broader play button selectors, scoped to area
        var playSelectors = [
            'a[aria-label*="play" i]', 'a[aria-label*="lire" i]',
            'a[aria-label*="lecture" i]', 'a[aria-label*="resume" i]',
            'a[aria-label*="reprendre" i]', 'a[aria-label*="continuer" i]',
            'a[aria-label*="watch" i]', 'a[aria-label*="regarder" i]',
            'a[aria-label*="start" i]',
            'button[aria-label*="play" i]', 'button[aria-label*="lire" i]',
            'button[aria-label*="lecture" i]', 'button[aria-label*="resume" i]',
            'button[aria-label*="reprendre" i]', 'button[aria-label*="continuer" i]',
            'button[aria-label*="watch" i]', 'button[aria-label*="regarder" i]',
            'button[aria-label*="start" i]',
            '[role="button"][aria-label*="play" i]', '[role="button"][aria-label*="lire" i]',
            '[role="button"][aria-label*="lecture" i]', '[role="button"][aria-label*="resume" i]',
            '[role="button"][aria-label*="reprendre" i]', '[role="button"][aria-label*="continuer" i]',
            '[role="button"][aria-label*="watch" i]', '[role="button"][aria-label*="regarder" i]',
            '[role="button"][aria-label*="start" i]',
            '.play-button', '.playLink', 'a.playLink',
            '.btn-play'
        ];
        var buttons = area.querySelectorAll(playSelectors.join(','));
        for (var bi = 0; bi < buttons.length; bi++) {
            var pb = buttons[bi];
            if (!isVisible(pb)) continue;
            if (isRelatedOrBadSection(pb)) continue;
            var ph = pb.href || '';
            // For /watch/ links: skip if expected title is known (prevents playing related titles)
            if (ph && ph.indexOf('/watch/') >= 0) {
                if (hasExpectedTitleInfo) {
                    console.log('[Netflix] Skipping generic watch link because expected title is known');
                    continue;
                }
                return JSON.stringify({
                    found: true, method: 'href', href: ph, tag: pb.tagName,
                    source: 'aria-play-selector'
                });
            }
            return markPlayButton(pb);
        }

        // Step 6: visible-text / aria-label / title-attribute label detection
        // Covers English: play, watch, start, resume
        // Covers French:  lire, lecture, reprendre, continuer, regarder
        // Climbs to nearest clickable ancestor, still scoped to area.
        var labels = ['play', 'watch', 'start', 'resume', 'lire', 'lecture', 'reprendre', 'continuer', 'regarder'];
        var textCandidates = area.querySelectorAll('span, svg, div, p, a, button, [role="button"]');
        for (var ti = 0; ti < textCandidates.length; ti++) {
            var node = textCandidates[ti];
            var lt = '';
            // 1) Direct text child nodes
            var ownText = '';
            for (var ci = 0; ci < node.childNodes.length; ci++) {
                if (node.childNodes[ci].nodeType === 3) { ownText += node.childNodes[ci].textContent; }
            }
            lt = ownText.trim().toLowerCase();
            // 2) Nested span text (first meaningful span child)
            if (!lt || labels.indexOf(lt) === -1) {
                var spans = node.querySelectorAll('span');
                for (var si = 0; si < spans.length; si++) {
                    var spanText = (spans[si].textContent || '').trim().toLowerCase();
                    if (spanText && labels.indexOf(spanText) !== -1) { lt = spanText; break; }
                }
            }
            // 3) aria-label attribute
            if (!lt || labels.indexOf(lt) === -1) {
                var ariaVal = (node.getAttribute && node.getAttribute('aria-label') || '').trim().toLowerCase();
                if (ariaVal && labels.indexOf(ariaVal) !== -1) { lt = ariaVal; }
            }
            // 4) title attribute
            if (!lt || labels.indexOf(lt) === -1) {
                var titleVal = (node.getAttribute && node.getAttribute('title') || '').trim().toLowerCase();
                if (titleVal && labels.indexOf(titleVal) !== -1) { lt = titleVal; }
            }
            if (!lt || labels.indexOf(lt) === -1) continue;
            var clickable = node.closest('button, a, [role="button"], [tabindex]') || node;
            if (!isVisible(clickable)) continue;
            if (isRelatedOrBadSection(clickable)) continue;
            var lh = clickable.href || '';
            if (lh && lh.indexOf('/watch/') >= 0) {
                if (hasExpectedTitleInfo) {
                    console.log('[Netflix] Skipping generic watch link because expected title is known');
                    continue;
                }
                return JSON.stringify({
                    found: true, method: 'href', href: lh, tag: clickable.tagName,
                    source: 'text-label', foundLabel: lt.slice(0, 40)
                });
            }
            return markPlayButton(clickable, lt.slice(0, 40));
        }

        // Step 7: diagnostics — log container info when no button matched
        var diagTag = area ? (area.tagName || 'unknown') : 'none';
        var diagCls = area ? (area.className || '').toString().slice(0, 80) : '';
        var diagHasTitle = false;
        if (area && expLow) {
            diagHasTitle = ((area.innerText || area.textContent || '').toLowerCase().indexOf(expLow) >= 0);
        }
        var allBtns = area ? area.querySelectorAll('button, a, [role="button"]') : [];
        var diagCount = allBtns.length;
        var diagSamples = [];
        for (var di = 0; di < Math.min(allBtns.length, 5); di++) {
            var dc = allBtns[di];
            diagSamples.push(
                (dc.tagName || '') + '|t:' + (dc.textContent || '').trim().slice(0, 20)
                + '|a:' + (dc.getAttribute && dc.getAttribute('aria-label') || '').slice(0, 20)
                + '|u:' + (dc.getAttribute && dc.getAttribute('data-uia') || '').slice(0, 20)
            );
        }
        console.log('[Netflix] Scoped container diagnostics: tag=' + diagTag
            + ' cls=' + diagCls + ' hasTitle=' + diagHasTitle
            + ' btnCandidates=' + diagCount
            + ' samples=' + diagSamples.join(' ; '));

        return JSON.stringify({ found: false, reason: 'no_play_button_in_container' });
    }
    """
    try:
        if page is not None:
            raw = sess.run(
                page.evaluate(
                    scoped_js,
                    {
                        "expectedTitle": expected_title or "",
                        "expectedTitleId": expected_title_id or "",
                    },
                )
            )
        else:
            # evaluate_background_media doesn't accept params — embed title in JS
            bg_js = scoped_js.replace(
                "var expTitle = args.expectedTitle || '';",
                f"var expTitle = {json.dumps(expected_title or '')};",
            )
            bg_js = bg_js.replace(
                "var expId = args.expectedTitleId || '';",
                f"var expId = {json.dumps(expected_title_id or '')};",
            )
            raw = sess.run(sess.evaluate_background_media("netflix.com", bg_js))
    except Exception as exc:
        return f"Scoped play button detection JS failed: {exc}"

    import json as _json

    try:
        info = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return "Could not parse scoped play button result."

    if not info.get("found"):
        reason = info.get("reason", "unknown")
        if reason == "no_title_container":
            print(
                f"[Netflix] Scoped play: no title container found (expected_title={expected_title!r})"
            )
            return (
                f"I opened {expected_title or 'the title'} on Netflix, "
                "but could not identify the title detail area to find the Play button."
            )
        print(
            f"[Netflix] Scoped play: no play button found (reason={reason}, expected_title={expected_title!r})"
        )
        return (
            f"I opened {expected_title or 'the title'} on Netflix, "
            "but could not find a Play button inside the verified title area."
        )

    if info.get("foundLabel"):
        print(f"[Netflix] Scoped primary play button found by label: {info['foundLabel']}")

    try:
        if info.get("method") == "href":
            href = info["href"]
            # The JS already validated the link is not inside a bad section
            print(f"[Netflix] Following scoped Watch link: {href[:80]}")
            if page is not None:
                sess.run(page.goto(href, wait_until="domcontentloaded", timeout=30_000))
            else:
                nav = f"window.location.href = {_json.dumps(href)};"
                sess.run(sess.evaluate_background_media("netflix.com", nav))
            time.sleep(2.0)

            # Reacquire + refocus to avoid stale page/empty URL verification
            try:
                from actions.netflix_control import (
                    _acquire_netflix_page as _acq,
                )  # self-import safe

                sess2, page2 = _acq()
                if page2 is not None:
                    page = page2
                    _focus_netflix_foreground(page, sess2)
                    sess = sess2
            except Exception:
                # Keep original verification path if reacquire fails
                pass
        elif info.get("marked"):
            marker = info["marker"]
            rect = info.get("rect", {})
            x, y = rect.get("x"), rect.get("y")
            selector = f'[data-era-primary-play="{marker}"]'

            print("[Netflix] Scoped primary play marked for real click")
            clicked = False

            # Method 1: Playwright native locator.click (real browser-level events)
            try:
                if page is not None:
                    sess.run(page.locator(selector).click(timeout=3000, force=True))
                    print("[Netflix] Scoped primary play clicked via locator.click")
                    clicked = True
            except Exception:
                pass

            # Method 2: Viewport mouse click on exact center coordinates
            if not clicked and x is not None and y is not None:
                try:
                    if page is not None:
                        sess.run(page.mouse.click(x, y))
                        print("[Netflix] Scoped primary play clicked via viewport mouse")
                        clicked = True
                except Exception:
                    pass

            # Method 3: DOM click via JS evaluation on the exact marked element
            if not clicked:
                try:
                    dom_js = (
                        "() => {"
                        "  var el = document.querySelector(" + _json.dumps(selector) + ");"
                        "  if (el) { el.scrollIntoView({block:'center', inline:'center'}); el.click(); return 'ok'; }"
                        "  return 'no_element';"
                        "}"
                    )
                    if page is not None:
                        sess.run(page.evaluate(dom_js))
                    else:
                        sess.run(sess.evaluate_background_media("netflix.com", dom_js))
                    print("[Netflix] Scoped primary play clicked via dom fallback")
                    clicked = True
                except Exception:
                    pass

            if not clicked:
                return (
                    f"Could not click the marked primary play button "
                    f"for {expected_title or 'the title'}."
                )

            # Clean up marker attribute
            try:
                cleanup_js = (
                    "() => {"
                    "  var el = document.querySelector(" + _json.dumps(selector) + ");"
                    "  if (el) el.removeAttribute('data-era-primary-play');"
                    "}"
                )
                if page is not None:
                    sess.run(page.evaluate(cleanup_js))
                else:
                    sess.run(sess.evaluate_background_media("netflix.com", cleanup_js))
            except Exception:
                pass

            time.sleep(2.0)
        elif info.get("clicked"):
            method = info.get("method", "dom_click")
            print(f"[Netflix] Scoped primary play clicked via {method}")
            time.sleep(2.0)
        else:
            x, y = info["x"], info["y"]
            print(f"[Netflix] Clicking scoped Play button at ({x}, {y}): {info.get('text', '')}")
            click_js = f"""
            () => {{
                const el = document.elementFromPoint({x}, {y});
                if (el) {{ el.click(); return 'clicked'; }}
                return 'no element at point';
            }}
            """
            if page is not None:
                sess.run(page.evaluate(click_js))
            else:
                sess.run(sess.evaluate_background_media("netflix.com", click_js))
            time.sleep(2.0)
    except Exception as exc:
        return f"Scoped play button click failed: {exc}"

    if _verify_playback_started(sess, page):
        return None
    return "Scoped play button was clicked but playback could not be verified."


def click_netflix_play_button(sess, page) -> str | None:
    """Detect and click the Play/Lire/Resume button on a Netflix title details page.

    Uses DOM selectors (aria-label, data-uia, class names, href) to find the
    play button. Clicks via CDP. After click, verifies playback started.

    Returns None on success (playback verified) or an error message string.
    """
    _focus_netflix_foreground(page, sess)
    try:
        if page is not None:
            raw = sess.run(page.evaluate(_NETFLIX_PLAY_BUTTON_JS))
        else:
            raw = sess.run(sess.evaluate_background_media("netflix.com", _NETFLIX_PLAY_BUTTON_JS))
    except Exception as exc:
        return f"Play button detection JS failed: {exc}"

    import json as _json

    try:
        info = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return "Could not parse play button detection result."

    if not info.get("found"):
        return "No visible Netflix Play/Lire/Resume button found in DOM."

    # Click via CDP
    try:
        href = info.get("href", "")
        if href and "/watch/" in href:
            print(f"[Netflix] Found Watch link: {href[:80]}")
            if page is not None:
                sess.run(page.goto(href, wait_until="domcontentloaded", timeout=30_000))
            else:
                nav = f"window.location.href = {_json.dumps(href)};"
                sess.run(sess.evaluate_background_media("netflix.com", nav))
            time.sleep(2.0)

            # Reacquire + refocus to avoid stale page/empty URL verification
            try:
                from actions.netflix_control import (
                    _acquire_netflix_page as _acq,
                )  # self-import safe

                sess2, page2 = _acq()
                if page2 is not None:
                    page = page2
                    _focus_netflix_foreground(page, sess2)
                    sess = sess2
            except Exception:
                pass
        else:
            x, y = info["x"], info["y"]
            print(f"[Netflix] Clicking Play button at ({x}, {y}): {info.get('text', '')}")
            click_js = f"""
            () => {{
                const el = document.elementFromPoint({x}, {y});
                if (el) {{ el.click(); return 'clicked'; }}
                return 'no element at point';
            }}
            """
            if page is not None:
                sess.run(page.evaluate(click_js))
            else:
                sess.run(sess.evaluate_background_media("netflix.com", click_js))
            time.sleep(2.0)
    except Exception as exc:
        return f"Play button click failed: {exc}"

    # Verify playback
    if _verify_playback_started(sess, page):
        return None  # success
    return "Play button was clicked but playback could not be verified."


# ── Bounded Vision fallback ───────────────────────────────────────────────────


def _call_screen_process_narrow(sess, page, player=None) -> str | None:
    """Bounded Netflix-specific screen_process fallback — at most 1 call per command.

    Returns None if screen_process should not be called (cooldown/limit).
    Returns 'vision_fallback_unavailable_due_to_quota_or_timeout' on Vision failure.
    Returns the screen_process result string on success.
    """
    global _VISION_COOLDOWN_UNTIL, _VISION_ALREADY_ATTEMPTED

    if _VISION_ALREADY_ATTEMPTED:
        print("[Netflix] screen_process already attempted this command — skipping")
        return None

    if time.monotonic() < _VISION_COOLDOWN_UNTIL:
        remaining = _VISION_COOLDOWN_UNTIL - time.monotonic()
        print(f"[Netflix] screen_process cooldown active ({remaining:.0f}s remaining) — skipping")
        return "vision_fallback_unavailable_due_to_quota_or_timeout"

    _VISION_ALREADY_ATTEMPTED = True

    try:
        from actions.screen_processor import screen_process
    except ImportError:
        return None

    try:
        title_el = None
        try:
            if page is not None:
                title_el = sess.run(page.evaluate("document.title"))
        except Exception:
            pass
        question = "Is there a Play / Lire / Resume / Continuer button visible? Answer yes or no with the exact visible text."
        follow_up = "If yes, what are the approximate screen coordinates of the button?"

        # NOTE: screen_process()'s real signature in actions/screen_processor.py
        # does not necessarily match the kwarg names used historically here
        # (it previously raised: "unexpected keyword argument 'instruction'").
        # Inspect the live signature and only pass kwargs it actually accepts,
        # trying the most likely aliases for the "question" parameter so this
        # keeps working even if that module's signature changes again.
        import inspect

        candidate_kwargs = {
            "instruction": question,
            "prompt": question,
            "question": question,
            "follow_up": follow_up,
            "page_title": str(title_el or ""),
            "current_url": "",
            "player": player,
        }
        try:
            sig = inspect.signature(screen_process)
            accepted = set(sig.parameters)
            has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if has_var_kw:
                call_kwargs = candidate_kwargs
            else:
                call_kwargs = {k: v for k, v in candidate_kwargs.items() if k in accepted}
            if not any(k in call_kwargs for k in ("instruction", "prompt", "question")):
                print(
                    "[Netflix] screen_process signature has no recognised question kwarg; skipping vision fallback"
                )
                return None
        except (TypeError, ValueError):
            # Signature couldn't be inspected (e.g. it's a builtin/C function) —
            # fall back to the original best-guess call.
            call_kwargs = candidate_kwargs

        result = screen_process(**call_kwargs)
        result = str(result or "")
        print(f"[Netflix] screen_process result: {result[:120]}")
    except Exception as exc:
        print(f"[Netflix] screen_process call failed: {exc}")
        # Try to extract retry delay from exception
        exc_str = str(exc).lower()
        cooldown = 45.0
        import re

        delay_match = re.search(r"retry\s*delay[:\s]*(\d+)", exc_str)
        if delay_match:
            cooldown = float(delay_match.group(1)) + 5.0
        _VISION_COOLDOWN_UNTIL = time.monotonic() + cooldown
        return "vision_fallback_unavailable_due_to_quota_or_timeout"

    # Check for failure signals
    failure_signals = [
        "could not analyze the screen quickly enough",
        "could not analyze the screen",
        "429",
        "503",
        "resource_exhausted",
        "retry delay",
    ]
    result_lower = (result or "").lower()
    for signal in failure_signals:
        if signal in result_lower:
            print(f"[Netflix] screen_process failed: {signal}")
            _VISION_COOLDOWN_UNTIL = time.monotonic() + 45.0
            return "vision_fallback_unavailable_due_to_quota_or_timeout"

    return result


# ── Action normalisation map ───────────────────────────────────────────────────

_ACTION_ALIASES: dict[str, str] = {
    "resume": "play",
    "play_pause": "toggle_playback",
    "toggle": "toggle_playback",
    "skip": "skip_intro",
    "skip_the_intro": "skip_intro",
    "skip_opening": "skip_intro",
    "skip_the_recap": "skip_recap",
    "recap": "skip_recap",
    "skip_outro": "skip_credits",
    "skip_ending": "skip_credits",
    "fast_forward": "forward",
    "ff": "forward",
    "back_10s": "rewind",
    "go_back": "rewind",
    "unmute": "mute",
    "watch": "search",
    # Episode navigation
    "next": "next_episode",
    "next_ep": "next_episode",
    "skip_episode": "next_episode",
    "previous": "previous_episode",
    "prev_episode": "previous_episode",
    "previous_ep": "previous_episode",
    "last_episode": "previous_episode",
    # Seeking
    "jump_to": "seek",
    "go_to": "seek",
    "start_over": "restart",
    "from_the_beginning": "restart",
    "replay": "restart",
    # Player chrome
    "episode_list": "episodes",
    "show_episodes": "episodes",
    "captions": "subtitles",
    "subtitle": "subtitles",
    "audio": "subtitles",
    "audio_track": "subtitles",
    "exit_player": "back",
    "leave_player": "back",
    "stop": "back",
    # Volume — Netflix's OWN volume, never the system mixer.
    "louder": "volume_up",
    "quieter": "volume_down",
    "volume": "set_volume",
    # Recovery — a stalled player needs a reload, not another seek.
    "reload_player": "reload",
    "refresh": "reload",
    "reload_netflix": "reload",
    # Launch
    "open": "open",
    "launch": "open",
    "start": "open",
    "open_netflix": "open",
    # Profile picker
    "switch_profile": "select_profile",
    "choose_profile": "select_profile",
    "pick_profile": "select_profile",
}

# "back" used to alias to "rewind", so a user saying "go back" got a 10-second
# seek. It now means "leave the player" (the common reading), and "go_back"
# above preserves the rewind sense. Kept explicit so the change is visible.

_PLAY_ACTIONS = frozenset({"play", "grid_play", "toggle_playback"})

# JS scripts for each standard action
_JS_SCRIPTS: dict[str, str] = {
    "play": (
        "const v = document.querySelector('video'); "
        "if(v) return v.play().then(() => 'Netflix resumed').catch(e => 'Netflix resume blocked: ' + e.message); "
        "const billboard = document.querySelector('.billboard a.playLink, .billboard button.color-primary'); "
        "if(billboard) { billboard.click(); return 'Started the featured Netflix show.'; } "
        "return 'No active Netflix video element found';"
    ),
    "pause": (
        "const v = document.querySelector('video'); "
        "if(v) { v.pause(); return 'Netflix paused'; } "
        "return 'No active Netflix video element found';"
    ),
    "toggle_playback": (
        "const v = document.querySelector('video'); "
        "if(!v) return 'No active Netflix video element found'; "
        "if(v.paused) { return v.play().then(() => 'Netflix resumed').catch(e => 'Netflix resume blocked: ' + e.message); } "
        "v.pause(); return 'Netflix paused';"
    ),
    "skip_intro": (
        "const btn = document.querySelector("
        "  '.watch-video--skip-content-button, .skip-credits, "
        '   button[data-uia="player-skip-intro"], button[data-uia="player-skip-recap"]\''
        "); "
        "if(btn) { btn.click(); return 'Intro sequence bypassed successfully'; } "
        "return 'Skip button element not found on canvas frame';"
    ),
    # forward / rewind deliberately REMOVED from this table (2026-09-15).
    #
    # They assigned to `video.currentTime` directly. Netflix streams through
    # Media Source Extensions, and writing currentTime on an MSE element
    # desynchronises the buffer from the player's state machine — every rewind
    # or fast-forward killed the playback session and produced Netflix error
    # M7375, confirmed by the user. Seeking now goes through Netflix's own
    # player API in actions/_netflix/player_api.py, which is the only safe way
    # to move the position, and PLAYER_ACTIONS routes these before this table.
    # Do not re-add them here.
    "fullscreen": "document.querySelector('video')?.requestFullscreen?.().then(() => 'ok').catch(() => 'blocked') || 'no_video';",
    "mute": (
        "const v = document.querySelector('video'); "
        "if(v) { v.muted = !v.muted; return 'Audio mute status toggled'; } "
        "return 'No video context';"
    ),
}


# ── Single-playback enforcer ──────────────────────────────────────────────────


def _enforce_single_playback(params: dict) -> None:
    """Pause Spotify and YouTube before Netflix playback.

    Each import is guarded so a missing module never blocks Netflix playback.
    """
    if "spotify_running" in params:
        is_spotify_active = params["spotify_running"]
    else:
        try:
            import psutil

            is_spotify_active = any(
                p.info["name"] and p.info["name"].lower() == "spotify.exe"
                for p in psutil.process_iter(["name"])
            )
        except Exception:
            is_spotify_active = True

    if is_spotify_active:
        try:
            from actions.spotify_control import spotify_control

            result = spotify_control({"action": "pause_if_running_only"})
            print(f"[Netflix Enforcer] Spotify silenced -> {result}")
        except Exception as exc:
            print(f"[Netflix Enforcer] Spotify silence skipped: {exc}")
    else:
        print(
            "[Netflix Enforcer] Spotify is confirmed closed. Skipping pause call to prevent launch."
        )

    try:
        from actions.youtube_video import youtube_video

        result = youtube_video({"action": "pause"})
        print(f"[Netflix Enforcer] YouTube silenced -> {result}")
    except Exception as exc:
        print(f"[Netflix Enforcer] YouTube silence skipped: {exc}")


# ── Candidate grid helpers ────────────────────────────────────────────────────


def _normalize_netflix_candidate(card, index: int = 0) -> dict | None:
    """Normalize a Netflix card/candidate (dict or Card object) to a uniform dict.

    Returns dict with keys: title, url, visible_order, source_selector, raw
    Returns None if no valid title or URL can be extracted.
    """
    title = ""
    url = ""
    source_selector = ""
    if isinstance(card, dict):
        title = card.get("title") or card.get("name") or ""
        url = card.get("url") or card.get("href") or ""
        source_selector = str(card.get("source_selector", ""))
    else:
        title = getattr(card, "title", "") or ""
        url = getattr(card, "href", "") or getattr(card, "url", "") or ""
        source_selector = str(getattr(card, "source_selector", ""))
    cleaned = _clean_netflix_title(title)
    if not cleaned:
        return None
    # Search-result cards carry /search?...&jbv=<id> hrefs — normalize to a
    # direct /title/<id> URL so ordinal clicks navigate straight to the title.
    url = _normalize_netflix_title_href(url) or url
    return {
        "title": cleaned,
        "url": url,
        "visible_order": index + 1,
        "source_selector": source_selector or None,
        "raw": card,
    }


def _convert_cards_to_candidates(cards: list) -> list:
    """Convert Card objects (from scan_netflix_cards) to a uniform list for ordinal selection."""
    result = []
    for i, card in enumerate(cards):
        norm = _normalize_netflix_candidate(card, i)
        if norm and norm["title"]:
            result.append(norm)
    return result


def _filter_candidates_safely_for_search(
    candidates: list,
    query: str,
    page_url: str,
) -> list:
    """Prevent wrong /search?q=... candidate pollution.

    If we're on a Netflix search URL and have a non-empty query, keep
    only candidates whose cleaned titles have token overlap with the query.
    If query has multiple meaningful tokens, require at least one token match.
    """
    q = (query or "").strip()
    if not q:
        return candidates

    url = (page_url or "").lower()
    if "netflix.com/search" not in url:
        return candidates

    def _tokens(s: str) -> list[str]:
        s2 = (s or "").lower()
        # split on non-alphanumeric; keep short tokens too (like "it", "u")
        import re

        raw = re.split(r"[^a-z0-9]+", s2)
        return [t for t in raw if t and len(t) >= 1]

    q_tokens = _tokens(q)
    if not q_tokens:
        return candidates

    keep: list = []
    for c in candidates:
        t = (c.get("title") or "").strip().lower()
        t_tokens = set(_tokens(t))
        # token overlap heuristic
        if any(tok in t_tokens for tok in q_tokens):
            keep.append(c)

    return keep


def _fresh_scan_or_empty(sess, page, query: str) -> tuple[list, bool]:
    """Scan the current Netflix page for candidates without reusing saved grid."""
    try:
        from actions.browser_intelligence import scan_netflix_cards

        cards = scan_netflix_cards("brave", page=page)
    except Exception:
        cards = []

    if cards:
        candidates = _convert_cards_to_candidates(cards)
        _save_netflix_grid(cards, query)
        print(f"[Netflix] Scanned page: {len(candidates)} candidates")
        return candidates, False

    # Rescan returned 0 — check if saved grid exists (freshness already checked)
    try:
        from core.browser_grid_state import get_recent_grid

        snap = get_recent_grid("netflix")
        if snap and snap.candidates:
            now = time.time()
            grid_time = getattr(snap, "timestamp", 0) or 0
            if not isinstance(grid_time, (int, float)):
                grid_time = 0
            GRID_MAX_AGE = 300.0
            if grid_time > 0 and (now - grid_time) > GRID_MAX_AGE:
                return [], False
            c = []
            for i, gc in enumerate(snap.candidates):
                norm = _normalize_netflix_candidate(
                    {
                        "title": gc.title,
                        "url": gc.url or getattr(gc, "href", ""),
                        "visible_order": gc.visible_order,
                    },
                    i,
                )
                if norm and norm["title"] and norm["url"]:
                    c.append(norm)
            if c:
                print(f"[Netflix] Rescan returned 0, reusing saved grid: {len(c)} candidates")
                return c, True
    except Exception:
        pass

    return [], False


def _get_or_scan_candidates(sess, page, query: str) -> tuple[list, bool]:
    """Return (candidates_list, from_saved_grid).

    Safety rules:
      - If query is empty, do NOT reuse saved grid (prevents query='' pollution
        and prevents unrelated candidate reuse).
      - If query exists, reuse saved grid only when it has a non-empty query
        as well.
      - Reject stale grids older than 5 minutes (Fix 12).
      - Reject grids with mismatched query (Fix 12).
    """
    q = (query or "").strip()
    try:
        from core.browser_grid_state import get_recent_grid

        snap = get_recent_grid("netflix")
        if (
            q
            and snap
            and snap.candidates
            and isinstance(getattr(snap, "query", ""), str)
            and snap.query.strip()
        ):
            # Fix 12: Reject stale grids older than 5 minutes
            GRID_MAX_AGE = 300.0  # 5 minutes
            now = time.time()
            grid_time = getattr(snap, "timestamp", 0) or 0
            if not isinstance(grid_time, (int, float)):
                grid_time = 0
            if grid_time > 0 and (now - grid_time) > GRID_MAX_AGE:
                print(
                    f"[Netflix] Saved grid is stale ({now - grid_time:.0f}s old > {GRID_MAX_AGE:.0f}s) — discarding"
                )
                snap = None
                return _fresh_scan_or_empty(sess, page, q)

            # Verify saved grid query matches current page URL query
            # (prevents stale grid from a different search being reused)
            grid_matches = True
            try:
                page_url = str(page.url or "").lower() if page is not None else ""
                if "/search" in page_url and "q=" in page_url:
                    import re as _re_url

                    url_q = _re_url.search(r"[?&]q=([^&]+)", page_url)
                    if url_q:
                        from urllib.parse import unquote_plus

                        url_query = unquote_plus(url_q.group(1)).lower().strip()
                        grid_query = (snap.query or "").lower().strip()
                        if url_query and grid_query and url_query != grid_query:
                            grid_matches = False
                            print(
                                f"[Netflix] Saved grid query '{snap.query}' ≠ URL query '{url_q.group(1)}' — discarding"
                            )
            except Exception:
                pass
            if not grid_matches:
                snap = None
            else:
                # Fix 12: Compare normalized query with current query
                norm_saved = (snap.query or "").lower().strip()
                norm_current = q.lower().strip()
                if norm_saved and norm_current and norm_saved != norm_current:
                    print(
                        f"[Netflix] Saved grid query '{norm_saved}' ≠ current query '{norm_current}' — discarding"
                    )
                    snap = None
                else:
                    c = []
                    for i, gc in enumerate(snap.candidates):
                        norm = _normalize_netflix_candidate(
                            {
                                "title": gc.title,
                                "url": gc.url or getattr(gc, "href", ""),
                                "visible_order": gc.visible_order,
                            },
                            i,
                        )
                        if norm and norm["title"]:
                            c.append(norm)
                    if c:
                        print(
                            f"[Netflix] Using saved grid: {len(c)} candidates (query={snap.query})"
                        )
                        return c, True
    except Exception as e:
        print(f"[Netflix] get_recent_grid error: {e}")

    return _fresh_scan_or_empty(sess, page, q)


# ── Pause other media ─────────────────────────────────────────────────────────


def _pause_other_media(player=None) -> None:
    """Pause Spotify and YouTube before Netflix starts. Never resumes on failure."""
    try:
        from core.media_state import enforce_single_playback

        enforce_single_playback("netflix", player=player)
    except Exception:
        pass


# ── Set playback state ────────────────────────────────────────────────────────


def _set_playing(title: str = "") -> None:
    try:
        from core.media_state import set_status

        set_status("netflix", "playing")
    except Exception:
        pass
    try:
        from core.browser_grid_state import set_scroll_context

        set_scroll_context("netflix")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Simple mode: direct URL + DOM text matching (no smart card scanner)
# ═══════════════════════════════════════════════════════════════════════════════


def _close_netflix_overlays(page, sess) -> None:
    """Dismiss notification panels, profile menus, and account dropdowns."""
    # Escape exits fullscreen — don't blind-fire it over a fullscreen player.
    try:
        if page is not None and sess.run(page.evaluate("!!document.fullscreenElement")) is True:
            return
    except Exception:
        pass
    for _ in range(3):
        try:
            sess.run(page.keyboard.press("Escape"))
        except Exception:
            pass
        time.sleep(0.3)


def _close_netflix_modal_for_grid_selection(sess, page=None) -> None:
    """Close Netflix details modal only when user is explicitly selecting a grid card.

    Safe to ignore failure — the grid click may still work even if the modal
    doesn't close.
    """
    if page is None:
        return
    # Try clicking a visible close button first
    close_js = """
    (() => {
        var btn = document.querySelector('[aria-label*="Close"][tabindex]')
            || document.querySelector('[aria-label*="Fermer"][tabindex]')
            || document.querySelector('[data-uia*="close"]')
            || document.querySelector('button[class*="close"]');
        if (btn) {
            var r = btn.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) { btn.click(); return 'clicked_close'; }
        }
        // Check for modal overlay
        var modal = document.querySelector('[role="dialog"]')
            || document.querySelector('.previewModal--wrapper')
            || document.querySelector('.modal-overlay');
        if (modal) {
            var mr = modal.getBoundingClientRect();
            if (mr.width > 0 && mr.height > 0) return 'modal_found_no_close';
        }
        return 'no_modal';
    })()
    """
    try:
        result = str(sess.run(page.evaluate(close_js)) or "")
        if "clicked_close" in result:
            print("[Netflix] Closed modal before grid selection")
            time.sleep(0.5)
            return
    except Exception:
        pass
    # Fallback: press Escape once
    try:
        sess.run(page.keyboard.press("Escape"))
        time.sleep(0.5)
    except Exception:
        pass


def _open_netflix_native_search(page, sess, query: str) -> bool:
    """Open Netflix native search UI and type the query.

    Returns True if the search input was found and text was entered.
    This is a fallback — simple mode prefers direct URL navigation first.
    """
    # Try clicking the search launcher
    launcher_js = """
    (() => {
        var el = document.querySelector('[data-uia="search-box-launcher"]')
            || document.querySelector('button[aria-label*="Search"]')
            || document.querySelector('[aria-label*="Search"][role="button"]')
            || document.querySelector('input[aria-label*="Search"]');
        if (el) { el.click(); return 'clicked'; }
        return 'not_found';
    })()
    """
    try:
        result = str(sess.run(page.evaluate(launcher_js)))
        if "not_found" in result:
            return False
    except Exception:
        return False

    time.sleep(0.5)

    # Type query into the search input
    input_js = """
    (() => {
        var input = document.querySelector('input[data-uia*="search"]')
            || document.querySelector('input[aria-label*="Search"]')
            || document.querySelector('input[placeholder*="Search"]')
            || document.querySelector('input[type="search"]');
        if (input) {
            input.focus();
            input.value = '';
            return 'found';
        }
        return 'not_found';
    })()
    """
    try:
        result = str(sess.run(page.evaluate(input_js)))
        if "not_found" in result:
            return False
    except Exception:
        return False

    time.sleep(0.2)

    # Type the query character by character via keyboard
    try:
        for ch in query:
            sess.run(page.keyboard.type(ch, delay=30))
            time.sleep(0.02)
    except Exception:
        try:
            sess.run(page.keyboard.type(query, delay=30))
        except Exception:
            pass

    return True


def _fast_search_discover_candidates(
    query: str,
    page,
    sess,
) -> tuple[list[dict], bool]:
    """Fast-path: aggressive early-exit polling for Netflix search candidates.

    Polls up to 8 s for a visible non-chrome title link, then extracts and
    scores candidates via the existing _find_netflix_title_by_text.

    Args:
        query: The search query string.
        page: Playwright page object.
        sess: Browser session object.

    Returns:
        (candidates, success):
          candidates — scored list from _find_netflix_title_by_text (may be
                       empty if no cards rendered).
          success    — True when the fast path completed normally (caller
                       should continue to stable-native-search fallback on
                       zero candidates); False when the fast path is unsafe
                       (wrong URL, exception, page is None) and the caller
                       should run the full stable path.
    """
    _close_netflix_overlays(page, sess)
    search_url = f"https://www.netflix.com/search?q={quote_plus(query)}"
    try:
        sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=30_000))
    except Exception:
        pass

    for _ in range(8):
        try:
            raw = str(sess.run(page.evaluate(_FAST_SEARCH_READY_JS)) or "")
            import json as _json

            info = _json.loads(raw) if isinstance(raw, str) else {}
            if not info.get("urlOk"):
                return [], False
            if info.get("ready"):
                candidates = _find_netflix_title_by_text(page, sess, query)
                return candidates, True
        except Exception:
            return [], False
        time.sleep(1.0)

    return [], True


def _find_netflix_title_by_text(page, sess, query: str) -> list[dict]:
    """Scan visible DOM elements and score them against the query.

    Returns a list of {text, href, score, reason, rect} dicts, sorted by
    score descending then position (top-left first).
    """
    try:
        raw = sess.run(page.evaluate(_SIMPLE_MODE_CANDIDATE_JS))
        raw_str = str(raw) if not isinstance(raw, str) else raw
        candidates = json.loads(raw_str) if raw_str.strip().startswith("[") else []
    except Exception as e:
        print(f"[Netflix] Simple mode candidate scan failed: {e}")
        return []

    if not candidates:
        return []

    try:
        match_js = _SIMPLE_MODE_TITLE_MATCH_JS
        raw_scored = sess.run(page.evaluate(match_js, {"candidates": candidates, "query": query}))
        raw_scored_str = str(raw_scored) if not isinstance(raw_scored, str) else raw_scored
        return json.loads(raw_scored_str) if raw_scored_str.strip().startswith("[") else []
    except Exception as e:
        print(f"[Netflix] Simple mode title matching failed: {e}")
        return []


def _click_netflix_title_candidate(page, sess, candidate: dict) -> bool:
    """Navigate to the matched candidate's URL. Returns True on success."""
    href = candidate.get("href") or candidate.get("url") or ""
    if not _is_valid_netflix_href(href):
        print(f"[Netflix] Refusing candidate with invalid href: {href!r}")
        return False
    try:
        sess.run(page.goto(href, wait_until="domcontentloaded", timeout=30_000))
        return True
    except Exception as exc:
        print(f"[Netflix] Failed to open candidate href={href!r}: {exc}")
        try:
            nav_script = f"window.location.href = {json.dumps(href)};"
            sess.run(sess.evaluate_background_media("netflix.com", nav_script))
            return True
        except Exception:
            return False


def _play_title_id_directly(sess, page, title_id: str) -> bool:
    """Start playback by navigating straight to /watch/<title_id>.

    This skips the details modal and its Play/Reprendre button entirely —
    Netflix's player auto-starts on /watch/ URLs. Returns True once the
    player is up with a ready video element; False if Netflix bounced the
    navigation (non-playable id, profile gate) so the caller can fall back
    to the title-page + play-button flow.
    """
    if not title_id or page is None:
        return False
    watch_url = f"https://www.netflix.com/watch/{title_id}"
    print(f"[Netflix] direct watch: {watch_url}")
    try:
        sess.run(page.goto(watch_url, wait_until="domcontentloaded", timeout=30_000))
    except Exception as exc:
        print(f"[Netflix] direct watch navigation failed: {exc}")
        return False

    probe_js = (
        "(() => {"
        "  var v = document.querySelector('video');"
        "  return JSON.stringify({"
        "    url: location.href,"
        "    video: !!v,"
        "    ready: v ? v.readyState : 0,"
        "    paused: v ? v.paused : true"
        "  });"
        "})()"
    )
    bad_probe_streak = 0
    for _ in range(12):
        try:
            info = json.loads(str(sess.run(page.evaluate(probe_js)) or "{}"))
        except Exception:
            info = {}
        if not isinstance(info, dict):
            info = {}
        url = str(info.get("url") or "")
        if not url:
            # A live page always reports location.href — repeated empty
            # probes mean the page/session is unusable; fall back fast.
            bad_probe_streak += 1
            if bad_probe_streak >= 3:
                print("[Netflix] direct watch: page state unreadable — falling back")
                return False
            time.sleep(1.0)
            continue
        bad_probe_streak = 0
        if "/watch/" not in url:
            print(f"[Netflix] direct watch bounced to {url[:80]!r}")
            return False
        if info.get("video") and int(info.get("ready") or 0) >= 2:
            if info.get("paused"):
                try:
                    sess.run(
                        page.evaluate(
                            "(() => { var v = document.querySelector('video');"
                            " if (v) { v.play(); } })()"
                        )
                    )
                except Exception:
                    pass
            print("[Netflix] direct watch: player active")
            return True
        time.sleep(1.0)
    print("[Netflix] direct watch: player did not become ready")
    return False


def _simple_search_and_play(
    query: str,
    page,
    sess,
    player=None,
    auto_play: bool = True,
) -> str:
    """Simple Netflix search-and-play using direct URL + DOM text matching.

    Flow:
       1. Navigate to /search?q=...
       2. Wait for visible title links
       3. Score candidates by text match against query
       4. Filter by valid Netflix href (Fix 1)
       5. If auto_play and score >= AUTOPLAY_MIN_SCORE: lock candidate,
          pause other media, open title, verify identity, click play, verify
       6. Otherwise: return numbered list of safe candidates
       7. If zero candidates from direct URL, try native search UI as fallback
    """
    if not query:
        return "Netflix search requires a title to search for, sir."

    # "Play X on Netflix" from cold lands on the profile picker, and every
    # search below it then scans a page of profile avatars instead of titles.
    # The dispatcher's probe ran before this navigation, so the gate is cleared
    # here, where the search actually starts.
    if page is not None:
        _clear_profile_gate(sess, page, wait=False)

    candidates = None

    # ── FAST PATH: aggressive early-exit polling ─────────────────────────
    if NETFLIX_FAST_SEARCH_ENABLED and page is not None:
        print(f"[Netflix] Fast search path: attempting fast discovery for '{query}'")
        try:
            fast_candidates, fast_ok = _fast_search_discover_candidates(query, page, sess)
            if fast_ok and fast_candidates:
                print(
                    f"[Netflix] Fast search path: found {len(fast_candidates)} candidates "
                    f"('{fast_candidates[0].get('text', '?')}' top)"
                )
                candidates = fast_candidates
            elif fast_ok:
                print("[Netflix] Fast search path: completed but zero candidates")
            else:
                print("[Netflix] Fast search failed, falling back to stable path")
        except Exception as exc:
            print(f"[Netflix] Fast search failed ({exc}), falling back to stable path")

    # ── STABLE PATH: preserved exactly as-is ─────────────────────────────
    if candidates is None:
        print("[Netflix] Stable path preserved")
        search_url = f"https://www.netflix.com/search?q={quote_plus(query)}"

        # Step 1: Close overlays
        _close_netflix_overlays(page, sess)

        # Step 2: Navigate to direct search URL
        try:
            sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=30_000))
        except Exception:
            pass

        # Step 3: Wait for URL stabilization
        for _ in range(6):
            try:
                u = str(sess.run(page.evaluate("window.location.href")) or "").lower()
                if "netflix.com/search" in u and "?q=" in u:
                    if quote_plus(query).lower() in u or query.lower().replace(" ", "+") in u:
                        break
            except Exception:
                pass
            time.sleep(0.5)

        # Step 3b: Wait for at least one title card to appear via Playwright selector
        try:
            sess.run(
                page.wait_for_selector(
                    'a[href*="/title/"], a[href*="/watch/"], a[href*="jbv="]',
                    state="attached",
                    timeout=12_000,
                )
            )
            time.sleep(0.5)
        except Exception:
            pass

        # Step 4: Wait for visible title links to appear (reject notification/nav chrome)
        for _ in range(8):
            try:
                has_links = sess.run(
                    page.evaluate(
                        "() => {"
                        "  var links = document.querySelectorAll("
                        '    \'a[href*="/title/"], a[href*="/watch/"], a[href*="jbv="]\''
                        "  );"
                        "  for (var i = 0; i < links.length; i++) {"
                        "    var a = links[i];"
                        "    var r = a.getBoundingClientRect();"
                        "    if (r.width <= 50 || r.height <= 50) { continue; }"
                        "    var cur = a;"
                        "    var forbidden = false;"
                        "    while (cur && cur !== document.body) {"
                        "      var d = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase();"
                        "      var c = String(cur.className || '').toLowerCase();"
                        "      var t = String(cur.tagName || '').toLowerCase();"
                        "      if (t === 'header' || t === 'nav' ||"
                        "          d.indexOf('navigation') >= 0 ||"
                        "          d.indexOf('notifications') >= 0 ||"
                        "          d.indexOf('account') >= 0 ||"
                        "          d.indexOf('profile') >= 0 ||"
                        "          c.indexOf('notification') >= 0 ||"
                        "          c.indexOf('navigation') >= 0 ||"
                        "          c.indexOf('pinning-header') >= 0 ||"
                        "          c.indexOf('account') >= 0) {"
                        "        forbidden = true; break;"
                        "      }"
                        "      cur = cur.parentElement;"
                        "    }"
                        "    if (forbidden) { continue; }"
                        "    var txt = (a.textContent || '').trim();"
                        "    var img = a.querySelector('img[alt]');"
                        "    if (txt.length > 1 || (img && img.getAttribute('alt'))) {"
                        "      return true;"
                        "    }"
                        "  }"
                        "  return false;"
                        "}"
                    )
                )
                if has_links:
                    break
            except Exception:
                pass
            time.sleep(1.5)

        time.sleep(1.0)

        # Step 5: Find and score candidates
        candidates = _find_netflix_title_by_text(page, sess, query)

        # Fallback: if direct URL yielded zero candidates, try native search UI
        if not candidates:
            print("[Netflix] Simple mode: zero candidates from direct URL, trying native search UI")
            _close_netflix_overlays(page, sess)
            try:
                sess.run(
                    page.goto(
                        "https://www.netflix.com/browse",
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                )
            except Exception:
                pass
            time.sleep(1.0)
            if _open_netflix_native_search(page, sess, query):
                time.sleep(2.0)
                candidates = _find_netflix_title_by_text(page, sess, query)

        if not candidates:
            print(f"[Netflix] Simple mode: no candidates found for query='{query}'")
            return (
                f"I searched Netflix for {query}, but could not identify "
                "any matching results. Please try a different search."
            )

    # Step 6: Normalize candidate hrefs (e.g. /search?...&jbv=<id> → /title/<id>)
    for c in candidates:
        raw = c.get("href") or c.get("url") or ""
        normalized = _normalize_netflix_title_href(raw)
        if normalized and normalized != raw:
            print(f"[Netflix] candidate href normalized {raw[:80]!r} -> {normalized[:80]!r}")
            c["href"] = normalized

    # Step 6b: Filter candidates by valid Netflix href (Fix 1 — href guard)
    playable_candidates = [
        c for c in candidates if _is_valid_netflix_href(c.get("href") or c.get("url") or "")
    ]
    if not playable_candidates and any(c.get("score", 0) >= AUTOPLAY_MIN_SCORE for c in candidates):
        print("[Netflix] Simple mode: strong candidate(s) found but none have a valid Netflix href")
        # ── Diagnostic logging for debugging href extraction ──
        strong_debug = [c for c in candidates if float(c.get("score") or 0) >= AUTOPLAY_MIN_SCORE][
            :5
        ]
        for i, c in enumerate(strong_debug, start=1):
            href = str(c.get("href") or "")
            rejected = "no_href" if not href else "invalid_href"
            print(
                "[Netflix] Candidate debug "
                f"#{i}: "
                f"text={str(c.get('text') or c.get('title') or '')[:80]!r} "
                f"score={float(c.get('score') or 0):.2f} "
                f"reason={str(c.get('reason') or '')[:80]!r} "
                f"href={href[:160]!r} "
                f"tag={str(c.get('tag') or '')[:40]!r} "
                f"role={str(c.get('role') or '')[:40]!r} "
                f"aria={str(c.get('aria') or c.get('ariaLabel') or c.get('aria-label') or '')[:80]!r} "
                f"data_uia={str(c.get('data_uia') or c.get('dataUia') or c.get('data-uia') or '')[:80]!r} "
                f"class={str(c.get('class') or c.get('className') or '')[:80]!r} "
                f"rejected={rejected}"
            )
        # V9C.2: Block autoplay geometry click when query is dirty
        if auto_play and _looks_like_dirty_media_query(query):
            return (
                f"I searched Netflix for {query}, but the query looks unclear. "
                f"Please say the exact title, for example: Play War Machine."
            )
        # V9C.2.1: Block autoplay geometry click when title cannot be verified
        if auto_play:
            strong = next((c for c in candidates if c.get("score", 0) >= AUTOPLAY_MIN_SCORE), None)
            if strong and not _is_verified_title_match(strong, query):
                return (
                    f"I found Netflix results for {query}, but I could not verify the exact title safely. "
                    f"Please say 'select the first title' or choose a number from the results."
                )
        if auto_play:
            strong = next((c for c in candidates if c.get("score", 0) >= AUTOPLAY_MIN_SCORE), None)
            # Safety gate: reject candidates whose text matches privacy/footer/bad tokens
            if strong:
                strong_text = strong.get("text", "")
                if _is_bad_netflix_element(strong_text):
                    print(
                        f"[Netflix] Auto-play blocked: candidate text '{strong_text}' contains bad tokens"
                    )
                    return (
                        f"I found {query} on Netflix, but the target appears to be a "
                        f"non-title element. Please try a different search."
                    )
            # Try to find any candidate with a valid href (re-check with improved getHref)
            best_href_candidate = None
            for c in candidates:
                href_val = c.get("href") or c.get("url") or ""
                if _is_valid_netflix_href(href_val) and c.get("score", 0) >= AUTOPLAY_MIN_SCORE:
                    best_href_candidate = c
                    break
            if best_href_candidate:
                print(
                    f"[Netflix] Auto-play: found valid href candidate '{best_href_candidate.get('text', '')}'"
                )
                _bhc_id = _extract_netflix_title_id(
                    best_href_candidate.get("href") or best_href_candidate.get("url") or ""
                )
                if _bhc_id and _play_title_id_directly(sess, page, _bhc_id):
                    _set_playing(query)
                    return f"Playing: {query}"
                if _click_netflix_title_candidate(page, sess, best_href_candidate):
                    time.sleep(2.0)
                    play_err = click_netflix_play_button_for_current_title(
                        sess, page, expected_title=query
                    )
                    if play_err is None:
                        _set_playing(query)
                        return f"Playing: {query}"
                    # Do NOT use generic non-scoped fallback when expected_title is known.
                    # It may click the wrong title's Play button.
                return (
                    f"I found {query} on Netflix, but could not open the title page automatically."
                )
            # No href anywhere — the strong candidate's title was already
            # verified above (V9C.2.1), so try a live DOM geometry click on
            # its visible position before giving up (V9C.1). Only applies
            # when the candidate has no href at all; a candidate with an
            # invalid/forbidden href (e.g. /account) must never be clicked.
            if strong and not (strong.get("href") or strong.get("url")):
                visible_order = strong.get("visible_order") or 1
                if _click_visible_netflix_card_by_ordinal(visible_order, page, sess):
                    time.sleep(2.0)
                    play_err = click_netflix_play_button_for_current_title(
                        sess, page, expected_title=query
                    )
                    if play_err is None:
                        _set_playing(query)
                        return f"Playing: {query}"
                return (
                    f"I found {query} on Netflix, but could not open the title page automatically."
                )
            # No valid href found — report clean failure instead of broad-clicking
            return (
                f"I found {query} on Netflix, but I could not verify a safe title card to open, "
                "so I stopped for safety."
            )
        return (
            f"I found {candidates[0]['text']} on Netflix, but the result has no valid "
            "title URL, so I stopped for safety."
        )

    # Step 7: Filter to safe candidates only (score >= 0.3 for listing,
    # score >= AUTOPLAY_MIN_SCORE for autoplay)
    safe_for_list = [c for c in candidates if c.get("score", 0) >= 0.3]
    # For autoplay, only consider candidates with valid hrefs
    safe_for_play = [c for c in playable_candidates if c.get("score", 0) >= AUTOPLAY_MIN_SCORE]

    if not safe_for_list and candidates:
        print(
            f"[Netflix] Simple mode: all {len(candidates)} candidates below safety threshold for listing"
        )

    # Step 8: List-only mode
    if not auto_play:
        if not safe_for_list:
            return (
                f"I searched Netflix for {query}, but could not identify "
                "any safe matching results. Please try a different search."
            )
        lines = [f"{i + 1}. {c['text']}" for i, c in enumerate(safe_for_list[:5])]
        msg = "I found these Netflix titles. Please tell me which one you want to watch."
        if lines:
            msg += "\n" + "\n".join(lines)
        return msg

    # Step 9: Auto-play mode — require strong match with valid href
    # V9C.2: Block autoplay when query is dirty (unreliable match)
    if auto_play and _looks_like_dirty_media_query(query):
        if safe_for_list:
            lines = [f"{i + 1}. {c['text']}" for i, c in enumerate(safe_for_list[:5])]
            msg = (
                f"I searched Netflix for {query}, but the query looks unclear. "
                f"Please say the exact title, for example: Play War Machine."
            )
            msg += "\n" + "\n".join(lines)
        else:
            msg = (
                f"I searched Netflix for {query}, but the query looks unclear. "
                "Please say the exact title, for example: Play War Machine."
            )
        return msg
    best = safe_for_play[0] if safe_for_play else None
    if not best:
        print(f"[Netflix] Simple mode: no strong match for autoplay (query='{query}')")
        if safe_for_list:
            lines = [f"{i + 1}. {c['text']}" for i, c in enumerate(safe_for_list[:5])]
            msg = (
                f"I searched Netflix for {query}, but I'm not confident enough "
                "to auto-play. Please tell me which one you want to watch."
            )
            msg += "\n" + "\n".join(lines)
        else:
            msg = (
                f"I searched Netflix for {query}, but could not identify "
                "a safe matching result to play. Please try a different search."
            )
        return msg

    selected_title = best["text"]
    href = best.get("href") or best.get("url") or ""
    extracted_id = _extract_netflix_title_id(href)
    score = best.get("score", 0)

    # Fix 2: Create candidate lock
    locked = {
        "expected_title": selected_title,
        "expected_href": href,
        "expected_title_id": extracted_id,
        "expected_query": query,
        "expected_score": score,
    }
    print(
        f"[Netflix] Simple mode: locked candidate '{locked['expected_title']}' "
        f"(href={locked['expected_href']}, id={locked['expected_title_id']})"
    )

    # Fix 9: Pause other media BEFORE Netflix playback
    _pause_other_media(player=player)

    # Step 10a: Direct watch — /watch/<id> starts the player immediately,
    # skipping the details modal and its Play/Reprendre button entirely.
    if locked["expected_title_id"] and _play_title_id_directly(
        sess, page, locked["expected_title_id"]
    ):
        _set_playing(selected_title)
        return f"Playing: {selected_title}"

    # Step 10b: Fall back to the title page + play-button flow (Fix 3)
    if not _click_netflix_title_candidate(page, sess, best):
        # Fix 4: Do NOT click generic Play after candidate nav fails
        print("[Netflix] Simple mode: failed to navigate to candidate — stopping")
        return (
            f"I found {selected_title}, but Netflix opened a different or unverifiable "
            "title, so I stopped for safety."
        )

    time.sleep(2.0)
    # NEW: Validate we landed on the correct title page
    signals = _check_local_signals(sess, page)
    page_title = signals.get("page_title", "").lower()
    if selected_title.lower() not in page_title:
        # Check current URL contains /title/ or /watch/
        current_url = signals.get("current_url", "")
        if "/title/" in current_url or "/watch/" in current_url:
            print(
                f"[Netflix] WARNING: Expected '{selected_title}' but page title is '{page_title}'"
            )
            # Optionally: return error instead of playing wrong title
            # return f"Navigation mismatch: expected '{selected_title}' but found '{page_title}'"    # Fix 6: Verify title/page identity after navigation
    if not _verify_title_context_matches(
        page,
        sess,
        expected_title=locked["expected_title"],
        expected_title_id=locked["expected_title_id"],
    ):
        print(
            f"[Netflix] Simple mode: title context mismatch after navigation "
            f"(expected='{locked['expected_title']}', id={locked['expected_title_id']})"
        )
        return (
            f"I found {selected_title}, but Netflix opened a different or unverifiable "
            "title, so I stopped for safety."
        )

    # Fix 7+8: Verify playback identity — if active and identity safe, commit success immediately
    if _verify_playback_started(sess, page):
        fix7_identity_ok = False
        if locked["expected_title_id"]:
            try:
                cur_url = str(sess.run(page.evaluate("window.location.href")) or "")
                fix7_identity_ok = locked["expected_title_id"] in cur_url
            except Exception:
                fix7_identity_ok = False
        if not fix7_identity_ok:
            # Even without ID, if we verified context above, trust it
            fix7_identity_ok = True
        if fix7_identity_ok:
            _set_playing(selected_title)
            print(f"[Netflix] Simple mode: playback verified active for '{selected_title}'")
            return f"Playing: {selected_title}"

    # Step 11: First verification failed — try clicking Play/Lire/Resume/
    # Reprendre exactly once, scoped to locked candidate (Fix 5).
    play_err = click_netflix_play_button_for_current_title(
        sess,
        page,
        expected_title=locked["expected_title"],
        expected_title_id=locked["expected_title_id"],
        expected_href=locked["expected_href"],
    )
    if play_err is not None:
        return (
            f"I opened the Netflix title page for {selected_title}, "
            "but I could not start playback automatically."
        )

    # Fix 8: Success verified — commit, do NOT verify again
    _set_playing(selected_title)
    return f"Playing: {selected_title}"


# ═══════════════════════════════════════════════════════════════════════════════
# State-based action handlers
# ═══════════════════════════════════════════════════════════════════════════════


def _handle_search(state: str, params: dict, sess, page, player=None) -> str:
    """Execute Netflix search and auto-play if requested.

    - Always navigates to /search?q=...
    - Waits for cards, saves grid
    - If auto_play: strong match -> play, ambiguous -> list
    - If not auto_play: return numbered list
    """
    query_param = _sanitize_media_query(_media_query_from_params(params))
    auto_play = params.get("_auto_play", False)

    # Preserve query for grid context safety (e.g., /search?q=One+Piece)
    query = _sanitize_media_query(
        _infer_netflix_query_from_page(page, sess, {"query": query_param, **(params or {})})
        or query_param
    )

    if not query:
        return "Netflix search requires a title to search for, sir."

    # ── Simple mode: early dispatch to direct URL + DOM text matching ──────
    if NETFLIX_SIMPLE_MODE:
        print(f"[Netflix] Simple mode: searching for '{query}' (auto_play={auto_play})")
        return _simple_search_and_play(query, page, sess, player=player, auto_play=auto_play)

    # ── Legacy scanner path (below) ───────────────────────────────────────
    try:
        from core.browser_grid_state import set_last_user_query

        set_last_user_query(query, "netflix")
    except Exception:
        pass

    try:
        from actions.browser_intelligence import is_netflix_profile_page, scan_netflix_cards
        from utils.media_match import select_best_media_candidate
    except ImportError as e:
        print(f"[Netflix] media_match or browser_intelligence not available: {e}")
        _set_netflix_vision_cooldown()
        _set_netflix_failure("netflix_control.search", f"import error: {e}")
        return "Netflix search had an internal error before selecting a safe title."

    search_url = f"https://www.netflix.com/search?q={quote_plus(query)}"

    # Close any notification/account overlay before navigating
    if page is not None:
        try:
            sess.run(page.keyboard.press("Escape"))
            time.sleep(0.3)
        except Exception:
            pass

    # Navigate owned page to search URL
    if page is not None:
        try:
            sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=30_000))
        except Exception:
            pass

    # Phase 1: URL readiness check (3s total, 6 × 0.5s).
    # Wait for the search URL to stabilise with the correct query.
    if page is not None:
        for _p1 in range(6):
            try:
                u = str(sess.run(page.evaluate("window.location.href")) or "").lower()
                if "netflix.com/search" in u and "?q=" in u:
                    if quote_plus(query).lower() in u or query.lower().replace(" ", "+") in u:
                        break
            except Exception:
                pass
            time.sleep(0.5)

    # Phase 2: scan for visible title/watch cards (12s total, 8 × 1.5s).
    # Uses filtered card-check JS to verify at least one real (non-chrome) card.
    _phase2_ok = False
    if page is not None:
        for _p2 in range(8):
            try:
                _has_cards = sess.run(
                    page.evaluate(
                        "() => {"
                        "  var cards = document.querySelectorAll("
                        '    \'a[href*="/title/"], a[href*="/watch/"], a[href*="jbv="]\''
                        "  );"
                        "  for (var i = 0; i < cards.length; i++) {"
                        "    var a = cards[i];"
                        "    var r = a.getBoundingClientRect();"
                        "    if (r.width <= 50 || r.height <= 50) { continue; }"
                        "    var cur = a;"
                        "    var forbidden = false;"
                        "    while (cur && cur !== document.body) {"
                        "      var d = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase();"
                        "      var c = String(cur.className || '').toLowerCase();"
                        "      var t = String(cur.tagName || '').toLowerCase();"
                        "      if (t === 'header' || t === 'nav' ||"
                        "          d.indexOf('navigation') >= 0 ||"
                        "          d.indexOf('notifications') >= 0 ||"
                        "          d.indexOf('account') >= 0 ||"
                        "          d.indexOf('profile') >= 0 ||"
                        "          c.indexOf('notification') >= 0 ||"
                        "          c.indexOf('navigation') >= 0 ||"
                        "          c.indexOf('pinning-header') >= 0 ||"
                        "          c.indexOf('account') >= 0) {"
                        "        forbidden = true; break;"
                        "      }"
                        "      cur = cur.parentElement;"
                        "    }"
                        "    if (forbidden) { continue; }"
                        "    var txt = (a.textContent || '').trim();"
                        "    var img = a.querySelector('img[alt]');"
                        "    if (txt.length > 1 || (img && img.getAttribute('alt'))) {"
                        "      return true;"
                        "    }"
                        "  }"
                        "  return false;"
                        "}"
                    )
                )
                if _has_cards:
                    _phase2_ok = True
                    break
            except Exception:
                pass
            time.sleep(1.5)
    else:
        _phase2_ok = True  # no page object — skip card wait

    # Final settle + rescan for lazy-rendered cards.
    time.sleep(2.0)

    # Verify URL is a Netflix search page
    if page is not None:
        search_url_ok = "netflix.com/search" in str(page.url).lower()
        if not search_url_ok:
            try:
                sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=15_000))
                time.sleep(1.0)
            except Exception:
                pass
            search_url_ok = "netflix.com/search" in str(page.url).lower()
            if not search_url_ok:
                _set_netflix_vision_cooldown()
                _set_netflix_failure("netflix_control.search", "could not navigate to search page")
                return (
                    "I opened Netflix but could not navigate to the search page. Please try again."
                )

    # Check for profile page
    if is_netflix_profile_page("brave"):
        try:
            from actions.browser_intelligence import _click_netflix_profile

            _click_netflix_profile("brave")
            time.sleep(1.5)
        except Exception:
            pass

    _focus_netflix_foreground(page, sess)

    # Close any notification/account overlay before scanning
    if page is not None:
        try:
            sess.run(page.keyboard.press("Escape"))
            time.sleep(0.3)
        except Exception:
            pass

    # Scan cards
    try:
        if page is not None:
            cards = scan_netflix_cards("brave", page=page)
        else:
            cards = scan_netflix_cards("brave")
    except Exception as exc:
        print(f"[Netflix] Search scan failed: {exc}")
        _set_netflix_failure("netflix_control.search", f"scan failed: {exc}")
        return "Netflix search had an internal error before selecting a safe title."

    retry_count = 0
    while not cards and retry_count < 8:
        try:
            current = (
                str(page.url).lower() if page is not None else str(sess.run(sess.get_url())).lower()
            )
            if "netflix.com/search" not in current and page is not None:
                sess.run(page.goto(search_url, wait_until="domcontentloaded", timeout=15_000))
        except Exception:
            pass
        time.sleep(1.0)
        try:
            if page is not None:
                cards = scan_netflix_cards("brave", page=page)
            else:
                cards = scan_netflix_cards("brave")
        except Exception:
            pass
        retry_count += 1

    if not cards:
        _set_netflix_failure("netflix_control.search", "could not read any title cards")
        return (
            "I opened Netflix search for " + query + ", but I could not read any title cards. "
            "Please wait for Netflix to finish loading, then say 'I mean the second title'."
        )

    candidates = _convert_cards_to_candidates(cards)

    # ── Logging: full visibility into scan results ──
    _page_url = ""
    try:
        _page_url = str(page.url or "") if page is not None else ""
    except Exception:
        pass
    print(f"[Netflix] search_page_url={_page_url}")
    print(f"[Netflix] raw_candidate_titles={[c['title'] for c in candidates]}")

    # ── Query-token validation: reject unrelated candidates (autoplay path) ──
    # Only when auto_play is True and query has meaningful tokens.
    # This prevents listing/browse-row cards as valid search results.
    if auto_play and query and candidates:
        import re as _re_tv

        q_tokens = {t for t in _re_tv.split(r"[^a-z0-9]+", query.lower()) if t and len(t) >= 2}
        if q_tokens:
            validated = []
            rejected = []
            for c in candidates:
                c_tokens = set(_re_tv.split(r"[^a-z0-9]+", c["title"].lower()))
                if q_tokens & c_tokens:
                    validated.append(c)
                else:
                    rejected.append(c["title"])
            if rejected:
                print(f"[Netflix] rejected_candidate_titles={rejected}")
            if validated:
                candidates = validated
                print(f"[Netflix] cleaned_candidate_titles={[c['title'] for c in candidates]}")
            else:
                print(
                    f"[Netflix] Autoplay rejected: no candidates match query tokens for '{query}'"
                )
                return (
                    f"I searched Netflix for {query}, but could not identify "
                    f"a safe matching result to play. Please try a different search."
                )

    # Native search: return list, do NOT auto-play
    if not auto_play:
        # Apply query-token validation for list-only mode too —
        # do not present unrelated recommendation cards as "results for <query>".
        if query and candidates:
            import re as _re_tv

            q_tokens = {t for t in _re_tv.split(r"[^a-z0-9]+", query.lower()) if t and len(t) >= 2}
            if q_tokens:
                validated = []
                for c in candidates:
                    c_tokens = set(_re_tv.split(r"[^a-z0-9]+", c["title"].lower()))
                    if q_tokens & c_tokens:
                        validated.append(c)
                if validated:
                    candidates = validated
                else:
                    print(
                        f"[Netflix] List-only rejected: no candidates match query tokens for '{query}'"
                    )
                    return (
                        f"I searched Netflix for {query}, but could not identify "
                        f"a safe matching result to play. Please try a different search."
                    )
        # Save grid with filtered candidates (only if any survived filtering)
        if candidates:
            _save_netflix_grid(cards, query)
        lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates[:5])]
        msg = "I found these Netflix titles. Please tell me which one you want to watch."
        if lines:
            msg += "\n" + "\n".join(lines)
        return msg

    # auto_play: find best match
    best, score, reason = select_best_media_candidate(query, candidates, "netflix")

    if best and reason == "selected":
        selected_title = best.get("title", query)
        nav_url = best["url"]
        if page is not None:
            try:
                sess.run(page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000))
            except Exception:
                nav_script = f"window.location.href = {json.dumps(nav_url)};"
                sess.run(sess.evaluate_background_media("netflix.com", nav_script))
        else:
            nav_script = f"window.location.href = {json.dumps(nav_url)};"
            sess.run(sess.evaluate_background_media("netflix.com", nav_script))
        time.sleep(2.0)

        if not _verify_playback_started(sess, page):
            play_err = click_netflix_play_button_for_current_title(
                sess, page, expected_title=selected_title
            )
            if play_err is not None:
                return (
                    f"I opened the Netflix title page for {selected_title}, "
                    f"but I could not start playback automatically."
                )

        _set_playing()
        if candidates:
            _save_netflix_grid(cards, query)
        return f"Playing: {selected_title}"

    # Practical fallback: token overlap matching
    if reason == "ambiguous" or reason == "low_confidence":
        print(f"[Netflix] Autoplay rejected: reason={reason} for query={query}")
        if candidates:
            _save_netflix_grid(cards, query)
        lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates[:5])]
        msg = "I found several possible Netflix titles. Please choose one."
        if lines:
            msg += "\n" + "\n".join(lines)
        return msg

    print(f"[Netflix] Autoplay rejected: no suitable match for query={query}")
    return "No suitable Netflix title found for that search, sir."


def _click_visible_netflix_card_by_ordinal(ordinal: int, page, sess) -> bool:
    """Click a visible Netflix card by ordinal (1-based) using DOM.

    Three-level fallback:
    1. Strict a[href*='/title/'] and a[href*='/watch/'] links
    2. Broader selectors (title-card, slider, lolomo)
    3. Geometry-based scan of all visible elements on page

    All levels filter out GPC/privacy/footer/nav/bad-token elements.
    Returns True on successful click, False otherwise.
    """
    if page is None or ordinal < 1:
        return False

    # Level 1: strict href links
    click_js = (
        "() => {"
        "  var BAD=['privacy','confidentialit','confidentialité','preferences','préférences','cookie','legal',"
        "    'footer','help','aide','account','profile','notification',"
        "    'navigation','settings','param','botlink','gpc','opt-out',"
        "    'optout','consent','gdpr','terms','cgu','mentions-legales'];"
        "  function hasBad(el){"
        "    var p=(el.id+' '+el.className+' '+(el.getAttribute('data-uia')||'')+' '+(el.getAttribute('aria-label')||'')+' '+(el.getAttribute('href')||'')).toLowerCase();"
        "    var t=(el.textContent||'').trim().toLowerCase();if(t.length<120)p+=' '+t;"
        "    for(var bi=0;bi<BAD.length;bi++){if(p.indexOf(BAD[bi])>=0)return true;}return false;"
        "  }"
        "  var targets = document.querySelectorAll("
        '    \'a[href*="/title/"], a[href*="/watch/"], a[href*="jbv="]\''
        "  );"
        "  var visible = [];"
        "  for (var i = 0; i < targets.length; i++) {"
        "    var el = targets[i];"
        "    if (hasBad(el) || hasBad(el.closest('header,nav,footer,[role=banner],[role=navigation],[role=contentinfo]')||el)) { continue; }"
        "    var r = el.getBoundingClientRect();"
        "    if (r.width < 30 || r.height < 30) { continue; }"
        "    if (r.top > window.innerHeight + 200 || r.left > window.innerWidth + 200) { continue; }"
        "    var cur = el;"
        "    var hidden = false;"
        "    while (cur && cur !== document.body) {"
        "      var s = window.getComputedStyle(cur);"
        "      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') {"
        "        hidden = true; break;"
        "      }"
        "      cur = cur.parentElement;"
        "    }"
        "    if (hidden) { continue; }"
        "    var txt = (el.textContent || '').trim();"
        "    var img = el.querySelector('img[alt]');"
        "    if (txt.length > 1 || (img && img.getAttribute('alt'))) {"
        "      visible.push(el);"
        "    }"
        "  }"
        "  if (visible.length === 0) { return false; }"
        "  var idx = " + str(ordinal - 1) + ";"
        "  if (idx >= visible.length) { idx = visible.length - 1; }"
        "  visible[idx].click();"
        "  return true;"
        "}"
    )
    try:
        result = sess.run(page.evaluate(click_js))
        if result:
            print(f"[Netflix] DOM-clicked card #{ordinal} via a[href] links")
            return True
    except Exception as exc:
        print(f"[Netflix] DOM click failed: {exc}")

    # Level 2: broader selectors (title cards, thumbnails) — with bad-token filter
    broad_js = (
        "() => {"
        "  var BAD=['privacy','confidentialit','confidentialité','preferences','préférences','cookie','legal',"
        "    'footer','help','aide','account','profile','notification',"
        "    'navigation','settings','param','botlink','gpc','opt-out',"
        "    'optout','consent','gdpr','terms','cgu','mentions-legales'];"
        "  function hasBad(el){"
        "    var p=(el.id+' '+el.className+' '+(el.getAttribute('data-uia')||'')+' '+(el.getAttribute('aria-label')||'')+' '+(el.getAttribute('href')||'')).toLowerCase();"
        "    var t=(el.textContent||'').trim().toLowerCase();if(t.length<120)p+=' '+t;"
        "    for(var bi=0;bi<BAD.length;bi++){if(p.indexOf(BAD[bi])>=0)return true;}return false;"
        "  }"
        "  var selectors = ["
        "    '[data-uia*=\"title\"]',"
        "    '[class*=\"title-card\"]',"
        "    '[class*=\"slider\"] a',"
        "    '.lolomoRow a',"
        "    '[class*=\"card\"] a'"
        "  ];"
        "  var visible = [];"
        "  for (var s = 0; s < selectors.length; s++) {"
        "    var els = document.querySelectorAll(selectors[s]);"
        "    for (var i = 0; i < els.length; i++) {"
        "      var el = els[i];"
        "      if (hasBad(el)) { continue; }"
        "      var ancestor = el.parentElement;"
        "      var insideBad = false;"
        "      while (ancestor && ancestor !== document.body) {"
        "        var aRole = (ancestor.getAttribute('role') || '').toLowerCase();"
        "        var aCls = (ancestor.className || '').toLowerCase();"
        "        var aTag = (ancestor.tagName || '').toLowerCase();"
        "        if (aRole === 'dialog' || aTag === 'footer' ||"
        "            aCls.indexOf('modal') >= 0 || aCls.indexOf('overlay') >= 0 ||"
        "            aCls.indexOf('privacy') >= 0 || aCls.indexOf('preference') >= 0 ||"
        "            aCls.indexOf('cookie') >= 0 || aCls.indexOf('legal') >= 0) {"
        "          insideBad = true; break;"
        "        }"
        "        ancestor = ancestor.parentElement;"
        "      }"
        "      if (insideBad) { continue; }"
        "      var r = el.getBoundingClientRect();"
        "      if (r.width < 30 || r.height < 30) { continue; }"
        "      if (r.top > window.innerHeight + 200 || r.left > window.innerWidth + 200) { continue; }"
        "      var txt = (el.textContent || '').trim();"
        "      if (txt.length > 1) { visible.push(el); }"
        "    }"
        "  }"
        "  if (visible.length === 0) { return false; }"
        "  var idx = " + str(ordinal - 1) + ";"
        "  if (idx >= visible.length) { idx = visible.length - 1; }"
        "  visible[idx].click();"
        "  return true;"
        "}"
    )
    try:
        result = sess.run(page.evaluate(broad_js))
        if result:
            print(f"[Netflix] DOM-clicked card #{ordinal} via broad selectors")
            return True
    except Exception as exc:
        print(f"[Netflix] Broad DOM click failed: {exc}")

    # Level 3: geometry-based scan — find visible card-like elements by size/position
    geo_js = (
        "() => {"
        "  var BAD=['privacy','confidentialit','confidentialité','preferences','préférences','cookie','legal',"
        "    'footer','help','aide','account','profile','notification',"
        "    'navigation','settings','param','botlink','gpc','opt-out',"
        "    'optout','consent','gdpr','terms','cgu','mentions-legales'];"
        "  function hasBad(el){"
        "    var p=(el.id+' '+el.className+' '+(el.getAttribute('data-uia')||'')+' '+(el.getAttribute('aria-label')||'')+' '+(el.getAttribute('href')||'')).toLowerCase();"
        "    var t=(el.textContent||'').trim().toLowerCase();if(t.length<120)p+=' '+t;"
        "    for(var bi=0;bi<BAD.length;bi++){if(p.indexOf(BAD[bi])>=0)return true;}return false;"
        "  }"
        "  function isBadChrome(el) {"
        "    if (el.closest("
        '      \'header, nav, footer, [role="dialog"],'
        '      [data-uia*="navigation"], [data-uia*="notification"],'
        '      [data-uia*="profile"], [data-uia*="account"], [data-uia*="search-box"],'
        '      input, [data-uia="PIN-entry"],'
        '      [role="banner"], [role="navigation"], [role="contentinfo"]\''
        "    )) return true;"
        "    return hasBad(el);"
        "  }"
        "  function isVisibleRect(r) {"
        "    return ("
        "      r.width >= 90 && r.height >= 70 &&"
        "      r.top > 80 &&"
        "      r.bottom > 0 && r.top < window.innerHeight &&"
        "      r.right > 0 && r.left < window.innerWidth &&"
        "      r.width < window.innerWidth * 0.8 &&"
        "      r.height < window.innerHeight * 0.8"
        "    );"
        "  }"
        "  function hasVisualSignal(el) {"
        "    var style = window.getComputedStyle(el);"
        "    var bg = style.backgroundImage && style.backgroundImage !== 'none';"
        "    var img = el.tagName === 'IMG' || !!el.querySelector('img');"
        "    var aria = el.getAttribute('aria-label') || el.getAttribute('title');"
        "    var clickable = el.closest('a, button, [role=\"button\"], [tabindex]');"
        "    return !!(bg || img || aria || clickable);"
        "  }"
        "  var seen = {};"
        "  var candidates = [];"
        "  var allEls = document.querySelectorAll("
        "    'main *, [role=\"main\"], .mainView *, .search-results *,'"
        "    ' .billboard *, a, button, [role=\"button\"], [tabindex], img'"
        "  );"
        "  for (var i = 0; i < allEls.length; i++) {"
        "    var el = allEls[i];"
        "    if (isBadChrome(el)) { continue; }"
        "    var card = el.closest('a, button, [role=\"button\"], [tabindex], [data-uia]') || el;"
        "    if (hasBad(card)) { continue; }"
        "    var r = card.getBoundingClientRect();"
        "    if (!isVisibleRect(r)) { continue; }"
        "    if (!hasVisualSignal(card)) { continue; }"
        "    var key = Math.round(r.top / 30) + '_' + Math.round(r.left / 30) + '_' + Math.round(r.width / 30);"
        "    if (seen[key]) { continue; }"
        "    seen[key] = true;"
        "    var score = 0;"
        "    if (card.tagName === 'IMG' || card.querySelector('img')) { score += 4; }"
        "    var st = window.getComputedStyle(card);"
        "    if (st.backgroundImage && st.backgroundImage !== 'none') { score += 3; }"
        "    if (card.getAttribute('aria-label') || card.getAttribute('title')) { score += 2; }"
        "    if (card.closest('a, button, [role=\"button\"], [tabindex]')) { score += 2; }"
        "    var txt = (card.textContent || '').trim();"
        "    if (txt.length > 2 && txt.length < 200) { score += 1; }"
        "    if (score >= 3) {"
        "      candidates.push({el: card, top: r.top, left: r.left, score: score, text: txt.slice(0, 60)});"
        "    }"
        "  }"
        "  candidates.sort(function(a, b) {"
        "    return a.top !== b.top ? a.top - b.top : a.left - b.left;"
        "  });"
        "  if (candidates.length === 0) { return JSON.stringify({ok: false, count: 0}); }"
        "  var idx = " + str(ordinal - 1) + ";"
        "  if (idx >= candidates.length) { idx = candidates.length - 1; }"
        "  var target = candidates[idx].el;"
        "  var tr = target.getBoundingClientRect();"
        "  var cx = tr.left + tr.width / 2;"
        "  var cy = tr.top + tr.height / 2;"
        "  try {"
        "    target.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, clientX: cx, clientY: cy}));"
        "    target.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: cx, clientY: cy}));"
        "    target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: cx, clientY: cy}));"
        "    target.click();"
        "    target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, clientX: cx, clientY: cy}));"
        "  } catch(e) {"
        "    var elAt = document.elementFromPoint(cx, cy);"
        "    if (elAt) { elAt.click(); }"
        "  }"
        "  return JSON.stringify({"
        "    ok: true, method: 'geometry_click',"
        "    count: candidates.length, ordinal: " + str(ordinal) + ","
        "    x: Math.round(cx), y: Math.round(cy),"
        "    text: candidates[idx].text"
        "  });"
        "}"
    )
    try:
        before_url = ""
        try:
            from utils.human_click import human_like_click, page_url, wait_for_url_change

            before_url = page_url(sess, page)
        except Exception:
            human_like_click = None  # type: ignore[assignment]
        result = sess.run(page.evaluate(geo_js))
        result_str = str(result) if result else ""
        if (
            '"ok": true' in result_str
            or "'ok': true" in result_str
            or (isinstance(result, dict) and result.get("ok"))
        ):
            print(f"[Netflix] Geometry-clicked card #{ordinal} via visual scan: {result_str[:200]}")
            # The geometry click dispatches untrusted MouseEvents, which
            # Netflix may ignore. The scan already returned the card's center
            # (x, y) — if the page didn't react, redo it as a trusted
            # human-like mouse click at those coordinates.
            try:
                if (
                    human_like_click
                    and before_url
                    and not wait_for_url_change(sess, page, before_url, timeout_s=2.0)
                ):
                    data = result if isinstance(result, dict) else {}
                    if not data:
                        try:
                            data = json.loads(result_str)
                        except Exception:
                            data = {}
                    cx, cy = data.get("x"), data.get("y")
                    if cx and cy:
                        print(
                            f"[Netflix] Synthetic geometry click had no effect — human-like fallback at ({cx}, {cy})"
                        )
                        human_like_click(
                            sess, page, float(cx), float(cy), label=f"netflix #{ordinal}"
                        )
            except Exception as exc:
                print(f"[Netflix] Human-click fallback failed: {exc}")
            return True
    except Exception as exc:
        print(f"[Netflix] Geometry click failed: {exc}")
    return False


def _handle_grid_play(state: str, params: dict, sess, page, player=None) -> str:
    """Execute grid ordinal selection.

    Uses saved Netflix grid FIRST. Falls back to scanning current page.
    """
    ordinal = params.get("ordinal")
    row_val = params.get("row")
    col_val = params.get("col")
    position_text = params.get("position", "").strip().lower().replace("_", " ")

    # V9C.3: Row/col fast path — click via visible grid cache
    if row_val is not None and col_val is not None:
        try:
            _r = int(row_val)
            _c = int(col_val)
        except (ValueError, TypeError):
            _r, _c = 0, 0
        if _r >= 1 and _c >= 1:
            result = _handle_grid_play_row_col(_r, _c, state, params, sess, page, player)
            if result is not None:
                return result

    grid_query_param = _sanitize_media_query(_media_query_from_params(params))
    # Preserve search query for grid context safety (e.g., /search?q=One+Piece)
    title = _sanitize_media_query(
        _infer_netflix_query_from_page(
            page,
            sess,
            {"query": grid_query_param, **(params or {})},
        )
        or grid_query_param
    )

    candidates, from_saved = _get_or_scan_candidates(sess, page, title)

    if not candidates:
        # Fresh-scan retry: wait 1.5s and try one more scan before giving up.
        print("[Netflix] grid_play: initial scan returned 0 cards — fresh retry in 1.5s")
        time.sleep(1.5)
        try:
            from actions.browser_intelligence import scan_netflix_cards

            retry_cards = scan_netflix_cards("brave", page=page)
            if retry_cards:
                candidates = _convert_cards_to_candidates(retry_cards)
                _save_netflix_grid(retry_cards, title)
                print(f"[Netflix] grid_play: fresh scan recovered {len(candidates)} candidates")
        except Exception:
            pass

    if not candidates:
        if ordinal is not None and page is not None:
            print(
                "[Netflix] grid_play: no saved/scanned cards — trying live visible-card ordinal click"
            )
            _close_netflix_modal_for_grid_selection(sess, page)
            geo_ok = _click_visible_netflix_card_by_ordinal(ordinal, page, sess)
            if geo_ok:
                time.sleep(2.0)
                if _verify_playback_started(sess, page):
                    _set_playing(f"Netflix card #{ordinal}")
                    return f"Playing Netflix card #{ordinal}"
                play_err = click_netflix_play_button_for_current_title(
                    sess, page, expected_title=""
                )
                if play_err is None:
                    _set_playing(f"Netflix card #{ordinal}")
                    return f"Playing Netflix card #{ordinal}"
                play_err2 = click_netflix_play_button(sess, page)
                if play_err2 is None:
                    _set_playing(f"Netflix card #{ordinal}")
                    return f"Playing Netflix card #{ordinal}"
                return (
                    f"I clicked Netflix visible card #{ordinal}, "
                    f"but I could not click Play automatically."
                )
        _set_netflix_failure("netflix_control.grid_play", "could not read Netflix title cards")
        return "I could not read or click Netflix title cards. Please search for a title first."

    # Fix B: search-context safety only when no explicit ordinal
    if ordinal is None:
        try:
            page_url = ""
            if page is not None:
                try:
                    page_url = str(page.url or "").strip()
                except Exception:
                    page_url = ""
            if not page_url and page is not None:
                try:
                    page_url = str(sess.run(page.evaluate("window.location.href")) or "").strip()
                except Exception:
                    page_url = ""
            filtered = _filter_candidates_safely_for_search(candidates, title, page_url)
            if filtered is not None:
                candidates = filtered
        except Exception:
            pass

        if not candidates:
            return (
                f"I found Netflix results for your search context, but none matched safely for '{title}'. "
                f"Please try a different title/ordinal."
            )

    # Ordinal selection
    if ordinal is not None:
        try:
            ordinal_val = int(ordinal)
        except (ValueError, TypeError):
            ordinal_val = 1

        if ordinal_val < 1 or ordinal_val > len(candidates):
            return f"I only found {len(candidates)} Netflix titles. Which one should I open?"

        candidate = candidates[ordinal_val - 1]
        href = candidate.get("url", "")

        # V9C.3.1: Close modal before explicit ordinal selection
        _close_netflix_modal_for_grid_selection(sess, page)

        if not href:
            # DOM click fallback for cards without URL
            if _click_visible_netflix_card_by_ordinal(ordinal_val, page, sess):
                time.sleep(2.0)
                _focus_netflix_foreground(page, sess)
                if not _verify_playback_started(sess, page):
                    play_err = click_netflix_play_button_for_current_title(
                        sess, page, expected_title=candidate.get("title", "")
                    )
                    if play_err is not None:
                        return (
                            f"I selected Netflix card #{ordinal_val} ({candidate.get('title', '')}), "
                            f"but I could not start playback automatically."
                        )
                _set_playing(f"Netflix card #{ordinal_val}")
                return f"Playing Netflix card #{ordinal_val}"
            return (
                f"Selected card #{ordinal_val} has no valid URL and could not be clicked directly."
            )

        # Direct watch first — /watch/<id> skips the details modal entirely
        _ord_id = _extract_netflix_title_id(href)
        if _ord_id and _play_title_id_directly(sess, page, _ord_id):
            _set_playing(candidate.get("title", ""))
            return f"Playing: {candidate['title']}"

        # Navigate to selected URL
        if page is not None:
            try:
                sess.run(page.goto(href, wait_until="domcontentloaded", timeout=30_000))
            except Exception as exc:
                return f"Failed to open Netflix card #{ordinal_val}: {exc}"
        else:
            nav_script = f"window.location.href = {json.dumps(href)};"
            try:
                sess.run(sess.evaluate_background_media("netflix.com", nav_script))
            except Exception as exc:
                return f"Failed to open Netflix card #{ordinal_val}: {exc}"

        time.sleep(1.5)
        _focus_netflix_foreground(page, sess)

        # If not on /watch/ already, click Play button
        if not _verify_playback_started(sess, page):
            play_err = click_netflix_play_button_for_current_title(
                sess, page, expected_title=candidate.get("title", "")
            )
            if play_err is not None:
                # Do NOT use generic non-scoped fallback when expected_title is known.
                return (
                    f"I opened the Netflix title page for {candidate['title']}, "
                    f"but I could not start playback automatically."
                )

        _set_playing()
        return f"Playing: {candidate['title']}"

    # Title / position selection via BrowserIntel
    try:
        from actions.browser_intelligence import (
            Card as IntelCard,
        )
        from actions.browser_intelligence import (
            choose_card,
            scan_netflix_cards,
            validate_netflix_href,
        )

        if not candidates:
            cards = scan_netflix_cards("brave", page=page)
            _save_netflix_grid(cards, title)
        else:
            # Convert normalized dict candidates back to Card objects for choose_card API
            cards = []
            for c in candidates:
                cards.append(
                    IntelCard(
                        index=c.get("visible_order", 1) - 1,
                        title=c.get("title", ""),
                        href=c.get("url", ""),
                        platform="netflix",
                    )
                )

        if not cards:
            _set_netflix_failure(
                "netflix_control.grid_play", "could not read Netflix title cards (choose_card path)"
            )
            return "I could not read Netflix title cards. Please wait for the page to load and try again."

        # Guard: no query, no position, no ordinal → list and ask, don't default to first
        if not title and not position_text:
            lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates[:10])]
            msg = "I found Netflix titles. Please say first, second, third, or the title name."
            if lines:
                msg += "\n" + "\n".join(lines)
            return msg

        sel = choose_card(cards, query=title, position=position_text, platform="netflix")

        if sel.status == "selected" and sel.card:
            selected_title = sel.card.title
            href = sel.card.href
            if not validate_netflix_href(href):
                return f"Invalid Netflix URL for '{selected_title}'."
            _sel_id = _extract_netflix_title_id(_normalize_netflix_title_href(href) or href)
            if _sel_id and _play_title_id_directly(sess, page, _sel_id):
                _set_playing(selected_title)
                return f"Playing: {selected_title}"
            if page is not None:
                sess.run(page.goto(href, wait_until="domcontentloaded", timeout=30_000))
            else:
                nav_script = f"window.location.href = {json.dumps(href)};"
                sess.run(sess.evaluate_background_media("netflix.com", nav_script))
            time.sleep(1.5)
            _focus_netflix_foreground(page, sess)

            if not _verify_playback_started(sess, page):
                play_err = click_netflix_play_button_for_current_title(
                    sess, page, expected_title=selected_title
                )
                if play_err is not None:
                    return (
                        f"I opened the Netflix title page for {selected_title}, "
                        f"but I could not start playback automatically."
                    )

            _set_playing()
            return f"Playing: {selected_title}"

        if sel.status == "candidates":
            lines = [
                f"{i + 1}. {c.title}" + (f" \u2014 {c.subtitle}" if c.subtitle else "")
                for i, c in enumerate(sel.candidates)
            ]
            return (
                "I found multiple Netflix matches:\n"
                + "\n".join(lines)
                + "\nWhich one should I open?"
            )

        msg = "No confident Netflix match found."
        if title:
            msg += f" (query: '{title}')"
        return msg

    except ImportError:
        print("[Netflix] browser_intelligence not available, using fallback")
    except Exception as e:
        print(f"[Netflix] Scanner crashed ({e}), using fallback")

    # Fallback: use ordinal from params
    if ordinal is not None:
        target_index = int(ordinal) if str(ordinal).isdigit() else 1
    elif position_text:
        mapping = {
            "top left": 1,
            "first": 1,
            "top center": 2,
            "top middle": 2,
            "center": 2,
            "middle": 2,
            "top right": 3,
            "third": 3,
            "bottom left": 4,
            "middle left": 4,
            "lower left": 4,
            "bottom center": 5,
            "bottom middle": 5,
            "lower center": 5,
            "bottom right": 6,
            "middle right": 6,
            "lower right": 6,
        }
        target_index = mapping.get(position_text) or (
            int(position_text) if position_text.isdigit() else 1
        )
    else:
        # No ordinal, no position, no title — do NOT default to first result
        lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates[:10])]
        msg = "I found Netflix titles. Please say first, second, third, or the title name."
        if lines:
            msg += "\n" + "\n".join(lines)
        return msg

    if not candidates:
        return f"Could not find Netflix card #{target_index}."

    if target_index < 1 or target_index > len(candidates):
        return f"I only found {len(candidates)} Netflix titles. Which one should I open?"

    selected_candidate = candidates[target_index - 1]
    selected_title = selected_candidate.get("title", "")
    href = selected_candidate.get("url", "")
    if not href:
        # DOM click fallback for cards without URL
        if _click_visible_netflix_card_by_ordinal(target_index, page, sess):
            time.sleep(2.0)
            _focus_netflix_foreground(page, sess)
            if not _verify_playback_started(sess, page):
                play_err = click_netflix_play_button_for_current_title(
                    sess, page, expected_title=selected_title
                )
                if play_err is not None:
                    return (
                        f"I selected Netflix card #{target_index} ({selected_title}), "
                        f"but I could not start playback automatically."
                    )
            _set_playing()
            return f"Playing: {selected_title}"
        return f"Selected card #{target_index} has no valid URL and could not be clicked directly."

    if page is not None:
        try:
            sess.run(page.goto(href, wait_until="domcontentloaded", timeout=30_000))
        except Exception as exc:
            return f"Failed to open Netflix card #{target_index}: {exc}"
    else:
        nav_script = f"window.location.href = {json.dumps(href)};"
        try:
            sess.run(sess.evaluate_background_media("netflix.com", nav_script))
        except Exception as exc:
            return f"Failed to open Netflix card #{target_index}: {exc}"

    time.sleep(1.5)
    _focus_netflix_foreground(page, sess)

    if not _verify_playback_started(sess, page):
        play_err = click_netflix_play_button_for_current_title(
            sess, page, expected_title=selected_title
        )
        if play_err is not None:
            return (
                f"I opened the Netflix title page for {selected_title}, "
                f"but I could not start playback automatically."
            )

    _set_playing()
    return f"Playing: {selected_title}"


def _handle_play(state: str, params: dict, sess, page, player=None) -> str:
    """Handle standard play/resume action — state-aware.

    - BROWSE: never guesses. Netflix autoplays a muted hero-billboard preview
      on /browse, so there is no reliable way to tell what the user means by
      "play" here. Ask them to search instead of falsely claiming success.
    - SEARCH_RESULTS: ask which title, rather than picking blindly.
    - PLAYER / DETAILS: try DOM play button -> CDP play script -> bounded
      vision fallback, in that order, verifying after each step.
    Only ever returns a "playing"/"resumed" message after
    _verify_playback_started() actually confirms it (no <video> tag alone).
    """
    _focus_netflix_foreground(page, sess)
    if state == _STATE_BROWSE:
        return "I don't know what to play. Please search for a title on Netflix."

    if state == _STATE_SEARCH_RESULTS:
        try:
            from core.browser_grid_state import get_recent_grid

            saved = get_recent_grid("netflix")
            if saved and saved.candidates:
                # Verify saved grid belongs to current search context
                grid_fresh = True
                try:
                    page_url = str(page.url or "").lower() if page is not None else ""
                    if "/search" in page_url and "q=" in page_url:
                        import re as _re_gf

                        url_q = _re_gf.search(r"[?&]q=([^&]+)", page_url)
                        if url_q:
                            from urllib.parse import unquote_plus

                            url_query = unquote_plus(url_q.group(1)).lower().strip()
                            grid_query = (saved.query or "").lower().strip()
                            if url_query and grid_query and url_query != grid_query:
                                grid_fresh = False
                                print(
                                    f"[Netflix] Grid stale: grid query='{saved.query}' ≠ URL query='{url_q.group(1)}'"
                                )
                except Exception:
                    pass
                if grid_fresh:
                    grid_params = dict(params)
                    grid_params["ordinal"] = 1
                    return _handle_grid_play(state, grid_params, sess, page, player)
        except Exception:
            pass
        return (
            "I see search results on Netflix. "
            "Please tell me which title to play (e.g., 'I mean the first title')."
        )

    if _verify_playback_started(sess, page):
        _set_playing()
        return "Netflix is already playing."

    signals = _check_local_signals(sess, page)
    print(
        f"[Netflix] Local signals: url_watch={signals['url_contains_watch']} "
        f"video={signals['has_video']} play_btn={signals['has_play_button']} "
        f"controls={signals['has_player_controls']}"
    )

    if signals["has_play_button"]:
        play_err = click_netflix_play_button(sess, page)
        if play_err is None:
            _set_playing()
            return "Netflix resumed."

    video_result = None
    try:
        video_result = sess.run(sess.evaluate_background_media("netflix.com", _JS_SCRIPTS["play"]))
        print(f"[Netflix] CDP play result: {video_result}")
    except Exception as exc:
        print(f"[Netflix] CDP play failed: {exc}")

    if _verify_playback_started(sess, page):
        _set_playing()
        return str(video_result) if video_result else "Netflix resumed."

    # A details page (/title/<id> or ?jbv=<id>) carries the title id, and
    # /watch/<id> auto-starts the player — no Play button to find. Without this
    # the call fell through to the quota-bound vision pass and reported "could
    # not click the Play button" (2026-09-27 23:43, media_coordinator play).
    try:
        current_url = str(page.url or "") if page is not None else ""
    except Exception:
        current_url = ""
    title_id = _extract_netflix_title_id(current_url) if "/watch/" not in current_url else None
    if title_id:
        _play_title_id_directly(sess, page, title_id)
        if _verify_playback_started(sess, page):
            _set_playing()
            return "Netflix playback started."

    vision_result = _call_screen_process_narrow(sess, page, player)
    if vision_result and vision_result != "vision_fallback_unavailable_due_to_quota_or_timeout":
        print(f"[Netflix] screen_process result: {vision_result[:100]}")

    if _verify_playback_started(sess, page):
        _set_playing()
        return "Netflix playback started."

    # Do NOT call _set_playing() here — playback never verified. Marking the
    # shared media state as "playing" on a failure path makes Spotify-pause
    # logic and status checks elsewhere in Era One believe Netflix is active
    # when it isn't.
    _set_netflix_failure("netflix_control.play", "could not click the Play button automatically")
    return "I opened the Netflix title page, but I could not click the Play button automatically."


def _handle_fullscreen_real_click(sess, page) -> str:
    """Toggle Netflix fullscreen via the browser window state (CDP-driven).

    Why not the "f" hotkey / requestFullscreen: HTML5 fullscreen is silently
    suppressed while a CDP client is attached — `requestFullscreen()` resolves and
    `document.fullscreenElement` gets set, but the viewport never grows, so the
    tab is not actually fullscreen (verified live). That false-positive is exactly
    why Era reported "Fullscreen toggled" while nothing changed, and why even a
    *manual* "f" did nothing while Era was connected.

    Instead we drive the browser WINDOW into fullscreen with CDP
    `Browser.setWindowBounds` (the F11 equivalent). The window manager honors it
    regardless of the CDP link, it needs no OS foreground focus, and it is a clean
    reversible toggle. A real OS F11 keystroke is kept only as a last-ditch
    fallback.
    """
    if page is None:
        return "Cannot toggle fullscreen: no active Netflix page."

    import time

    # Keep focus pollers off the window while it transitions.
    _mark_fullscreen_pending()

    def _window_state(cdp) -> tuple[str, int]:
        info = sess.run(cdp.send("Browser.getWindowForTarget"))
        return (info.get("bounds") or {}).get("windowState", "normal"), info["windowId"]

    try:
        cdp = sess.run(page.context.new_cdp_session(page))
        state, window_id = _window_state(cdp)

        if state == "fullscreen":
            # Already fullscreen — toggle back out. Chromium rejects a direct
            # fullscreen→maximized transition ("restore to normal first"), so
            # step through 'normal'.
            sess.run(
                cdp.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                )
            )
            time.sleep(0.05)
            try:
                sess.run(
                    cdp.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"windowState": "maximized"}},
                    )
                )
            except Exception:
                pass
            print("[Netflix] Exited fullscreen via CDP window state")
            return "Exited fullscreen."

        # Some window managers reject a direct →fullscreen unless the window
        # passes through 'normal' first.
        if state != "normal":
            try:
                sess.run(
                    cdp.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"windowState": "normal"}},
                    )
                )
                time.sleep(0.05)
            except Exception:
                pass

        sess.run(
            cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "fullscreen"}},
            )
        )
        _mark_fullscreen_pending()
        time.sleep(0.2)

        # Verify via the window state itself — pixel/innerHeight checks are
        # unreliable across multi-monitor and HiDPI setups.
        confirmed = sess.run(cdp.send("Browser.getWindowBounds", {"windowId": window_id}))
        if (confirmed.get("bounds") or {}).get("windowState") == "fullscreen":
            print("[Netflix] Fullscreen via CDP window state")
            return "Fullscreen toggled."
        print("[Netflix] CDP window fullscreen not confirmed — trying F11 fallback")
    except Exception as exc:
        print(f"[Netflix] CDP window fullscreen failed: {exc}")

    # ── Fallback: real OS F11 keystroke (browser-chrome fullscreen) ────────
    # Only reached if the CDP window-state path failed. Needs the browser at OS
    # foreground, which netflix_control() arranges before dispatch.
    try:
        import pyautogui

        _mark_fullscreen_pending()
        pyautogui.press("f11")
        print("[Netflix] Fullscreen via F11 fallback")
        return "Fullscreen toggled."
    except Exception as exc:
        return f"Fullscreen error: {exc}"


def _handle_cdp_action(action: str, sess, page, player=None) -> str:
    """Execute CDP-based action (pause, toggle, forward, rewind, skip_intro, fullscreen, mute).

    NOTE: _focus_netflix_foreground is already called by netflix_control() before
    dispatch.  Calling it again here would double-fire focus_window("brave"),
    potentially landing on a different Brave window if multiple exist.
    """
    # ── Fullscreen: Netflix native 'f' hotkey (CDP keyboard = trusted gesture) ──
    if action == "fullscreen":
        return _handle_fullscreen_real_click(sess, page)

    script = _JS_SCRIPTS.get(action)
    if not script:
        return f"Unsupported action: {action}"

    try:
        result = sess.run(sess.evaluate_background_media("netflix.com", script))
        print(f"[Netflix] CDP response: {result}")

        try:
            from core.media_state import set_status

            if action == "pause":
                set_status("netflix", "paused")
            elif action in ("play", "toggle_playback"):
                set_status("netflix", "playing")
        except Exception:
            pass

        if action in ("play", "toggle_playback"):
            try:
                from core.browser_grid_state import set_scroll_context

                set_scroll_context("netflix")
            except Exception:
                pass

        return str(result)
    except Exception as exc:
        return f"Netflix background communication exception: {exc}"


def _netflix_watch_page(sess, page):
    """The Netflix player tab: the page we were handed, else any /watch/ tab."""
    candidates = [page] if page is not None else []
    try:
        candidates += list(sess._context.pages) if sess._context else []
    except Exception:
        pass
    for pg in candidates:
        try:
            if pg is not None and not pg.is_closed() and "netflix.com/watch" in (pg.url or ""):
                return pg
        except Exception:
            continue
    return None


async def _trusted_next_episode(page, start_url: str) -> bool:
    """Real input (Playwright), which Netflix cannot tell from a person: wake the
    controls, click "Next episode", else press its N shortcut. True only if the
    /watch/<id> changed from `start_url` (the URL before ANY attempt), so a late
    in-page click is never followed by a second skip."""

    async def moved(seconds: float) -> bool:
        for _ in range(int(seconds / 0.25)):
            if page.url != start_url:
                return True
            await asyncio.sleep(0.25)
        return page.url != start_url

    if page.url != start_url:
        return True
    try:
        w, h = await page.evaluate("[window.innerWidth, window.innerHeight]")
        await page.mouse.move(w / 2, h / 2)
        await page.mouse.move(w / 2 + 8, h / 2 + 8)
    except Exception:
        pass
    btn = page.get_by_role("button", name=re.compile(r"next episode|[ée]pisode suivant", re.I))
    try:
        if await btn.count() > 0:
            await btn.first.click(timeout=3_000)
            if await moved(6):
                return True
    except Exception:
        pass
    if page.url != start_url or "/watch/" not in page.url:
        return page.url != start_url
    try:
        await page.keyboard.press("N")
    except Exception:
        return False
    return await moved(6)


def _finish_next_episode(result: str, sess, page, start_url: str | None) -> str:
    """Turn the in-page attempt into a verified answer; never report a skip that
    did not happen (it said "Next episode starting" twice on 2026-09-26 while the
    episode stayed put)."""
    from actions._netflix.player_api import NEXT_EPISODE_OK, NEXT_EPISODE_UNMOVED

    if result != NEXT_EPISODE_UNMOVED:
        return result
    watch = _netflix_watch_page(sess, page)
    if watch is not None and start_url:
        try:
            if sess.run(_trusted_next_episode(watch, start_url)):
                print("[Netflix] next_episode: moved via trusted click/N key")
                return NEXT_EPISODE_OK
        except Exception as exc:
            print(f"[Netflix] next_episode trusted fallback failed: {exc}")
    return (
        "I could not move to the next episode — Netflix ignored the player, the "
        "button and the N shortcut. This may be the last episode."
    )


def _handle_player_action(action: str, params: dict, sess, page, state: str) -> str:
    """Run one of the rich player actions built in actions/_netflix/player_api.py.

    Guarded on state: every one of these needs an actual player. Firing
    "next episode" on the browse grid used to return a confusing DOM miss;
    saying so plainly is more useful than reporting a missing element.
    """
    from actions._netflix.player_api import build_player_script

    script = build_player_script(action, params)
    if script is None:
        return (
            f"[Netflix] '{action}' needs a value I didn't get "
            "(for example a position to jump to, or a volume level)."
        )

    # Deliberately NOT gated on `state` before running. State detection is a
    # heuristic over URL and DOM signals, and refusing up-front turns every
    # misread into "nothing is playing" while the user is watching something.
    # The script itself is the authority — it either acts or reports a miss —
    # and `state` is used only to turn that miss into a better sentence.
    start_url = None
    if action in ("next_episode", "skip_credits"):
        _watch = _netflix_watch_page(sess, page)
        start_url = _watch.url if _watch is not None else None
    try:
        result = str(sess.run(sess.evaluate_background_media("netflix.com", script)) or "")
        print(f"[Netflix] player action {action} -> {result[:120]}")
    except Exception as exc:
        return f"Netflix background communication exception: {exc}"

    if action in ("next_episode", "skip_credits"):
        result = _finish_next_episode(result, sess, page, start_url)

    missed = any(
        token in result.lower()
        for token in ("no active media", "element not found", "not found on canvas")
    )
    if missed and state not in (_STATE_PLAYER, _STATE_UNKNOWN):
        return (
            f"Nothing is playing on Netflix right now, so there's no "
            f"{action.replace('_', ' ')} to do. Play something first."
        )

    # Leaving the player invalidates the cached scroll/grid context.
    if action == "back":
        try:
            from core.browser_grid_state import set_scroll_context

            set_scroll_context("netflix")
        except Exception:
            pass

    if action in ("next_episode", "previous_episode", "restart"):
        try:
            from core.media_state import set_status

            set_status("netflix", "playing")
        except Exception:
            pass

    return result or f"Netflix {action.replace('_', ' ')} done."


def _clear_profile_gate(
    sess, page, wait: bool = True, profile: str | None = None, ordinal: int | None = None
) -> dict:
    """Put Netflix on the configured profile. Logs, never raises.

    Thin wrapper so every call site shares one policy and one log line.
    `profile` / `ordinal` override the configured profile when the user named
    one ("the fourth profile", "Catherine's profile").
    """
    try:
        from actions._netflix.profile_gate import ensure_profile_selected

        outcome = ensure_profile_selected(
            sess, page, profile_name=profile, wait_for_gate=wait, ordinal=ordinal
        )
        if outcome["status"] != "no-gate":
            print(f"[Netflix] Profile gate: {outcome['status']} — {outcome['message']}")
        return outcome
    except Exception as exc:
        print(f"[Netflix] Profile gate skipped: {exc}")
        return {"status": "unavailable", "message": str(exc), "profile": ""}


def _profile_request(params: dict) -> tuple[str | None, int | None]:
    """The profile the user asked for, if any: (name, 1-based ordinal)."""
    name = str(params.get("profile") or "").strip() or None
    ordinal = params.get("ordinal")
    try:
        ordinal = int(ordinal) if ordinal not in (None, "") else None
    except (TypeError, ValueError):
        ordinal = None
    if ordinal is not None and ordinal < 1:
        ordinal = None
    # A name is the more specific instruction; never combine the two.
    return name, (None if name else ordinal)


def _profile_outcome_text(outcome: dict, prefix: str = "") -> str:
    status = outcome["status"]
    if status == "selected":
        return f"{prefix}Netflix is on the {outcome['profile']} profile."
    if status in ("not-found", "selected-unsure"):
        return f"{prefix}{outcome['message']}"
    return ""


def _handle_select_profile(sess, page, params: dict) -> str:
    """Pick a profile on the picker that is on screen: by name or by position."""
    name, ordinal = _profile_request(params)
    _focus_netflix_foreground(page, sess)
    outcome = _clear_profile_gate(sess, page, wait=True, profile=name, ordinal=ordinal)
    if outcome["status"] == "no-gate":
        return "Netflix is not showing the profile picker, so there is no profile to choose."
    if outcome["status"] == "unavailable":
        return f"Could not reach the Netflix profile picker. {outcome['message']}"
    return _profile_outcome_text(outcome)


def _handle_open(sess, page, player=None, params: dict | None = None) -> str:
    """Open Netflix and land on the configured profile, ready to use.

    This is what "open Netflix" routes to. Without it the command went through
    open_app, which loads the page and stops — leaving the profile picker on
    screen for the user to click, which is the friction this removes.
    """
    home_url = "https://www.netflix.com/browse"
    on_netflix = False
    try:
        current = str(sess.run(page.evaluate("window.location.href")) or "") if page else ""
        on_netflix = "netflix.com" in current
    except Exception:
        pass

    if page is not None and not on_netflix:
        try:
            sess.run(page.goto(home_url, wait_until="domcontentloaded", timeout=30_000))
        except Exception:
            sess.run(
                sess.evaluate_background_media(
                    "netflix.com", f"window.location.href = '{home_url}';"
                )
            )

    _focus_netflix_foreground(page, sess)
    name, ordinal = _profile_request(params or {})
    outcome = _clear_profile_gate(sess, page, wait=True, profile=name, ordinal=ordinal)

    if outcome["status"] == "selected":
        return f"Netflix is open on the {outcome['profile']} profile."
    return _profile_outcome_text(outcome, prefix="Netflix is open. ") or "Netflix is open."


def _handle_scroll(params: dict) -> str:
    """Handle Netflix scroll action."""
    direction = "down" if params.get("action", "") in ("scroll", "scroll_down") else "up"
    try:
        from actions.browser_intelligence import scroll_page
        from actions.platform_tab import get_or_open_platform_tab

        get_or_open_platform_tab(platform="netflix", browser="brave")
        result_dict = scroll_page(platform="netflix", browser_name="brave", direction=direction)
        if result_dict.get("scrolled"):
            count = result_dict.get("visible_cards", 0)
            return f"Scrolled Netflix {direction}. Visible cards: {count}."
        return f"Failed to scroll Netflix: {result_dict.get('error', 'unknown error')}"
    except ImportError:
        sess = get_browser_session("brave")
        sess.run(sess.scroll(direction))
        return f"Scrolled Netflix {direction} (basic)."


def _handle_status(state_info: dict) -> str:
    """Return brief Netflix status summary.

    If Netflix Vision cooldown is active, returns a controlled message
    directing the user to search or use saved titles instead of spamming
    screen_process.
    """
    if is_netflix_failure_context_active():
        return (
            "Netflix screen analysis is temporarily disabled after a Netflix automation failure. "
            "Please run a Netflix search again or choose a numbered title from the saved list."
        )

    if is_netflix_vision_blocked():
        return (
            "Netflix screen vision is temporarily unavailable. "
            "Please use search again or choose a numbered title from the saved list."
        )

    state = state_info.get("state", _STATE_UNKNOWN)
    signals = state_info.get("signals", {})
    title = signals.get("page_title", "")
    signals.get("current_url", "")
    parts = [f"Netflix state: {state}"]
    if signals["has_video"]:
        parts.append("video present")
    if signals["url_contains_watch"]:
        parts.append("on /watch/ page")
    if signals["has_player_controls"]:
        parts.append("player controls visible")
    if title:
        parts.append(f"title: {title[:60]}")
    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Main exported function
# ═══════════════════════════════════════════════════════════════════════════════


def netflix_control(parameters: dict | None = None, player=None) -> str:
    """Netflix media controller.

    Supported actions
    -----------------
    focus             — open Netflix if closed, focus the existing tab if open;
                         never searches or navigates
    search            — navigate to /search?q=..., scan cards, save grid, return
                         list; with a query and not list-only, auto-plays a safe
                         strong match
    play              — with a query: treated as search-and-play (same as search
                         + auto_play); without a query: resume/start playback on
                         the current Netflix page
    grid_play         — select a title card by ordinal/position from saved grid
    pause             — pause playback
    toggle_playback   — play if paused, pause if playing
    skip_intro / skip — click the skip-intro / skip-recap button
    forward           — seek +10 s
    rewind / back_10s — seek -10 s
    fullscreen        — toggle fullscreen
    mute              — toggle mute
    scroll/scroll_down/scroll_up — scroll page
    status / current  — report current page state

    All action aliases are normalised via _ACTION_ALIASES.
    """
    _reset_vision_attempted()
    params = parameters or {}
    raw_action = str(params.get("action", "")).lower().strip()

    # Backward-compatible normalization: registry may pass netflix_-prefixed actions
    if raw_action in {"netflix_play", "netflix_pause", "netflix_resume"}:
        raw_action = raw_action.replace("netflix_", "")

    action = _ACTION_ALIASES.get(raw_action, raw_action)

    print(f"[Netflix] Action received: {raw_action!r} -> normalised: {action!r}")

    # List-only detection: explicit user intent to NOT autoplay
    import re as _re_local

    _list_only = False
    try:
        _list_only = bool(
            _re_local.search(
                r"\b(list|show\s+only|just\s+search|do\s+not\s+play|search\s+only|"
                r"don'?t\s+play|ne\s+joue\s+pas|affiche\s+seulement|liste)\b",
                (params.get("user_text", "") or "").lower(),
            )
        )
    except Exception:
        pass

    # Fix A: play/watch+query -> search+_auto_play by default
    query_from_params = _sanitize_media_query(_media_query_from_params(params))
    if action in ("play", "watch") and query_from_params:
        print(f"[Netflix] Safety: {raw_action} with query -> search ({query_from_params})")
        action = "search"
        params["action"] = "search"
        if not _list_only:
            params["_auto_play"] = True
        try:
            from core.browser_grid_state import set_last_user_query

            set_last_user_query(query_from_params, "netflix")
        except Exception:
            pass

    # Fix A: search+query autoplays by default (unless explicit list-only)
    elif action == "search" and query_from_params and "_auto_play" not in params:
        if not _list_only:
            params["_auto_play"] = True

    if player:
        player.write_log(f"[Netflix] {action}")

    # Intent memory
    try:
        remember_action(
            tool="netflix_control",
            action=action,
            app="browser",
            media_app="netflix",
            browser="brave",
        )
    except Exception:
        pass

    # Single-playback enforcer for play actions
    if action in _PLAY_ACTIONS:
        _pause_other_media(player=player)

    # Scroll actions (no page acquire needed)
    if action in ("scroll", "scroll_down", "scroll_up"):
        return _handle_scroll(params)

    # Focus-only action: open Netflix if closed, focus the existing tab if
    # already open. Does not navigate, search, or touch playback.
    if action == "focus":
        sess, page = _acquire_netflix_page()
        _focus_netflix_foreground(page, sess)
        return "Netflix focused."

    # "Open Netflix" — land on the configured profile, ready to use.
    if action == "open":
        sess, page = _acquire_netflix_page()
        return _handle_open(sess, page, player=player, params=params)

    # "Pick the fourth profile" / "choose Catherine's profile" on the picker.
    if action == "select_profile":
        sess, page = _acquire_netflix_page()
        return _handle_select_profile(sess, page, params)

    # Home / browse actions
    if action in ("home", "browse", "go_home"):
        sess, page = _acquire_netflix_page()
        _focus_netflix_foreground(page, sess)
        home_url = "https://www.netflix.com/browse"
        if page is not None:
            try:
                sess.run(page.goto(home_url, wait_until="domcontentloaded", timeout=30_000))
            except Exception:
                nav_script = f"window.location.href = '{home_url}';"
                sess.run(sess.evaluate_background_media("netflix.com", nav_script))
        else:
            nav_script = f"window.location.href = '{home_url}';"
            sess.run(sess.evaluate_background_media("netflix.com", nav_script))
        time.sleep(1.0)
        _focus_netflix_foreground(page, sess)
        # Navigating to /browse is the single most likely moment for the
        # profile picker to appear. This branch returns before the dispatcher's
        # gate handling, so it has to clear the gate itself.
        _clear_profile_gate(sess, page, wait=True)
        try:
            from actions.browser_intelligence import scan_netflix_cards

            cards = scan_netflix_cards("brave", page=page)
            if cards:
                _save_netflix_grid(cards, "browse")
        except Exception:
            pass
        return "Netflix home opened."

    # Acquire owned Netflix page and session.
    # Passive actions (status polls, playback-state CDP tweaks) must not
    # steal OS focus — the router fires them constantly, and each focus call
    # yanked the user out of whatever window (or fullscreen) they were in.
    _passive_action = action in ("status", "current", "pause", "mute", "toggle_playback")
    try:
        sess, page = _acquire_netflix_page()
        if not _passive_action:
            _focus_netflix_foreground(page, sess)
    except Exception as exc:
        if action in _PLAY_ACTIONS:
            return f"Could not access Netflix browser tab: {exc}"
        sess = get_browser_session("brave")
        page = None

    # Cheap catch-all for a picker that is ALREADY on screen: one probe, no
    # polling. The waiting version belongs only where we just navigated and
    # therefore expect the gate to render asynchronously (`open`, `home`) —
    # putting a multi-second wait here would tax every pause and skip for a
    # gate that is almost never there.
    if page is not None:
        # "Pick the fourth profile" can arrive as grid_play + ordinal (the
        # ordinal router has no notion of profiles). While the picker is up that
        # ordinal means a profile, not a title card - without this the line
        # below would pick the DEFAULT profile and then play title #4.
        if (
            action == "grid_play"
            and _profile_request(params)[1] is not None
            and "profil" in str(params.get("user_text", "") or "").lower()
        ):
            from actions._netflix.profile_gate import gate_visible

            if gate_visible(sess, page):
                return _handle_select_profile(sess, page, params)
        _clear_profile_gate(sess, page, wait=False)

    # Detect state
    state_info = _detect_state(sess, page)
    state = state_info["state"]
    print(f"[Netflix] State: {state} (url={state_info['signals']['current_url'][:80]})")

    # State-based dispatch
    if action == "search":
        return _handle_search(state, params, sess, page, player=player)

    if action == "grid_play":
        return _handle_grid_play(state, params, sess, page, player=player)

    if action == "play":
        return _handle_play(state, params, sess, page, player=player)

    # Fix E: block fullscreen on search_results
    if action == "fullscreen" and state == _STATE_SEARCH_RESULTS:
        return "Cannot enter fullscreen on Netflix search results. Play a title first."

    # Rich player actions (skip intro/recap/credits, next & previous episode,
    # parameterised seek, Netflix's own volume, restart, episode list,
    # subtitles, leave-player). Checked BEFORE _JS_SCRIPTS so the
    # parameterised forward/rewind win over the fixed ±10s originals.
    if action in PLAYER_ACTIONS:
        return _handle_player_action(action, params, sess, page, state)

    # CDP-based actions (pause, toggle, skip_intro, fullscreen, mute)
    if action in _JS_SCRIPTS:
        return _handle_cdp_action(action, sess, page, player=player)

    # Status report
    if action in ("status", "current"):
        return _handle_status(state_info)

    return (
        f"[Netflix] Unknown action: '{action}'. "
        f"Valid actions: {', '.join(sorted(set(_JS_SCRIPTS) | set(PLAYER_ACTIONS) | {'grid_play', 'search', 'status', 'open', 'home', 'select_profile'}))}"
    )


# NOTE (2026-09-15): four "direct internal-player control" helpers used to sit
# here — _netflix_direct_play_pause / _skip_intro / _skip_recap / _seek. They
# were added on 2026-09-14 referencing five JS constants that were never written
# into actions/_netflix/js_snippets.py, so importing this module raised
# ImportError and Era could not start at all. Nothing called them: play/pause,
# skip and seek all run through _JS_SCRIPTS -> _handle_cdp_action, which is
# intact. Removed rather than reconstructed — inventing the missing JS would be
# guessing at behaviour that was never exercised.


def _autoselect_marwa_profile(page, sess=None):
    """Deprecated shim — use actions._netflix.profile_gate directly.

    The original body had a second, quieter bug on top of never being waited
    for: it called `page.evaluate(...)` without `sess.run(...)`. On the async
    Playwright page that returns a coroutine, so the `"marwa-selected" in
    result` test raised TypeError, got swallowed by the except, and the
    coroutine was returned as the "result" — the selection never actually ran,
    even on the occasions the gate WAS detected.

    Kept as a delegating shim rather than deleted so any out-of-tree caller
    keeps working, and now returns the real outcome.
    """
    from actions._netflix.profile_gate import ensure_profile_selected

    outcome = ensure_profile_selected(sess, page, wait_for_gate=False)
    return f"{outcome['status']}|{outcome['message']}"
