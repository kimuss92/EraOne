"""Owned-page DOM playback tests; navigation or accepting play is not success."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from actions import youtube_video as yt
from core.tool_result import ToolResult

URL = "https://www.youtube.com/watch?v=abcdefghijk"


def sample(**overrides):
    return {
        "url": URL,
        "time": 1,
        "paused": False,
        "ended": False,
        "ready": 4,
        "ad": False,
        "muted": False,
        **overrides,
    }


class Page:
    def __init__(self, samples, paused=True):
        self.url = URL
        self.samples = iter(samples)
        self.last = sample()
        self.paused = paused
        self.clicks = 0
        self.navigations = []
        self.first = self

    async def wait_for_selector(self, *args, **kwargs):
        pass

    async def goto(self, url, **kwargs):
        self.navigations.append(url)
        self.url = url

    async def evaluate(self, script):
        if "return !v || v.paused" in script:
            return self.paused
        self.last = next(self.samples, self.last)
        return self.last

    def locator(self, selector):
        assert "play-button" in selector
        return self

    async def is_visible(self):
        return True

    async def click(self, **kwargs):
        self.clicks += 1


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


@pytest.mark.parametrize("paused,clicks", [(True, 1), (False, 0)])
def test_selected_video_must_advance_and_running_video_is_not_toggled(paused, clicks):
    page = Page([sample(), sample(time=1.5)], paused=paused)
    result = asyncio.run(yt._start_and_observe(page, "abcdefghijk"))
    assert result["playing"] and result["progress"] == 0.5
    assert page.clicks == clicks


@pytest.mark.parametrize(
    "state",
    [
        sample(),
        sample(paused=True, time=2),
        sample(muted=True, time=2),
        sample(ad=True, time=2),
        sample(ended=True, time=2),
        sample(ready=1, time=2),
        sample(url="https://www.youtube.com/watch?v=lmnopqrstuv", time=2),
    ],
)
def test_stalled_paused_muted_ad_or_wrong_video_is_not_success(state):
    page = Page([sample(), state])
    assert not asyncio.run(yt._start_and_observe(page, "abcdefghijk"))["playing"]
    assert page.clicks == 1


def test_existing_wrong_watch_page_is_rejected_without_clicking():
    page = Page([])
    page.url = "https://www.youtube.com/watch?v=lmnopqrstuv"
    assert not asyncio.run(yt._start_and_observe(page, "abcdefghijk"))["playing"]
    assert page.clicks == 0


@pytest.fixture
def browser(monkeypatch):
    from actions import browser_intelligence, platform_tab

    page = Page([sample(), sample(time=2)])
    monkeypatch.setattr(yt, "get_browser_session", lambda _: SimpleNamespace(run=asyncio.run))
    monkeypatch.setattr(
        platform_tab, "get_or_open_platform_tab", lambda **_: SimpleNamespace(page=page)
    )
    monkeypatch.setattr(yt, "_save_youtube_grid", lambda *_: None)
    monkeypatch.setattr(
        yt, "_os_open_url", Mock(side_effect=AssertionError("no generic OS fallback"))
    )
    monkeypatch.setattr(
        yt, "_scrape_first_video_url", Mock(side_effect=AssertionError("DOM required"))
    )
    monkeypatch.setattr(
        browser_intelligence,
        "scan_youtube_cards",
        lambda _, **kw: [SimpleNamespace(title="Sabr El Rijal", href=URL)],
    )
    return page


def test_specific_title_search_scans_dom_and_verifies_same_page(browser):
    result = yt._handle_play({"query": "Sabr El Rijal"}, None, "brave")
    assert result.success and result.verified and result.verifier_name == "youtube_dom"
    assert "search_query=Sabr+El+Rijal" in browser.navigations[0]
    assert browser.navigations[-1] == URL and browser.clicks == 1


def test_direct_url_still_requires_actual_advancing_playback(browser):
    browser.samples = iter([sample()])
    result = ToolResult.from_any(yt._handle_play({"query": URL}, None, "brave"))
    assert not result.success and result.error_type == "PlaybackNotVerified"
    assert browser.navigations == [URL]


def test_unrelated_result_does_not_play_first_card(browser, monkeypatch):
    from actions import browser_intelligence

    monkeypatch.setattr(
        browser_intelligence,
        "scan_youtube_cards",
        lambda _, **kw: [SimpleNamespace(title="Different unrelated result", href=URL)],
    )
    result = ToolResult.from_any(yt._handle_play({"query": "Sabr El Rijal"}, None, "brave"))
    assert not result.success and browser.clicks == 0
    assert len(browser.navigations) == 1


def test_ambiguous_video_choices_name_the_visible_candidates(browser, monkeypatch):
    from actions import browser_intelligence

    monkeypatch.setattr(
        browser_intelligence,
        "scan_youtube_cards",
        lambda _, **kw: [
            SimpleNamespace(title="Hassen Thabet - Saber Elrjel", href=URL),
            SimpleNamespace(
                title="Sabr Al Rijal (Full Version)",
                href="https://www.youtube.com/watch?v=lmnopqrstuv",
            ),
        ],
    )
    from utils import media_match

    monkeypatch.setattr(
        media_match,
        "select_best_media_candidate",
        lambda *_args: (None, 0.86, "ambiguous"),
    )
    result = ToolResult.from_any(yt._handle_play({"query": "Sabr Al Rijal"}, None, "brave"))
    assert not result.success and result.error_type == "VideoSelectionRequired"
    assert "Hassen Thabet - Saber Elrjel" in result.message
    assert "Sabr Al Rijal (Full Version)" in result.message
    assert browser.clicks == 0


def test_no_dom_results_never_claims_playing(browser, monkeypatch):
    from actions import browser_intelligence

    monkeypatch.setattr(browser_intelligence, "scan_youtube_cards", lambda _, **kw: [])
    result = ToolResult.from_any(yt._handle_play({"query": "Sabr El Rijal"}, None, "brave"))
    assert not result.success and result.error_type == "NoResults"


def test_organic_watch_link_with_radio_parameters_is_a_video(browser, monkeypatch):
    from actions import browser_intelligence

    monkeypatch.setattr(
        browser_intelligence,
        "scan_youtube_cards",
        # scan_youtube_cards is called with page=<owned page>; a lambda that
        # cannot take it raised TypeError and the test read that as a failure.
        lambda _, **kw: [
            SimpleNamespace(title="Sabr El Rijal", href=URL + "&list=RDabcdefghijk&start_radio=1")
        ],
    )
    result = ToolResult.from_any(yt._handle_play({"query": "Sabr El Rijal"}, None, "brave"))
    assert result.success and browser.navigations[-1] == URL
