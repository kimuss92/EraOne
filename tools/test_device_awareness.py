"""Passive endpoint evidence, callbacks, freshness, routing boundaries."""

import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.device_awareness import (
    DeviceAwareness,
    empty_defaults,
    endpoint_events,
    make_notifications,
)


def evidence():
    defaults = empty_defaults()
    defaults["input"]["communications"] = "mic-stable-id"
    return {
        "endpoints": [
            {
                "id": "mic-stable-id",
                "name": "Glasses microphone",
                "flow": "input",
                "kind": "microphone",
            }
        ],
        "processDefaults": defaults,
    }


def test_observation_copies_evidence_and_does_not_invent_bluetooth_or_stream_route():
    service = DeviceAwareness()
    rows = evidence()
    service.observe(rows)
    rows["endpoints"].clear()
    view = service.snapshot()
    assert view["health"] == "ready" and view["endpoints"][0]["id"] == "mic-stable-id"
    assert view["bluetoothConnection"] == view["streamRoute"] == "unknown"
    view["processDefaults"]["input"].clear()
    assert service.snapshot()["processDefaults"]["input"]["communications"] == "mic-stable-id"


@pytest.mark.parametrize("delta", [36, -1])
def test_stale_or_future_evidence_does_not_claim_active_devices(delta):
    now = [100.0]
    service = DeviceAwareness(clock=lambda: now[0])
    service.observe(evidence())
    now[0] += delta
    view = service.snapshot()
    assert view["health"] == "stale" and not view["endpoints"]
    assert view["processDefaults"] == empty_defaults()


def test_role_change_is_visible_but_heartbeat_does_not_create_activity_noise():
    published = []
    service = DeviceAwareness(publisher=lambda v, **kw: published.append(kw["changed"]))
    rows = evidence()
    service.observe(rows)
    service.observe(rows)
    rows["processDefaults"]["input"]["console"] = "mic-stable-id"
    service.observe(rows)
    assert published == [True, False, True]


def test_default_inventory_race_is_degraded_not_a_false_disconnect():
    service = DeviceAwareness()
    rows = evidence()
    rows["processDefaults"]["input"]["communications"] = "not-in-this-sample"
    service.observe(rows)
    assert service.snapshot()["health"] == "degraded"
    service.unavailable()
    assert not service.snapshot()["endpoints"]


@pytest.mark.parametrize(
    "field,value", [("id", ""), ("name", "x" * 257), ("flow", "bluetooth"), ("kind", None)]
)
def test_invalid_endpoint_evidence_is_rejected(field, value):
    rows = evidence()
    rows["endpoints"][0][field] = value
    with pytest.raises(ValueError):
        DeviceAwareness().observe(rows)


def test_duplicate_ids_and_bad_role_shapes_are_rejected():
    rows = evidence()
    rows["endpoints"] *= 2
    with pytest.raises(ValueError):
        DeviceAwareness().observe(rows)
    rows = evidence()
    rows["processDefaults"]["input"]["inventedRole"] = "mic-stable-id"
    with pytest.raises(ValueError):
        DeviceAwareness().observe(rows)


def test_notification_callbacks_only_mark_dirty_even_for_unknown_state_flags():
    dirty = Mock()
    callback = make_notifications(object, dirty)
    for method in [
        "OnDeviceAdded",
        "OnDeviceRemoved",
        "OnDefaultDeviceChanged",
        "OnDeviceStateChanged",
        "OnPropertyValueChanged",
    ]:
        getattr(callback, method)("opaque-id", 999)
    assert dirty.set.call_count == 5
    assert dirty.mock_calls == [call for call in dirty.mock_calls if call[0] == "set"]


def test_registration_reads_then_unregisters_on_same_owner():
    enumerator, callback = Mock(), object()
    service = DeviceAwareness()

    def reader():
        service._stop.set()
        return evidence()

    service._reader = reader
    endpoint_events(service, enumerator, callback, threading.Event())
    enumerator.RegisterEndpointNotificationCallback.assert_called_once_with(callback)
    enumerator.UnregisterEndpointNotificationCallback.assert_called_once_with(callback)
    assert service._registration is None


