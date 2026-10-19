"""Production ingress captures source before crossing to the Era loop."""

import asyncio
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

import main
from core.input_envelope import InputEnvelope


def test_envelope_preserves_text_and_is_immutable():
    text = '  Open "C:\\My files\\notes.txt"  '
    request = InputEnvelope.create(text, "telegram")
    assert request.text == text
    assert request.id != InputEnvelope.create(text, "telegram").id
    with pytest.raises(FrozenInstanceError):
        request.source = "typed"
    with pytest.raises(ValueError):
        InputEnvelope.create(text, "invented")


def test_threaded_inputs_keep_explicit_source_and_do_not_leak_telegram(monkeypatch):
    seen, notifications = [], []

    def handle(self, text):
        seen.append((text, self._input_envelope.source, self._tg_reply_pending))

    monkeypatch.setattr(main.EraLive, "_handle_text_command", handle, raising=False)
    monkeypatch.setattr(main, "_tg_notify", notifications.append)

    async def run():
        era = SimpleNamespace(_loop=asyncio.get_running_loop(), _tg_reply_pending=False)

        def post():
            main.EraLive._on_text_command(era, "remote task", source="telegram")
            main.EraLive._on_text_command(era, "local task", source="typed")

        await asyncio.to_thread(post)
        for _ in range(20):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.01)
        assert seen == [("remote task", "telegram", True), ("local task", "typed", False)]
        assert len(notifications) == 1 and "interrupted" in notifications[0].lower()

    asyncio.run(run())


def test_abort_bypasses_blocked_handler_and_cancels_older_queued_input(monkeypatch):
    started, release = threading.Event(), threading.Event()
    seen, notifications = [], []
    monkeypatch.setattr(main, "_tg_notify", notifications.append)

    def handle(self, text):
        seen.append(text)
        if text == "blocked":
            started.set()
            assert release.wait(3)

    monkeypatch.setattr(main.EraLive, "_handle_text_command", handle)

    async def wait_until(predicate):
        while not predicate():
            await asyncio.sleep(0.01)

    async def run():
        era = SimpleNamespace(
            _loop=asyncio.get_running_loop(),
            _tg_reply_pending=False,
            _is_abort_order=lambda text: text == "abort mission",
        )
        try:
            main.EraLive._on_text_command(era, "blocked", source="typed")
            await asyncio.wait_for(wait_until(started.is_set), 1)
            main.EraLive._on_text_command(era, "queued", source="telegram")
            await asyncio.sleep(0)
            main.EraLive._on_text_command(era, "abort mission", source="typed")
            assert seen == ["blocked", "abort mission"]
            assert not release.is_set()
            release.set()
            await asyncio.gather(*era._input_dispatch_tasks)
            assert "queued" not in seen
            assert notifications == ["Your queued command was cancelled by the abort request."]
        finally:
            release.set()
            tasks = getattr(era, "_input_dispatch_tasks", set())
            if tasks:
                await asyncio.gather(*tasks)

    asyncio.run(run())


def test_queued_dispatch_failure_reports_to_its_origin(monkeypatch):
    notifications = []

    def broken(self, text):
        raise RuntimeError("test dispatch failure")

    monkeypatch.setattr(main.EraLive, "_handle_text_command", broken, raising=False)
    monkeypatch.setattr(main, "_tg_notify", notifications.append)

    async def run():
        era = SimpleNamespace(_loop=asyncio.get_running_loop(), _tg_reply_pending=False)
        await asyncio.to_thread(
            main.EraLive._on_text_command, era, "remote task", source="telegram"
        )
        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.01)
        assert len(notifications) == 1 and "couldn't" in notifications[0]
        assert not era._tg_reply_pending

    asyncio.run(run())


def test_offline_ingress_does_not_need_a_running_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(
        main.EraLive,
        "_handle_text_command",
        lambda self, text: seen.append((text, self._input_envelope.source)),
        raising=False,
    )
    era = SimpleNamespace(_loop=None, _tg_reply_pending=False)
    main.EraLive._on_text_command(era, "hello", source="remote")
    assert seen == [("hello", "remote")]


def test_direct_telegram_failure_disarms_before_adapter_reports_it(monkeypatch):
    def broken(self, text):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(main.EraLive, "_handle_text_command", broken)
    era = SimpleNamespace(_loop=None, _tg_reply_pending=False)
    with pytest.raises(RuntimeError):
        main.EraLive._on_text_command(era, "hello", source="telegram")
    assert not era._tg_reply_pending


def test_lan_request_reports_acceptance_not_execution(monkeypatch):
    received = []
    monkeypatch.setenv("ERA_REMOTE_TOKEN", "test-only-token")
    era = SimpleNamespace(
        _loop=None,
        _standby_active=False,
        _on_text_command=lambda text, **kw: received.append((text, kw["source"])),
    )
    monkeypatch.setattr(main, "active_era_instance", era)
    client = main.watch_bridge.test_client()
    assert client.post("/remote_voice", json={"command": "hello"}).status_code == 403
    assert received == []
    response = client.post(
        "/remote_voice",
        json={"command": "hello"},
        headers={"X-Era-Token": "test-only-token"},
    )
    assert response.status_code == 202
    assert response.json == {"status": "accepted", "queued": "hello"}
    assert received == [("hello", "remote")]
    for payload in ([], {"command": []}, {"command": 123}):
        response = client.post(
            "/remote_voice",
            json=payload,
            headers={"X-Era-Token": "test-only-token"},
        )
        assert response.status_code == 400
    assert received == [("hello", "remote")]


@pytest.mark.parametrize("threaded", [True, False])
def test_blocking_handler_keeps_loop_live_and_input_acceptance_ordered(monkeypatch, threaded):
    started = threading.Event()
    release = threading.Event()
    seen = []
    monkeypatch.setattr(main, "_tg_notify", lambda text: None)

    def handle(self, text):
        seen.append((text, self._input_envelope.source))
        if text == "first":
            started.set()
            assert release.wait(3)

    monkeypatch.setattr(main.EraLive, "_handle_text_command", handle)

    async def wait_until(predicate):
        while not predicate():
            await asyncio.sleep(0.01)

    async def run():
        era = SimpleNamespace(_loop=asyncio.get_running_loop(), _tg_reply_pending=False)
        try:
            if threaded:
                await asyncio.to_thread(
                    main.EraLive._on_text_command, era, "first", source="telegram"
                )
            else:
                main.EraLive._on_text_command(era, "first", source="telegram")
            await asyncio.wait_for(wait_until(started.is_set), 1)
            await asyncio.to_thread(main.EraLive._on_text_command, era, "second", source="typed")
            await asyncio.sleep(0.02)  # executes while the OS handler is still blocked
            assert not release.is_set()
            assert era._input_envelope.source == "telegram"
            assert seen == [("first", "telegram")]
            release.set()
            await asyncio.wait_for(wait_until(lambda: len(seen) == 2), 1)
            assert seen == [("first", "telegram"), ("second", "typed")]
        finally:
            release.set()
            tasks = getattr(era, "_input_dispatch_tasks", set())
            if tasks:
                await asyncio.gather(*tasks)

    asyncio.run(run())
