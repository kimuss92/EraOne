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
import unicodedata

from core.cancel_registry import raise_if_cancelled, sleep_cancellable

try:
    from pywinauto import Desktop

    _UIA = True
except Exception:
    _UIA = False

# A chrome-only window exposes ~4 buttons (min/max/close). A rendered content page
# exposes dozens. Use that gap to know the a11y tree has actually populated.
_TREE_READY_MIN_BUTTONS = 20


class _CachedButton:
    """Batch-read names/rectangles; avoid one cross-process COM call per field."""

    def __init__(self, element):
        self.element = element
        self.name = element.CachedName
        self.rect = element.CachedBoundingRectangle

    def window_text(self):
        return self.name

    def rectangle(self):
        return self.rect

    def _wrapper(self):
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo

        return UIAWrapper(UIAElementInfo(self.element))

    def invoke(self):
        return self._wrapper().iface_invoke.Invoke()

    def click_input(self):
        return self._wrapper().click_input()


def _buttons(win):
    raise_if_cancelled()
    try:
        from pywinauto.uia_defines import IUIA

        automation = IUIA().iuia
        cache = automation.CreateCacheRequest()
        cache.AddProperty(30005)  # Name
        cache.AddProperty(30001)  # BoundingRectangle
        condition = automation.CreatePropertyCondition(30003, 50000)  # Button
        elements = win.element_info.element.FindAllBuildCache(4, condition, cache)
        return [_CachedButton(elements.GetElement(i)) for i in range(elements.Length)]
    except Exception:
        pass
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
        raise_if_cancelled()
        if len(btns) >= _TREE_READY_MIN_BUTTONS:
            return btns
        sleep_cancellable(0.4)
        raise_if_cancelled()
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


def _find_named(btns, label: str):
    """The "Play <label>" / "Pause <label>" pair for a named context.

    The name proves WHICH context it is: right after a URI or hotkey navigation the
    first unnamed "Play <x>" can still belong to the page being left (2026-10-16).
    "Play Liked Songs" also sits in the library sidebar, which plays the same
    context from any page."""
    want = (label or "").strip().lower()
    play = pause = None
    for b in btns:
        try:
            n = (b.window_text() or "").strip().lower()
        except Exception:
            continue
        if n == f"play {want}" and play is None:
            play = b
        elif n == f"pause {want}" and pause is None:
            pause = b
    return play, pause


def open_library_row(hwnd: int, prefix: str, timeout_s: float = 4.0) -> bool:
    """Open a sidebar library entry ("Release Radar Playlist • Made for …") by name.

    A navigation click, not a Play: the page's own named control is pressed after."""
    if not _UIA or not hwnd:
        return False
    try:
        win = Desktop(backend="uia").window(handle=hwnd)
    except Exception:
        return False
    want = prefix.strip().lower()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for b in _buttons(win):
            try:
                n = (b.window_text() or "").strip().lower()
            except Exception:
                continue
            if n.startswith(want) and not n.startswith(("play ", "pause ")):
                for method in ("click_input", "invoke"):
                    raise_if_cancelled()
                    try:
                        getattr(b, method)()
                        return True
                    except Exception:
                        continue
                return False
        sleep_cancellable(0.25)
    return False


def invoke_primary_play(
    hwnd: int, timeout_s: float = 8.0, prefer: str | None = None
) -> tuple[bool, str]:
    """Find and invoke the primary Play control on the current page. Returns
    (ok, detail). ok=True also when the context is already playing.

    With `prefer` ("Liked Songs") ONLY the control naming that context may be
    pressed: there is no fallback to the page's first Play. That fallback is what
    pressed "Play Liked Songs" for New Releases once its shortcut stopped working,
    and the stale search result for Liked Songs while its page was still loading
    (measured 2026-10-16: the new page lands 1-2 s after the URI)."""
    if not _UIA:
        return False, "ui automation unavailable"
    if not hwnd:
        return False, "no spotify window"
    try:
        win = Desktop(backend="uia").window(handle=hwnd)
    except Exception as exc:
        return False, f"uia connect failed: {exc}"

    btns = _wait_for_tree(win, timeout_s)
    if prefer:
        deadline = time.time() + timeout_s
        play, pause = _find_named(btns, prefer)
        while play is None and pause is None and time.time() < deadline:
            sleep_cancellable(0.25)
            btns = _buttons(win)
            play, pause = _find_named(btns, prefer)
        if play is None and pause is None:
            return False, f"no '{prefer}' play control appeared"
    else:
        play, pause = _find_primary(btns)
    if play is None and pause is not None:
        return True, "already playing"
    if play is None:
        return False, "no play control in the accessibility tree"

    try:
        raise_if_cancelled()
        play.invoke()  # coordinate-free; resize-proof
        return True, "invoked"
    except Exception:
        try:
            raise_if_cancelled()
            play.click_input()  # last-resort real click on the element's own rect
            return True, "clicked"
        except Exception as exc:
            return False, f"invoke failed: {exc}"


def _norm(s: str) -> str:
    """Fold punctuation that varies between a spoken query and Spotify's titles
    ('Catch & Release' vs 'catch and release', 'X - Remix' vs 'X Remix')."""
    s = unicodedata.normalize("NFKC", s).lower().replace("&", " and ")
    s = "".join(ch for ch in s if not unicodedata.combining(ch) and ch != "ـ")
    for ch in "()[]-–—.,':\"":
        s = s.replace(ch, " ")
    return " ".join(s.split())


