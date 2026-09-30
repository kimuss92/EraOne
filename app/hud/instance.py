"""Single-instance guards and the backend's discovery file.

Named Windows mutexes are the mechanism: the kernel releases them when the
owning process dies, however it dies, so there is never a stale lock to clean
up — which is exactly what a PID file cannot promise after a hard kill.

  Local\\EraOne.Backend     held by main.py for its whole life
  Local\\EraOne.Supervisor  held by era_supervisor.py
  (suffixed with a hash of ERA_HOME when that is overridden, e.g. in tests)

The discovery file (~/.era/eraone_backend.json) only tells the launcher WHERE
the backend listens (the port may have fallen back); it is advisory. Liveness is
always confirmed against the mutex and /api/health, never taken from the file.
It deliberately holds no session token.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# main.py exits with this when another backend already owns Era; the supervisor
# treats it as "someone else is running Era" and stands down instead of looping.
# (Not 3: that is an ordinary crash code the supervisor tests use.)
EXIT_ALREADY_RUNNING = 86

_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000


def era_home() -> Path:
    return Path(os.environ.get("ERA_HOME") or (Path.home() / ".era"))


def _scoped(base: str) -> str:
    """An overridden ERA_HOME (tests, stand-ins) gets its own mutex name, so a
    test instance never collides with the real, running Era. Must match
    era_supervisor.SUPERVISOR_MUTEX."""
    if not os.environ.get("ERA_HOME"):
        return base
    import hashlib

    return base + "." + hashlib.sha1(str(era_home()).lower().encode()).hexdigest()[:10]


def backend_mutex_name() -> str:
    return _scoped("Local\\EraOne.Backend")


def supervisor_mutex_name() -> str:
    return _scoped("Local\\EraOne.Supervisor")


def backend_info_path() -> Path:
    return era_home() / "eraone_backend.json"


def startup_error_path() -> Path:
    return era_home() / "eraone_startup_error.json"


class NamedMutex:
    """A held Windows named mutex. On other platforms it degrades to a no-op
    that always acquires (Era only ships on Windows; tests run anywhere)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        """True if this process now owns the name; False if another does."""
        if sys.platform != "win32":
            return True
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = k32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            return False
        if err == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle and sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None


def mutex_exists(name: str) -> bool:
    """Is some process holding this name right now? (Does not take it.)"""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.OpenMutexW(_SYNCHRONIZE, False, name)
    if handle:
        k32.CloseHandle(handle)
        return True
    return False


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def write_backend_info(*, port: int, ui_mode: str) -> None:
    try:
        _atomic_write(
            backend_info_path(),
            {
                "pid": os.getpid(),
                "port": int(port),
                "url": f"http://127.0.0.1:{int(port)}/",
                "ui_mode": ui_mode,
                "started_at": time.time(),
            },
        )
        clear_startup_error()
    except OSError:
        pass


def read_backend_info() -> dict | None:
    try:
        data = json.loads(backend_info_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_backend_info(pid: int | None = None) -> None:
    """Remove the discovery file — only if it is ours (a newer backend may own it)."""
    info = read_backend_info()
    if info is None:
        return
    if pid is not None and info.get("pid") != pid:
        return
    try:
        backend_info_path().unlink()
    except OSError:
        pass


def write_startup_error(reason: str, detail: str = "") -> None:
    """Explain a startup failure to the launcher, which has no console to read."""
    try:
        _atomic_write(
            startup_error_path(),
            {"reason": reason, "detail": detail, "pid": os.getpid(), "at": time.time()},
        )
    except OSError:
        pass


def read_startup_error(newer_than: float = 0.0) -> dict | None:
    try:
        data = json.loads(startup_error_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or float(data.get("at") or 0) < newer_than:
        return None
    return data


def clear_startup_error() -> None:
    try:
        startup_error_path().unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000 | _SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
