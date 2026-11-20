"""Offline regressions for the 2026-10-14 Protocol One findings."""

from types import SimpleNamespace

import pytest

from actions import web_search
from core.tool_result import ToolResult


@pytest.mark.parametrize(
    "args,expected",
    [
        (["brave.exe"], True),
        (None, True),
        (["brave.exe", "--type=renderer"], False),
        (
            [
                "brave.exe",
                "--headless=new",
                "--user-data-dir=C:/isolated",
                "--remote-debugging-port=0",
            ],
            False,
        ),
        (["brave.exe", "--headless"], True),
    ],
)
def test_browser_detection_ignores_isolated_automation(monkeypatch, args, expected):
    import psutil

    from actions.browser_control import _BrowserSession

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _: [SimpleNamespace(info={"name": "brave.exe", "cmdline": args})],
    )
    session = _BrowserSession.__new__(_BrowserSession)
    session.browser_name = "brave"
    assert session._browser_process_running() is expected


def test_search_transport_failure_retries_once(monkeypatch):
    calls = []

    def search(query, limit):
        calls.append(query)
        if len(calls) == 1:
            raise RuntimeError("RequestError: error sending request")
        return [{"title": "Recovered", "url": "https://example.test"}]

    monkeypatch.setattr(web_search, "_ddg_search_once", search)
    monkeypatch.setattr(web_search, "sleep_cancellable", lambda _: None)
    assert web_search._ddg_search("example")[0]["title"] == "Recovered"
    assert calls == ["example", "example"]


def test_search_permanent_failure_is_not_retried(monkeypatch):
    def missing(*_):
        raise RuntimeError("ddgs is not installed")

    monkeypatch.setattr(web_search, "_ddg_search_once", missing)
    monkeypatch.setattr(web_search, "sleep_cancellable", lambda _: pytest.fail("retry"))
    with pytest.raises(RuntimeError, match="not installed"):
        web_search._ddg_search("example")


def test_search_retry_remains_bounded(monkeypatch):
    calls = []

    def offline(*_):
        calls.append(True)
        raise RuntimeError("RequestError: offline")

    monkeypatch.setattr(web_search, "_ddg_search_once", offline)
    monkeypatch.setattr(web_search, "sleep_cancellable", lambda _: None)
    with pytest.raises(RuntimeError, match="offline"):
        web_search._ddg_search("example")
    assert len(calls) == 2


def test_search_cancel_before_retry_never_contacts_backend(monkeypatch):
    from core.cancel_registry import CancelledTask, CancelRegistry, bind

    token = CancelRegistry().register("fallback-retry", "web_search")

    def offline(*_):
        token.event.set()
        raise RuntimeError("RequestError: offline")

    monkeypatch.setattr(web_search, "_ddg_search_once", offline)
    monkeypatch.setattr(web_search, "sleep_cancellable", lambda _: pytest.fail("retry"))
    with bind(token), pytest.raises(CancelledTask):
        web_search._ddg_search("example")


def test_forgetting_absent_memory_is_idempotent(monkeypatch):
    from memory import memory_manager

    monkeypatch.setattr(memory_manager, "load_memory", lambda: {"preferences": {}})
    monkeypatch.setattr(memory_manager, "save_memory", lambda _: pytest.fail("write"))
    result = memory_manager.forget("favorite_track", "preferences")
    assert "Already absent" in result
    assert ToolResult.from_any(result).success


@pytest.mark.parametrize("action,job_id", [("retry", "bad"), ("reject", "../outside")])
async def test_invalid_development_id_returns_validation_result(action, job_id):
    from app.era_live.tool_dispatch import ToolDispatchMixin

    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
    )
    result = await ToolDispatchMixin._execute_tool_raw(
        era, "autonomous_development", {"action": action, "job_id": job_id}
    )
    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert "status" in result["message"]
