"""page_snapshot / click_ref: text-first page reading for browser_control.

The real-browser tests drive headless Edge (ships with Windows) against inline
HTML, so the generated JS is exercised by an actual engine — Python-side checks
alone once let broken page scripts ship silently. They skip if Edge can't start.
"""

import asyncio

import pytest

import actions.page_refs as pr
from core.safety_guard import classify_action

PAGE = """
<html><head><title>Shop</title></head><body>
  <a href="#docs">Docs</a>
  <button id="buy" onclick="document.title='bought'">Place order</button>
  <label>Email <input id="email" type="email" value="a@b.c"></label>
  <input type="checkbox" aria-label="Remember me" checked>
  <button style="display:none">Hidden</button>
  <div aria-hidden="true"><button>Also hidden</button></div>
  <button id="later" style="margin-top:3000px"
          onclick="document.title='later clicked'">Load more</button>
</body></html>
"""


@pytest.fixture(autouse=True)
def _clean_state():
    pr._reset_for_tests()
    yield
    pr._reset_for_tests()


# ── pure logic ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [(3, 3), ("3", 3), ("[12]", 12), (" [7] ", 7), (0, None), ("x", None), (True, None)],
)
def test_parse_ref(value, expected):
    assert pr.parse_ref(value) == expected


@pytest.mark.parametrize(
    "label,sensitive",
    [
        ("button 'Place order'", True),
        ("button 'Send'", True),
        ("button 'Sign in'", True),
        ("link 'Delete account'", True),
        ("link 'Docs'", False),
        ("button 'Sender settings'", False),  # word boundary, not substring
        ("button 'Load more'", False),
    ],
)
def test_sensitive_labels(label, sensitive):
    assert pr.is_sensitive_label(label) is sensitive


def test_safety_gates_click_ref_on_recorded_label_not_params():
    assert classify_action("browser_control", {"action": "click_ref", "ref": 1})[
        "requires_confirmation"
    ], "unknown ref must fail closed"

    pr._STATE.update(nonce="n", url="https://x.test/", labels={1: "link 'Docs'", 2: "button 'Pay'"})
    low = classify_action("browser_control", {"action": "click_ref", "ref": 1})
    assert low["risk_level"] == "LOW" and not low["requires_confirmation"]
    # A harmless-sounding description from the model changes nothing.
    high = classify_action(
        "browser_control", {"action": "click_ref", "ref": "2", "description": "docs link"}
    )
    assert high["risk_level"] == "HIGH" and high["requires_confirmation"]


def test_page_snapshot_is_low_risk():
    assert not classify_action("browser_control", {"action": "page_snapshot"})[
        "requires_confirmation"
    ]


# ── real browser ─────────────────────────────────────────────────────────────


def _with_page(body, html=PAGE):
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(channel="msedge", headless=True)
            except Exception as e:
                pytest.skip(f"headless Edge unavailable: {e}")
            page = await browser.new_page()
            await page.set_content(html)
            try:
                return await body(page)
            finally:
                await browser.close()

    return asyncio.run(run())


def _ref_of(text: str, name: str) -> int:
    line = next(ln for ln in text.splitlines() if name in ln)
    return int(line[1 : line.index("]")])


def test_snapshot_lists_visible_controls_only():
    async def body(page):
        return await pr.snapshot(page)

    text = _with_page(body)
    assert "not instructions" in text
    assert "link 'Docs'" in text
    assert "button 'Place order'" in text
    assert "input:email 'Email" in text and "value='a@b.c'" in text
    assert "input:checkbox 'Remember me' (checked)" in text
    assert "Hidden" not in text and "Also hidden" not in text
    assert "button 'Load more' (offscreen)" in text


def test_click_ref_scrolls_and_clicks():
    async def body(page):
        text = await pr.snapshot(page)
        result = await pr.click_ref(page, _ref_of(text, "Load more"))
        return result, await page.title()

    result, title = _with_page(body)
    assert result.startswith("Clicked [") and "Load more" in result
    assert title == "later clicked"


def test_click_ref_refuses_when_label_changed():
    async def body(page):
        text = await pr.snapshot(page)
        ref = _ref_of(text, "Load more")
        await page.evaluate("document.getElementById('later').innerText = 'Place order'")
        return await pr.click_ref(page, ref), await page.title()

    result, title = _with_page(body)
    assert "changed from" in result
    assert title == "Shop"


def test_planted_ref_attribute_is_ignored():
    """A page can't pre-stamp data-era-ref onto a button: refs carry a fresh nonce."""

    async def body(page):
        text = await pr.snapshot(page)
        docs = _ref_of(text, "Docs")
        await page.evaluate(
            f"document.getElementById('buy').setAttribute('data-era-ref', 'evil:{docs}')"
        )
        return await pr.click_ref(page, docs), await page.title()

    result, title = _with_page(body)
    assert "Docs" in result and title == "Shop"


def test_click_ref_refuses_after_navigation():
    async def body(page):
        text = await pr.snapshot(page)
        # set_content pages are about:blank, so go somewhere with a different URL
        # that still has a "Docs" link — only the URL check can stop this click.
        await page.goto("data:text/html,<a href='#'>Docs</a>")
        return await pr.click_ref(page, _ref_of(text, "Docs"))

    assert "page changed" in _with_page(body)


