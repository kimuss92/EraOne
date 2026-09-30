"""JavaScript / data snippets extracted verbatim from netflix_control.py.

Pure data only — no behavior. Re-imported by actions/netflix_control.py so every
existing name (e.g. actions.netflix_control._NETFLIX_PLAY_BUTTON_JS) is preserved.
"""

_MARWA_PROFILE_JS = r"""
// AUTONOMOUS Marwa profile select — resolution-independent, robust
(() => {
  'use strict';
  const picker = document.querySelector(
    'section[data-uia="profile-picker"], section[data-uia="profile-picker-section"], ' +
    '.profile-gate-container, #profileSelector, [data-uia="profile-selector"], ' +
    '.ProfilesBillboard, .profile-management, .profile-selection, .profile-selector'
  );
  if (!picker) return 'no-picker';
  const rawCards = Array.from(picker.querySelectorAll(
    'button, a, [role="button"], [data-uia*="profile"], [data-uia*="Profile"]'
  ));
  const profiles = rawCards.filter(b => {
    const txt = (b.textContent || '').trim();
    if (txt.length === 0) return false;
    const low = txt.toLowerCase();
    if (/close|cancel|manage|manage profiles|create profile/i.test(low)) return false;
    return true;
  });
  if (profiles.length === 0) return 'no-profile-cards';
  let bestScore = -1, bestEl = null, bestReason = '';
  for (let i = 0; i < profiles.length; i++) {
    const p = profiles[i];
    const txt = (p.textContent || '').trim().toLowerCase();
    const aria = (p.getAttribute('aria-label') || '').toLowerCase();
    const title = (p.getAttribute('title') || '').toLowerCase();
    const combined = txt + ' ' + aria + ' ' + title;
    let score = 0, reason = '';
    if (combined.indexOf('marwa') !== -1) { score += 100; reason = 'name-match'; }
    const inner = p.innerHTML || '';
    const hasGreenImg = !!p.querySelector('img[src*="green"], img[src*="#7ed000"], img[src*="green-icon"], img[src*="marwa"]');
    const hasGreenClass = /\bgreen\b/i.test(p.className) || /\bgreen\b/i.test(p.id || '');
    const hasGreenSvg = !!p.querySelector('svg[fill*="#7ed000"], svg[fill*="green"], svg[fill*="#4CAF50"], svg[fill*="#66BB6A"]');
    const greenStyle = (p.getAttribute('style') || '').toLowerCase();
    const hasGreenStyle = greenStyle.indexOf('green') !== -1 || greenStyle.indexOf('#7ed') !== -1;
    const greenMarker = /green/i.test(inner) && !!p.querySelector('img, svg');
    if (hasGreenImg || hasGreenClass || hasGreenSvg || hasGreenStyle || greenMarker) {
      score += 25; reason = reason ? reason + '+green' : 'green-visual';
    }
    if (score > bestScore) { bestScore = score; bestEl = p; bestReason = reason || 'fallback'; }
  }
  if (bestEl === null || bestScore <= 0) {
    for (let i = 0; i < profiles.length; i++) {
      const p = profiles[i];
      if ((p.textContent || '').trim().toLowerCase().indexOf('marwa') !== -1) {
        bestEl = p; bestReason = 'name-found-in-rescan'; bestScore = 100; break;
      }
    }
  }
  if (bestEl) {
    const rect = bestEl.getBoundingClientRect();
    const cx = Math.round(rect.left + rect.width / 2);
    const cy = Math.round(rect.top + rect.height / 2);
    bestEl.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    bestEl.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: cx, clientY: cy }));
    bestEl.dispatchEvent(new MouseEvent('mousedown',  { bubbles: true, clientX: cx, clientY: cy }));
    bestEl.click();
    bestEl.dispatchEvent(new MouseEvent('mouseup',    { bubbles: true, clientX: cx, clientY: cy }));
    return 'marwa-selected-' + bestReason + '|count=' + profiles.length + '|cx=' + cx + '|cy=' + cy;
  }
  if (profiles.length > 0) {
    const p = profiles[0];
    const rect = p.getBoundingClientRect();
    const cx = Math.round(rect.left + rect.width / 2);
    const cy = Math.round(rect.top + rect.height / 2);
    p.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    p.click();
    return 'marwa-fallback-first-profile|count=' + profiles.length + '|cx=' + cx + '|cy=' + cy;
  }
  return 'profiles=' + profiles.length + '|no-match';
})();
"""

