"""Read-only local interface evidence; never infer internet access from link up."""

from __future__ import annotations

import os
import socket
import threading
import time
from copy import deepcopy

from core.foreground_awareness import FRESH_S, ForegroundAwareness, _log


def windows_interface_index(name):
    import ctypes
    from ctypes import wintypes

    library = ctypes.WinDLL("iphlpapi")
    luid, index = ctypes.c_ulonglong(), wintypes.ULONG()
    library.ConvertInterfaceAliasToLuid.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
    ]
    library.ConvertInterfaceAliasToLuid.restype = wintypes.ULONG
    library.ConvertInterfaceLuidToIndex.argtypes = [
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(wintypes.ULONG),
    ]
    library.ConvertInterfaceLuidToIndex.restype = wintypes.ULONG
    if (
        library.ConvertInterfaceAliasToLuid(name, ctypes.byref(luid)) == 0
        and library.ConvertInterfaceLuidToIndex(ctypes.byref(luid), ctypes.byref(index)) == 0
        and index.value > 0
    ):
        return index.value
    return None


def read_interfaces():
    import psutil

    rows = []
    for name, info in psutil.net_if_stats().items():
        try:
            index = socket.if_nametoindex(name)
        except (OSError, ValueError):
            index = None
            if os.name == "nt":
                try:
                    index = windows_interface_index(name)
                except OSError:
                    pass
        rows.append({"name": name, "index": index, "isUp": bool(info.isup)})
    return rows


def native_network_events(observer):
    if observer._registration is not None:
        raise RuntimeError("previous network registration still active")
    if os.name != "nt":
        raise OSError("Windows network events unavailable")
    import ctypes
    from ctypes import wintypes

    library = ctypes.WinDLL("iphlpapi")
    change = threading.Event()
    callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
    callback = callback_type(lambda *_ignored: change.set())
    handle = wintypes.HANDLE()
    library.NotifyIpInterfaceChange.argtypes = [
        ctypes.c_ushort,
        callback_type,
        ctypes.c_void_p,
        ctypes.c_ubyte,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    library.NotifyIpInterfaceChange.restype = wintypes.ULONG
    library.CancelMibChangeNotify2.argtypes = [wintypes.HANDLE]
    library.CancelMibChangeNotify2.restype = wintypes.ULONG
    if library.NotifyIpInterfaceChange(0, callback, None, 0, ctypes.byref(handle)) != 0:
        raise OSError("network notification registration failed")
    observer._registration = (library, handle, callback)
    try:
        observer.observe(observer._reader())
        last_read = time.monotonic()
        while not observer._stop.wait(0.1):
            now = time.monotonic()
            if now - last_read >= 15 or (change.is_set() and now - last_read >= 0.25):
                change.clear()
                try:
                    observer.observe(observer._reader())
                except Exception as exc:
                    observer.unavailable()
                    _log("read_failed", exc, kind="network")
                last_read = now
    finally:
        # Do not cancel from the callback: Windows documents that as a deadlock.
        if library.CancelMibChangeNotify2(handle) != 0:
            raise OSError("network notification cancellation failed")
        observer._registration = None


class NetworkAwareness(ForegroundAwareness):
    kind = "network"

    def __init__(
        self,
        *,
        publisher=None,
        reader=read_interfaces,
        driver=native_network_events,
        clock=time.time,
    ):
        super().__init__(publisher=publisher, driver=driver, clock=clock)
        self._reader, self._fallback_read, self._max_retry_s = reader, reader, 15.0
        self._registration = None
        self._view = {
            "revision": 0,
            "health": "unavailable",
            "coverage": "none",
            "observedAt": 0.0,
            "interfaces": [],
            "internet": "unknown",
        }

    def start(self):
        if self._registration is not None:
            return False
        return super().start()

    def stop(self):
        stopped = super().stop()
        if stopped and self._registration is not None:
            library, handle, _callback = self._registration
            if library.CancelMibChangeNotify2(handle) == 0:
                self._registration = None
        return stopped and self._registration is None

    def snapshot(self):
        with self._lock:
            view = deepcopy(self._view)
        if (
            view["health"] in ("ready", "degraded")
            and not 0 <= self._clock() - view["observedAt"] <= FRESH_S
        ):
            view["health"], view["interfaces"], view["coverage"] = "stale", [], "none"
        return view

    def observe(self, rows, *, coverage="interface-events+reconciliation"):
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError("invalid network inventory")
        names, indexes = set(), set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"name", "index", "isUp"}:
                raise ValueError("invalid network fields")
            name, index, up = row["name"], row["index"], row["isUp"]
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 256
                or name in names
                or type(up) is not bool
                or (
                    index is not None and (type(index) is not int or index <= 0 or index in indexes)
                )
            ):
                raise ValueError("invalid network identity")
            names.add(name)
            if index is not None:
                indexes.add(index)
        health = (
            "degraded"
            if coverage == "reconciliation-only" or any(r["index"] is None for r in rows)
            else "ready"
        )
        self._publish(health, sorted(rows, key=lambda row: row["name"]), coverage, self._clock())

    def unavailable(self):
        self._publish("unavailable", [], "none", 0.0)

    def _publish(self, health, rows, coverage, observed_at):
        with self._lock:
            old = self._view
            changed = (old["health"], old["interfaces"], old["coverage"]) != (
                health,
                rows,
                coverage,
            )
            self._view = {
                "revision": old["revision"] + 1,
                "health": health,
                "coverage": coverage,
                "observedAt": observed_at,
                "interfaces": deepcopy(rows),
                "internet": "unknown",
            }
            view = deepcopy(self._view)
        if self._publisher:
            try:
                self._publisher(view, changed=changed)
            except Exception as exc:
                _log("publish_failed", exc, kind=self.kind)


_service = None
_service_lock = threading.Lock()


def get_network_awareness():
    global _service
    with _service_lock:
        if _service is None:
            from app.hud.events import hud

            _service = NetworkAwareness(publisher=hud.network_observed)
        return _service
