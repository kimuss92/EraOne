"""Foreground evidence must stay detached, fresh, read-only and process-scoped."""

import threading

import pytest

from core.foreground_awareness import ForegroundAwareness


def test_manual_app_change_and_heartbeat():
    events = []
    service = ForegroundAwareness(publisher=lambda view, **kw: events.append((view, kw)))
    first = {"hwnd": 1, "pid": 2, "app": "brave.exe"}
    service.observe(first)
    service.observe(first)
    service.observe({"hwnd": 3, "pid": 4, "app": "notepad.exe"})
    assert [kw["changed"] for _, kw in events] == [True, False, True]
    assert service.snapshot()["foreground"]["app"] == "notepad.exe"


def test_stale_future_and_failure_clear_evidence():
    now = [100.0]
    service = ForegroundAwareness(clock=lambda: now[0])
    service.observe({"hwnd": 1, "pid": 2, "app": "brave.exe"})
    now[0] = 140
    assert service.snapshot()["health"] == "stale"
    assert service.snapshot()["foreground"] is None
    now[0] = 90
    assert service.snapshot()["foreground"] is None
    service.unavailable()
    assert service.snapshot()["health"] == "unavailable"


def test_snapshots_and_input_are_detached():
    service = ForegroundAwareness()
    info = {"hwnd": 1, "pid": 2, "app": "brave.exe"}
    service.observe(info)
    info["app"] = "changed"
    service.snapshot()["foreground"]["app"] = "also changed"
    assert service.snapshot()["foreground"]["app"] == "brave.exe"


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"hwnd": True, "pid": 2, "app": "x"},
        {"hwnd": 1, "pid": -1, "app": "x"},
        {"hwnd": 1, "pid": 2, "app": "x", "title": "private"},
        {"hwnd": 1, "pid": 2, "app": "x" * 257},
    ],
)
def test_invalid_payload_cannot_add_private_fields(info):
    service = ForegroundAwareness()
    with pytest.raises(ValueError):
        service.observe(info)
    assert service.snapshot()["foreground"] is None


def test_publisher_failure_does_not_destroy_native_evidence():
    def fail(*args, **kwargs):
        raise OSError("test publisher failure")

    service = ForegroundAwareness(publisher=fail)
    service.observe({"hwnd": 1, "pid": 2, "app": "brave.exe"})
    assert service.snapshot()["health"] == "ready"


def test_one_worker_and_clean_stop():
    started = threading.Event()

    def driver(service):
        service.observe({"hwnd": 1, "pid": 2, "app": "brave.exe"})
        started.set()
        service._stop.wait(3)

    service = ForegroundAwareness(driver=driver)
    assert service.start()
    try:
        assert started.wait(2)
        assert not service.start()
    finally:
        assert service.stop()
    assert service.snapshot()["health"] == "unavailable"
    assert not service._thread.is_alive()


def test_native_driver_failure_is_visible_and_can_stop():
    failed = threading.Event()

    def driver(service):
        failed.set()
        raise OSError("test registration failure")

    service = ForegroundAwareness(driver=driver)
    service.start()
    try:
        assert failed.wait(2)
        assert service.snapshot()["health"] == "unavailable"
    finally:
        assert service.stop()


def test_hud_snapshot_and_event_are_detached():
    from app.hud.events import HudBus

    bus = HudBus()
    events = []
    bus.attach(events.append)
    service = ForegroundAwareness(publisher=bus.foreground_observed)
    service.observe({"hwnd": 1, "pid": 2, "app": "brave.exe"})
    snap = bus.snapshot()
    snap["foregroundObservation"]["foreground"]["app"] = "changed"
    assert bus.snapshot()["foregroundObservation"]["foreground"]["app"] == "brave.exe"
    assert any(e.get("event", {}).get("type") == "system.foreground.observed" for e in events)