_NETFLIX_VISIBLE_GRID_JS = r"""
(() => {
  var BAD_TOKENS = [
    'privacy','confidentialit','confidentialité','preferences','préférences','pr',
    'cookie','cookies','legal','footer','help','aide',
    'account','profile','notification','navigation',
    'settings','param','botlink','bot-link','loc',
    'gpc','opt-out','optout','consent','gdpr',
    'privacy-center','preference-center','privacycenter',
    'preferencecenter','manage-cookies','cookiepolicy',
    'cookie-policy','terms','cgu','mentions-legales'
  ];
  function hasBadToken(el) {
    var parts = [
      el.id || '',
      el.className || '',
      el.getAttribute('data-uia') || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('href') || ''
    ].join(' ').toLowerCase();
    var txt = (el.textContent || '').trim().toLowerCase();
    if (txt.length < 120) parts += ' ' + txt;
    for (var bi = 0; bi < BAD_TOKENS.length; bi++) {
      if (parts.indexOf(BAD_TOKENS[bi]) >= 0) return true;
    }
    return false;
  }
  function isBadChrome(el) {
    if (el.closest(
      'header, nav, footer, '
      + '[data-uia*="navigation"], [data-uia*="notification"], '
      + '[data-uia*="profile"], [data-uia*="account"], '
      + '[data-uia*="search-box"], [data-uia="PIN-entry"], '
      + 'input, [class*="pinning-header"], [class*="Notification"], '
      + '[role="banner"], [role="navigation"], [role="contentinfo"], '
      + 'button[class*="close"], [aria-label*="Close"], [aria-label*="Fermer"]'
    )) return true;
    return hasBadToken(el);
  }
  function isVisible(el, r) {
    if (r.width < 80 || r.height < 60) return false;
    if (r.top > window.innerHeight || r.bottom < 0) return false;
    if (r.left > window.innerWidth || r.right < 0) return false;
    if (r.width > window.innerWidth * 0.85) return false;
    var cur = el;
    while (cur && cur !== document.body) {
      var s = window.getComputedStyle(cur);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
      cur = cur.parentElement;
    }
    return true;
  }
  function hasVisualSignal(el) {
    var bg = window.getComputedStyle(el).backgroundImage;
    var img = el.tagName === 'IMG' || !!el.querySelector('img');
    var aria = el.getAttribute('aria-label') || el.getAttribute('title');
    var clickable = el.closest('a, button, [role="button"], [tabindex]');
    return !!(bg && bg !== 'none' || img || aria || clickable);
  }
  var seen = {};
  var raw = [];
  var allEls = document.querySelectorAll(
    'a[href*="/title/"], a[href*="/watch/"], '
    + '[data-uia="title-card"], [class*="title-card"], '
    + '[class*="slider"] a, .lolomoRow a, [class*="card"] a, '
    + 'main a, [role="main"] a, .mainView a, .search-results a, '
    + 'a, button, [role="button"], [tabindex], img'
  );
  for (var i = 0; i < allEls.length; i++) {
    var el = allEls[i];
    if (isBadChrome(el)) continue;
    var card = el.closest('a, button, [role="button"], [tabindex], [data-uia]') || el;
    var r = card.getBoundingClientRect();
    if (!isVisible(card, r)) continue;
    if (!hasVisualSignal(card)) continue;
    var key = Math.round(r.top / 30) + '_' + Math.round(r.left / 30) + '_' + Math.round(r.width / 30);
    if (seen[key]) continue;
    seen[key] = true;
    var href = '';
    var linkEl = card.closest('a') || (card.tagName === 'A' ? card : null);
    if (linkEl && linkEl.href) href = linkEl.href;
    var title = (card.getAttribute('aria-label') || card.getAttribute('title') || '').trim();
    if (!title) {
      var imgEl = card.querySelector('img[alt]');
      if (imgEl) title = (imgEl.getAttribute('alt') || '').trim();
    }
    if (!title) {
      var txt = (card.textContent || '').trim();
      if (txt.length > 1 && txt.length < 120) title = txt;
    }
    raw.push({
      title: title.slice(0, 120),
      href: href,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height)
    });
  }
  if (raw.length === 0) return JSON.stringify({rows: [], count: 0});
  raw.sort(function(a, b) { return a.y !== b.y ? a.y - b.y : a.x - b.x; });
  var ROW_GAP = 50;
  var rows = [];
  var curRow = [raw[0]];
  var curRowY = raw[0].y;
  for (var j = 1; j < raw.length; j++) {
    if (Math.abs(raw[j].y - curRowY) <= ROW_GAP) {
      curRow.push(raw[j]);
    } else {
      rows.push(curRow);
      curRow = [raw[j]];
      curRowY = raw[j].y;
    }
  }
  rows.push(curRow);
  var grid = [];
  for (var ri = 0; ri < rows.length; ri++) {
    rows[ri].sort(function(a, b) { return a.x - b.x; });
    for (var ci = 0; ci < rows[ri].length; ci++) {
      var item = rows[ri][ci];
      grid.push({
        row: ri + 1,
        col: ci + 1,
        title: item.title,
        href: item.href,
        x: item.x,
        y: item.y,
        w: item.w,
        h: item.h
      });
    }
  }
  return JSON.stringify({rows: grid, count: grid.length, rowCount: rows.length});
})()
"""

