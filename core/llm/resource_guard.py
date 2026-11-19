"""Cheap admission control before Ollama cold-loads a generative model.

The normal hot path only asks Ollama's local ``/api/ps`` endpoint. Temperature
and RAM are sampled only when the requested model is not already resident. This
keeps voice latency unchanged while preventing a new multi-gigabyte load when
the laptop is already hot or short on memory.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str = ""
    cold_load: bool = True
    gpu_temp_c: float | None = None
    available_ram_gb: float | None = None


def _setting(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _loaded_models(base_url: str, timeout_s: float = 0.5) -> set[str]:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/ps", timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {
            str(row.get("name") or row.get("model") or "").strip()
            for row in body.get("models") or []
            if isinstance(row, dict)
        }
    except Exception:  # noqa: BLE001 - unknown means perform the safety sample
        return set()


def _gpu_temp_c() -> float | None:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            creationflags=flags,
        )
        first = result.stdout.strip().splitlines()[0]
        return float(first)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _available_ram_gb() -> float | None:
    if sys.platform == "win32":
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullAvailPhys / (1024**3)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return float(pages * page_size) / (1024**3)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def admit_cold_load(
    model: str,
    base_url: str,
    *,
    loaded_reader: Callable[[str], set[str]] | None = None,
    temp_reader: Callable[[], float | None] | None = None,
    ram_reader: Callable[[], float | None] | None = None,
) -> Admission:
    """Return whether ``model`` may start without pressuring the laptop.

    Missing telemetry is recorded but does not disable local AI: Ollama's
    one-model/one-request limits remain the hard memory boundary in that case.
    """
    loaded = (loaded_reader or _loaded_models)(base_url)
    if model in loaded or model.split(":", 1)[0] in loaded:
        return Admission(True, cold_load=False)

    temp = (temp_reader or _gpu_temp_c)()
    ram = (ram_reader or _available_ram_gb)()
    max_temp = _setting("ERA_LOCAL_MAX_GPU_TEMP_C", 82.0, 60.0, 95.0)
    min_ram = _setting("ERA_LOCAL_MIN_AVAILABLE_RAM_GB", 4.0, 1.0, 16.0)
    if temp is not None and temp >= max_temp:
        return Admission(
            False,
            f"local model load paused: GPU is {temp:.0f} C (limit {max_temp:.0f} C)",
            gpu_temp_c=temp,
            available_ram_gb=ram,
        )
    if ram is not None and ram < min_ram:
        return Admission(
            False,
            f"local model load paused: only {ram:.1f} GiB RAM is available",
            gpu_temp_c=temp,
            available_ram_gb=ram,
        )
    return Admission(True, gpu_temp_c=temp, available_ram_gb=ram)
