"""Regression tests for the 2026-09-18 Gmail + Brave failures.

1. `browser_control go_to` died with "'NoneType' object has no attribute
   'new_page'": go_to never attached to the browser before opening a tab, so
   the first go_to after an Era relaunch hit a null context.
2. Gmail search "could not find" archived mail: `label_ids=[]` (meant as "whole
   mailbox") was turned back into ["INBOX"] by `label_ids or ["INBOX"]`.
"""

from __future__ import annotations

import asyncio

import pytest

from actions import gmail_control as gc
from core import gmail_service as gs

# ---------------------------------------------------------------------------
# Browser: every tab-creating path must attach first
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self._closed = False

    def is_closed(self):
        return self._closed

    def on(self, *_a, **_k):
        pass

    async def goto(self, url, **_k):
        self.url = url

    async def bring_to_front(self):
        pass

    async def evaluate(self, *_a, **_k):
        return None

    async def title(self):
        return ""


class _FakeContext:
    def __init__(self, fail_times=0):
        self.pages: list[_FakePage] = []
        self.fail_times = fail_times

    async def new_page(self):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("Target page, context or browser has been closed")
        p = _FakePage()
        self.pages.append(p)
        return p

    def on(self, *_a, **_k):
        pass


class _FakeBrowser:
    def __init__(self, ctx):
        self.contexts = [ctx]

    def is_connected(self):
        return True


def _session(ctx_factory):
    """A _BrowserSession whose _launch() attaches to a fake browser."""
    from actions.browser_control import _BrowserSession

    sess = _BrowserSession("brave")
    launches = []

    async def fake_launch():
        if sess._connection_alive():
            return
        launches.append(1)
        ctx = ctx_factory()
        sess._browser_obj = _FakeBrowser(ctx)
        sess._context = ctx

    sess._launch = fake_launch
    sess._launches = launches
    return sess


def test_go_to_after_relaunch_attaches_instead_of_null_context(monkeypatch):
    import actions.browser_control as bc

    monkeypatch.setattr(bc, "_OS", "linux")  # skip the Windows focus call
    sess = _session(_FakeContext)
    assert sess._context is None  # fresh process, never attached

    out = asyncio.run(sess.go_to("https://mail.google.com"))

    assert "NoneType" not in out
    assert out.startswith("Navigated to: https://mail.google.com")
    assert sess._launches  # it attached


def test_new_page_reattaches_once_when_the_link_died_mid_call():
    contexts = [_FakeContext(fail_times=1), _FakeContext()]
    sess = _session(lambda: contexts.pop(0))

    page = asyncio.run(sess._open_new_page())

    assert page is not None
    assert len(sess._launches) == 2  # dropped the dead link and reattached


def test_go_to_gmail_reuses_the_open_gmail_tab(monkeypatch):
    import actions.browser_control as bc

    monkeypatch.setattr(bc, "_OS", "linux")
    ctx = _FakeContext()
    gmail = _FakePage("https://mail.google.com/mail/u/0/#inbox")
    ctx.pages.append(gmail)
    sess = _session(lambda: ctx)

    async def rescan():
        await sess._launch()
        sess._pages = list(ctx.pages)

    sess._fresh_cdp_rescan = rescan
    out = asyncio.run(sess.go_to("https://mail.google.com"))

    assert "Switched to the open tab" in out
    assert len(ctx.pages) == 1  # no duplicate Gmail tab


# ---------------------------------------------------------------------------
# Gmail: search must cover the whole mailbox and survive bad operators
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeGmail:
    def __init__(self, hits_for=lambda q: []):
        self.list_calls: list[dict] = []
        self.hits_for = hits_for

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        self.list_calls.append(kw)
        ids = self.hits_for(kw.get("q", ""))
        return _FakeRequest({"messages": [{"id": i} for i in ids]})

    def get(self, userId, id, **_kw):
        return _FakeRequest(
            {
                "id": id,
                "threadId": "t",
                "labelIds": [],
                "payload": {"headers": [{"name": "Subject", "value": "Latest renders"}]},
            }
        )


@pytest.fixture
def fake_gmail(monkeypatch):
    svc = _FakeGmail()
    monkeypatch.setattr(gs, "_require_service", lambda: svc)
    monkeypatch.setattr(gc, "is_connected", lambda: True)
    return svc


def test_empty_label_list_means_whole_mailbox(fake_gmail):
    gs.list_messages(query="x", label_ids=[])
    assert "labelIds" not in fake_gmail.list_calls[-1]


def test_default_listing_is_still_the_inbox(fake_gmail):
    gs.list_messages(query="")
    assert fake_gmail.list_calls[-1]["labelIds"] == ["INBOX"]


def test_search_does_not_filter_to_inbox(fake_gmail):
    gc.gmail_control({"action": "search", "query": "latest renders"})
    assert all("labelIds" not in c for c in fake_gmail.list_calls)


def test_body_operator_is_rewritten_to_plain_text():
    assert gc._normalize_query('body:"latest renders"') == '"latest renders"'
    assert gc._normalize_query("subject:x OR body:y") == "subject:x OR y"


def test_search_relaxes_field_operators_when_exact_finds_nothing(fake_gmail):
    fake_gmail.hits_for = lambda q: ["m9"] if q == '"latest renders"' else []

    out = gc.gmail_control({"action": "search", "query": 'subject:"latest renders"'})

    assert "looser search" in out
    assert "m9" in out


def test_search_that_finds_nothing_says_where_it_looked(fake_gmail):
    out = gc.gmail_control({"action": "search", "query": "nothing-here"})
    assert "whole mailbox" in out


def test_reads_retry_once_on_rate_limit(monkeypatch):
    class Resp:
        status = 429

    calls = []

    class Req:
        def execute(self):
            calls.append(1)
            if len(calls) == 1:
                err = Exception("rate limited")
                err.resp = Resp()
                raise err
            return {"ok": 1}

    monkeypatch.setattr(gs, "_require_service", lambda: object())
    monkeypatch.setattr(gs.time, "sleep", lambda _s: None)
    assert gs._execute_read(lambda svc: Req()) == {"ok": 1}
    assert len(calls) == 2