_NETFLIX_PLAY_BUTTON_JS = """
() => {
    const selectors = [
        'a[aria-label*="play" i]', 'a[aria-label*="lire" i]',
        'a[aria-label*="resume" i]', 'a[aria-label*="lecture" i]',
        'a[aria-label*="reprendre" i]', 'a[aria-label*="continuer" i]',
        'button[aria-label*="play" i]', 'button[aria-label*="lire" i]',
        'button[aria-label*="resume" i]', 'button[aria-label*="lecture" i]',
        'button[aria-label*="reprendre" i]', 'button[aria-label*="continuer" i]',
        'button[aria-label*="watch" i]',
        '.play-button', '.playLink', 'a.playLink',
        'button[data-uia*="play"]', 'a[data-uia*="play"]',
        '[data-uia*="resume"]',
        '[data-uia="action-play-pretitle"]', '[data-uia="action-play-title"]',
        'a[href*="/watch"]', '[data-uia="play"]',
        '.btn-play',
        '.billboard a.playLink', '.billboard button.color-primary',
        '[data-uia="action-select-profile"] ~ a[href*="/watch"]',
    ].join(',');
    const all = document.querySelectorAll(selectors);
    for (const el of all) {
        const rect = el.getBoundingClientRect();
        if (rect.width > 10 && rect.height > 10 && el.offsetParent !== null) {
            return JSON.stringify({ found: true, tag: el.tagName, text: (el.textContent || '').trim().slice(0, 40),
                x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2),
                href: el.href || '' });
        }
    }

    // Fallback: visible-text label (Play/Resume/Lire/Reprendre/Continuer) on a
    // span/svg/icon element with no aria-label or data-uia, climbed to the
    // nearest clickable ancestor (button, a, [role="button"], [tabindex]).
    const labels = ['play', 'resume', 'lire', 'reprendre', 'continuer'];
    const textCandidates = document.querySelectorAll('span, svg, div, p, a, button');
    for (const node of textCandidates) {
        let ownText = '';
        for (const child of node.childNodes) {
            if (child.nodeType === 3) { ownText += child.textContent; }
        }
        const t = ownText.trim().toLowerCase();
        if (!t || labels.indexOf(t) === -1) continue;
        const clickable = node.closest('button, a, [role="button"], [tabindex]') || node;
        const rect = clickable.getBoundingClientRect();
        if (rect.width > 10 && rect.height > 10 && clickable.offsetParent !== null) {
            return JSON.stringify({ found: true, tag: clickable.tagName, text: t.slice(0, 40),
                x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2),
                href: clickable.href || '' });
        }
    }

    return JSON.stringify({ found: false });
}
"""

