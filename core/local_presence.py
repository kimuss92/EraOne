"""Physical keyboard/mouse return-home evidence; ignore injected automation."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable


def physical_input(kind: str, message: int, flags: int) -> bool:
    if kind == "keyboard":
        return message in {0x100, 0x104} and not flags & 0x12
    if kind == "mouse":
        return message in {0x201, 0x204, 0x207, 0x20B, 0x20A, 0x20E} and not flags & 0x03
    return False


class LocalPresence:
    def __init__(self, notify: Callable[[], None]) -> None:
        self.notify = notify
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self.available = False
        self._last_event = 0.0

    def _observed(self) -> None:
        now = time.monotonic()
        if now - self._last_event < 1 or self._stopping.is_set():
            return
        self._last_event = now
        try:
            self.notify()
        except Exception:
            pass

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="era-local-presence", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self.available

    def stop(self) -> None:
        self._stopping.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x12, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        from ctypes import wintypes

        class Keyboard(ctypes.Structure):
            _fields_ = [
                ("vk", wintypes.DWORD),
                ("scan", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        class Mouse(ctypes.Structure):
            _fields_ = [
                ("point", wintypes.POINT),
                ("data", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
        )
        user.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            callback_type,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.CallNextHookEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user.CallNextHookEx.restype = ctypes.c_ssize_t
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user.UnhookWindowsHookEx.restype = wintypes.BOOL
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = kernel.GetCurrentThreadId()

        def keyboard(code: int, message: int, pointer: int) -> int:
            if code == 0 and physical_input(
                "keyboard",
                message,
                ctypes.cast(pointer, ctypes.POINTER(Keyboard)).contents.flags,
            ):
                self._observed()
            return int(user.CallNextHookEx(None, code, message, pointer))

        def mouse(code: int, message: int, pointer: int) -> int:
            if code == 0 and physical_input(
                "mouse",
                message,
                ctypes.cast(pointer, ctypes.POINTER(Mouse)).contents.flags,
            ):
                self._observed()
            return int(user.CallNextHookEx(None, code, message, pointer))

        keys, buttons = callback_type(keyboard), callback_type(mouse)
        handles = []
        try:
            # A queue is needed before another thread can post WM_QUIT.
            msg = wintypes.MSG()
            user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            for kind, callback in ((13, keys), (14, buttons)):
                handle = user.SetWindowsHookExW(kind, callback, kernel.GetModuleHandleW(None), 0)
                if not handle:
                    raise OSError("Local input hook unavailable.")
                handles.append(handle)
            self.available = True
            self._ready.set()
            while (
                not self._stopping.is_set() and user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0
            ):
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self.available = False
        finally:
            for handle in handles:
                user.UnhookWindowsHookEx(handle)
            self.available = False
            self._ready.set()
