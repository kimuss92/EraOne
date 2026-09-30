"""
Tests for domain-family validation functions in actions/browser_control.py.

Pure unit tests — no live browser, no network calls.
"""

import sys
from pathlib import Path

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


from actions.browser_control import (
    _DOMAIN_FAMILIES,
    _domain_family_matches,
    _domain_matches,
    _expected_domain_family_from_url,
)

# ── _domain_matches tests ─────────────────────────────────────────────────────


def test_domain_matches() -> None:
    # YouTube direct
    check(
        "youtube.com https://www.youtube.com/watch?v=x",
        _domain_matches("https://www.youtube.com/watch?v=x", "youtube.com"),
    )
    check(
        "youtube.com https://m.youtube.com/watch?v=x",
        _domain_matches("https://m.youtube.com/watch?v=x", "youtube.com"),
    )
    check("youtube.com https://youtube.com", _domain_matches("https://youtube.com", "youtube.com"))
    check("youtube.com http://youtube.com", _domain_matches("http://youtube.com", "youtube.com"))

    # Phishing / wrong domain rejects
    check(
        "youtube.com.fake.com rejected",
        not _domain_matches("https://youtube.com.fake.com/page", "youtube.com"),
    )
    check(
        "fake-youtube.com rejected", not _domain_matches("https://fake-youtube.com", "youtube.com")
    )
    check("youtubexyz.com rejected", not _domain_matches("https://youtubexyz.com", "youtube.com"))
    check(
        "facebook.com rejected for youtube",
        not _domain_matches("https://facebook.com", "youtube.com"),
    )

    # youtu.be is NOT a match for _domain_matches (different domain)
    check(
        "youtu.be not matched as youtube.com directly",
        not _domain_matches("https://youtu.be/abc123", "youtube.com"),
    )

    # Netflix
    check(
        "netflix.com https://www.netflix.com/browse",
        _domain_matches("https://www.netflix.com/browse", "netflix.com"),
    )
    check("netflix.com https://netflix.com", _domain_matches("https://netflix.com", "netflix.com"))
    check(
        "netflix.com.fake.com rejected",
        not _domain_matches("https://netflix.com.fake.com", "netflix.com"),
    )
    check(
        "fake-netflix.com rejected", not _domain_matches("https://fake-netflix.com", "netflix.com")
    )

    # Edge cases
    check("empty url rejected", not _domain_matches("", "youtube.com"))
    check("about:blank rejected", not _domain_matches("about:blank", "youtube.com"))
    check("no scheme handled", not _domain_matches("www.youtube.com", "youtube.com"))


# ── _domain_family_matches tests ──────────────────────────────────────────────


def test_domain_family_matches() -> None:
    # YouTube family (includes youtu.be)
    check(
        "yt family https://www.youtube.com/watch?v=x",
        _domain_family_matches("https://www.youtube.com/watch?v=x", "youtube.com"),
    )
    check(
        "yt family https://m.youtube.com/feed",
        _domain_family_matches("https://m.youtube.com/feed", "youtube.com"),
    )
    check(
        "yt family https://youtube.com",
        _domain_family_matches("https://youtube.com", "youtube.com"),
    )
    check(
        "yt family https://youtu.be/abc123",
        _domain_family_matches("https://youtu.be/abc123", "youtube.com"),
    )

    # YouTube family rejects
    check(
        "yt family youtube.com.fake.com rejected",
        not _domain_family_matches("https://youtube.com.fake.com/foo", "youtube.com"),
    )
    check(
        "yt family fake-youtube.com rejected",
        not _domain_family_matches("https://fake-youtube.com", "youtube.com"),
    )
    check(
        "yt family facebook.com rejected",
        not _domain_family_matches("https://facebook.com/something", "youtube.com"),
    )

    # Netflix family
    check(
        "nf family https://www.netflix.com/browse",
        _domain_family_matches("https://www.netflix.com/browse", "netflix.com"),
    )
    check(
        "nf family https://netflix.com",
        _domain_family_matches("https://netflix.com", "netflix.com"),
    )
    check(
        "nf family netflix.com.fake.com rejected",
        not _domain_family_matches("https://netflix.com.fake.com/foo", "netflix.com"),
    )
    check(
        "nf family fake-netflix.com rejected",
        not _domain_family_matches("https://fake-netflix.com", "netflix.com"),
    )


# ── _expected_domain_family_from_url tests ────────────────────────────────────


def test_expected_domain_family_from_url() -> None:
    check_eq(
        "yt url returns youtube.com",
        _expected_domain_family_from_url("https://www.youtube.com/watch?v=x"),
        "youtube.com",
    )
    check_eq(
        "youtu.be url returns youtube.com",
        _expected_domain_family_from_url("https://youtu.be/abc123"),
        "youtube.com",
    )
    check_eq(
        "netflix url returns netflix.com",
        _expected_domain_family_from_url("https://www.netflix.com/browse"),
        "netflix.com",
    )
    check_eq(
        "facebook url returns None", _expected_domain_family_from_url("https://facebook.com"), None
    )
    check_eq(
        "google url returns None", _expected_domain_family_from_url("https://google.com"), None
    )


# ── _find_tab_by_keywords tests (mocked pages) ────────────────────────────────


