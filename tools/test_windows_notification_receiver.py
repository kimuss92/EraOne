from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timezone

import pytest

import features.windows_notification_receiver as receiver


@dataclass
class Notice:
    id: int
    app_name: str
    texts: list[str]
    created_at: datetime


class FakeBackend:
    def __init__(self, notices=(), *, denied=False):
        self.notices = list(notices)
        self.denied = denied
        self.connected = 0
        self.closed = 0
        self.polls = 0

    def connect(self):
        self.connected += 1
        if self.denied:
            raise PermissionError("test access denied")
        return "allowed"

    def list_notifications(self):
        self.polls += 1
        return list(self.notices)

    def close(self):
        self.closed += 1


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


@pytest.fixture(autouse=True)
def clean_receiver():
    receiver.run({"action": "stop", "timeout_seconds": 1})
    with receiver._STATE.lock:
        receiver._STATE.pending.clear()
        receiver._STATE.seen.clear()
        receiver._STATE.seen_order.clear()
        receiver._STATE.last_error = ""
        receiver._STATE.access = "not_requested"
        receiver._STATE.poll_count = 0
        receiver._STATE.apps = ("whatsapp",)
        receiver._STATE.sinks.clear()
    yield
    receiver.run({"action": "stop", "timeout_seconds": 1})
    with receiver._STATE.lock:
        receiver._STATE.pending.clear()
        receiver._STATE.seen.clear()
        receiver._STATE.seen_order.clear()
        receiver._STATE.sinks.clear()


def test_feature_metadata_and_safe_default_status():
    assert receiver.FEATURE["id"] == "windows_notification_receiver"
    assert "Windows" in receiver.FEATURE["description"]
    result = receiver.run({})
    assert result["success"] is True
    assert result["running"] is False
    assert result["access"] == "not_requested"


def test_background_receiver_filters_for_whatsapp_and_suppresses_history(monkeypatch):
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    backend = FakeBackend(
        [
            Notice(1, "WhatsApp", ["Nadia", "Are you free?"], stamp),
            Notice(2, "Microsoft Outlook", ["Inbox", "Status report"], stamp),
        ]
    )
    monkeypatch.setattr(receiver, "_make_backend", lambda: backend)

    started = receiver.run({"action": "start", "interval_seconds": 0.05})
    assert started["success"] is True
    wait_until(lambda: backend.polls >= 2)

    drained = receiver.run({"action": "drain"})
    assert drained["success"] is True
    assert drained["remaining"] == 0
    assert drained["notifications"] == [
        {
            "id": drained["notifications"][0]["id"],
            "app": "WhatsApp",
            "title": "Nadia",
            "body": "Are you free?",
            "created_at": stamp.isoformat(),
        }
    ]
    assert "untrusted local data" in drained["message"]

    # Windows returns notification history on every poll. It must not become a
    # new alert each time.
    assert receiver.run({"action": "drain"})["notifications"] == []
    stopped = receiver.run({"action": "stop"})
    assert stopped["success"] is True
    assert backend.closed == 1


def test_start_is_idempotent_and_updates_the_live_filter(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(receiver, "_make_backend", lambda: backend)

    first = receiver.run({"action": "start", "interval_seconds": 0.2})
    thread = receiver._STATE.thread
    second = receiver.run(
        {"action": "start", "apps": ["Signal", "WhatsApp"], "interval_seconds": 0.1}
    )

    assert first["success"] is True
    assert second["success"] is True
    assert "already running" in second["message"]
    assert receiver._STATE.thread is thread
    assert receiver._STATE.apps == ("signal", "whatsapp")
    assert backend.connected == 1


def test_poll_once_can_receive_all_apps_without_starting_a_thread(monkeypatch):
    stamp = datetime(2026, 2, 3, tzinfo=UTC)
    backend = FakeBackend([Notice(7, "Signal", ["Mina", "hello"], stamp)])
    monkeypatch.setattr(receiver, "_make_backend", lambda: backend)

    result = receiver.run({"action": "poll_once", "apps": []})
    assert result == {
        "success": True,
        "message": "Windows notification poll completed with 1 new alert(s).",
        "new_count": 1,
    }
    assert receiver.run({"action": "status"})["running"] is False
    assert receiver.run({"action": "drain"})["notifications"][0]["app"] == "Signal"
    assert backend.closed == 1


def test_access_denial_does_not_launch_background_work(monkeypatch):
    backend = FakeBackend(denied=True)
    monkeypatch.setattr(receiver, "_make_backend", lambda: backend)

    result = receiver.run({"action": "start"})

    assert result["success"] is False
    assert result["error_type"] == "PermissionError"
    assert result["running"] is False
    status = receiver.run({"action": "status"})
    assert status["running"] is False
    assert status["access"] == "denied"
    assert backend.polls == 0


def test_notification_content_is_sanitized_and_bounded(monkeypatch):
    stamp = datetime(2026, 3, 4, tzinfo=UTC)
    body = "hello\x00\x07 " + ("x" * 3000)
    backend = FakeBackend([Notice(8, "WhatsApp", [" Sender\nName ", body], stamp)])
    monkeypatch.setattr(receiver, "_make_backend", lambda: backend)

    assert receiver.run({"action": "poll_once"})["success"] is True
    alert = receiver.run({"action": "drain"})["notifications"][0]

    assert alert["title"] == "Sender Name"
    assert "\x00" not in alert["body"]
    assert "\x07" not in alert["body"]
    assert len(alert["body"]) <= receiver._MAX_TEXT


def test_alert_sink_is_invoked_but_its_failure_cannot_kill_polling(monkeypatch):
    stamp = datetime(2026, 4, 5, tzinfo=UTC)
    backend = FakeBackend(
        [
            Notice(10, "WhatsApp", ["First"], stamp),
            Notice(11, "WhatsApp", ["Second"], stamp),
        ]
    )
    monkeypatch.setattr(receiver, "_make_backend", lambda: backend)
    observed = []

    receiver.register_alert_sink(lambda alert: observed.append(alert["title"]))

    def broken_sink(_alert):
        raise RuntimeError("presentation failed")

    unsubscribe = receiver.register_alert_sink(broken_sink)
    result = receiver.run({"action": "poll_once"})
    unsubscribe()

    assert result["success"] is True
    assert observed == ["First", "Second"]
    assert len(receiver.run({"action": "drain"})["notifications"]) == 2


def test_arguments_are_validated_without_touching_a_backend(monkeypatch):
    called = False

    def forbidden_backend():
        nonlocal called
        called = True
        raise AssertionError("backend should not be created")

    monkeypatch.setattr(receiver, "_make_backend", forbidden_backend)

    bad_apps = receiver.run({"action": "start", "apps": "WhatsApp"})
    bad_interval = receiver.run({"action": "start", "interval_seconds": 0})
    unknown = receiver.run({"action": "launch"})

    assert bad_apps["error_type"] == "ValueError"
    assert bad_interval["error_type"] == "ValueError"
    assert unknown["error_type"] == "UnknownAction"
    assert called is False