def test_click_ref_without_snapshot():
    async def body(page):
        return await pr.click_ref(page, 1)

    assert "no page snapshot" in _with_page(body)


# ── smart_click gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params,confirm",
    [
        ({"description": "Buy now"}, True),
        ({"text": "Place order"}, True),  # the router sometimes sends `text`
        ({"description": "Docs"}, False),
        ({"description": "Sender settings"}, False),
    ],
)
def test_safety_gates_smart_click_description(params, confirm):
    got = classify_action("browser_control", {"action": "smart_click", **params})
    assert got["requires_confirmation"] is confirm, got


def test_smart_click_holds_harmless_description_matching_sensitive_control():
    async def body(page):
        el = page.get_by_role("button", name="order").first
        return await pr.hold_if_sensitive(el, "order"), await page.title()

    held, title = _with_page(body)
    assert held and held.startswith("Held for confirmation") and "Place order" in held
    assert title == "Shop"
    from core.tool_result import looks_like_failure

    assert not looks_like_failure(held)  # a hold must not file an error report


def test_smart_click_hold_passes_confirmed_or_harmless():
    async def body(page):
        buy = page.get_by_role("button", name="Place order").first
        docs = page.get_by_role("link", name="Docs").first
        return (
            await pr.hold_if_sensitive(buy, "Place order"),
            await pr.hold_if_sensitive(docs, "docs"),
        )

    assert _with_page(body) == (None, None)


# ── snapshot shaping (found on live YouTube 2026-09-26) ──────────────────────

BUSY = """
<html><body>
  <div style="position:absolute; top:5000px"><a href="#footer">Footer link</a></div>
  <a href="https://example.com/home">
    <img src="x.png" width="40" height="20">
  </a>
  <a href="#subs"><div role="link">Subscriptions</div></a>
  <a href="#home">Home</a>
  <a href="#home">Home</a>
  <a href="#other">Home</a>
  <button>More actions</button>
  <button>More actions</button>
</body></html>
"""


def test_snapshot_shapes_a_busy_page():
    async def body(page):
        return await pr.snapshot(page)

    lines = [ln for ln in _with_page(body, BUSY).splitlines() if ln.startswith("[")]
    # on-screen first, even though the footer link comes first in the DOM
    assert "Footer link" in lines[-1] and "(offscreen)" in lines[-1]
    # same label + same destination merged; different destination kept
    assert sum("link 'Home'" in ln for ln in lines) == 2
    # repeated buttons are distinct controls — never merged
    assert sum("button 'More actions'" in ln for ln in lines) == 2
    # a role=link nested in an <a> with the same label is one control (YouTube)
    assert sum("'Subscriptions'" in ln for ln in lines) == 1
    # unlabelled link named by where it goes, even with whitespace-only text
    assert any("link 'to example.com/home'" in ln for ln in lines)


def test_unlabelled_link_is_still_clickable():
    """The derived name must match at click time, or every such click reports 'changed'."""

    async def body(page):
        text = await pr.snapshot(page)
        return await pr.click_ref(page, _ref_of(text, "to example.com/home"))

    assert _with_page(body, BUSY).startswith("Clicked [")


def test_budget_spent_on_screen_and_rest_counted(monkeypatch):
    monkeypatch.setattr(pr, "MAX_ELEMENTS", 2)

    async def body(page):
        return await pr.snapshot(page)

    text = _with_page(body, BUSY)
    assert "Footer link" not in text
    assert "5 more off-screen controls not listed" in text


def test_summarize_is_one_line():
    full = (
        "Page: YouTube — https://youtube.com/\n(data)\n[1] link 'a'\n[2] button 'b'\nUse click_ref"
    )
    assert pr.summarize(full) == "Page: YouTube — https://youtube.com/ | 2 controls listed"
    assert pr.summarize("Snapshot error: boom") == "Snapshot error: boom"


# ── Netflix failure context (live log 2026-09-26) ────────────────────────────


@pytest.mark.parametrize(
    "params,result,flagged",
    [
        (
            {"action": "page_snapshot"},
            "Page: Netflix — https://www.netflix.com/watch/1\n[1] button 'x'",
            False,
        ),
        ({"action": "click_ref", "ref": 5}, "Clicked [5] button 'Next episode (N)'.", False),
        # a refused click on Netflix is still a Netflix problem
        ({"action": "click_ref", "ref": 5}, "Click failed: [5] is gone from netflix.com", True),
        ({"action": "go_to"}, "Opened https://www.netflix.com/browse", True),
    ],
)
def test_successful_snapshot_or_click_is_not_a_netflix_failure(params, result, flagged):
    import actions.netflix_control as nc
    from core.self_healing_router import record_tool_result

    nc._clear_netflix_failure()
    try:
        record_tool_result("browser_control", params, result=result)
        assert nc.is_netflix_failure_context_active() is flagged
    finally:
        nc._clear_netflix_failure()