def make_mock_page(url: str, title: str = "", closed: bool = False):
    class MockPage:
        def is_closed(self):
            return closed

        async def title(self):
            return title

        @property
        def url(self):
            return url

    return MockPage()


async def _mock_fresh_cdp_rescan(self) -> None:
    pass


async def _mock_launch(self) -> None:
    pass


def make_mock_session(pages: list) -> object:
    import asyncio

    from actions.browser_control import _BrowserSession

    sess = _BrowserSession.__new__(_BrowserSession)
    sess._pages = pages
    sess._active = 0
    sess._context = None
    sess._fresh_cdp_rescan = _mock_fresh_cdp_rescan.__get__(sess, _BrowserSession)
    sess._launch = _mock_launch.__get__(sess, _BrowserSession)
    return sess


async def _find_tab_correct_tab_async() -> None:
    sess = make_mock_session(
        [
            make_mock_page("https://facebook.com", "Facebook - YouTube Video"),
            make_mock_page("https://www.youtube.com/watch?v=abc", "YouTube - My Video"),
        ]
    )
    result = await sess._find_tab_by_keywords(["youtube.com", "youtu.be"])
    check(
        "yt keywords picks www.youtube.com over facebook with yt in title",
        result is not None and "youtube.com" in (result.url or ""),
    )


async def _find_tab_prefers_url_async() -> None:
    sess = make_mock_session(
        [
            make_mock_page("https://www.youtube.com", "YouTube Home"),
            make_mock_page("https://www.youtube.com/watch?v=abc", "YouTube - My Video"),
        ]
    )
    result = await sess._find_tab_by_keywords(["youtube.com", "youtu.be"])
    check(
        "yt keywords finds a youtube tab",
        result is not None and "youtube.com" in (result.url or ""),
    )


async def _find_tab_netflix_over_random_async() -> None:
    sess = make_mock_session(
        [
            make_mock_page("https://google.com", "Google"),
            make_mock_page("https://www.netflix.com/browse", "Netflix"),
        ]
    )
    result = await sess._find_tab_by_keywords(["netflix.com"])
    check(
        "nf keywords picks netflix.com over google",
        result is not None and "netflix.com" in (result.url or ""),
    )


async def _find_tab_non_domain_title_async() -> None:
    sess = make_mock_session(
        [
            make_mock_page("https://example.com", "Some Page About Cats"),
            make_mock_page("https://google.com", "Google Search"),
        ]
    )
    result = await sess._find_tab_by_keywords(["cats"])
    check("non-domain kw matches title", result is not None and "example.com" in (result.url or ""))


async def _find_tab_domain_not_in_title_async() -> None:
    sess = make_mock_session(
        [
            make_mock_page("https://facebook.com", "Facebook - Check out my YouTube video!"),
        ]
    )
    result = await sess._find_tab_by_keywords(["youtube.com", "youtu.be"])
    check("domain kw does NOT match via title only", result is None)


async def _find_tab_skip_closed_async() -> None:
    sess = make_mock_session(
        [
            make_mock_page("https://www.youtube.com/watch?v=abc", "YouTube", closed=True),
            make_mock_page("https://www.netflix.com/browse", "Netflix"),
        ]
    )
    result = await sess._find_tab_by_keywords(["youtube.com", "youtu.be"])
    check("closed tab skipped, no match", result is None)
    result = await sess._find_tab_by_keywords(["netflix.com"])
    check(
        "closed tab skipped, netflix found",
        result is not None and "netflix.com" in (result.url or ""),
    )


import asyncio as _asyncio


def test_find_tab_by_keywords_selects_correct_tab() -> None:
    _asyncio.run(_find_tab_correct_tab_async())


def test_find_tab_by_keywords_prefers_url_over_title() -> None:
    _asyncio.run(_find_tab_prefers_url_async())


def test_find_tab_by_keywords_selects_netflix_over_random() -> None:
    _asyncio.run(_find_tab_netflix_over_random_async())


def test_find_tab_by_keywords_non_domain_keyword_matches_title() -> None:
    _asyncio.run(_find_tab_non_domain_title_async())


def test_find_tab_by_keywords_does_not_match_domain_in_title() -> None:
    _asyncio.run(_find_tab_domain_not_in_title_async())


def test_find_tab_by_keywords_skips_closed_tabs() -> None:
    _asyncio.run(_find_tab_skip_closed_async())


# ── Run all tests ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== _domain_matches ===")
    test_domain_matches()

    print("\n=== _domain_family_matches ===")
    test_domain_family_matches()

    print("\n=== _expected_domain_family_from_url ===")
    test_expected_domain_family_from_url()

    print("\n=== _find_tab_by_keywords ===")
    test_find_tab_by_keywords_selects_correct_tab()
    test_find_tab_by_keywords_prefers_url_over_title()
    test_find_tab_by_keywords_selects_netflix_over_random()
    test_find_tab_by_keywords_non_domain_keyword_matches_title()
    test_find_tab_by_keywords_does_not_match_domain_in_title()
    test_find_tab_by_keywords_skips_closed_tabs()

    total = passed + failed
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed, {failed}/{total} failed")
    if __name__ == "__main__":
        sys.exit(0 if failed == 0 else 1)
