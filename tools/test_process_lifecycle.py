"""Process observations preserve instance identity, uncertainty and lifecycle."""

import threading
from types import SimpleNamespace

import pytest

from core.process_lifecycle import ProcessLifecycle


def inventory(*rows, omitted=0):
    return {
        "processes": [{"pid": pid, "app": app, "startedAt": born} for pid, app, born in rows],
        "omitted": omitted,
    }


def test_same_name_instances_exit_and_pid_reuse():
    service = ProcessLifecycle()
    service.observe(inventory((1, "brave.exe", 100), (2, "brave.exe", 200)))
    before = service.snapshot()["processes"]
    assert len(before) == 2 and before[0]["id"] != before[1]["id"]
    service.observe(inventory((1, "brave.exe", 300)))
    after = service.snapshot()["processes"]
    assert len(after) == 1 and before[0]["id"] != after[0]["id"]


def test_windows_unknown_birth_does_not_discard_inventory(monkeypatch):
    import psutil

    from core.process_lifecycle import read_inventory

    def processes(attrs, ad_value):
        assert attrs == ["pid", "name", "create_time"]
        return [
            SimpleNamespace(info={"pid": 4, "name": "System", "create_time": 0.0}),
            SimpleNamespace(info={"pid": 5, "name": "app.exe", "create_time": 100.0}),
        ]

    monkeypatch.setattr(psutil, "process_iter", processes)
    data = read_inventory()
    assert data["processes"][0]["startedAt"] is None
    service = ProcessLifecycle()
    service.observe(data)
    assert service.snapshot()["health"] == "degraded"
    assert len(service.snapshot()["processes"]) == 2


def test_uncertain_identity_and_omissions_are_degraded():
    service = ProcessLifecycle()
    service.observe(inventory((1, "protected.exe", None), omitted=3))
    view = service.snapshot()
    assert view["health"] == "degraded" and view["omitted"] == 3
    assert not view["processes"][0]["identityVerified"]


def test_heartbeat_is_not_a_change_and_snapshots_are_detached():
    events = []
    service = ProcessLifecycle(publisher=lambda v, **kw: events.append(kw["changed"]))
    data = inventory((1, "app.exe", 100))
    service.observe(data)
    service.observe(data)
    assert events == [True, False]
    data["processes"][0]["app"] = "changed"
    service.snapshot()["processes"].clear()
    assert service.snapshot()["processes"][0]["app"] == "app.exe"


@pytest.mark.parametrize(
    "data",
    [
        inventory((1, "a.exe", 100), (1, "b.exe", 200)),
        inventory((True, "a.exe", 100)),
        inventory((1, "a.exe", float("nan"))),
        inventory((1, "a.exe", 100), omitted=-1),
        {
            "processes": [{"pid": 1, "app": "a.exe", "startedAt": 100, "cmdline": "private"}],
            "omitted": 0,
        },
    ],
)
def test_invalid_or_private_data_rejected(data):
    service = ProcessLifecycle()
    with pytest.raises(ValueError):
        service.observe(data)
    assert not service.snapshot()["processes"]


def test_freshness_and_failure_do_not_resurrect_processes():
    now = [100.0]
    service = ProcessLifecycle(clock=lambda: now[0])
    service.observe(inventory((1, "a.exe", 10)))
    now[0] += 40
    assert service.snapshot()["health"] == "stale" and not service.snapshot()["processes"]
    service.unavailable()
    now[0] = 100
    assert not service.snapshot()["processes"]


def test_single_worker_and_stop():
    started = threading.Event()

    def driver(service):
        service.observe(inventory((1, "a.exe", 100)))
        started.set()
        service._stop.wait(3)

    service = ProcessLifecycle(driver=driver)
    assert service.start()
    try:
        assert started.wait(2) and not service.start()
    finally:
        assert service.stop()
    assert not service._thread.is_alive() and not service.snapshot()["processes"]


def test_registration_failure_uses_explicit_reconciliation_coverage():
    updated = threading.Event()

    def fail(service):
        raise OSError("test hook unavailable")

    service = ProcessLifecycle(
        driver=fail,
        reader=lambda: inventory((1, "a.exe", 100)),
        publisher=lambda v, **kw: updated.set() if v["health"] == "degraded" else None,
    )
    service.start()
    try:
        assert updated.wait(2)
        view = service.snapshot()
        assert view["health"] == "degraded" and view["coverage"] == "reconciliation-only"
    finally:
        assert service.stop()


def test_hud_snapshot_and_context_are_bounded(monkeypatch):
    from app.hud.events import HudBus
    from core import process_lifecycle

    bus = HudBus()
    service = ProcessLifecycle(publisher=bus.processes_observed)
    service.observe(inventory(*[(i, f"app{i}.exe", i) for i in range(1, 61)]))
    monkeypatch.setattr(process_lifecycle, "get_process_lifecycle", lambda: service)
    summary = process_lifecycle.process_summary()
    assert len(summary["apps"]) == 40 and summary["unlistedAppNames"] == 20
    bus.snapshot()["processObservation"]["processes"].clear()
    assert len(bus.snapshot()["processObservation"]["processes"]) == 60
