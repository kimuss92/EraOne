"""Passive Core Audio endpoint evidence; does not open or reroute streams."""

from __future__ import annotations

import os
import threading
import time
from copy import deepcopy

from core.foreground_awareness import FRESH_S, ForegroundAwareness, _log

ROLES = ("console", "multimedia", "communications")
FLOWS = ("input", "output")


def empty_defaults():
    return {flow: dict.fromkeys(ROLES) for flow in FLOWS}


def _initialize_com() -> bool:
    """Return ownership of one COM initialization, or borrow an existing MTA.

    Core Audio enumeration and notifications support either apartment. Era sets
    MTA at startup; CoInitialize requests STA and cannot replace that mode.
    Failed initialization adds no reference, so a borrowed apartment must never
    be balanced with CoUninitialize. All unrelated failures remain errors.
    """
    import comtypes

    try:
        comtypes.CoInitialize()
    except OSError as exc:
        if getattr(exc, "winerror", None) not in (-2147417850, 0x80010106):
            raise
        return False
    return True


def read_audio_devices():
    import comtypes
    from pycaw.constants import DEVICE_STATE
    from pycaw.utils import AudioUtilities

    from core.audio_endpoints import _make

    owns_com = _initialize_com()
    try:
        endpoints, defaults = [], empty_defaults()
        enumerator = AudioUtilities.GetDeviceEnumerator()
        for flow, flow_id in (("output", 0), ("input", 1)):
            for device in AudioUtilities.GetAllDevices(flow_id, DEVICE_STATE.ACTIVE.value):
                endpoint = _make(device, flow)
                endpoints.append(
                    {"id": endpoint.id, "name": endpoint.name, "flow": flow, "kind": endpoint.kind}
                )
            for role_id, role in enumerate(ROLES):
                try:
                    defaults[flow][role] = str(
                        enumerator.GetDefaultAudioEndpoint(flow_id, role_id).GetId()
                    )
                except comtypes.COMError as exc:
                    # No default endpoint is a valid observation; other COM
                    # failures must not masquerade as disconnected hardware.
                    if exc.hresult != -2147023728:  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
                        raise
        return {"endpoints": endpoints, "processDefaults": defaults}
    finally:
        if owns_com:
            comtypes.CoUninitialize()


def make_notifications(base, dirty):
    class Notifications(base):
        # Callbacks only flag dirty. Enumeration/publication/unregistration
        # happen on the worker, never in Windows' callback context.
        def OnDefaultDeviceChanged(self, *_args):
            dirty.set()

        def OnDeviceAdded(self, *_args):
            dirty.set()

        def OnDeviceRemoved(self, *_args):
            dirty.set()

        def OnDeviceStateChanged(self, *_args):
            dirty.set()

        def OnPropertyValueChanged(self, *_args):
            dirty.set()

    return Notifications()


def endpoint_events(observer, enumerator, callback, dirty, *, pump=lambda: None):
    """Own one registration; retain references if Windows refuses cleanup."""
    if observer._registration is not None:
        raise RuntimeError("previous endpoint registration still active")
    enumerator.RegisterEndpointNotificationCallback(callback)
    observer._registration = (enumerator, callback)
    try:
        observer.observe(observer._reader())
        last_read = time.monotonic()
        while not observer._stop.wait(0.05):
            pump()
            now = time.monotonic()
            if now - last_read >= 15 or (dirty.is_set() and now - last_read >= 0.25):
                dirty.clear()
                try:
                    observer.observe(observer._reader())
                except Exception as exc:
                    observer.unavailable()
                    _log("read_failed", exc, kind="devices")
                last_read = now
    finally:
        enumerator.UnregisterEndpointNotificationCallback(callback)
        observer._registration = None


def native_audio_events(observer):
    if os.name != "nt":
        raise OSError("Windows audio endpoint notifications unavailable")
    if observer._registration is not None:
        raise RuntimeError("previous endpoint registration still active")
    import comtypes
    import pythoncom
    from pycaw.callbacks import MMNotificationClient
    from pycaw.utils import AudioUtilities

    owns_com = _initialize_com()
    try:
        dirty = threading.Event()
        endpoint_events(
            observer,
            AudioUtilities.GetDeviceEnumerator(),
            make_notifications(MMNotificationClient, dirty),
            dirty,
            pump=pythoncom.PumpWaitingMessages,
        )
    finally:
        if owns_com and observer._registration is None:
            comtypes.CoUninitialize()


