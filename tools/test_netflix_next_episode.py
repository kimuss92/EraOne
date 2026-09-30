"""next_episode must only say "starting" once the episode actually changed.

2026-09-26 (live log): netflix_control next_episode answered "Next episode
starting" twice while the URL stayed on /watch/81756300 — `playNextEpisode()`
returned without error and did nothing. A real click on "Next episode (N)"
(via click_ref) is what moved it. These tests drive a stand-in player in
headless Edge (served by page.route, no network) that reproduces that.
"""

import asyncio

import pytest

import actions.netflix_control as nc
from actions._netflix import player_api

WATCH = "https://www.netflix.com/watch/100"


def _player_html(*, api_works: bool, button: str | None, key_n: bool) -> str:
    """button: None | "any" (any click works) | "trusted" (ignores synthetic clicks)."""
    advance = "history.pushState({}, '', '/watch/101');"
    api = f"""
      window.netflix = {{appContext: {{state: {{playerApp: {{getAPI: () => ({{videoPlayer: {{
        getAllPlayerSessionIds: () => ['s1'],
        getVideoPlayerBySessionId: () => ({{
          seek() {{}}, getCurrentTime: () => 1000, getDuration: () => 60000,
          getMovieId: () => Number(location.pathname.split('/').pop()),
          playNextEpisode() {{ {advance if api_works else "/* the live bug: no-op */"} }},
        }}),
      }}}})}}}}}}}};"""
    btn = ""
    if button:
        guard = "if (!e.isTrusted) return;" if button == "trusted" else ""
        btn = f"""<button aria-label="Next episode (N)"
                   onclick="(function(e){{ {guard} window.clicks=(window.clicks||0)+1; {advance} }})(event)"
                 >Next</button>"""
    key = (
        f"document.addEventListener('keydown', e => {{ if (e.isTrusted && e.key.toLowerCase() === 'n') {{ {advance} }} }});"
        if key_n
        else ""
    )
    return f"<html><body style='height:600px'>{btn}<script>{api}{key}</script></body></html>"


def _run(html: str, body):
    async def main():
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(channel="msedge", headless=True)
            except Exception as e:
                pytest.skip(f"headless Edge unavailable: {e}")
            page = await browser.new_page()
            await page.route(
                "https://www.netflix.com/**",
                lambda route: route.fulfill(content_type="text/html", body=html),
            )
            await page.goto(WATCH)
            try:
                return await body(page)
            finally:
                await browser.close()

    return asyncio.run(main())


def _in_page(page):
    script = player_api.build_player_script("next_episode")
    return page.evaluate(f"(() => {{ {script} }})()")


def test_api_that_does_nothing_is_not_reported_as_success():
    """The exact live bug: API no-op, no usable button."""

    async def body(page):
        return await _in_page(page), page.url

    result, url = _run(_player_html(api_works=False, button=None, key_n=False), body)
    assert result == player_api.NEXT_EPISODE_UNMOVED
    assert url == WATCH


def test_working_api_is_verified():
    async def body(page):
        return await _in_page(page)

    assert (
        _run(_player_html(api_works=True, button=None, key_n=False), body)
        == player_api.NEXT_EPISODE_OK
    )


def test_button_used_when_api_does_nothing():
    async def body(page):
        return await _in_page(page), page.url

    result, url = _run(_player_html(api_works=False, button="any", key_n=False), body)
    assert result == player_api.NEXT_EPISODE_OK
    assert url.endswith("/watch/101")


def test_trusted_click_when_page_ignores_synthetic_clicks():
    """What happened live: only a real click on 'Next episode (N)' worked."""

    async def body(page):
        assert await _in_page(page) == player_api.NEXT_EPISODE_UNMOVED
        ok = await nc._trusted_next_episode(page, WATCH)
        return ok, page.url, await page.evaluate("window.clicks || 0")

    ok, url, clicks = _run(_player_html(api_works=False, button="trusted", key_n=False), body)
    assert ok and url.endswith("/watch/101") and clicks == 1


def test_n_shortcut_when_there_is_no_button():
    async def body(page):
        return await nc._trusted_next_episode(page, WATCH), page.url

    ok, url = _run(_player_html(api_works=False, button=None, key_n=True), body)
    assert ok and url.endswith("/watch/101")


def test_no_second_skip_when_episode_already_moved():
    """A late in-page click must not be followed by another skip."""

    async def body(page):
        await page.evaluate("history.pushState({}, '', '/watch/101')")
        ok = await nc._trusted_next_episode(page, WATCH)
        return ok, page.url, await page.evaluate("window.clicks || 0")

    ok, url, clicks = _run(_player_html(api_works=False, button="any", key_n=True), body)
    assert ok and url.endswith("/watch/101") and clicks == 0


def test_nothing_works_says_so_plainly():
    class Sess:
        _context = None

        def run(self, coro):
            coro.close()
            return False

    class Page:
        url = WATCH

        def is_closed(self):
            return False

    msg = nc._finish_next_episode(player_api.NEXT_EPISODE_UNMOVED, Sess(), Page(), WATCH)
    assert "could not move to the next episode" in msg
    assert "starting" not in msg.lower()


def test_verified_success_passes_through_untouched():
    assert (
        nc._finish_next_episode(player_api.NEXT_EPISODE_OK, None, None, WATCH)
        == player_api.NEXT_EPISODE_OK
    )


def test_skip_credits_uses_the_verified_path():
    assert player_api.build_player_script("skip_credits") == player_api.build_player_script(
        "next_episode"
    )
