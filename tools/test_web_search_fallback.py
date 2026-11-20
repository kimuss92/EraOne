"""Web search: a spent Gemini quota goes straight to DuckDuckGo, and an empty
DuckDuckGo answer is reported as "nothing found", not as a broken tool."""

from __future__ import annotations

import pytest

from actions import web_search as ws


class _Quota(Exception):
    pass


def test_quota_error_is_not_retried(monkeypatch):
    calls = []

    def once(query):
        calls.append(query)
        raise _Quota("429 RESOURCE_EXHAUSTED: all Gemini keys latched for gemini-3.8-flash")

    monkeypatch.setattr(ws, "_gemini_search_once", once)
    monkeypatch.setattr(ws, "sleep_cancellable", lambda s: pytest.fail("must not wait"))
    with pytest.raises(_Quota):
        ws._gemini_search("anything")
    assert len(calls) == 1


def test_a_real_transient_error_still_gets_one_retry(monkeypatch):
    calls = []

    def once(query):
        calls.append(query)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return "answer"

    monkeypatch.setattr(ws, "_gemini_search_once", once)
    monkeypatch.setattr(ws, "sleep_cancellable", lambda s: None)
    assert ws._gemini_search("q") == "answer" and len(calls) == 2


def _gemini_down(monkeypatch):
    def down(query):
        raise _Quota("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(ws, "_gemini_search", down)


def test_empty_duckduckgo_answer_is_reported_as_nothing_found(monkeypatch):
    _gemini_down(monkeypatch)

    def empty(query):
        raise RuntimeError("No results found.")

    monkeypatch.setattr(ws, "_ddg_search", empty)
    text = ws.web_search({"query": "informatique Soukra reviews"})
    assert text.startswith("Search came up empty for: informatique Soukra reviews")
    assert "Search failed" not in text


def test_a_broken_fallback_still_fails_loudly(monkeypatch):
    _gemini_down(monkeypatch)

    def broken(query):
        raise RuntimeError("the 'ddgs' package is not installed")

    monkeypatch.setattr(ws, "_ddg_search", broken)
    assert ws.web_search({"query": "x"}).startswith("Search failed, sir:")
