# actions/spotify_ui.py
"""Resize-proof Spotify content playback via UI Automation (accessibility tree).

Replaces the old pixel-ratio clicks (which broke whenever Spotify changed or
resized its layout) with UIA: navigate to a page by URI, then INVOKE the page's
primary "Play" control by its accessibility name — no coordinates, so window
size and control position are irrelevant.

The catch this module handles: Spotify is Chromium-based and does not expose its
web UI to the accessibility tree until an assistive-tech client asks for it. The
first `descendants()` call triggers Chromium to build the tree ASYNCHRONOUSLY, so
we poll until it is populated before looking for the button.

Window-automation lane; never touches the dormant Spicetify bridge.
"""

from __future__ import annotations

import time

try:
    from pywinauto import Desktop

    _UIA = True
except Exception:
    _UIA = False

# A chrome-only window exposes ~4 buttons (min/max/close). A rendered content page
# exposes dozens. Use that gap to know the a11y tree has actually populated.
_TREE_READY_MIN_BUTTONS = 20


def _buttons(win):
    try:
        return win.descendants(control_type="Button")
    except Exception:
        return []


def _wait_for_tree(win, timeout_s: float = 8.0):
    """Poll until Chromium has built the accessibility tree (or timeout). The
    first call is the trigger; later calls see the growing tree."""
    deadline = time.time() + timeout_s
    btns = _buttons(win)  # trigger a11y build
    while time.time() < deadline:
        if len(btns) >= _TREE_READY_MIN_BUTTONS:
            return btns
        time.sleep(0.4)
        btns = _buttons(win)
    return btns


def _find_primary(btns):
    """The page's primary context control. Its accessibility name is
    "Play <context>" (e.g. "Play Liked Songs") when stopped, "Pause <context>"
    when already playing. Per-track buttons are "Play <track> by <artist>" and
    are excluded via the " by " marker; the bare transport "Pause" is excluded by
    requiring a trailing context word."""
    play = pause = None
    for b in btns:
        try:
            n = (b.window_text() or "").strip().lower()
        except Exception:
            continue
        if " by " in n:
            continue
        if n.startswith("play ") and play is None:
            play = b
        elif n.startswith("pause ") and pause is None:
            pause = b
    return play, pause


def invoke_primary_play(hwnd: int, timeout_s: float = 8.0) -> tuple[bool, str]:
    """Find and invoke the primary Play control on the current page. Returns
    (ok, detail). ok=True also when the context is already playing."""
    if not _UIA:
        return False, "ui automation unavailable"
    if not hwnd:
        return False, "no spotify window"
    try:
        win = Desktop(backend="uia").window(handle=hwnd)
    except Exception as exc:
        return False, f"uia connect failed: {exc}"

    btns = _wait_for_tree(win, timeout_s)
    play, pause = _find_primary(btns)
    if play is None and pause is not None:
        return True, "already playing"
    if play is None:
        return False, "no play control in the accessibility tree"

    try:
        play.invoke()  # coordinate-free; resize-proof
        return True, "invoked"
    except Exception:
        try:
            play.click_input()  # last-resort real click on the element's own rect
            return True, "clicked"
        except Exception as exc:
            return False, f"invoke failed: {exc}"


def _norm(s: str) -> str:
    """Fold punctuation that varies between a spoken query and Spotify's titles
    ('Catch & Release' vs 'catch and release', 'X - Remix' vs 'X Remix')."""
    s = s.lower().replace("&", " and ")
    for ch in "()[]-–—.,':\"":
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _btn_top(b) -> int:
    try:
        return b.rectangle().top
    except Exception:
        return 10**9