# Words too common in titles to prove a partial match on their own.
_GENERIC_WORDS = frozenset(
    {"music", "song", "songs", "the", "a", "an", "of", "and", "by", "feat", "ft", "official"}
)


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
    requested_title, separator, requested_artist = (query or "").lower().partition(" by ")
    title_tokens = set(_norm(requested_title).split())
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
        # With a named title and artist, a partial hit on e.g. "Music" must
        # never start an unrelated result from a stale or unrelated section.
        if separator and not title_tokens.issubset(ttokens):
            continue
        if separator and artist_raw and not set(_norm(requested_artist).split()).issubset(atokens):
            continue
        top = _btn_top(b)
        if separator and title == _norm(requested_title):
            score = (6 if artist_raw else 5, 0, -len(title))
        elif q and title == q:
            score = (5, 0, -len(title))
        elif qtokens - {"by"} and (qtokens - {"by"}).issubset(ttokens | atokens):
            score = (4, 0, -len(title))
        elif qtokens and qtokens.issubset(atokens):
            score = (3, 0, -len(title))
        elif (qtokens & ttokens) - _GENERIC_WORDS:
            # Only a DISTINCTIVE shared word counts: "Konoha Music Station" shares
            # just "music" with "سواد الغيم Mikasa Music" and got picked off the
            # previous search's stale page (2026-10-16).
            score = (2, len((qtokens & ttokens) - _GENERIC_WORDS), -len(title))
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
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        automation = IUIA().iuia
        cache = automation.CreateCacheRequest()
        cache.AddProperty(30005)
        fields = automation.CreateOrCondition(
            automation.CreatePropertyCondition(30003, 50003),
            automation.CreatePropertyCondition(30003, 50004),
        )
        elements = win.element_info.element.FindAllBuildCache(4, fields, cache)
        for index in range(elements.Length):
            element = elements.GetElement(index)
            name = (element.CachedName or "").lower()
            if "what do you want" in name or name == "search":
                return UIAWrapper(UIAElementInfo(element))
        return None
    except Exception:
        pass
    for e in win.descendants(control_type="ComboBox"):
        try:
            n = (e.window_text() or "").lower()
        except Exception:
            continue
        if "what do you want" in n or "search" in n:
            return e
    return None


def set_search_query(hwnd: int, query: str, timeout_s: float = 8.0) -> bool:
    """Set and read back literal search text, including spaces and Unicode.

    Chromium may ignore SetValue; verified, focused keyboard input then fires
    the real input events without changing the user's clipboard.
    """
    if not _UIA or not hwnd:
        return False
    try:
        win = Desktop(backend="uia").window(handle=hwnd)
    except Exception:
        return False
    _buttons(win)  # trigger the Chromium a11y build
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        raise_if_cancelled()
        box = _find_search_box(win)
        if box is not None:
            try:
                box.iface_value.SetValue(query)
                if box.iface_value.CurrentValue == query:
                    return True
            except Exception:
                pass
            try:
                from pywinauto.keyboard import send_keys

                from actions.spotify_control import _focus_spotify_window

                if not _focus_spotify_window(hwnd):
                    return False
                box.set_focus()
                if not box.element_info.element.CurrentHasKeyboardFocus:
                    return False
                raise_if_cancelled()
                literal = "".join(
                    "{" + char + "}" if char in "+^%~(){}" else char for char in query
                )
                send_keys("^a{BACKSPACE}", pause=0.001)
                send_keys(literal, with_spaces=True, pause=0.001, vk_packet=True)
                while time.time() < deadline:
                    raise_if_cancelled()
                    if box.iface_value.CurrentValue == query:
                        return True
                    sleep_cancellable(0.05)
                return False
            except Exception:
                return False
        sleep_cancellable(0.15)
    return False


def play_names(btns) -> frozenset[str]:
    """The "Play …" control names on screen — a fingerprint of the current page."""
    out = set()
    for b in btns:
        try:
            n = (b.window_text() or "").strip()
        except Exception:
            continue
        if n.lower().startswith("play "):
            out.add(n)
    return frozenset(out)


# How long to wait for the results page to replace the previous one before
# matching anyway (re-searching what is already on screen never changes it).
_PAGE_CHANGE_WAIT_S = 4.0  # measured 2026-10-16: the results page swapped in at 2.6 s


def _on_screen(btns, win):
    """Drop controls laid out below/above the window: a click there lands on
    nothing (the stale Konoha row sat at y=2043 in a ~1000 px window)."""
    try:
        r = win.rectangle()
        top, bottom = r.top, r.bottom
    except Exception:
        return btns
    keep = []
    for b in btns:
        t = _btn_top(b)
        if t == 10**9 or top <= t < bottom:
            keep.append(b)
    return keep


def invoke_search_result(
    hwnd: int,
    query: str,
    timeout_s: float = 10.0,
    prefer_click: bool = False,
    stale_names: frozenset[str] | None = None,
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
            raise_if_cancelled()
            try:
                getattr(target, method)()
                return True, how
            except Exception as exc:
                last_exc = exc
        return False, f"invoke failed: {last_exc}"

    _buttons(win)  # trigger the Chromium a11y build
    start = time.time()
    deadline = start + timeout_s
    while time.time() < deadline:
        btns = _buttons(win)
        # `stale_names`: the page as it was BEFORE navigating. Until it changes,
        # every match is a leftover of the previous search.
        if (
            stale_names
            and play_names(btns) == stale_names
            and time.time() - start < _PAGE_CHANGE_WAIT_S
        ):
            sleep_cancellable(0.15)
            continue
        target, level = _best_search_match(_on_screen(btns, win), query)
        # Only act on a real textual match (exact / all-words / overlap / substring).
        # level 0 (no shared word) means the new results are not up yet — keep
        # polling rather than grab the stale page's top result.
        if target is not None and level >= 1:
            return _do(target)
        sleep_cancellable(0.15)
    return False, "no matching result in the accessibility tree"
