# actions/browser_intelligence.py
"""
Browser Intelligent Automation for YouTube and Netflix grid/card selection.

Provides:
  - scan_youtube_cards / scan_netflix_cards  — extract structured Card objects
  - choose_card                              — match by title, position, or row
  - scroll_page                              — scroll + re-scan without clicking
  - validate_youtube_href / validate_netflix_href  — safe href checks
  - normalize_query                          — detect ordinal/spatial in query
  - focus_or_open_domain                     — safe tab focusing by domain
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

try:
    from actions.browser_control import _domain_family_matches, get_browser_session

    _BROWSER_AVAILABLE = True
except ImportError:
    _BROWSER_AVAILABLE = False
    _domain_family_matches = None


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class Card:
    """A single card/thumbnail from a YouTube or Netflix grid."""

    index: int = 0
    title: str = ""
    subtitle: str = ""
    href: str = ""
    row_index: int = 0
    col_index: int = 0
    x: int = 0
    y: int = 0
    visible: bool = True
    platform: str = ""


@dataclass
class SelectionResult:
    """Result of a card selection attempt."""

    status: str = "not_found"
    card: Card | None = None
    candidates: list[Card] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""


# ── JavaScript scanners ──────────────────────────────────────────────────────

# Combined container + link selectors for robust extraction across YouTube layouts
_YT_CONTAINER_SEL = (
    "ytd-rich-item-renderer, ytd-rich-grid-media, "
    "ytd-video-renderer, ytd-grid-video-renderer, ytd-compact-video-renderer"
)
_YT_LINK_SEL = 'a#thumbnail[href*="/watch"], a[href*="/watch?v="]'
_YT_TITLE_SEL = "#video-title, a#video-title, a#video-title-link, yt-formatted-string#video-title"

_YT_SCAN_JS = f"""
const CONTAINERS = '{_YT_CONTAINER_SEL}';
const LINKS = '{_YT_LINK_SEL}';
const TITLES = '{_YT_TITLE_SEL}';
const results = [];
const seen = new Set();

const items = Array.from(document.querySelectorAll(CONTAINERS));
const W = window.innerWidth, H = window.innerHeight;

