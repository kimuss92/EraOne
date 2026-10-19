"""Local link evidence must not turn into false internet confirmation."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.network_awareness import NetworkAwareness, read_interfaces


def link(up=True):
    return [{"name": "Ethernet", "index": 1, "isUp": up}]


def test_link_change_removal_and_no_internet_assumption():
    events = []
    service = NetworkAwareness(publisher=lambda v, **kw: events.append(kw["changed"]))
    service.observe(link())
    service.observe(link())
    assert service.snapshot()["internet"] == "unknown"
    service.observe(link(False))
    service.observe([])
    assert events == [True, False, True, True]
    assert not service.snapshot()["interfaces"]


def test_detached_and_stale_data():
    now = [100.0]
    service = NetworkAwareness(clock=lambda: now[0])
    data = link()
    service.observe(data)
    data.clear()
    service.snapshot()["interfaces"].clear()
    assert service.snapshot()["interfaces"]
    now[0] = 140
    assert service.snapshot()["health"] == "stale"
    assert not service.snapshot()["interfaces"]


@pytest.mark.parametrize(
    "data",
    [
        [{"name": "Ethernet", "index": 1, "isUp": True, "ip": "private"}],
        link() * 2,
        [{"name": "a", "index": -1, "isUp": True}],
        [{"name": "a", "index": 1, "isUp": "yes"}],
    ],
)
def test_rejects_invalid_or_private_fields(data):
    service = NetworkAwareness()
    with pytest.raises(ValueError):
        service.observe(data)
    assert not service.snapshot()["interfaces"]


def test_unknown_index_is_explicit():
    service = NetworkAwareness()
    service.observe([{"name": "unresolved", "index": None, "isUp": True}])
    assert service.snapshot()["health"] == "degraded"
    assert service.snapshot()["internet"] == "unknown"


def test_reader_never_reads_addresses_or_contacts_network(monkeypatch):
    import psutil

    from core import network_awareness

    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Ethernet": SimpleNamespace(isup=True)})
    monkeypatch.setattr(network_awareness.socket, "if_nametoindex", lambda name: 3)
    addresses = Mock(side_effect=AssertionError("addresses must not be read"))
    connect = Mock(side_effect=AssertionError("no active probe"))
    monkeypatch.setattr(psutil, "net_if_addrs", addresses)
    monkeypatch.setattr(network_awareness.socket, "create_connection", connect)
    assert read_interfaces() == [{"name": "Ethernet", "index": 3, "isUp": True}]
    addresses.assert_not_called()
    connect.assert_not_called()


def test_single_worker_stop_and_failed_registration_fallback():
    updated = threading.Event()

    def fail(service):
        raise OSError("test registration failure")

    service = NetworkAwareness(
        driver=fail,
        reader=link,
        publisher=lambda v, **kw: updated.set() if v["health"] == "degraded" else None,
    )
    assert service.start()
    try:
        assert updated.wait(2) and not service.start()
        assert service.snapshot()["coverage"] == "reconciliation-only"
    finally:
        assert service.stop()
    assert not service._thread.is_alive()


def test_windows_alias_fallback_preserves_interface_identity(monkeypatch):
    import psutil

    from core import network_awareness

    def unknown(name):
        raise OSError("friendly alias not a socket interface name")

    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Ethernet": SimpleNamespace(isup=True)})
    monkeypatch.setattr(network_awareness.socket, "if_nametoindex", unknown)
    monkeypatch.setattr(network_awareness.os, "name", "nt")
    resolve = Mock(return_value=7)
    monkeypatch.setattr(network_awareness, "windows_interface_index", resolve)
    assert read_interfaces() == [{"name": "Ethernet", "index": 7, "isUp": True}]
    resolve.assert_called_once_with("Ethernet")


def test_cancel_failure_retains_callback_and_refuses_new_start():
    cancel = Mock(return_value=1)
    service = NetworkAwareness()
    callback = object()
    service._registration = (SimpleNamespace(CancelMibChangeNotify2=cancel), 123, callback)
    assert not service.stop()
    assert service._registration[2] is callback
    assert not service.start()
    cancel.return_value = 0
    assert service.stop()
    assert service._registration is None


def test_hud_snapshot_is_detached():
    from app.hud.events import HudBus

    bus = HudBus()
    service = NetworkAwareness(publisher=bus.network_observed)
    service.observe(link())
    bus.snapshot()["networkObservation"]["interfaces"].clear()
    assert bus.snapshot()["networkObservation"]["interfaces"]