class DeviceAwareness(ForegroundAwareness):
    kind = "devices"

    def __init__(
        self,
        *,
        publisher=None,
        reader=read_audio_devices,
        driver=native_audio_events,
        clock=time.time,
    ):
        super().__init__(publisher=publisher, driver=driver, clock=clock)
        self._reader, self._fallback_read, self._max_retry_s = reader, reader, 15.0
        self._registration = None
        self._view = self._state(0, "unavailable", [], empty_defaults(), "none", 0.0)

    def start(self):
        if self._registration is not None:
            return False
        return super().start()

    def stop(self):
        return super().stop() and self._registration is None

    @staticmethod
    def _state(revision, health, endpoints, defaults, coverage, observed_at):
        return {
            "revision": revision,
            "health": health,
            "observedAt": observed_at,
            "coverage": coverage,
            "scope": "active-audio-endpoints",
            "endpoints": deepcopy(endpoints),
            "processDefaults": deepcopy(defaults),
            "bluetoothConnection": "unknown",
            "streamRoute": "unknown",
        }

    def snapshot(self):
        with self._lock:
            view = deepcopy(self._view)
        if (
            view["health"] in ("ready", "degraded")
            and not 0 <= self._clock() - view["observedAt"] <= FRESH_S
        ):
            view.update(
                health="stale", endpoints=[], processDefaults=empty_defaults(), coverage="none"
            )
        from core.pnp_awareness import get_pnp_awareness

        view["hardware"] = get_pnp_awareness().snapshot()
        return view

    def observe(self, evidence, *, coverage="endpoint-events+reconciliation"):
        if coverage not in {"endpoint-events+reconciliation", "reconciliation-only"}:
            raise ValueError("invalid endpoint coverage")
        if not isinstance(evidence, dict) or set(evidence) != {"endpoints", "processDefaults"}:
            raise ValueError("invalid endpoint evidence")
        rows, defaults = evidence["endpoints"], evidence["processDefaults"]
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError("invalid endpoint inventory")
        ids = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "name", "flow", "kind"}
                or row["flow"] not in FLOWS
                or any(
                    not isinstance(row[key], str) or not row[key] or len(row[key]) > maximum
                    for key, maximum in (("id", 512), ("name", 256), ("kind", 64))
                )
                or row["id"] in ids
            ):
                raise ValueError("invalid endpoint identity")
            ids.add(row["id"])
        if not isinstance(defaults, dict) or set(defaults) != set(FLOWS):
            raise ValueError("invalid endpoint defaults")
        mismatch = False
        for flow, roles in defaults.items():
            if not isinstance(roles, dict) or set(roles) != set(ROLES):
                raise ValueError("invalid endpoint roles")
            for endpoint_id in roles.values():
                if endpoint_id is not None:
                    if (
                        not isinstance(endpoint_id, str)
                        or not endpoint_id
                        or len(endpoint_id) > 512
                    ):
                        raise ValueError("invalid default identity")
                    mismatch |= not any(r["id"] == endpoint_id and r["flow"] == flow for r in rows)
        health = "degraded" if coverage == "reconciliation-only" or mismatch else "ready"
        self._publish(
            health,
            sorted(rows, key=lambda row: (row["flow"], row["id"])),
            defaults,
            coverage,
            self._clock(),
        )

    def unavailable(self):
        self._publish("unavailable", [], empty_defaults(), "none", 0.0)

    def _publish(self, health, rows, defaults, coverage, observed_at):
        with self._lock:
            old = self._view
            changed = (
                old["health"],
                old["endpoints"],
                old["processDefaults"],
                old["coverage"],
            ) != (health, rows, defaults, coverage)
            self._view = self._state(
                old["revision"] + 1, health, rows, defaults, coverage, observed_at
            )
            view = self.snapshot()
        if self._publisher:
            try:
                self._publisher(view, changed=changed)
            except Exception as exc:
                _log("publish_failed", exc, kind=self.kind)


_service = None
_service_lock = threading.Lock()


def get_device_awareness():
    global _service
    with _service_lock:
        if _service is None:
            from app.hud.events import hud

            _service = DeviceAwareness(publisher=hud.devices_observed)
        return _service


def device_summary():
    view = get_device_awareness().snapshot()
    hardware = view["hardware"]
    view["hardware"] = {
        **hardware,
        "devices": hardware["devices"][:8],
        "unlistedDevices": hardware["unlistedDevices"] + max(0, len(hardware["devices"]) - 8),
    }
    return {
        **view,
        "endpoints": view["endpoints"][:8],
        "unlistedEndpoints": max(0, len(view["endpoints"]) - 8),
    }