def test_read_failure_still_unregisters():
    enumerator = Mock()
    service = DeviceAwareness(reader=Mock(side_effect=RuntimeError("fixture")))
    with pytest.raises(RuntimeError):
        endpoint_events(service, enumerator, object(), threading.Event())
    assert enumerator.UnregisterEndpointNotificationCallback.call_count == 1
    assert service._registration is None


def test_cleanup_failure_retains_callback_and_refuses_duplicate_registration():
    enumerator, callback = Mock(), object()
    enumerator.UnregisterEndpointNotificationCallback.side_effect = OSError("fixture")
    service = DeviceAwareness()
    service._stop.set()
    with pytest.raises(OSError):
        endpoint_events(service, enumerator, callback, threading.Event())
    assert service._registration == (enumerator, callback)
    assert not service.start() and not service.stop()


def test_failed_native_registration_has_explicit_reconciliation_coverage():
    updated = threading.Event()
    service = DeviceAwareness(
        reader=evidence,
        driver=Mock(side_effect=OSError("fixture")),
        publisher=lambda v, **kw: updated.set() if v["health"] == "degraded" else None,
    )
    assert service.start()
    try:
        assert updated.wait(2) and not service.start()
        assert service.snapshot()["coverage"] == "reconciliation-only"
    finally:
        assert service.stop()


def test_hud_snapshot_does_not_change_agent_state_or_share_device_buffers():
    from app.hud.events import HudBus

    bus = HudBus()
    before = deepcopy(bus.state())
    service = DeviceAwareness(publisher=bus.devices_observed)
    service.observe(evidence())
    assert bus.state() == before
    view = bus.snapshot()["deviceObservation"]
    view["endpoints"].clear()
    assert bus.snapshot()["deviceObservation"]["endpoints"]


def test_model_summary_limits_inventory_and_marks_unlisted(monkeypatch):
    from core import device_awareness

    service = DeviceAwareness()
    rows = evidence()
    rows["endpoints"] = [{**rows["endpoints"][0], "id": f"id-{i}"} for i in range(20)]
    rows["processDefaults"] = empty_defaults()
    service.observe(rows)
    monkeypatch.setattr(device_awareness, "get_device_awareness", lambda: service)
    view = device_awareness.device_summary()
    assert len(view["endpoints"]) == 8 and view["unlistedEndpoints"] == 12
    assert len(service.snapshot()["endpoints"]) == 20