_SIMPLE_MODE_CANDIDATE_JS = r"""
(() => {
    /* Collect all potentially-interesting clickable elements, starting with
       /title/ and /watch/ anchors, then broadening to buttons and focusable
       elements.  Reject anything that lives inside Netflix chrome (header,
       nav, notification, account, profile). */
    var links = [];
    var seen = new Set();

    var primarySelectors = [
        'a[href*="/title/"]',
        'a[href*="/watch/"]',
        'a[href*="jbv="]',
    ];
    var secondarySelectors = [
        'button',
        '[role="button"]',
        '[tabindex]',
    ];

    function cleanText(s) {
        return String(s || '').replace(/\\s+/g, ' ').trim();
    }

    function isForbidden(el) {
    var cur = el;
    while (cur && cur !== document.body) {
        var tag = String(cur.tagName || '').toLowerCase();
        var data = String(cur.getAttribute('data-uia') || '').toLowerCase();
        var cls = String(cur.className || '').toLowerCase();
        var role = String(cur.getAttribute('role') || '').toLowerCase();
        var aria = String(cur.getAttribute('aria-label') || '').toLowerCase();
        var href = String(cur.getAttribute('href') || '').toLowerCase();
        var badParts = data + ' ' + cls + ' ' + role + ' ' + aria + ' ' + href;
        var txt = (cur.textContent || '').trim().toLowerCase();
        if (txt.length < 120) badParts += ' ' + txt;

        if (tag === 'header' || tag === 'nav' || tag === 'footer' ||
            role === 'dialog' ||
            badParts.indexOf('privacy') >= 0 ||
            badParts.indexOf('confidentialit') >= 0 ||
            badParts.indexOf('preferences') >= 0 ||
            badParts.indexOf('préférences') >= 0 ||
            badParts.indexOf('cookie') >= 0 ||
            badParts.indexOf('legal') >= 0 ||
            badParts.indexOf('footer') >= 0 ||
            badParts.indexOf('help') >= 0 ||
            badParts.indexOf('account') >= 0 ||
            badParts.indexOf('navigation') >= 0 ||
            badParts.indexOf('notifications') >= 0 ||
            badParts.indexOf('profile') >= 0 ||
            badParts.indexOf('notification') >= 0 ||
            badParts.indexOf('billboard') >= 0 ||
            badParts.indexOf('hero') >= 0 ||
            badParts.indexOf('jumbotron') >= 0 ||
            badParts.indexOf('featured') >= 0 ||
            badParts.indexOf('navigation') >= 0 ||
            badParts.indexOf('pinning-header') >= 0 ||
            badParts.indexOf('modal') >= 0 ||
            badParts.indexOf('overlay') >= 0) {
            return true;
        }
        cur = cur.parentElement;
    }
    return false;
}

    function extractText(el) {
        var aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
        if (aria) return cleanText(aria);
        var title = (el.getAttribute && el.getAttribute('title')) || '';
        if (title) return cleanText(title);
        var alt = (el.getAttribute && el.getAttribute('alt')) || '';
        if (alt) return cleanText(alt);
        var img = el.querySelector && el.querySelector('img[alt]');
        if (img) {
            alt = img.getAttribute('alt') || '';
            if (alt) return cleanText(alt);
        }
        var raw = (el.textContent || '').trim().split(/\\n/)[0];
        return cleanText(raw);
    }

    function getHref(el) {
    // If the element itself is an anchor, use its href directly
    if (el.tagName && el.tagName.toLowerCase() === 'a' && el.href) {
        return el.href;
    }

    // For non-anchor elements, look for a CHILD link (not ancestor!)
    // This ensures the href belongs to the same logical card as the text
    var childLink = el.querySelector &&
        el.querySelector('a[href*="/title/"], a[href*="/watch/"]');
    if (childLink) return childLink.href || '';

    // Climb ancestors for a Netflix-specific link, depth-limited to avoid
    // grabbing an unrelated distant ancestor's href.
    var cur = el.parentElement;
    var depth = 0;
    while (cur && depth < 3) {
        if (cur.tagName && cur.tagName.toLowerCase() === 'a') {
            var h = cur.getAttribute('href') || '';
            if (h) {
                // Normalize relative hrefs to absolute Netflix URLs
                if (h.charAt(0) === '/') {
                    h = 'https://www.netflix.com' + h;
                }
                // Only accept Netflix title/watch hrefs
                if (h.indexOf('/title/') >= 0 || h.indexOf('/watch/') >= 0) {
                    return h;
                }
            }
        }
        cur = cur.parentElement;
        depth++;
    }

    return '';
}

    function processNodes(selectors) {
        for (var si = 0; si < selectors.length; si++) {
            var nodes = [];
            try { nodes = Array.from(document.querySelectorAll(selectors[si])); } catch(e) { continue; }
            for (var ni = 0; ni < nodes.length; ni++) {
                var node = nodes[ni];
                if (isForbidden(node)) continue;
                var text = extractText(node);
                if (!text || text.length < 2) continue;
                var href = getHref(node);
                var key = text.toLowerCase() + '|' + href;
                if (seen.has(key)) continue;
                seen.add(key);
                var rect = {};
                try { var r = node.getBoundingClientRect(); rect = {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)}; } catch(e) { rect = {x:0,y:0,w:0,h:0}; }
                links.push({text: text, href: href, rect: rect});
            }
        }
    }

    processNodes(primarySelectors);
    processNodes(secondarySelectors);

    return JSON.stringify(links);
})()
"""