def _best_search_match(btns, query: str):
    """Pick the result to play for `query` from the "Play <title>" buttons.

    Scoring, best first: exact title match > title contains every query word >
    the ARTIST ("Play <title> by <artist>") contains every query word > partial
    title overlap (most shared words wins) > substring. The artist tier is what
    makes "Snoop Dogg" work: every track row reads "Play <song> by Snoop Dogg",
    and stripping the " by " part left an artist query with no match unless the
    artist card itself had rendered. If nothing shares a word (a
    vibe/genre query like "chill music", whose top result is a playlist named
    "Chill Hits"), fall back to the TOP-of-screen result — Spotify orders the best
    hit first, so that is what the user means. Ties break toward the shorter title
    (plain track over "... - Live at ...") then the higher-on-screen result."""
    q = _norm(query or "")
    qtokens = set(q.split())
    plays = []  # (score_tuple, top, button)
    for b in btns:
        try:
            n = (b.window_text() or "").strip()
        except Exception:
            continue
        if not n.lower().startswith("play "):
            continue
        title_raw, _, artist_raw = n[5:].partition(" by ")  # strip "Play ", split artist
        title = _norm(title_raw)
        ttokens = set(title.split())
        atokens = set(_norm(artist_raw).split())
        top = _btn_top(b)
        if q and title == q:
            score = (5, 0, -len(title))
        elif qtokens and qtokens.issubset(ttokens):
            score = (4, 0, -len(title))
        elif qtokens and qtokens.issubset(atokens):
            score = (3, 0, -len(title))
        elif qtokens and (qtokens & ttokens):
            score = (2, len(qtokens & ttokens), -len(title))
        elif q and q in title:
            score = (1, 0, -len(title))
        else:
            score = (0, 0, 0)  # no textual signal — eligible only for top-result fallback
        plays.append((score, top, b))

    if not plays:
        return None, -1
    best = max(plays, key=lambda x: (x[0], -x[1]))
    level = best[0][0]
    if level == 0:
        # No result shares a word with the query. In practice this means the new
        # results have not rendered yet (the page still shows the previous query),
        # so return no match and let the caller keep polling — grabbing a "top
        # result" here plays the STALE page's hit (that is how "viva la vida" once
        # played a leftover track). A real search almost always yields a word
        # overlap ("chill music" -> "Chill Hits"); if it genuinely never does, a
        # clean miss beats the wrong song.
        return None, 0
    return best[2], level


def _find_search_box(win):
    """Spotify's search field — the combobox labelled 'What do you want to play?'."""
    for e in win.descendants(control_type="ComboBox"):
        try:
            n = (e.window_text() or "").lower()
        except Exception:
            continue
        if "what do you want" in n or "search" in n:
            return e
    return None


def set_search_query(hwnd: int, query: str, timeout_s: float = 8.0) -> bool:
    """Enter `query` into Spotify's search field via UIA SetValue.

    Focus-independent (writes straight to the element, so it does not depend on the
    OS foreground window the way keystrokes do) and shows clean text in the box
    ("chill music", not "chill+music"). Chromium fires the input event on SetValue,
    so Spotify's live search reacts. Returns True on success."""
    if not _UIA or not hwnd:
        return False
    try:
        win = Desktop(backend="uia").window(handle=hwnd)
    except Exception:
        return False
    _buttons(win)  # trigger the Chromium a11y build
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        box = _find_search_box(win)
        if box is not None:
            try:
                box.iface_value.SetValue(query)
                return True
            except Exception:
                return False
        time.sleep(0.4)
    return False


def invoke_search_result(
    hwnd: int, query: str, timeout_s: float = 10.0, prefer_click: bool = False
) -> tuple[bool, str]:
    """Play the search result best matching `query` by invoking its Play control
    from the accessibility tree — no coordinates. Polls until a matching result
    appears, because navigation to the results page and the Chromium a11y build
    are both asynchronous (a single-shot check races the stale/old page).

    `prefer_click` does a real click on the element's own rect first. Spotify's
    per-row Play buttons are hover-revealed, and UIA invoke() on them can return
    cleanly while doing nothing — the caller retries with this after verifying
    that the first attempt did not change the track."""
    if not _UIA:
        return False, "ui automation unavailable"
    if not hwnd:
        return False, "no spotify window"
    try:
        win = Desktop(backend="uia").window(handle=hwnd)
    except Exception as exc:
        return False, f"uia connect failed: {exc}"

    def _do(target):
        steps = [("clicked", "click_input"), ("invoked", "invoke")]
        if not prefer_click:
            steps.reverse()
        last_exc = None
        for how, method in steps:
            try:
                getattr(target, method)()
                return True, how
            except Exception as exc:
                last_exc = exc
        return False, f"invoke failed: {last_exc}"

    _buttons(win)  # trigger the Chromium a11y build
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        target, level = _best_search_match(_buttons(win), query)
        # Only act on a real textual match (exact / all-words / overlap / substring).
        # level 0 (no shared word) means the new results are not up yet — keep
        # polling rather than grab the stale page's top result.
        if target is not None and level >= 1:
            return _do(target)
        time.sleep(0.5)
    return False, "no matching result in the accessibility tree"
