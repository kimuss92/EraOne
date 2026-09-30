"""
Tests for actions/browser_intelligence.py — pure data functions, no live browser.
"""

import json
import sys
from pathlib import Path

__test__ = False

passed = 0
failed = 0


def check(name: str, condition: bool) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


def check_eq(name: str, actual, expected) -> None:
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} (got {actual!r}, expected {expected!r})")


from actions.browser_intelligence import (
    Card,
    SelectionResult,
    _build_cards,
    _exact_match,
    _group_by_row,
    _parse_json_result,
    _partial_match,
    _select_by_position,
    _select_by_row,
    _token_overlap_match,
    _token_overlap_score,
    choose_card,
    focus_or_open_domain,
    normalize_query,
    scroll_page,
    validate_netflix_href,
    validate_youtube_href,
)


def _card(
    title: str,
    y: int = 0,
    x: int = 0,
    index: int = 0,
    href: str = "https://youtube.com/watch?v=abc",
) -> Card:
    return Card(index=index, title=title, href=href, y=y, x=x, platform="youtube")


# ═══════════════════════════════════════════════════════════════════
# 1.  _parse_json_result — malformed JSON -> []
# ═══════════════════════════════════════════════════════════════════
print("=== _parse_json_result ===")
check("valid JSON list", _parse_json_result('[{"title":"A"}]') == [{"title": "A"}])
check("empty input -> []", _parse_json_result("") == [])
check("None -> []", _parse_json_result(None) == [])
check("malformed JSON -> []", _parse_json_result("{bad") == [])
check("Error prefix -> []", _parse_json_result("Error: no html5") == [])
check("Exception prefix -> []", _parse_json_result("Exception: timeout") == [])
check("list input passes through", _parse_json_result([{"a": 1}]) == [{"a": 1}])
check("dict input -> []", _parse_json_result({"a": 1}) == [])
check("__PROFILE_PAGE__ -> []", _parse_json_result("__PROFILE_PAGE__") == [])

