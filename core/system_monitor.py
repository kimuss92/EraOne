"""
system_monitor.py — Real-time system metrics collector for ERA ONE.

Collects CPU, RAM, network, GPU, and system info via psutil + optional NVML.
Runs in a background thread, pushes metrics every N seconds.
"""

from __future__ import annotations

import threading
import time
from typing import Any

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# Optional NVIDIA GPU monitoring
_nvml = None
_nvml_handle = None
_nvml_available = False


def _init_nvml() -> None:
    """Try to initialize NVIDIA NVML for GPU metrics."""
    global _nvml, _nvml_handle, _nvml_available
    try:
        import pynvml

        _nvml = pynvml
        pynvml.nvmlInit()
        _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        _nvml_available = True
    except Exception:
        _nvml_available = False


def _shutdown_nvml() -> None:
    global _nvml, _nvml_handle, _nvml_available
    if _nvml and _nvml_available:
        try:
            _nvml.nvmlShutdown()
        except Exception:
            pass
    _nvml = None
    _nvml_handle = None
    _nvml_available = False


class SystemMonitor:
    """Background collector for real system metrics.

    Usage:
        monitor = SystemMonitor(interval=2.0)
        monitor.start()
        metrics = monitor.get_latest()
        monitor.stop()
    """

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.running = False
        self.latest: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._prev_net_sent = 0
        self._prev_net_recv = 0
        self._prev_net_time = 0.0

        if _PSUTIL_AVAILABLE:
            _init_nvml()
            # Prime cpu_percent (first call returns 0.0)
            psutil.cpu_percent(interval=None)

    def start(self) -> None:
        """Start the background metrics collection thread."""
        if not _PSUTIL_AVAILABLE:
            return
        self.running = True
        self._prev_net_time = time.time()
        try:
            net = psutil.net_io_counters()
            self._prev_net_sent = net.bytes_sent
            self._prev_net_recv = net.bytes_recv
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop collection and clean up GPU handles."""
        self.running = False
        _shutdown_nvml()

    def get_latest(self) -> dict[str, Any]:
        """Return the most recently collected metrics dict."""
        return self.latest.copy()

    # ── Internal ────────────────────────────────────────────────

    def _loop(self) -> None:
        while self.running:
            try:
                self.latest = self._collect()
            except Exception:
                self.latest = {}
            time.sleep(self.interval)

    def _collect(self) -> dict[str, Any]:
        if not _PSUTIL_AVAILABLE:
            return {}
        data: dict[str, Any] = {}

        # CPU
        try:
            data["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        except Exception:
            data["cpu_percent"] = None

        # RAM
        try:
            mem = psutil.virtual_memory()
            data["ram_percent"] = mem.percent
            data["ram_used_gb"] = round(mem.used / (1024**3), 1)
            data["ram_total_gb"] = round(mem.total / (1024**3), 1)
        except Exception:
            data["ram_percent"] = None
            data["ram_used_gb"] = None
            data["ram_total_gb"] = None

        # Network (bytes per second)
        try:
            now = time.time()
            net = psutil.net_io_counters()
            dt = now - self._prev_net_time if self._prev_net_time else 0
            if dt > 0:
                data["net_sent_bytes_per_sec"] = round((net.bytes_sent - self._prev_net_sent) / dt)
                data["net_recv_bytes_per_sec"] = (
                    round((net.recv_bytes - self._prev_net_recv) / dt)
                    if hasattr(net, "recv_bytes")
                    else round((net.bytes_recv - self._prev_net_recv) / dt)
                )
            else:
                data["net_sent_bytes_per_sec"] = 0
                data["net_recv_bytes_per_sec"] = 0
            self._prev_net_sent = net.bytes_sent
            self._prev_net_recv = net.bytes_recv if hasattr(net, "bytes_recv") else net.bytes_recv
            self._prev_net_time = now
        except Exception:
            data["net_sent_bytes_per_sec"] = None
            data["net_recv_bytes_per_sec"] = None

        # Wi-Fi connected
        data["wifi_connected"] = self._check_wifi()

        # GPU (NVIDIA via NVML)
        if _nvml_available and _nvml and _nvml_handle:
            try:
                util = _nvml.nvmlDeviceGetUtilizationRates(_nvml_handle)
                data["gpu_usage_percent"] = util.gpu
            except Exception:
                data["gpu_usage_percent"] = None
            try:
                data["gpu_temp_celsius"] = _nvml.nvmlDeviceGetTemperature(
                    _nvml_handle, _nvml.NVML_TEMPERATURE_GPU
                )
            except Exception:
                data["gpu_temp_celsius"] = None
            try:
                mem_info = _nvml.nvmlDeviceGetMemoryInfo(_nvml_handle)
                data["gpu_mem_used_mb"] = round(mem_info.used / (1024**2))
                data["gpu_mem_total_mb"] = round(mem_info.total / (1024**2))
            except Exception:
                data["gpu_mem_used_mb"] = None
                data["gpu_mem_total_mb"] = None
        else:
            data["gpu_usage_percent"] = None
            data["gpu_temp_celsius"] = None
            data["gpu_mem_used_mb"] = None
            data["gpu_mem_total_mb"] = None

        # Uptime
        try:
            data["uptime_seconds"] = int(time.time() - psutil.boot_time())
        except Exception:
            data["uptime_seconds"] = None

        # Process count
        try:
            data["process_count"] = len(psutil.pids())
        except Exception:
            data["process_count"] = None

        return data

    @staticmethod
    def _check_wifi() -> bool | None:
        """Check if a Wi-Fi/WLAN interface has an active IPv4 address."""
        try:
            addrs = psutil.net_if_addrs()
            for iface, addr_list in addrs.items():
                name_lower = iface.lower()
                if "wi-fi" in name_lower or "wlan" in name_lower or "wifi" in name_lower:
                    for addr in addr_list:
                        try:
                            if addr.family.name == "AF_INET":
                                return True
                        except Exception:
                            continue
            return False
        except Exception:
            return None
