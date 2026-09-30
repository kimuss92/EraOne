"""Netflix profile-gate and player JavaScript, built at call time.

Why this file exists separately from js_snippets.py: those snippets are fixed
strings. Everything here is *parameterised* — a profile name, a seek amount, a
volume level — so it is built by a function instead.

Two conventions every script here follows:

* **Bare body, no IIFE.** `BrowserSession.evaluate_background_media` wraps the
  script in `(() => { ... })()` before evaluating, so these use a top-level
  `return` exactly like the entries in `_JS_SCRIPTS`.
* **Sentinel phrases on failure.** That same wrapper retries the next matching
  tab when a result contains "no active media", "element not found" and
  friends, so a miss on a stale background tab falls through to the real
  player instead of being reported as the answer.

This module owns the PROFILE GATE only. Player controls (seek, volume, episode
navigation) moved to `actions/_netflix/player_api.py`, which drives Netflix's
own player API — the `<video>` element approach that used to live here did not
work, because `document.querySelector('video')` returns null on the watch page
even while playback is live.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Profile gate
# ---------------------------------------------------------------------------

# Every container Netflix has shipped the picker in. Checked as one selector
# list so a redesign that renames one of them still matches another.
PICKER_SELECTOR = (
    # Current markup (verified against the live French gate, 2026-09-16):
    # `div[data-uia="profile-gate-screen"]` holding `div[data-uia="profile-selector"]`,
    # one `<button data-uia="profile-selector+tile-<TOKEN>">` per profile.
    '[data-uia="profile-gate-screen"], [data-uia="profile-selector"], '
    # The 2026-09-15 markup was `ul.choose-profile` inside `.list-profiles`
    # with anchor cards; the rest are containers Netflix has shipped before or
    # uses in other locales.
    "ul.choose-profile, .choose-profile, .list-profiles, "
    'section[data-uia="profile-picker"], section[data-uia="profile-picker-section"], '
    ".profile-gate-container, #profileSelector, "
    ".ProfilesBillboard, .profile-management, .profile-selection, .profile-selector, "
    '[data-uia="profiles-gate-container"]'
)

# True when the profile gate is on screen. Deliberately also false when the
# picker exists but is hidden (Netflix keeps a detached copy around after
# selection), because "still displayed" is the question being asked.
DETECT_GATE_JS = f"""
const picker = document.querySelector({json.dumps(PICKER_SELECTOR)});
if (!picker) return 'no-gate';
const rect = picker.getBoundingClientRect();
const style = window.getComputedStyle(picker);
const visible = rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
return visible ? 'gate' : 'no-gate';
"""

# Prefix of every profile tile's data-uia on the current markup. The suffix is
# the profile's SwitchProfile token (see _ENUMERATE_CARDS_JS).
PROFILE_TILE_UIA_PREFIX = "profile-selector+tile-"

# Shared by the lookup and the click: enumerate the profile cards, in on-screen
# order, into `cards` as {name, el, url}. Handles both markups Netflix served:
#
#   2026-09-16  <button data-uia="profile-selector+tile-2XNV3RMVE5EZPK3C4UZKRLN5LQ">
#                 ...<span>Marwa</span></button>
#   2026-09-15  <li class="profile"><a class="profile-link"
#                 href="/SwitchProfile?tkn=2XNV3RMVE5EZPK3C4UZKRLN5LQ">
#                 <span class="profile-name">Marwa</span></a></li>
#
# The tile suffix IS the SwitchProfile token — Marwa carried the same value in
# both markups — so a button tile is followed exactly like the old anchor was.
# Only real profile tiles are listed: "Gérer les profils" / "add profile" carry
# no token or name, which keeps ordinals counting profiles and nothing else.
_ENUMERATE_CARDS_JS = """
const picker = document.querySelector(__PICKER__);
if (!picker) return 'no-gate';
const root = picker.closest('[data-uia="profile-gate-screen"]') || picker;

// Accent- and case-insensitive compare, so "gregoire" matches "Grégoire".
const fold = (s) => (s || '')
  .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
  .toLowerCase().replace(/\\s+/g, ' ').trim();
const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
const TILE = __TILE__;