# ═══════════════════════════════════════════════════════════════════
# 2.  validate_youtube_href — shorts rejected, unsafe href rejected
# ═══════════════════════════════════════════════════════════════════
print("\n=== validate_youtube_href ===")
check("valid watch URL", validate_youtube_href("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
check("shorts rejected", not validate_youtube_href("https://youtube.com/shorts/abc123"))
check("non-YouTube rejected", not validate_youtube_href("https://example.com"))
check("empty rejected", not validate_youtube_href(""))
check("None rejected", not validate_youtube_href(None))
check("non-string rejected", not validate_youtube_href(123))
check("youtu.be accepted", validate_youtube_href("https://youtu.be/dQw4w9WgXcQ"))

# ═══════════════════════════════════════════════════════════════════
# 3.  validate_netflix_href
# ═══════════════════════════════════════════════════════════════════
print("\n=== validate_netflix_href ===")
check("valid Netflix URL", validate_netflix_href("https://www.netflix.com/watch/12345"))
check("empty rejected", not validate_netflix_href(""))
check("None rejected", not validate_netflix_href(None))
check("non-string rejected", not validate_netflix_href(True))
check("non-Netflix rejected", not validate_netflix_href("https://hulu.com"))

# ═══════════════════════════════════════════════════════════════════
# 4.  _group_by_row — deterministic ordering
# ═══════════════════════════════════════════════════════════════════
print("\n=== _group_by_row ===")
cards_r = [
    _card("A", y=0, x=0),
    _card("B", y=0, x=100),
    _card("C", y=100, x=0),
    _card("D", y=100, x=50),
]
grouped = _group_by_row(cards_r)
check_eq("num cards preserved", len(grouped), 4)
check_eq("row0 col0", grouped[0].title, "A")
check_eq("row0 col1", grouped[1].title, "B")
check_eq("row1 col0", grouped[2].title, "C")
check_eq("row1 col1", grouped[3].title, "D")
check_eq("row_index correct", (grouped[0].row_index, grouped[3].row_index), (0, 1))
check_eq("col_index correct", (grouped[1].col_index, grouped[2].col_index), (1, 0))

# ═══════════════════════════════════════════════════════════════════
# 5.  _build_cards
# ═══════════════════════════════════════════════════════════════════
print("\n=== _build_cards ===")
raw = [
    {"title": "Video One", "href": "https://youtube.com/watch?v=1", "x": 10, "y": 0},
    {"title": "Video Two", "href": "https://youtube.com/watch?v=2", "x": 200, "y": 0},
]
built = _build_cards(raw, "youtube")
check_eq("builds two cards", len(built), 2)
check_eq("title 0", built[0].title, "Video One")
check_eq("href 1", built[1].href, "https://youtube.com/watch?v=2")
check_eq("platform set", built[0].platform, "youtube")
check("empty raw -> empty", _build_cards([], "youtube") == [])
check("missing title/href skipped", len(_build_cards([{"x": 0}], "youtube")) == 0)
check("non-dict skipped", len(_build_cards([None, "str"], "youtube")) == 0)

# ═══════════════════════════════════════════════════════════════════
# 6.  Title matching helpers
# ═══════════════════════════════════════════════════════════════════
print("\n=== Title matching ===")
c1 = _card("The Big Bang Theory", index=0, href="https://youtube.com/watch?v=bbt1")
c2 = _card("Breaking Bad S5 Trailer", index=1, href="https://youtube.com/watch?v=bb2")
c3 = _card("Stranger Things Recap", index=2, href="https://youtube.com/watch?v=str1")
cards = [c1, c2, c3]

check("exact match finds card", _exact_match("The Big Bang Theory", cards) == c1)
check("exact case insensitive", _exact_match("the big bang theory", cards) == c1)
check("partial match string in title", _partial_match("Bang", cards) == c1)
check("partial match title in string", _partial_match("The Big Bang Theory Extended", cards) == c1)
check("partial not found", _partial_match("XYZ", cards) is None)

check_eq("token same", _token_overlap_score("Big Bang", "Big Bang"), 1.0)
check_eq("token partial", round(_token_overlap_score("Big Bang", "Big Crash"), 2), 0.33)
check_eq("token none", _token_overlap_score("XYZ", "Big Bang"), 0.0)

scored = _token_overlap_match("Breaking", cards)
check("token match finds results", len(scored) > 0)
check("best is Breaking Bad", scored[0][0].title == "Breaking Bad S5 Trailer")

# ═══════════════════════════════════════════════════════════════════
# 7.  choose_card — exact, partial, token thresholds
# ═══════════════════════════════════════════════════════════════════
print("\n=== choose_card: confidence thresholds ===")
sel = choose_card(cards, query="The Big Bang Theory")
check_eq("exact -> selected", sel.status, "selected")
check_eq("exact -> correct card", sel.card, c1)
check_eq("exact -> confidence 1.0", sel.confidence, 1.0)

sel = choose_card(cards, query="Bang")
check_eq("partial -> selected", sel.status, "selected")
check_eq("partial -> confidence 0.85", sel.confidence, 0.85)

sel = choose_card(cards, query="Theory Big Bang")
check_eq("token >= 0.70 -> selected", sel.status, "selected")
check("token >= 0.70 -> confidence >= 0.70", sel.confidence >= 0.70)

sel = choose_card(cards, query="Big Bang Stranger Things")
check_eq("token 0.30-0.69 -> candidates", sel.status, "candidates")
check("token 0.30-0.69 -> list non-empty", len(sel.candidates) > 0)
check("token 0.30-0.69 -> confidence in range", 0.30 <= sel.confidence < 0.70)

sel = choose_card(cards, query="Bang Stranger")
check_eq("token < 0.30 -> candidates", sel.status, "candidates")
check("token < 0.30 -> list non-empty", len(sel.candidates) > 0)
check("token < 0.30 -> confidence < 0.30", sel.confidence < 0.30)

sel = choose_card(cards, query="XYZ QRS")
check_eq("no match -> not_found", sel.status, "not_found")

sel = choose_card([], query="anything")
check_eq("empty cards -> not_found", sel.status, "not_found")

# ═══════════════════════════════════════════════════════════════════
# 8.  choose_card — position selects (no low-confidence fallthrough)
# ═══════════════════════════════════════════════════════════════════
print("\n=== choose_card: position selection ===")
cards_pos = [
    _card("Alpha", y=0, x=0, index=0, href="https://youtube.com/watch?v=a"),
    _card("Beta", y=0, x=100, index=1, href="https://youtube.com/watch?v=b"),
    _card("Gamma", y=0, x=200, index=2, href="https://youtube.com/watch?v=c"),
    _card("Delta", y=100, x=0, index=3, href="https://youtube.com/watch?v=d"),
]

check_eq("first -> Alpha", choose_card(cards_pos, position="first").card.title, "Alpha")
check_eq("third -> Gamma", choose_card(cards_pos, position="third").card.title, "Gamma")
check_eq("top right -> Gamma", choose_card(cards_pos, position="top right").card.title, "Gamma")
check_eq("2 -> Beta", choose_card(cards_pos, position="2").card.title, "Beta")
check_eq(
    "invalid position -> not_found",
    choose_card(cards_pos, position="invalid_pos").status,
    "not_found",
)

# row selection
check_eq("row 0 -> Alpha", choose_card(cards_pos, row=0).card.title, "Alpha")
check_eq("row 1 -> Delta", choose_card(cards_pos, row=1).card.title, "Delta")
check_eq("bad row -> not_found", choose_card(cards_pos, row=99).status, "not_found")

# default (no query, no position)
check_eq("default first card", choose_card(cards_pos).card.title, "Alpha")

# ═══════════════════════════════════════════════════════════════════
# 9.  choose_card returns title in result reason
# ═══════════════════════════════════════════════════════════════════
print("\n=== choose_card: title in result ===")
sel = choose_card(cards, query="The Big Bang Theory")
check("selected card has title", sel.card is not None and "Big Bang" in sel.card.title)
sel = choose_card(cards_pos, position="third")
check("card title in position result", "Gamma" in sel.reason)

# ═══════════════════════════════════════════════════════════════════
# 10. Netflix row / position selection
# ═══════════════════════════════════════════════════════════════════
print("\n=== Netflix position selection ===")
netflix_cards = [
    Card(index=0, title="Naruto", href="https://netflix.com/watch/1", y=0, x=0, platform="netflix"),
    Card(
        index=1,
        title="One Piece",
        href="https://netflix.com/watch/2",
        y=0,
        x=100,
        platform="netflix",
    ),
    Card(index=2, title="AOT", href="https://netflix.com/watch/3", y=0, x=200, platform="netflix"),
    Card(
        index=3,
        title="Death Note",
        href="https://netflix.com/watch/4",
        y=100,
        x=0,
        platform="netflix",
    ),
]
check_eq("Netflix top right", choose_card(netflix_cards, position="top right").card.title, "AOT")
check_eq(
    "Netflix bottom left",
    choose_card(netflix_cards, position="bottom left").card.title,
    "Death Note",
)
check_eq(
    "Netflix title match", choose_card(netflix_cards, query="One Piece").card.title, "One Piece"
)

# ═══════════════════════════════════════════════════════════════════
# 11. scroll_page — no click (mocked no-browser)
# ═══════════════════════════════════════════════════════════════════
print("\n=== scroll_page (no-click verification) ===")
from unittest.mock import patch

with patch("actions.browser_intelligence._BROWSER_AVAILABLE", False):
    result = scroll_page()
    check("no browser -> scrolled=False", result.get("scrolled") is False)
    check("no browser -> has error", "error" in result)

    result_bad = scroll_page(platform="unknown")
    check("bad platform -> scrolled=False", result_bad.get("scrolled") is False)

    result_dir = scroll_page(direction="left")
    check("bad direction -> scrolled=False", result_dir.get("scrolled") is False)

# ═══════════════════════════════════════════════════════════════════
# 12. YouTube duplicate video IDs (dedup in JS, not _build_cards)
# ═══════════════════════════════════════════════════════════════════
print("\n=== Duplicate video ID handling ===")
dup_raw = [
    {"title": "Video A", "href": "https://youtube.com/watch?v=same", "x": 0, "y": 0},
    {"title": "Video A Dup", "href": "https://youtube.com/watch?v=same", "x": 100, "y": 0},
    {"title": "Video B", "href": "https://youtube.com/watch?v=unique", "x": 200, "y": 0},
]
built_dup = _build_cards(dup_raw, "youtube")
check("same ID cards both survive _build_cards", len(built_dup) == 3)

# ═══════════════════════════════════════════════════════════════════
# 13. normalize_query — ordinal/spatial detection
# ═══════════════════════════════════════════════════════════════════
print("\n=== normalize_query ===")
check_eq("3rd video -> position=3", normalize_query("3rd video"), ("", "3"))
check_eq("2nd video -> position=2", normalize_query("2nd video"), ("", "2"))
check_eq("1st -> position=1", normalize_query("1st"), ("", "1"))
check_eq("4th result -> position=4", normalize_query("4th result"), ("", "4"))
check_eq("video 3 -> position=3", normalize_query("video 3"), ("", "3"))
check_eq("number 2 -> position=2", normalize_query("number 2"), ("", "2"))
check_eq("item 1 -> position=1", normalize_query("item 1"), ("", "1"))
check_eq("first -> position=1", normalize_query("first"), ("", "1"))
check_eq("second -> position=2", normalize_query("second"), ("", "2"))
check_eq("third -> position=3", normalize_query("third"), ("", "3"))
check_eq("the first video -> position=1", normalize_query("the first video"), ("", "1"))
check_eq("the third one -> position=3", normalize_query("the third one"), ("", "3"))
check_eq("top right -> position=top right", normalize_query("top right"), ("", "top right"))
check_eq("bottom left -> position=bottom left", normalize_query("bottom left"), ("", "bottom left"))
check_eq("center -> position=center", normalize_query("center"), ("", "center"))
check_eq(
    "query unchanged -> no position",
    normalize_query("the big bang theory"),
    ("the big bang theory", None),
)
check_eq("empty unchanged", normalize_query(""), ("", None))
check_eq("None unchanged", normalize_query(None), (None, None))
check_eq("fifth video -> position=5", normalize_query("fifth video"), ("", "5"))
check_eq("fifth -> position=5", normalize_query("fifth"), ("", "5"))

# ═══════════════════════════════════════════════════════════════════
# 14. choose_card — low confidence does NOT select, returns candidates
# ═══════════════════════════════════════════════════════════════════
print("\n=== choose_card: low confidence = candidates (no silent select) ===")
cards_lowconf = [
    Card(
        index=0,
        title="Something Completely Different",
        href="https://youtube.com/watch?v=x",
        platform="youtube",
    ),
    Card(
        index=1,
        title="Another Unrelated Title",
        href="https://youtube.com/watch?v=y",
        platform="youtube",
    ),
]
sel_low = choose_card(cards_lowconf, query="Something Random Something Random")
check("low confidence -> candidates not selected", sel_low.status == "candidates")
check("low confidence -> no card selected", sel_low.card is None)
check("low confidence -> list non-empty", len(sel_low.candidates) > 0)

# ═══════════════════════════════════════════════════════════════════
# 15. choose_card — position selects directly (no confidence ambiguity)
# ═══════════════════════════════════════════════════════════════════
print("\n=== choose_card: position selects directly ===")
cards_pos2 = [
    Card(
        index=0,
        title="Position A",
        href="https://youtube.com/watch?v=a",
        y=0,
        x=0,
        platform="youtube",
    ),
    Card(
        index=1,
        title="Position B",
        href="https://youtube.com/watch?v=b",
        y=0,
        x=100,
        platform="youtube",
    ),
]
sel_pos = choose_card(cards_pos2, position="2")
check_eq("position=2 selects card #2", sel_pos.card.title, "Position B")
check_eq("position selected -> confidence 1.0", sel_pos.confidence, 1.0)

# ═══════════════════════════════════════════════════════════════════
# 16. choose_card default picks first card, not index #0 + 1
# ═══════════════════════════════════════════════════════════════════
print("\n=== choose_card: default first card ===")
sel_def = choose_card(cards_pos2)
check_eq("no params -> first card", sel_def.card.title, "Position A")

# ═══════════════════════════════════════════════════════════════════
# 17. scroll_page — no browser, no click (already tested above)
# ═══════════════════════════════════════════════════════════════════
print("\n=== scroll_page: no click (verified with mock) ===")
with patch("actions.browser_intelligence._BROWSER_AVAILABLE", False):
    result = scroll_page()
    check("no click on scroll", result.get("scrolled") is False)
    check("no click has error", "error" in result)
    check("dict has no 'clicked' key", "clicked" not in result)
    check("no cards key in error case", "cards" not in result)

# ═══════════════════════════════════════════════════════════════════
# 18. validate_youtube_href — shorts and unsafe href rejected
# ═══════════════════════════════════════════════════════════════════
print("\n=== validate_youtube_href: shorts and unsafe ===")
check("http accepted", validate_youtube_href("http://www.youtube.com/watch?v=abc"))
check("no protocol rejected", not validate_youtube_href("youtube.com/watch?v=abc"))
check("shorts path rejected", not validate_youtube_href("https://youtube.com/shorts/xyz"))
check("non-youtube rejected", not validate_youtube_href("https://vimeo.com/123"))

# ═══════════════════════════════════════════════════════════════════
# 19. Netflix row/col selection
# ═══════════════════════════════════════════════════════════════════
print("\n=== Netflix row/col integration ===")
netflix_multi = [
    Card(
        index=0, title="Title A", href="https://netflix.com/watch/a", y=0, x=0, platform="netflix"
    ),
    Card(
        index=1, title="Title B", href="https://netflix.com/watch/b", y=0, x=100, platform="netflix"
    ),
    Card(
        index=2, title="Title C", href="https://netflix.com/watch/c", y=100, x=0, platform="netflix"
    ),
    Card(
        index=3,
        title="Title D",
        href="https://netflix.com/watch/d",
        y=100,
        x=100,
        platform="netflix",
    ),
]
check_eq(
    "Netflix row 0 col 0", choose_card(netflix_multi, position="top left").card.title, "Title A"
)
check_eq(
    "Netflix row 1 col 1", choose_card(netflix_multi, position="bottom right").card.title, "Title D"
)
check_eq("Netflix exact title", choose_card(netflix_multi, query="Title C").card.title, "Title C")

# ═══════════════════════════════════════════════════════════════════
# 20. focus_or_open_domain — no browser returns False
# ═══════════════════════════════════════════════════════════════════
print("\n=== focus_or_open_domain: no browser ===")
with patch("actions.browser_intelligence._BROWSER_AVAILABLE", False):
    result = focus_or_open_domain("brave", "youtube.com", "https://youtube.com/")
    check("no browser -> returns False", result is False)

# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'=' * 50}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"{'=' * 50}")
if __name__ == "__main__":
    sys.exit(0 if failed == 0 else 1)