def test_sta_event_pump_marks_dirty_and_worker_performs_readback(monkeypatch):
    from core import device_awareness as devices

    service = DeviceAwareness(reader=Mock(side_effect=[evidence(), evidence()]))
    waits = iter([False, True])
    service._stop = SimpleNamespace(wait=lambda _: next(waits))
    dirty, enumerator, callback = threading.Event(), Mock(), object()
    clock = iter([0.0, 0.3])
    monkeypatch.setattr(devices, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    pump = Mock(side_effect=dirty.set)
    endpoint_events(service, enumerator, callback, dirty, pump=pump)
    assert service._reader.call_count == 2
    pump.assert_called_once()
    assert service._registration is None


def test_passive_reader_queries_identity_and_roles_without_opening_audio(monkeypatch):
    import comtypes
    from pycaw.utils import AudioUtilities

    from core import audio_endpoints, device_awareness

    initialize, uninitialize = Mock(), Mock()
    monkeypatch.setattr(comtypes, "CoInitialize", initialize)
    monkeypatch.setattr(comtypes, "CoUninitialize", uninitialize)
    enumerator = Mock()
    enumerator.GetDefaultAudioEndpoint.return_value.GetId.return_value = "opaque-default-id"
    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", lambda: enumerator)
    inventory = Mock(return_value=[object()])
    monkeypatch.setattr(AudioUtilities, "GetAllDevices", inventory)
    monkeypatch.setattr(
        audio_endpoints,
        "_make",
        lambda d, flow: SimpleNamespace(
            id=f"{flow}-id", name="Endpoint", kind="microphone" if flow == "input" else "output"
        ),
    )
    forbidden = Mock(side_effect=AssertionError("must not activate audio"))
    monkeypatch.setattr(AudioUtilities, "GetSpeakers", forbidden)
    data = device_awareness.read_audio_devices()
    assert {r["flow"] for r in data["endpoints"]} == {"input", "output"}
    assert inventory.call_count == 2 and enumerator.GetDefaultAudioEndpoint.call_count == 6
    initialize.assert_called_once()
    uninitialize.assert_called_once()
    forbidden.assert_not_called()


@pytest.mark.parametrize("hresult", [-2147417850, 0x80010106])
def test_reader_borrows_existing_apartment_without_uninitializing_it(monkeypatch, hresult):
    import comtypes
    from pycaw.utils import AudioUtilities

    from core import device_awareness

    changed_mode = OSError("COM thread already initialized in another apartment")
    changed_mode.winerror = hresult
    monkeypatch.setattr(comtypes, "CoInitialize", Mock(side_effect=changed_mode))
    uninitialize = Mock()
    monkeypatch.setattr(comtypes, "CoUninitialize", uninitialize)
    enumerator = Mock()
    enumerator.GetDefaultAudioEndpoint.return_value.GetId.return_value = "existing-endpoint"
    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", lambda: enumerator)
    monkeypatch.setattr(AudioUtilities, "GetAllDevices", Mock(return_value=[]))

    result = device_awareness.read_audio_devices()

    assert result["processDefaults"]["output"]["multimedia"] == "existing-endpoint"
    assert enumerator.GetDefaultAudioEndpoint.call_count == 6
    uninitialize.assert_not_called()


def test_unrelated_com_initialization_failure_is_not_treated_as_a_usable_apartment(monkeypatch):
    import comtypes
    from pycaw.utils import AudioUtilities

    from core import device_awareness

    denied = OSError("access denied")
    denied.winerror = 5
    monkeypatch.setattr(comtypes, "CoInitialize", Mock(side_effect=denied))
    uninitialize, enumerate_devices = Mock(), Mock()
    monkeypatch.setattr(comtypes, "CoUninitialize", uninitialize)
    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", enumerate_devices)

    with pytest.raises(OSError) as caught:
        device_awareness.read_audio_devices()

    assert caught.value is denied
    uninitialize.assert_not_called()
    enumerate_devices.assert_not_called()


@pytest.mark.parametrize("borrowed", [False, True])
def test_notification_driver_releases_only_its_own_com_initialization(monkeypatch, borrowed):
    import comtypes
    from pycaw.utils import AudioUtilities

    from core import device_awareness

    changed_mode = OSError("COM apartment already selected")
    changed_mode.winerror = -2147417850
    monkeypatch.setattr(
        comtypes, "CoInitialize", Mock(side_effect=changed_mode if borrowed else None)
    )
    uninitialize, events = Mock(), Mock()
    monkeypatch.setattr(comtypes, "CoUninitialize", uninitialize)
    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", Mock())
    monkeypatch.setattr(device_awareness, "make_notifications", Mock())
    monkeypatch.setattr(device_awareness, "endpoint_events", events)
    device_awareness.native_audio_events(DeviceAwareness())

    events.assert_called_once()
    assert uninitialize.call_count == (0 if borrowed else 1)


def test_failed_notification_unregister_retains_owned_apartment(monkeypatch):
    import comtypes
    from pycaw.utils import AudioUtilities

    from core import device_awareness

    monkeypatch.setattr(comtypes, "CoInitialize", Mock())
    uninitialize = Mock()
    monkeypatch.setattr(comtypes, "CoUninitialize", uninitialize)
    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", Mock())
    monkeypatch.setattr(device_awareness, "make_notifications", Mock())

    def failed_unregister(observer, *_args, **_kwargs):
        observer._registration = ("enumerator", "callback")
        raise OSError("unregister failed")

    monkeypatch.setattr(device_awareness, "endpoint_events", failed_unregister)
    with pytest.raises(OSError, match="unregister failed"):
        device_awareness.native_audio_events(DeviceAwareness())
    uninitialize.assert_not_called()