_SIMPLE_MODE_TITLE_MATCH_JS = r"""
((args) => {
    var candidates = args.candidates;
    var query = args.query;
    function norm(s) {
        return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
    }

    var qNorm = norm(query);
    var qTokens = qNorm.split(' ').filter(function(t) { return t.length >= 2; });
    var isOneWord = qTokens.length <= 1;

    var scored = [];
    for (var i = 0; i < candidates.length; i++) {
        var c = candidates[i];
        var tNorm = norm(c.text);
        var score = 0;
        var reason = '';

        if (tNorm === qNorm) {
            score = 1.0;
            reason = 'exact';
        } else if (isOneWord) {
            // One-word queries: only exact match is autoplay-safe.
            // Partial contains is listed but scored below autoplay threshold.
            if (tNorm.indexOf(qNorm) >= 0) {
                score = 0.5;
                reason = 'partial_one_word';
            }
        } else {
            // Multi-word queries: title must start with full query for autoplay.
            var sepRe = /[\s:;,.\-\/]/;
            var qEnd = qNorm.length;
            var charAfter = tNorm.length > qEnd ? tNorm[qEnd] : '';
            var startsWithQuery = tNorm.indexOf(qNorm) === 0;
            var followedBySepOrEnd = startsWithQuery && (tNorm.length === qEnd || sepRe.test(charAfter));

            if (followedBySepOrEnd) {
                score = 0.9;
                reason = 'startswith_query';
            } else {
                // all_tokens only if title starts with first query token
                var tTokens = tNorm.split(' ');
                var matched = 0;
                for (var qi = 0; qi < qTokens.length; qi++) {
                    for (var ti = 0; ti < tTokens.length; ti++) {
                        if (tTokens[ti].indexOf(qTokens[qi]) >= 0 || qTokens[qi].indexOf(tTokens[ti]) >= 0) {
                            matched++;
                            break;
                        }
                    }
                }
                if (qTokens.length > 0 && matched === qTokens.length) {
                    // Check if title starts with the first query token
                    if (tTokens.length > 0 && tTokens[0].indexOf(qTokens[0]) >= 0) {
                        score = 0.7;
                        reason = 'all_tokens';
                    } else {
                        // Query appears as suffix/middle — list only, not autoplay
                        score = 0.5;
                        reason = 'all_tokens_suffix';
                    }
                } else if (matched > 0) {
                    score = 0.3 * (matched / qTokens.length);
                    reason = 'partial_tokens';
                }
            }
        }

        if (score > 0) {
            scored.push({text: c.text, href: c.href, score: score, reason: reason, rect: c.rect});
        }
    }

    scored.sort(function(a, b) {
        if (b.score !== a.score) return b.score - a.score;
        if (a.rect.y !== b.rect.y) return a.rect.y - b.rect.y;
        return a.rect.x - b.rect.x;
    });

    return JSON.stringify(scored);
})
"""

_FAST_SEARCH_READY_JS = r"""
(() => {
    var url = window.location.href || '';
    if (url.indexOf('/search') === -1 || url.indexOf('?q=') === -1) {
        return JSON.stringify({ready: false, urlOk: false});
    }
    var links = document.querySelectorAll('a[href*="/title/"], a[href*="/watch/"], a[href*="jbv="]');
    for (var i = 0; i < links.length; i++) {
        var a = links[i];
        var r = a.getBoundingClientRect();
        if (r.width <= 50 || r.height <= 50) continue;
        var cur = a;
        var forbidden = false;
        while (cur && cur !== document.body) {
            var d = String(cur.getAttribute && cur.getAttribute('data-uia') || '').toLowerCase();
            var c = String(cur.className || '').toLowerCase();
            var t = String(cur.tagName || '').toLowerCase();
            if (t === 'header' || t === 'nav' ||
                d.indexOf('navigation') >= 0 || d.indexOf('notifications') >= 0 ||
                d.indexOf('account') >= 0 || d.indexOf('profile') >= 0 ||
                c.indexOf('notification') >= 0 || c.indexOf('navigation') >= 0 ||
                c.indexOf('pinning-header') >= 0 || c.indexOf('account') >= 0) {
                forbidden = true; break;
            }
            cur = cur.parentElement;
        }
        if (forbidden) continue;
        var txt = (a.textContent || '').trim();
        var img = a.querySelector('img[alt]');
        if (txt.length > 1 || (img && img.getAttribute('alt'))) {
            return JSON.stringify({ready: true, urlOk: true});
        }
    }
    return JSON.stringify({ready: false, urlOk: true});
})()
"""