if (items.length === 0) {{
    // Fallback: extract directly from visible watch links (search results, guide pages)
    const links = Array.from(document.querySelectorAll(LINKS));
    links.forEach((link) => {{
        const r = link.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        if (r.left > W || r.top > H || r.left + r.width < 0) return;
        const href = link.href || '';
        if (!href || href.includes('/shorts/')) return;
        try {{
            const vid = new URL(href).searchParams.get('v');
            if (!vid || seen.has(vid)) return;
            seen.add(vid);
            const container = link.closest(CONTAINERS) || link.parentElement;
            const titleEl = container?.querySelector(TITLES)
                || link.querySelector('[aria-label]');
            const title = titleEl?.textContent?.trim()
                || titleEl?.getAttribute?.('aria-label')?.trim()
                || link.getAttribute('title') || '';
            if (!title) return;
            const channelEl = container?.querySelector(
                '#channel-name, ytd-channel-name, [id="channel-info"]'
            );
            results.push({{
                title: title,
                subtitle: (channelEl?.textContent || '').trim(),
                href: href,
                x: Math.round(r.left), y: Math.round(r.top),
                width: Math.round(r.width), height: Math.round(r.height)
            }});
        }} catch(e) {{}}
    }});
}} else {{
    items.forEach((item) => {{
        const r = item.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        if (r.left > W || r.top > H || r.left + r.width < 0) return;
        const titleEl = item.querySelector(TITLES);
        const title = titleEl?.textContent?.trim()
            || titleEl?.getAttribute?.('aria-label')?.trim()
            || '';
        if (!title) return;
        const channelEl = item.querySelector(
            '#channel-name, ytd-channel-name, [id="channel-info"]'
        );
        const link = item.querySelector(LINKS);
        const href = link?.href || '';
        if (!href || href.includes('/shorts/')) return;
        try {{
            const vid = new URL(href, window.location.origin).searchParams.get('v');
            if (!vid || seen.has(vid)) return;
            seen.add(vid);
            const badgeEl = item.querySelector('#badges, ytd-badge-supported-renderer');
            const badge = (badgeEl?.textContent || '').toLowerCase();
            if (badge.includes('ad') || badge.includes('sponsored')) return;
            results.push({{
                title: title,
                subtitle: (channelEl?.textContent || '').trim(),
                href: href,
                x: Math.round(r.left), y: Math.round(r.top),
                width: Math.round(r.width), height: Math.round(r.height)
            }});
        }} catch(e) {{}}
    }});
}}
return JSON.stringify(results);
"""

_YT_DIAG_JS = """
JSON.stringify({
    url: window.location.href,
    readyState: document.readyState,
    richItems: document.querySelectorAll('ytd-rich-item-renderer').length,
    richGridMedia: document.querySelectorAll('ytd-rich-grid-media').length,
    videoRenderers: document.querySelectorAll('ytd-video-renderer').length,
    gridVideoRenderers: document.querySelectorAll('ytd-grid-video-renderer').length,
    watchLinks: document.querySelectorAll('a[href*="/watch?v="]').length,
    thumbLinks: document.querySelectorAll('a#thumbnail[href*="/watch"]').length,
});
"""

_NETFLIX_SCAN_JS = r"""
(() => {
    const profile = document.querySelector('.profile-link, [data-uia="action-select-profile"]');
    if (profile) { return JSON.stringify({scope: 'profile_page', cards: []}); }

    var url = window.location.href || '';
    var isSearch = url.indexOf('/search') !== -1;

    var selectors = [
        '[data-list-name="search"] a[href*="/title/"]',
        '[data-list-name="search"] a[href*="/watch/"]',
        'a[href*="jbv="]',
        '[data-list-name="search"] img[alt]',
        '[data-list-name="search"] a[href]',
        '[data-uia="search-result"] a[href]',
        '[data-uia="title-card"] a[href]',
        '[data-uia*="row"] a[href*="/title/"]',
        '[data-uia*="row"] a[href*="/watch/"]',
        '[role="listitem"] a[href*="/title/"]',
        '[role="listitem"] a[href*="/watch/"]',
        '.title-card a[href]',
        '.slider-item a[href]',
        'a[href*="/watch"]',
        'a[href*="/title"]',
        'img[alt]',
        '[data-uia="title-card"]',
        '[class*="title-card"]',
        '[class*="slider"] [tabindex]',
        '.lolomoRow [tabindex]',
    ];

    function cleanText(s) {
        return String(s || '').replace(/\s+/g, ' ').trim();
    }

    function extractTitle(node) {
        var aria = (node.getAttribute && node.getAttribute('aria-label')) || '';
        if (aria) { return cleanText(aria); }
        var alt = (node.getAttribute && node.getAttribute('alt')) || '';
        if (alt) { return cleanText(alt); }
        var img = node.querySelector && node.querySelector('img[alt]');
        if (img) {
            alt = img.getAttribute('alt') || '';
            if (alt) { return cleanText(alt); }
        }
        var raw = (node.textContent || '').trim().split(/\n/)[0];
        return cleanText(raw);
    }

    /* Extract cards from a given root. Pure function — does not mutate outer state.
       Tracks which selector matched and a short ancestor-chain hint per card so
       that if cards end up coming from document_fallback (i.e. no scoped
       container worked), we can see exactly where on the page they live. */
    function ancestorHint(node) {
        var el = node;
        var parts = [];
        var depth = 0;
        while (el && el.nodeType === 1 && depth < 4) {
            var tag = el.tagName ? el.tagName.toLowerCase() : '?';
            var idPart = el.id ? ('#' + el.id) : '';
            var uia = (el.getAttribute && el.getAttribute('data-uia')) || '';
            var uiaPart = uia ? ('[data-uia=' + uia + ']') : '';
            var clsRaw = (typeof el.className === 'string') ? el.className.trim() : '';
            var clsPart = clsRaw ? ('.' + clsRaw.split(/\s+/).slice(0, 2).join('.')) : '';
            parts.push(tag + idPart + uiaPart + clsPart);
            el = el.parentElement;
            depth++;
        }
        return parts.join(' < ');
    }

    /* ── Netflix chrome-overlay detection ──────────────────────────────────────
       Reject candidates whose ancestor chain lives inside a navigation bar,
       notification panel, account menu, profile picker, or header. These are
       never valid search-result cards. */
    function isForbiddenNetflixChrome(el) {
        var BAD=['privacy','confidentialit','preferences','cookie','cookies',
            'legal','footer','help','aide','account','profile','notification',
            'navigation','settings','param','botlink','bot-link','gpc','opt-out',
            'optout','consent','gdpr','privacy-center','preference-center',
            'terms','cgu','mentions-legales'];
        function hasBad(e){
            var p=((e.id||'')+' '+(e.className||'')+' '+(e.getAttribute('data-uia')||'')+' '+(e.getAttribute('aria-label')||'')+' '+(e.getAttribute('href')||'')).toLowerCase();
            var t=(e.textContent||'').trim().toLowerCase();if(t.length<120)p+=' '+t;
            for(var bi=0;bi<BAD.length;bi++){if(p.indexOf(BAD[bi])>=0)return true;}return false;
        }
        var cur = el;
        while (cur && cur !== document.body) {
            var data = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase();
            var cls = String(cur.className || '').toLowerCase();
            var tag = String(cur.tagName || '').toLowerCase();
            if (
                tag === 'header' ||
                tag === 'nav' ||
                tag === 'footer' ||
                data.indexOf('navigation') >= 0 ||
                data.indexOf('notifications') >= 0 ||
                data.indexOf('account') >= 0 ||
                data.indexOf('profile') >= 0 ||
                cls.indexOf('notification') >= 0 ||
                cls.indexOf('navigation') >= 0 ||
                cls.indexOf('pinning-header') >= 0 ||
                cls.indexOf('account') >= 0
            ) { return true; }
            if (hasBad(cur)) { return true; }
            cur = cur.parentElement;
        }
        return false;
    }

    function extractCards(root) {
        var seen = new Set();
        var noHrefSeen = new Set();
        var found = [];
        for (var si = 0; si < selectors.length; si++) {
            var nodes = [];
            try {
                nodes = Array.from(root.querySelectorAll(selectors[si]));
            } catch (e) {
                continue;
            }

            for (var ni = 0; ni < nodes.length; ni++) {
                var node = nodes[ni];
                if (isForbiddenNetflixChrome(node)) { continue; }
                var link = node.closest && node.closest('a[href]') || node;
                var href = (link && link.href) || '';

                var title = extractTitle(node);
                if (!title) { continue; }

                /* Search-result cards now use /search?q=...&jbv=<id> hrefs —
                   treat those as valid title links and dedupe on the jbv id
                   (splitting on '?' would collapse them all into one card). */
                var jbvMatch = href ? href.match(/[?&]jbv=(\d+)/) : null;
                var hasValidHref = href && href.indexOf('netflix.com') !== -1 &&
                    (href.indexOf('/watch') !== -1 || href.indexOf('/title') !== -1 ||
                     !!jbvMatch);

                if (hasValidHref) {
                    var cleanHref = jbvMatch ? ('jbv:' + jbvMatch[1]) : href.split('?')[0];
                    if (seen.has(cleanHref)) { continue; }
                    seen.add(cleanHref);
                } else {
                    var titleKey = title.toLowerCase();
                    if (noHrefSeen.has(titleKey)) { continue; }
                    noHrefSeen.add(titleKey);
                    href = '';
                }

                try {
                    var rect = (hasValidHref ? link : node).getBoundingClientRect();
                    found.push({
                        title: title,
                        subtitle: '',
                        href: href,
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        selectorUsed: selectors[si],
                        ancestor: ancestorHint(hasValidHref ? link : node),
                    });
                } catch (e) {
                    found.push({
                        title: title,
                        subtitle: '',
                        href: href,
                        x: 0, y: 0, width: 0, height: 0,
                        selectorUsed: selectors[si],
                        ancestor: ancestorHint(hasValidHref ? link : node),
                    });
                }
            }
        }
        return found;
    }

    /* ── Search-page detection: scope scan to the search-results container ──
       IMPORTANT: candidates are ordered from most-specific to least-specific,
       and a candidate is only "accepted" if it actually yields cards. This
       protects against e.g. '[data-uia*="search"]' matching the header
       search icon/input (which contains zero title cards) instead of the
       results grid — a generic-selector trap that previously caused 0 cards
       to be found on every retry regardless of page load state, since the
       wrong root never changed between retries. */
    var scope = 'fallback_visible_cards';
    var cards = [];

    if (isSearch) {
        var containerCandidates = [
            ['[data-list-name="search"]', 'search_container'],
            ['[data-uia="search-result"]', 'search_container'],
            ['[data-uia*="search-result"]', 'search_container'],
            ['[data-uia*="row"]', 'row_container'],
            ['[role="list"]', 'list_container'],
            ['[role="main"]', 'main'],
            ['[class*="searchResults"]', 'search_container'],
            ['[class*="search-results"]', 'search_container'],
            ['[class*="SearchResults"]', 'search_container'],
            ['main', 'main'],
        ];
        for (var ci = 0; ci < containerCandidates.length; ci++) {
            var container = document.querySelector(containerCandidates[ci][0]);
            if (!container || container === document.documentElement) { continue; }
            var attempt = extractCards(container);
            if (attempt.length > 0) {
                cards = attempt;
                scope = containerCandidates[ci][1];
                break;
            }
        }
    }

    /* Nothing matched a scoped container (or not a search page) — try a
       visible-title fallback that scans all /title/ and /watch/ anchors
       in the document, rejects chrome elements, and requires visible
       bounding rect + useful text/image metadata. Falls back to
       document_fallback only if this also yields nothing. */
    if (cards.length === 0 && isSearch) {
        var seen = new Set();
        var fallbackCards = [];
        var allAnchors = document.querySelectorAll('a[href*="/title/"], a[href*="/watch/"], a[href*="jbv="]');
        var rawTitleCount = allAnchors.length;
        var excludedCount = 0;
        for (var fi = 0; fi < allAnchors.length; fi++) {
            var a = allAnchors[fi];
            if (isForbiddenNetflixChrome(a)) { excludedCount++; continue; }
            var r = a.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) { excludedCount++; continue; }
            var txt = (a.textContent || '').trim();
            var img = a.querySelector('img[alt]');
            var imgAlt = img ? (img.getAttribute('alt') || '') : '';
            if (txt.length < 2 && !imgAlt) { excludedCount++; continue; }
            var href = a.href || '';
            if (!href || href.indexOf('netflix.com') === -1) { excludedCount++; continue; }
            var jbvM = href.match(/[?&]jbv=(\d+)/);
            var cleanHref = jbvM ? ('jbv:' + jbvM[1]) : href.split('?')[0];
            if (seen.has(cleanHref)) { excludedCount++; continue; }
            seen.add(cleanHref);
            var title = imgAlt || txt.split(/\n/)[0].trim();
            fallbackCards.push({
                title: title, subtitle: '', href: href,
                x: Math.round(r.left), y: Math.round(r.top),
                width: Math.round(r.width), height: Math.round(r.height),
                selectorUsed: 'visible_title_fallback',
                ancestor: ancestorHint(a),
            });
        }
        if (fallbackCards.length > 0) {
            cards = fallbackCards;
            scope = 'visible_title_fallback';
        }
        /* Diagnostics: log counts for debugging zero-card issues */
        console.log('[Netflix-Scan] visible_title_fallback: raw=' + rawTitleCount +
                    ' excluded=' + excludedCount + ' candidates=' + fallbackCards.length);
    }

    /* Last resort: scan the whole document with extractCards so we never
       silently return 0 cards just because every candidate container
       happened to be empty/wrong. */
    if (cards.length === 0) {
        cards = extractCards(document);
        scope = isSearch ? 'document_fallback' : 'fallback_visible_cards';
    }

    /* Post-filter for document_fallback on search pages:
       reject items whose ancestor string hint contains forbidden chrome elements
       (header, nav, notification, account, profile). Each card carries its own
       ancestor hint string, computed during extraction by ancestorHint().
       If the ancestor-based filter removes everything, fall back to keeping
       cards whose hint indicates search or main content. */
    if (isSearch && scope === 'document_fallback' && cards.length > 0) {
        var forbiddenPatterns = ['header', 'nav', 'notification', 'account', 'profile', 'pinning-header'];
        var cleanCards = cards.filter(function(c) {
            var hint = (c.ancestor || '').toLowerCase();
            for (var pi = 0; pi < forbiddenPatterns.length; pi++) {
                if (hint.indexOf(forbiddenPatterns[pi]) >= 0) { return false; }
            }
            return true;
        });
        if (cleanCards.length > 0) {
            cards = cleanCards;
        } else {
            /* All cards had forbidden ancestors — keep those at least hinting at search/main */
            cards = cards.filter(function(c) {
                var hint = (c.ancestor || '').toLowerCase();
                return hint.indexOf('search') >= 0 || hint.indexOf('main') >= 0;
            });
        }
    }

    return JSON.stringify({scope: scope, cards: cards});
})()
"""


# ── Query normalization ──────────────────────────────────────────────────────

_SPATIAL_MAP: dict[str, tuple[int, int]] = {
    "top left": (0, 0),
    "first": (0, 0),
    "top center": (0, 1),
    "top middle": (0, 1),
    "center": (0, 1),
    "middle": (0, 1),
    "top right": (0, -1),
    "third": (0, 2),
    "second": (0, 1),
    "bottom left": (-1, 0),
    "middle left": (-1, 0),
    "lower left": (-1, 0),
    "bottom center": (-1, 1),
    "bottom middle": (-1, 1),
    "lower center": (-1, 1),
    "bottom right": (-1, -1),
    "middle right": (-1, -1),
    "lower right": (-1, -1),
}

_WORD_ORDINALS: dict[str, str] = {
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
    "fifth": "5",
}


def normalize_query(query: str) -> tuple[str, str | None]:
    """
    Normalize a user query that may contain ordinal or spatial phrases.

    Returns (cleaned_query, position):
      - If query is an ordinal/spatial request, position is set and cleaned_query is empty.
      - Otherwise, cleaned_query is the original query and position is None.
    """
    if not query or not query.strip():
        return (query, None)

    q = query.strip().lower()

    # Numeric ordinal: "3rd video", "2nd one", "1st", "4th result"
    m = re.match(r"^(\d+)\s*(?:st|nd|rd|th)\s*(?:video|one|item|result)?$", q)
    if m:
        return ("", m.group(1))

    # "video 2", "number 2", "item 3", "result 1"
    m = re.match(r"^(?:video|number|item|result)\s+(\d+)$", q)
    if m:
        return ("", m.group(1))

    # Word ordinals: optional "the" prefix, ordinal word, optional suffix
    m = re.match(
        r"^(?:the\s+)?(first|second|third|fourth|fifth)"
        r"(?:\s+(?:video|one|item|result))?$",
        q,
    )
    if m:
        return ("", _WORD_ORDINALS[m.group(1)])

    # Spatial phrases
    spatial_check = q.replace("_", " ").strip()
    if spatial_check in _SPATIAL_MAP:
        return ("", spatial_check)

    return (query, None)


# ── Safe domain focusing ─────────────────────────────────────────────────────


def focus_or_open_domain(browser_name: str, domain: str, fallback_url: str) -> bool:
    """Focus an existing browser tab by exact domain, or open fallback_url in a new tab.

    Multi-step approach: check current → try focus_tab → try go_to → try new_tab.
    Verifies URL after every step. Never continues on wrong domain.
    Returns True only if the final URL contains the requested domain.
    """
    if not _BROWSER_AVAILABLE:
        print("[BrowserIntel] focus_or_open: browser not available")
        return False
    try:
        sess = get_browser_session(browser_name)

        def _url_has_domain(url: str, dom: str) -> bool:
            if _domain_family_matches:
                return _domain_family_matches(url, dom)
            return dom in url  # fallback if import failed

        # Step 1 — check current tab URL
        try:
            cu = str(sess.run(sess.get_url())).lower()
            if _url_has_domain(cu, domain):
                print(f"[BrowserIntel] Already on {domain}")
                return True
        except Exception:
            pass

        # Step 2 — try to focus existing tab by domain keyword
        try:
            sess.run(sess.focus_tab_by_url(domain))
            time.sleep(0.5)
            fu = str(sess.run(sess.get_url())).lower()
            if _url_has_domain(fu, domain):
                print(f"[BrowserIntel] Focused existing {domain} tab")
                return True
        except Exception as e:
            print(f"[BrowserIntel] focus_tab_by_url failed: {e}")

        # Step 3 — go_to on current/any tab
        print(f"[BrowserIntel] go_to: {fallback_url}")
        try:
            sess.run(sess.go_to(fallback_url))
            time.sleep(1.5)
            nu = str(sess.run(sess.get_url())).lower()
            if _url_has_domain(nu, domain):
                print(f"[BrowserIntel] go_to reached {domain}")
                return True
            print(f"[BrowserIntel] go_to landed on wrong URL: {nu}")
        except Exception as e:
            print(f"[BrowserIntel] go_to error: {e}")

        # Step 4 — new_tab as last resort (most reliable for correct domain)
        print(f"[BrowserIntel] new_tab: {fallback_url}")
        try:
            sess.run(sess.new_tab(fallback_url))
            time.sleep(1.5)
            nu = str(sess.run(sess.get_url())).lower()
            if _url_has_domain(nu, domain):
                print(f"[BrowserIntel] new_tab reached {domain}")
                return True
        except Exception as e:
            print(f"[BrowserIntel] new_tab error: {e}")

        print(f"[BrowserIntel] Failed to reach {domain}")
        return False

    except Exception as e:
        print(f"[BrowserIntel] focus_or_open error: {e}")
        return False


# ── Row grouping ─────────────────────────────────────────────────────────────

_ROW_TOLERANCE = 50


def _group_by_row(cards: list[Card]) -> list[Card]:
    """Assign row/col indices based on vertical position. Output sorted by (row, col, index)."""
    if not cards:
        return []
    sorted_cards = sorted(cards, key=lambda c: (c.y, c.x))
    rows = []
    current_row = [sorted_cards[0]]
    for card in sorted_cards[1:]:
        if abs(card.y - current_row[0].y) <= _ROW_TOLERANCE:
            current_row.append(card)
        else:
            rows.append(current_row)
            current_row = [card]
    if current_row:
        rows.append(current_row)
    result = []
    for ri, row in enumerate(rows):
        row.sort(key=lambda c: (c.x, c.index))
        for ci, card in enumerate(row):
            card.row_index = ri
            card.col_index = ci
            result.append(card)
    return result


# ── JSON parsing ─────────────────────────────────────────────────────────────


def _parse_json_result(raw: str | object) -> list[dict]:
    """Robustly parse JSON returned from browser JS evaluation. Returns [] on failure."""
    if not raw:
        return []
    if isinstance(raw, (list, dict)):
        return list(raw) if isinstance(raw, list) else []
    cleaned = str(raw).strip()
    if not cleaned:
        return []
    if cleaned.startswith("Error") or cleaned.startswith("Exception"):
        return []
    if "__PROFILE_PAGE__" in cleaned:
        return []
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _build_cards(raw_data: list[dict], platform: str) -> list[Card]:
    """Convert parsed JSON data to Card objects with deterministic ordering."""
    cards = []
    for i, item in enumerate(raw_data):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        href = (item.get("href") or "").strip()
        if not title or not href:
            continue
        card = Card(
            index=i,
            title=title,
            subtitle=(item.get("subtitle") or "").strip(),
            href=href,
            x=item.get("x", 0) or 0,
            y=item.get("y", 0) or 0,
            visible=True,
            platform=platform,
        )
        cards.append(card)
    return _group_by_row(cards)


# ── Public scanning API ──────────────────────────────────────────────────────


def scan_youtube_cards(browser_name: str = "brave", limit: int = 30) -> list[Card]:
    """Scan the active YouTube page for video cards.

    Returns a list of Card objects sorted by row then column.
    Returns [] when the scanner fails or no cards are found.
    When 0 cards, logs live diagnostics to help debug.
    """
    if not _BROWSER_AVAILABLE:
        return []
    sess = get_browser_session(browser_name)
    raw = sess.run(sess.evaluate_background_media("youtube.com", _YT_SCAN_JS))
    raw_str = str(raw) if not isinstance(raw, str) else raw
    parsed = _parse_json_result(raw_str)
    cards = _build_cards(parsed, "youtube")[:limit]
    print(f"[BrowserIntel] youtube scan: {len(cards)} cards")

    if not cards:
        try:
            diag_raw = sess.run(sess.evaluate_background_media("youtube.com", _YT_DIAG_JS))
            diag_str = str(diag_raw) if not isinstance(diag_raw, str) else diag_raw
            import json as _json

            diag = _json.loads(diag_str) if diag_str.startswith("{") else {}
            print(
                f"[BrowserIntel] youtube diagnostics: "
                f"url={diag.get('url', '?')[:80]} "
                f"readyState={diag.get('readyState', '?')} "
                f"watch_links={diag.get('watchLinks', 0)} "
                f"thumb_links={diag.get('thumbLinks', 0)} "
                f"rich_items={diag.get('richItems', 0)} "
                f"rich_grid={diag.get('richGridMedia', 0)} "
                f"video_renderers={diag.get('videoRenderers', 0)}"
            )
        except Exception:
            pass

    return cards


def scan_netflix_cards(browser_name: str = "brave", limit: int = 30, page=None) -> list[Card]:
    """Scan the active Netflix page for title cards.

    If a Playwright ``page`` object is provided, evaluate JS on that owned page.
    Otherwise falls back to evaluate_background_media on the background session.

    Auto-selects the Netflix profile if the profile chooser is detected.
    Returns a list of Card objects sorted by row then column.
    Returns [] when the scanner fails or no cards are found.
    """
    if not _BROWSER_AVAILABLE:
        return []
    sess = get_browser_session(browser_name)

    def _do_scan():
        if page is not None:
            raw = sess.run(page.evaluate(_NETFLIX_SCAN_JS))
            raw_str = str(raw) if not isinstance(raw, str) else raw
            return raw, raw_str
        raw = sess.run(sess.evaluate_background_media("netflix.com", _NETFLIX_SCAN_JS))
        raw_str = str(raw) if not isinstance(raw, str) else raw
        return raw, raw_str

    raw, raw_str = _do_scan()
    if "__PROFILE_PAGE__" in raw_str:
        clicked = _click_netflix_profile(browser_name)
        if clicked:
            time.sleep(1.5)
        else:
            return []
        raw, raw_str = _do_scan()
        if "__PROFILE_PAGE__" in raw_str:
            return []

    # Parse new {scope, cards} format or legacy cards-only format
    scope = "unknown"
    card_data = []
    try:
        import json as _json

        parsed_raw = _json.loads(raw_str) if isinstance(raw_str, str) else raw_str
        if isinstance(parsed_raw, dict):
            scope = parsed_raw.get("scope", "unknown")
            card_data = parsed_raw.get("cards", [])
        elif isinstance(parsed_raw, list):
            card_data = parsed_raw
            scope = "legacy_list"
    except Exception:
        card_data = _parse_json_result(raw_str)

    # Detect if the page URL is a search page
    _raw_for_search = raw_str.lower() if isinstance(raw_str, str) else ""
    _is_search_url = "/search" in _raw_for_search

    if scope == "document_fallback":
        if _is_search_url:
            if card_data:
                print(
                    f"[Netflix] ⚠ document_fallback returned {len(card_data)} cards — "
                    f"no scoped search container matched. Chrome-filtered cards:"
                )
                for c in card_data[:10]:
                    print(
                        f"    title={c.get('title', '')[:40]!r} "
                        f"selector={c.get('selectorUsed', '?')!r} "
                        f"ancestor={c.get('ancestor', '?')}"
                    )
            else:
                print(
                    "[Netflix] scan_scope=document_fallback on search page — "
                    "all candidates were rejected by chrome-overlay filter "
                    "or no title cards found at all"
                )
        elif card_data:
            print(
                f"[Netflix] document_fallback returned {len(card_data)} visible candidates"
                f"{' on search page' if _is_search_url else ''}"
            )

    cards = _build_cards(card_data, "netflix")[:limit]
    print(f"[BrowserIntel] netflix scan: {len(cards)} cards (scope={scope})")

    # V9C.2: Promote document_fallback visible candidates when strict scan returns 0
    # V9C.3.1: Filter out GPC/privacy/bad-token elements during promotion
    if not cards and card_data and scope in ("document_fallback", "visible_title_fallback"):
        from actions.netflix_control import _is_bad_netflix_element

        promoted = []
        for i, item in enumerate(card_data):
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            # Filter bad tokens in title, ancestor, selector, data-uia
            bad_info = " ".join(
                [
                    title,
                    (item.get("ancestor") or ""),
                    (item.get("selectorUsed") or ""),
                    (item.get("dataUia") or ""),
                ]
            )
            if _is_bad_netflix_element(bad_info):
                continue
            href = (item.get("href") or "").strip()
            promoted.append(
                Card(
                    index=i,
                    title=title,
                    subtitle=(item.get("subtitle") or "").strip(),
                    href=href,
                    x=item.get("x", 0) or 0,
                    y=item.get("y", 0) or 0,
                    visible=True,
                    platform="netflix",
                )
            )
        if promoted:
            cards = promoted[:limit]
            print(f"[BrowserIntel] netflix promoted {len(cards)} document_fallback candidates")

    # ── Diagnostics: detailed DOM analysis for empty/fallback scans ────────────
    if not cards or (
        scope in ("document_fallback", "visible_title_fallback") and "/search" in raw_str
    ):
        try:
            diag_js = """
            JSON.stringify({
                url: window.location.href,
                readyState: document.readyState,
                searchContainerFound: ['[data-uia="search-result"]','[data-uia*="search-result"]','[data-list-name="search"]','main','[role="main"]'].some(function(s){return document.querySelector(s) !== null && document.querySelector(s) !== document.documentElement;}),
                searchUiaEls: document.querySelectorAll('[data-uia*="search"]').length,
                searchResultsClass: document.querySelectorAll('[class*="searchResults"], [class*="search-results"], [class*="SearchResults"]').length,
                titleCardEls: document.querySelectorAll('[data-uia="title-card"], .title-card').length,
                watchLinks: document.querySelectorAll('a[href*="/watch"]').length,
                titleLinks: document.querySelectorAll('a[href*="/title"]').length,
                rawTitleLinkCount: document.querySelectorAll('a[href*="/title/"]').length,
                chromeExcludedCount: Array.from(document.querySelectorAll('a[href*="/title/"]')).filter(function(a) { var cur = a; while (cur && cur !== document.body) { var d = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase(); var c = String(cur.className || '').toLowerCase(); var t = String(cur.tagName || '').toLowerCase(); if (t === 'header' || t === 'nav' || d.indexOf('navigation') >= 0 || d.indexOf('notifications') >= 0 || d.indexOf('account') >= 0 || d.indexOf('profile') >= 0 || c.indexOf('notification') >= 0 || c.indexOf('navigation') >= 0 || c.indexOf('pinning-header') >= 0 || c.indexOf('account') >= 0) { return true; } cur = cur.parentElement; } return false; }).length,
                dataUiaNavItems: document.querySelectorAll('[data-uia*="navigation"],[data-uia*="notifications"],[data-uia*="account"],[data-uia*="profile"]').length,
                headerEl: document.querySelector('header, nav') !== null,
            });
            """
            if page is not None:
                diag_raw = sess.run(page.evaluate(diag_js))
            else:
                diag_raw = sess.run(sess.evaluate_background_media("netflix.com", diag_js))
            diag_str = str(diag_raw) if not isinstance(diag_raw, str) else diag_raw
            import json as _json

            diag = _json.loads(diag_str) if diag_str.strip().startswith("{") else {}
            is_search_page = (
                "/search" in diag.get("url", "").lower() and "q=" in diag.get("url", "").lower()
            )
            if is_search_page:
                print(f"[Netflix] search_container_found={diag.get('searchContainerFound', False)}")
                print(
                    f"[Netflix] available_search_like_nodes={diag.get('searchResultsClass', 0)} nodes with search-like classes"
                )
                print(f"[Netflix] main_exists={diag.get('headerEl', False)} (header/nav)")
                print(f"[Netflix] raw_title_link_count={diag.get('rawTitleLinkCount', 0)}")
                print(f"[Netflix] chrome_excluded_count={diag.get('chromeExcludedCount', 0)}")
                print(
                    f"[Netflix] candidate_real_cards={diag.get('titleLinks', 0) + diag.get('watchLinks', 0) - diag.get('chromeExcludedCount', 0)}"
                )
                print(f"[Netflix] usable_cards_after_scan={len(cards)}")
                print(
                    f"[Netflix] notification_link_count={diag.get('dataUiaNavItems', 0)} (data-uia nav/notification/profile items)"
                )
            print(
                f"[BrowserIntel] netflix diagnostics: "
                f"url={diag.get('url', '?')[:80]} "
                f"readyState={diag.get('readyState', '?')} "
                f"search_container_found={diag.get('searchContainerFound', False)} "
                f"search_uia_els={diag.get('searchUiaEls', 0)} "
                f"search_results_class={diag.get('searchResultsClass', 0)} "
                f"title_card_els={diag.get('titleCardEls', 0)} "
                f"watch_links={diag.get('watchLinks', 0)} "
                f"title_links={diag.get('titleLinks', 0)} "
                f"nav_items={diag.get('dataUiaNavItems', 0)}"
            )
        except Exception:
            pass

        # Zero-cards diagnostic: detailed data-list-name and link sampling
        if not cards:
            try:
                zc_js = """
                JSON.stringify({
                    dataListNames: Array.from(document.querySelectorAll('[data-list-name]')).map(function(el) { return el.getAttribute('data-list-name') }),
                    dataUiaAll: Array.from(document.querySelectorAll('[data-uia]')).slice(0, 20).map(function(el) { return el.getAttribute('data-uia') }),
                    titleLinks: Array.from(document.querySelectorAll('a[href*="/title/"]')).slice(0, 10).map(function(a) { return {href: a.href, text: (a.textContent || '').trim().slice(0, 40), dataUia: a.getAttribute('data-uia') || ''} }),
                    watchLinks: Array.from(document.querySelectorAll('a[href*="/watch/"]')).slice(0, 10).map(function(a) { return {href: a.href, text: (a.textContent || '').trim().slice(0, 40), dataUia: a.getAttribute('data-uia') || ''} }),
                });
                """
                if page is not None:
                    zc_raw = sess.run(page.evaluate(zc_js))
                else:
                    zc_raw = sess.run(sess.evaluate_background_media("netflix.com", zc_js))
                zc_str = str(zc_raw) if not isinstance(zc_raw, str) else zc_raw
                import json as _zc_json

                zc = _zc_json.loads(zc_str) if zc_str.strip().startswith("{") else {}
                print(
                    f"[BrowserIntel] netflix 0-cards diagnostic: "
                    f"data-list-names={zc.get('dataListNames', [])} "
                    f"data-uia-sample={zc.get('dataUiaAll', [])} "
                    f"title-links={len(zc.get('titleLinks', []))} "
                    f"watch-links={len(zc.get('watchLinks', []))}"
                )
                for link in zc.get("titleLinks", [])[:5]:
                    print(
                        f"  title-link: {link.get('href', '?')[:80]} text={link.get('text', '')[:40]!r} data-uia={link.get('dataUia', '')}"
                    )
                for link in zc.get("watchLinks", [])[:5]:
                    print(
                        f"  watch-link: {link.get('href', '?')[:80]} text={link.get('text', '')[:40]!r} data-uia={link.get('dataUia', '')}"
                    )
            except Exception:
                pass

    return cards


# ── Netflix DOM diagnostic (safe, no click, no screen_process) ──────────────

_NETFLIX_DIAGNOSTIC_JS = """
(() => {
    var url = window.location.href || '';
    var readyState = document.readyState;
    var visibleText = (document.body ? document.body.innerText || '' : '').slice(0, 500);

    function getVisibleText(el) {
        if (!el) return '';
        var t = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 100);
        return t;
    }

    function isChrome(el) {
        var cur = el;
        while (cur && cur !== document.body) {
            var d = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase();
            var c = String(cur.className || '').toLowerCase();
            var t = String(cur.tagName || '').toLowerCase();
            if (t === 'header' || t === 'nav' ||
                d.indexOf('navigation') >= 0 || d.indexOf('notifications') >= 0 ||
                d.indexOf('account') >= 0 || d.indexOf('profile') >= 0 ||
                c.indexOf('notification') >= 0 || c.indexOf('navigation') >= 0 ||
                c.indexOf('pinning-header') >= 0 || c.indexOf('account') >= 0) { return true; }
            cur = cur.parentElement;
        }
        return false;
    }

    // Collect all data-uia values containing search/title/card/row/slider
    var dataUiaSamples = [];
    var allWithDataUia = document.querySelectorAll('[data-uia]');
    for (var i = 0; i < allWithDataUia.length && i < 30; i++) {
        var val = allWithDataUia[i].getAttribute('data-uia') || '';
        if (val.indexOf('search') >= 0 || val.indexOf('title') >= 0 ||
            val.indexOf('card') >= 0 || val.indexOf('row') >= 0 ||
            val.indexOf('slider') >= 0 || val.indexOf('result') >= 0) {
            dataUiaSamples.push(val);
        }
    }

    // Visible anchors excluding chrome
    var visibleAnchors = [];
    var allAnchors = document.querySelectorAll('a[href]');
    for (var ai = 0; ai < allAnchors.length && ai < 50; ai++) {
        var a = allAnchors[ai];
        if (isChrome(a)) continue;
        try {
            var r = a.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            if (r.left > window.innerWidth || r.top > window.innerHeight) continue;
            visibleAnchors.push({
                href: (a.href || '').slice(0, 120),
                text: getVisibleText(a).slice(0, 60),
                rect: {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)},
                tag: a.tagName,
            });
        } catch(e) {}
    }

    // Visible images with alt text, excluding chrome
    var visibleImages = [];
    var allImages = document.querySelectorAll('img[alt]');
    for (var ii = 0; ii < allImages.length && ii < 30; ii++) {
        var img = allImages[ii];
        if (isChrome(img)) continue;
        try {
            var r = img.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            if (r.left > window.innerWidth || r.top > window.innerHeight) continue;
            var alt = (img.getAttribute('alt') || '').slice(0, 80);
            if (alt) {
                var parent = img.parentElement;
                var clickableAncestor = parent ? parent.closest('a, button, [role="button"], [tabindex]') : null;
                visibleImages.push({
                    alt: alt,
                    rect: {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)},
                    hasClickableParent: clickableAncestor !== null,
                    clickableTag: clickableAncestor ? (clickableAncestor.tagName || '') : '',
                });
            }
        } catch(e) {}
    }

    // Count hidden vs visible candidate elements
    var totalTitleAnchors = document.querySelectorAll('a[href*="/title/"]').length;
    var visibleTitleAnchors = 0;
    var titleAnchors = document.querySelectorAll('a[href*="/title/"]');
    for (var ti = 0; ti < titleAnchors.length; ti++) {
        try {
            var r = titleAnchors[ti].getBoundingClientRect();
            if (r.width > 10 && r.height > 10 && !isChrome(titleAnchors[ti])) {
                visibleTitleAnchors++;
            }
        } catch(e) {}
    }

    return JSON.stringify({
        url: url,
        readyState: readyState,
        visibleTextSample: visibleText.slice(0, 300),
        dataUiaSamples: dataUiaSamples,
        visibleAnchors: visibleAnchors,
        visibleImages: visibleImages,
        totalTitleAnchors: totalTitleAnchors,
        visibleTitleAnchors: visibleTitleAnchors,
        isSearch: url.indexOf('/search') >= 0,
        isBrowse: url.indexOf('/browse') >= 0,
        isTitle: url.indexOf('/title/') >= 0,
        isWatch: url.indexOf('/watch/') >= 0,
    });
})()
"""


def run_netflix_dom_diagnostic(browser_name: str = "brave", page=None) -> dict:
    """Run safe Netflix DOM diagnostic.

    Returns a dict with current URL, readyState, visible text sample, data-uia
    samples, visible anchors, visible images, and title anchor counts.
    Does NOT click anything. Does NOT use screen_process. Does NOT claim success.
    """
    from core.utils import log as _log

    result = {
        "status": "error",
        "error": "Diagnostic not executed",
        "url": "",
        "readyState": "",
    }
    try:
        sess = get_browser_session(browser_name)
        if page is not None:
            raw = sess.run(page.evaluate(_NETFLIX_DIAGNOSTIC_JS))
        else:
            raw = sess.run(sess.evaluate_background_media("netflix.com", _NETFLIX_DIAGNOSTIC_JS))
        raw_str = str(raw) if not isinstance(raw, str) else raw
        import json as _json

        parsed = _json.loads(raw_str) if raw_str.strip().startswith("{") else {}
        result = dict(parsed)
        result["status"] = "ok"
        _log(
            "Router",
            f"netflix_dom_diagnostic: url={parsed.get('url', '')[:80]} "
            f"visible_anchors={len(parsed.get('visibleAnchors', []))} "
            f"visible_images={len(parsed.get('visibleImages', []))} "
            f"total_title_anchors={parsed.get('totalTitleAnchors', 0)} "
            f"visible_title_anchors={parsed.get('visibleTitleAnchors', 0)}",
        )
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    return result


def _click_netflix_profile(browser_name: str) -> bool:
    """Click the first profile on the Netflix profile chooser page. Returns True if clicked."""
    click_js = """
    () => {
        const profile = document.querySelector('.profile-link, [data-uia="action-select-profile"]');
        if (profile) { profile.click(); return 'clicked'; }
        return 'not_found';
    }
    """
    try:
        sess = get_browser_session(browser_name)
        raw = str(sess.run(sess.evaluate_background_media("netflix.com", click_js)))
        return "clicked" in raw
    except Exception:
        return False


# ── URL validation ──────────────────────────────────────────────────────────


def validate_youtube_href(href: str) -> bool:
    """Validate that a YouTube href is safe to navigate to."""
    if not href or not isinstance(href, str):
        return False
    if not (href.startswith("https://") or href.startswith("http://")):
        return False
    if "youtube.com" not in href and "youtu.be" not in href:
        return False
    if "/shorts/" in href:
        return False
    return True


def validate_netflix_href(href: str) -> bool:
    """Validate that a Netflix href is safe to navigate to."""
    if not href or not isinstance(href, str):
        return False
    if not (href.startswith("https://") or href.startswith("http://")):
        return False
    if "netflix.com" not in href:
        return False
    return True


_NETFLIX_PROFILE_CHECK_JS = """
() => {
    const profile = document.querySelector('.profile-link, [data-uia="action-select-profile"]');
    return profile ? '__PROFILE_PAGE__' : '__NOT_PROFILE__';
}
"""


def is_netflix_profile_page(browser_name: str = "brave") -> bool:
    """Return True if the current Netflix tab is on the profile-chooser page."""
    if not _BROWSER_AVAILABLE:
        return False
    try:
        sess = get_browser_session(browser_name)
        raw = str(
            sess.run(sess.evaluate_background_media("netflix.com", _NETFLIX_PROFILE_CHECK_JS))
        )
        return "__PROFILE_PAGE__" in raw
    except Exception:
        return False


# ── Title matching ──────────────────────────────────────────────────────────


def _exact_match(query: str, cards: list[Card]) -> Card | None:
    q = query.strip().lower()
    for card in cards:
        if card.title.lower() == q:
            return card
    return None


def _partial_match(query: str, cards: list[Card]) -> Card | None:
    q = query.strip().lower()
    for card in cards:
        t = card.title.lower()
        if q in t or t in q:
            return card
    return None


def _token_overlap_score(query: str, title: str) -> float:
    q_tokens = set(query.lower().split())
    t_tokens = set(title.lower().split())
    if not q_tokens or not t_tokens:
        return 0.0
    intersection = q_tokens & t_tokens
    union = q_tokens | t_tokens
    return len(intersection) / len(union)


def _token_overlap_match(query: str, cards: list[Card]) -> list[tuple[Card, float]]:
    scored = []
    for card in cards:
        score = _token_overlap_score(query, card.title)
        if score > 0:
            scored.append((card, score))
    scored.sort(key=lambda x: -x[1])
    return scored


# ── Spatial / row selection ──────────────────────────────────────────────────


def _select_by_position(cards: list[Card], position: str) -> Card | None:
    """Select a card by spatial position string (e.g. 'top left', 'third', '2')."""
    if not cards:
        return None
    pos = position.strip().lower().replace("_", " ")

    try:
        idx = int(pos) - 1
        if 0 <= idx < len(cards):
            return cards[idx]
    except ValueError:
        pass

    if pos in _SPATIAL_MAP:
        row_offset, col_offset = _SPATIAL_MAP[pos]
        sorted_cards = sorted(cards, key=lambda c: (c.y, c.x, c.index))
        rows = []
        current = [sorted_cards[0]]
        for card in sorted_cards[1:]:
            if abs(card.y - current[0].y) <= _ROW_TOLERANCE:
                current.append(card)
            else:
                rows.append(sorted(current, key=lambda c: (c.x, c.index)))
                current = [card]
        if current:
            rows.append(sorted(current, key=lambda c: (c.x, c.index)))

        if not rows:
            return None

        row_idx = len(rows) - 1 if row_offset == -1 else row_offset
        if row_idx >= len(rows):
            row_idx = len(rows) - 1
        row = rows[row_idx]
        if col_offset == -1:
            col_idx = len(row) - 1
        elif col_offset >= len(row):
            col_idx = len(row) - 1
        else:
            col_idx = col_offset
        return row[col_idx]

    return None


def _select_by_row(cards: list[Card], row_index: int, col_index: int = 0) -> Card | None:
    """Select a card by row and column position (0-based)."""
    sorted_cards = sorted(cards, key=lambda c: (c.y, c.x, c.index))
    rows = []
    current = [sorted_cards[0]]
    for card in sorted_cards[1:]:
        if abs(card.y - current[0].y) <= _ROW_TOLERANCE:
            current.append(card)
        else:
            rows.append(sorted(current, key=lambda c: (c.x, c.index)))
            current = [card]
    if current:
        rows.append(sorted(current, key=lambda c: (c.x, c.index)))
    if row_index < 0 or row_index >= len(rows):
        return None
    row = rows[row_index]
    if col_index < 0 or col_index >= len(row):
        col_index = len(row) - 1
    return row[col_index]


# ── choose_card ──────────────────────────────────────────────────────────────

_CONFIDENCE_SELECT = 0.70
_CONFIDENCE_CANDIDATE = 0.30


def choose_card(
    cards: list[Card],
    query: str | None = None,
    position: str | None = None,
    row: int | None = None,
    platform: str | None = None,
) -> SelectionResult:
    """Select the best card by title match, position, or row.

    Returns SelectionResult with status:
      - "selected":  a single card was confidently matched → caller should navigate
      - "candidates": multiple possible matches (low confidence) → user should choose
      - "not_found":  no match found → report to user
    """
    if not cards:
        return SelectionResult("not_found", None, [], 0.0, "No cards available")

    pf = platform or (cards[0].platform if cards else "unknown")

    if query and query.strip():
        q = query.strip()
        exact = _exact_match(q, cards)
        if exact:
            result = SelectionResult("selected", exact, [], 1.0, "Exact title match")
            print(
                f"[BrowserIntel] {pf} selection: status=selected confidence=1.00 exact='{q[:40]}'"
            )
            return result
        partial = _partial_match(q, cards)
        if partial:
            result = SelectionResult("selected", partial, [], 0.85, "Strong partial title match")
            print(
                f"[BrowserIntel] {pf} selection: status=selected confidence=0.85 partial='{q[:40]}'"
            )
            return result
        scored = _token_overlap_match(q, cards)
        if scored:
            best_card, best_score = scored[0]
            if best_score >= _CONFIDENCE_SELECT:
                result = SelectionResult(
                    "selected", best_card, [], best_score, f"Token overlap match ({best_score:.2f})"
                )
                print(
                    f"[BrowserIntel] {pf} selection: status=selected confidence={best_score:.2f} token"
                )
                return result
            elif best_score >= _CONFIDENCE_CANDIDATE:
                candidates = [c for c, s in scored if s >= _CONFIDENCE_CANDIDATE][:5]
                result = SelectionResult(
                    "candidates", None, candidates, best_score, f"Low confidence ({best_score:.2f})"
                )
                print(
                    f"[BrowserIntel] {pf} selection: status=candidates confidence={best_score:.2f}"
                )
                return result
            else:
                candidates = [c for c, s in scored[:3]]
                result = SelectionResult(
                    "candidates",
                    None,
                    candidates,
                    best_score,
                    f"No strong match (best: {best_score:.2f})",
                )
                print(f"[BrowserIntel] {pf} selection: status=candidates low={best_score:.2f}")
                return result
        result = SelectionResult("not_found", None, [], 0.0, f"No match for '{q}'")
        print(f"[BrowserIntel] {pf} selection: status=not_found query='{q[:40]}'")
        return result

    if position and position.strip():
        card = _select_by_position(cards, position)
        if card:
            title_text = card.title[:60]
            result = SelectionResult(
                "selected", card, [], 1.0, f"Position: {position} -> '{title_text}'"
            )
            print(f"[BrowserIntel] {pf} selection: status=selected position={position}")
            return result
        result = SelectionResult("not_found", None, [], 0.0, f"Invalid position: '{position}'")
        print(f"[BrowserIntel] {pf} selection: status=not_found position='{position}'")
        return result

    if row is not None:
        card = _select_by_row(cards, row)
        if card:
            result = SelectionResult("selected", card, [], 1.0, f"Row: {row}")
            print(f"[BrowserIntel] {pf} selection: status=selected row={row}")
            return result
        result = SelectionResult("not_found", None, [], 0.0, f"Row {row} not found")
        print(f"[BrowserIntel] {pf} selection: status=not_found row={row}")
        return result

    result = SelectionResult("selected", cards[0], [], 0.5, "Default: first card")
    print(f"[BrowserIntel] {pf} selection: status=selected default=first")
    return result


# ── Scrolling ────────────────────────────────────────────────────────────────

_DOMAIN_MAP = {"youtube": "youtube.com", "netflix": "netflix.com"}
_SCAN_MAP = {
    "youtube": scan_youtube_cards,
    "netflix": scan_netflix_cards,
}


def scroll_page(
    platform: str = "youtube",
    browser_name: str = "brave",
    direction: str = "down",
    amount: str = "page",
) -> dict:
    """Scroll the page and re-scan for visible cards. Does NOT click anything.

    Uses the centralized scroll engine from ``utils.scroll``.

    Returns:
        dict with keys: scrolled, direction, amount_px, visible_cards, cards, at_bottom
    """
    if not _BROWSER_AVAILABLE:
        return {"scrolled": False, "error": "Browser not available"}
    domain = _DOMAIN_MAP.get(platform)
    if not domain:
        return {"scrolled": False, "error": f"Unknown platform: {platform}"}
    if direction not in ("down", "up"):
        return {"scrolled": False, "error": f"Invalid direction: {direction}"}

    from core.logger import log_event, main_logger
    from utils.scroll import (
        build_scroll_script,
        resolve_amount,
        resolve_behavior,
        resolve_direction,
    )

    x_sign, y_sign = resolve_direction(direction)

    # Determine viewport height for "page" preset support
    viewport_height = None
    try:
        sess = get_browser_session(browser_name)
        vh_script = "JSON.stringify({innerHeight: window.innerHeight, clientHeight: document.documentElement.clientHeight})"
        vh_raw = str(sess.run(sess.evaluate_background_media(domain, vh_script)))
        vh_data = json.loads(vh_raw) if vh_raw else {}
        vh = vh_data.get("innerHeight") or vh_data.get("clientHeight")
        if vh:
            viewport_height = int(vh)
    except Exception:
        pass

    amount_px = resolve_amount(amount, viewport_height=viewport_height)
    repeat = 1
    total_px = amount_px * repeat
    js_behavior = resolve_behavior("smooth")
    scroll_script = build_scroll_script(x_sign, y_sign, amount_px, js_behavior)

    try:
        sess = get_browser_session(browser_name)
        sess.run(sess.evaluate_background_media(domain, scroll_script))
        time.sleep(0.5)

        scan_fn = _SCAN_MAP.get(platform)
        cards = scan_fn(browser_name) if scan_fn else []

        pos_script = "JSON.stringify({scrollY: window.scrollY, innerHeight: window.innerHeight, bodyHeight: document.body.scrollHeight})"
        pos_raw = str(sess.run(sess.evaluate_background_media(domain, pos_script)))
        try:
            pos_data = json.loads(pos_raw)
        except Exception:
            pos_data = {}

        scroll_y = pos_data.get("scrollY", 0)
        body_h = pos_data.get("bodyHeight", 0)
        view_h = pos_data.get("innerHeight", 0)
        at_bottom = (scroll_y + view_h >= body_h - 50) if body_h else False

        log_event(
            main_logger,
            "scroll.performed",
            "INFO",
            source="browser_intelligence",
            platform=platform,
            direction=direction,
            amount_px=amount_px,
            repeat=repeat,
            total_px=total_px,
            behavior=js_behavior,
            method="js",
            visible_cards=len(cards),
            at_bottom=at_bottom,
        )

        return {
            "scrolled": True,
            "direction": direction,
            "amount_px": amount_px,
            "total_px": total_px,
            "visible_cards": len(cards),
            "cards": cards,
            "at_bottom": at_bottom,
        }
    except Exception as e:
        log_event(
            main_logger,
            "scroll.execution_error",
            "ERROR",
            source="browser_intelligence",
            platform=platform,
            error=str(e),
        )
        return {"scrolled": False, "error": str(e)}
