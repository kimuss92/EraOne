"""Passive Windows present-device inventory and native hotplug notifications."""

from __future__ import annotations

import ctypes
import os
import struct
import threading
import time
import uuid
from copy import deepcopy

from core.foreground_awareness import FRESH_S, ForegroundAwareness, _log


def read_present_devices():
    if os.name != "nt":
        raise OSError("Windows PnP inventory unavailable")
    from ctypes import wintypes

    cm = ctypes.WinDLL("cfgmgr32")
    cm.CM_Get_Device_ID_List_SizeW.argtypes = [
        ctypes.POINTER(wintypes.ULONG),
        wintypes.LPCWSTR,
        wintypes.ULONG,
    ]
    cm.CM_Get_Device_ID_ListW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    cm.CM_Locate_DevNodeW.argtypes = [
        ctypes.POINTER(wintypes.ULONG),
        wintypes.LPWSTR,
        wintypes.ULONG,
    ]
    cm.CM_Get_DevNode_Status.argtypes = [
        ctypes.POINTER(wintypes.ULONG),
        ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    length = wintypes.ULONG()
    for _ in range(3):
        if (
            cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(length), None, 0x100)
            or not 1 <= length.value <= 2000000
        ):
            raise OSError("PnP inventory sizing failed")
        buffer = ctypes.create_unicode_buffer(length.value)
        result = cm.CM_Get_Device_ID_ListW(None, buffer, length.value, 0x100)
        if result == 0x1A:  # CR_BUFFER_SMALL: inventory changed between calls.
            continue
        if result:
            raise OSError("PnP inventory read failed")
        ids = sorted(set(buffer[:].rstrip("\0").split("\0")))
        rows = []
        for identity in ids[:4096]:
            if not identity:
                continue
            node, status, problem = wintypes.ULONG(), wintypes.ULONG(), wintypes.ULONG()
            known = not cm.CM_Locate_DevNodeW(
                ctypes.byref(node), identity, 0
            ) and not cm.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), node, 0)
            rows.append(
                {
                    "id": identity,
                    "bus": identity.split("\\", 1)[0],
                    "started": bool(status.value & 8) if known else None,
                    "problem": int(problem.value) if known else None,
                }
            )
        return {"devices": rows, "unlistedDevices": max(0, len(ids) - 4096)}
    raise OSError("PnP inventory kept changing")


def native_pnp_events(service):
    if os.name != "nt":
        raise OSError("Windows PnP notifications unavailable")
    import win32api
    import win32gui

    dirty = threading.Event()
    name = "EraPnP_" + uuid.uuid4().hex

    def callback(hwnd, message, wparam, lparam):
        if message == 0x219:  # WM_DEVICECHANGE; callback never enumerates devices.
            dirty.set()
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    cls = win32gui.WNDCLASS()
    cls.lpszClassName, cls.lpfnWndProc = name, callback
    cls.hInstance = win32api.GetModuleHandle(None)
    atom = win32gui.RegisterClass(cls)
    hwnd = notification = None
    try:
        hwnd = win32gui.CreateWindowEx(0, atom, name, 0, 0, 0, 0, 0, 0, 0, cls.hInstance, None)
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.RegisterDeviceNotificationW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        user.RegisterDeviceNotificationW.restype = ctypes.c_void_p
        user.UnregisterDeviceNotification.argtypes = [ctypes.c_void_p]
        user.UnregisterDeviceNotification.restype = ctypes.c_int
        payload = ctypes.create_string_buffer(struct.pack("III", 28, 5, 0) + b"\0" * 16)
        notification = user.RegisterDeviceNotificationW(hwnd, payload, 4)
        if not notification:
            raise OSError("PnP notification registration failed")
        service.observe(service._reader())
        last = time.monotonic()
        while not service._stop.wait(0.05):
            win32gui.PumpWaitingMessages()
            now = time.monotonic()
            if now - last >= 15 or (dirty.is_set() and now - last >= 0.25):
                dirty.clear()
                service.observe(service._reader())
                last = now
    finally:
        if notification and not user.UnregisterDeviceNotification(notification):
            _log("unregister_failed", OSError(), kind="hardware")
        if hwnd:
            win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(atom, cls.hInstance)


class PnpAwareness(ForegroundAwareness):
    kind = "hardware"

    def __init__(
        self,
        *,
        publisher=None,
        reader=read_present_devices,
        driver=native_pnp_events,
        clock=time.time,
    ):
        super().__init__(publisher=publisher, driver=driver, clock=clock)
        self._reader, self._fallback_read, self._max_retry_s = reader, reader, 15.0
        self._view = {
            "revision": 0,
            "health": "unavailable",
            "coverage": "none",
            "observedAt": 0.0,
            "devices": [],
            "unlistedDevices": 0,
        }

    def snapshot(self):
        with self._lock:
            view = deepcopy(self._view)
        if (
            view["health"] in {"ready", "degraded"}
            and not 0 <= self._clock() - view["observedAt"] <= FRESH_S
        ):
            view.update(health="stale", coverage="none", devices=[])
        return view

    def observe(self, evidence, *, coverage="device-interface-events+reconciliation"):
        if not isinstance(evidence, dict) or set(evidence) != {
            "devices",
            "unlistedDevices",
        }:
            raise ValueError("Invalid PnP evidence")
        rows, hidden = evidence["devices"], evidence["unlistedDevices"]
        if type(hidden) is not int or hidden < 0 or not isinstance(rows, list) or len(rows) > 4096:
            raise ValueError("Invalid PnP inventory")
        seen = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "bus", "started", "problem"}
                or not isinstance(row["id"], str)
                or not 0 < len(row["id"]) <= 512
                or row["id"] in seen
                or not isinstance(row["bus"], str)
                or not 0 < len(row["bus"]) <= 64
                or (row["started"] is not None and type(row["started"]) is not bool)
                or (
                    row["problem"] is not None
                    and (type(row["problem"]) is not int or row["problem"] < 0)
                )
            ):
                raise ValueError("Invalid PnP identity/status")
            seen.add(row["id"])
        self._publish(
            "degraded" if hidden or coverage == "reconciliation-only" else "ready",
            rows,
            hidden,
            coverage,
        )

    def unavailable(self):
        self._publish("unavailable", [], 0, "none")

    def _publish(self, health, rows, hidden, coverage):
        with self._lock:
            changed = (
                self._view["health"],
                self._view["devices"],
                self._view["coverage"],
                self._view["unlistedDevices"],
            ) != (health, rows, coverage, hidden)
            self._view = {
                "revision": self._view["revision"] + 1,
                "health": health,
                "coverage": coverage,
                "observedAt": self._clock(),
                "devices": deepcopy(rows),
                "unlistedDevices": hidden,
            }
            view = deepcopy(self._view)
        if self._publisher:
            self._publisher(view, changed=changed)


_service = None
_lock = threading.Lock()


def get_pnp_awareness():
    global _service
    with _lock:
        if _service is None:

            def publish(_view, *, changed=True):
                from core.device_awareness import get_device_awareness

                service = get_device_awareness()
                with service._lock:
                    service._view["revision"] += 1
                if service._publisher:
                    service._publisher(service.snapshot(), changed=changed)

            _service = PnpAwareness(publisher=publish)
        return _service