const cards = [];
const tiles = Array.from(root.querySelectorAll('[data-uia^="' + TILE + '"]'));
if (tiles.length) {
  for (const el of tiles) {
    const token = (el.getAttribute('data-uia') || '').slice(TILE.length);
    if (!/^[A-Za-z0-9_-]+$/.test(token)) continue;
    const nameEl = el.querySelector('.profile-name, [data-uia="profile-name"]');
    const name = clean(nameEl ? nameEl.textContent
                              : (el.getAttribute('aria-label') || el.textContent));
    if (!name) continue;
    cards.push({name: name, el: el,
                url: new URL('/SwitchProfile?tkn=' + encodeURIComponent(token),
                             location.origin).href});
  }
} else {
  for (const li of Array.from(root.querySelectorAll('li, .profile'))) {
    const nameEl = li.querySelector('.profile-name, [data-uia="profile-name"]');
    const link = li.querySelector('a[href*="SwitchProfile"], a.profile-link, a[href]');
    const name = clean(nameEl ? nameEl.textContent
                              : ((link && link.getAttribute('aria-label')) || li.textContent));
    if (!name) continue;
    const el = link || li;
    if (cards.some((c) => c.el === el)) continue;  // li.profile matched twice
    const href = link && link.getAttribute('href');
    // Resolve against the origin so a relative href becomes absolute.
    cards.push({name: name, el: el, url: href ? new URL(href, location.origin).href : ''});
  }
}
""".replace("__PICKER__", json.dumps(PICKER_SELECTOR)).replace(
    "__TILE__", json.dumps(PROFILE_TILE_UIA_PREFIX)
)


def _select_card_js(profile_name: str | None, ordinal: int | None) -> str:
    """JS that binds `match` to the requested card, or returns not-found.

    An ordinal is an explicit user choice ("the fourth profile") and is used
    verbatim, counted in on-screen order. Otherwise matching is by name: exact
    first, then containing, so "Marwa" never resolves to "Marwa Kids" while an
    exact "Marwa" exists. There is no other fallback — selecting the wrong
    profile silently writes to somebody else's viewing history.
    """
    not_found = "return 'profile-not-found|seen=' + cards.map((c) => c.name).join(', ');"
    if ordinal is not None:
        return (
            f"\nconst idx = {int(ordinal) - 1};\n"
            "const match = (idx >= 0 && idx < cards.length) ? cards[idx] : null;\n"
            f"if (!match) {not_found}\n"
        )
    return (
        f"\nconst needle = {json.dumps(_fold(profile_name or ''))};\n"
        "let match = needle ? (cards.find((c) => fold(c.name) === needle) || null) : null;\n"
        "if (!match && needle) match = cards.find((c) => fold(c.name).includes(needle)) || null;\n"
        f"if (!match) {not_found}\n"
    )


def build_profile_lookup_js(profile_name: str | None = None, ordinal: int | None = None) -> str:
    """Find the requested profile and return its SwitchProfile URL — no clicking.

    This is the reliable path, and the reason clicking was abandoned: on the
    anchor markup Netflix's React handler consumed a scripted click and the
    anchor's default navigation never ran, so the picker just sat there.
    Navigating to the SwitchProfile URL is what choosing the card does anyway —
    no synthetic events, no trusted-gesture problem, no coordinates, nothing
    affected by window size or display scaling.

    Select by `ordinal` (1-based, on-screen order) or by `profile_name`
    (case- and accent-insensitive, so "gregoire" finds "Grégoire").

    Returns one of:
      "no-gate"
      "found|<absolute url>|<matched name>"
      "no-href|<matched name>"        (card found, but no URL to follow)
      "profile-not-found|seen=<names>"
    """
    return (
        _ENUMERATE_CARDS_JS
        + _select_card_js(profile_name, ordinal)
        + "if (!match.url) return 'no-href|' + match.name;\n"
        + "return 'found|' + match.url + '|' + match.name;\n"
    )


def build_profile_click_js(profile_name: str | None = None, ordinal: int | None = None) -> str:
    """Fallback: click the card. Used only when following the URL did not work.

    Kept because a future Netflix markup change could drop the token, but it is
    NOT the primary path — see build_profile_lookup_js for why clicking alone
    was not enough.
    """
    return (
        _ENUMERATE_CARDS_JS
        + _select_card_js(profile_name, ordinal)
        + """
const target = match.el;
target.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
const r = target.getBoundingClientRect();
const cx = Math.round(r.left + r.width / 2);
const cy = Math.round(r.top + r.height / 2);
for (const t of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
  try {
    const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
    target.dispatchEvent(new C(t, {bubbles: true, cancelable: true, clientX: cx, clientY: cy}));
  } catch (e) {}
}
if (typeof target.focus === 'function') target.focus();
target.click();
return 'clicked|' + match.name;
"""
    )


def _fold(name: str) -> str:
    """Python-side match of the JS `fold`: strip accents, lowercase, collapse."""
    import unicodedata

    decomposed = unicodedata.normalize("NFD", (name or "").strip())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())
