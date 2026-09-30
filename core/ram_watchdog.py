"""
ram_watchdog.py — automatic PC Boost when RAM pressure crosses a threshold.

Era already samples RAM for the UI (core/system_monitor.py), but that collector
only reports. This is the acting half: a daemon thread that reads
`psutil.virtual_memory().percent` on a short interval and, above the
threshold (default 87%), fires `actions.system_performance.pc_boost()` — Microsoft PC Manager's memory +
temp-file cleanup — with no prompt.

Because pc_boost() drives PC Manager through UI automation (it launches and
briefly focuses the app), a cooldown is mandatory rather than cosmetic: without
one, a machine that simply runs hot would get its foreground stolen every
polling interval. The cooldown starts when the boost RETURNS, and it applies to
failed attempts too — otherwise a machine with no PC Manager installed would
retry the launch forever.

Everything is overridable by environment variable so the behaviour can be tuned
or switched off without editing code:

    ERA_RAM_WATCHDOG=0          disable entirely
    ERA_RAM_BOOST_THRESHOLD=80  percent RAM that triggers a boost
    ERA_RAM_BOOST_INTERVAL=300  seconds between samples
    ERA_RAM_BOOST_COOLDOWN=1800 seconds of silence after a boost attempt
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from core.utils import is_windows, log

DEFAULT_THRESHOLD_PERCENT = 87.0
DEFAULT_INTERVAL_SECONDS = 60.0  # faster poll for efficiency
DEFAULT_COOLDOWN_SECONDS = 300.0  # shorter cooldown; manual override bypasses

_TAG = "RamWatchdog"


def _env_float(name: str, fallback: float, *, minimum: float) -> float:
    """Read a numeric override, ignoring anything unparseable or out of range.

    A typo in an env var must not silently turn the poll interval into 0 and
    spin the thread, so every value is floored.
    """
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log(_TAG, f"Ignoring {name}={raw!r}: not a number.")
        return fallback
    if value < minimum:
        log(_TAG, f"Ignoring {name}={raw!r}: below the {minimum} minimum.")
        return fallback
    return value


def _env_disabled() -> bool:
    return str(os.environ.get("ERA_RAM_WATCHDOG", "")).strip().lower() in {
        "0",
        "off",
        "false",
        "no",
    }


def _read_ram_percent() -> float | None:
    """Current RAM utilisation, or None when psutil can't answer."""
    try:
        import psutil

        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def _invoke_pc_boost() -> str:
    """Imported lazily: actions.system_performance pulls in pywinauto/COM."""
    from actions.system_performance import pc_boost

    return pc_boost()


class RamWatchdog:
    """Polls RAM usage and triggers PC Boost above `threshold`.

    `sampler` and `boost` are injectable so the decision logic can be tested
    without psutil readings or a real PC Manager window.
    """

    def __init__(
        self,
        threshold: float | None = None,
        interval: float | None = None,
        cooldown: float | None = None,
        sampler: Callable[[], float | None] | None = None,
        boost: Callable[[], str] | None = None,
    ) -> None:
        self.threshold = (
            threshold
            if threshold is not None
            else _env_float("ERA_RAM_BOOST_THRESHOLD", DEFAULT_THRESHOLD_PERCENT, minimum=1.0)
        )
        self.interval = (
            interval
            if interval is not None
            else _env_float("ERA_RAM_BOOST_INTERVAL", DEFAULT_INTERVAL_SECONDS, minimum=5.0)
        )
        self.cooldown = (
            cooldown
            if cooldown is not None
            else _env_float("ERA_RAM_BOOST_COOLDOWN", DEFAULT_COOLDOWN_SECONDS, minimum=0.0)
        )
        self._sampler = sampler or _read_ram_percent
        self._boost = boost or _invoke_pc_boost

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.last_percent: float | None = None
        self.last_boost_at: float = 0.0
        self.boost_count: int = 0

    # ── Lifecycle ───────────────────────────────────────────────

    def start(self) -> bool:
        """Start the polling thread. False when the watchdog can't run here."""
        if _env_disabled():
            log(_TAG, "Disabled by ERA_RAM_WATCHDOG.")
            return False
        if not is_windows():
            log(_TAG, "PC Boost is Windows-only — watchdog not started.")
            return False
        if self._sampler() is None:
            log(_TAG, "No RAM reading available (psutil missing?) — watchdog not started.")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="RamWatchdog")
        self._thread.start()
        log(
            _TAG,
            f"Watching RAM every {self.interval:.0f}s; boost above {self.threshold:.0f}% "
            f"(cooldown {self.cooldown / 60:.0f} min).",
        )
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def status(self) -> dict:
        remaining = self.cooldown_remaining()
        return {
            "running": self.running,
            "threshold_percent": self.threshold,
            "interval_seconds": self.interval,
            "cooldown_seconds": self.cooldown,
            "last_percent": self.last_percent,
            "boost_count": self.boost_count,
            "cooldown_remaining_seconds": round(remaining, 1),
        }

    # ── Internal ────────────────────────────────────────────────

    def cooldown_remaining(self) -> float:
        if not self.last_boost_at:
            return 0.0
        return max(0.0, self.cooldown - (time.monotonic() - self.last_boost_at))

    def _loop(self) -> None:
        # Wait one full interval before the first sample: Era's own startup
        # (model warm-ups, vision preload) spikes memory, and boosting into
        # that spike would fight the launch it just interrupted.
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception as e:
                log(_TAG, f"Check failed: {e!r}")

    def check_once(self) -> str:
        """Sample RAM and boost if warranted. Returns the outcome, for tests."""
        percent = self._sampler()
        if percent is None:
            return "no-reading"
        self.last_percent = percent

        if percent <= self.threshold:
            return "below-threshold"

        remaining = self.cooldown_remaining()
        if remaining > 0:
            log(
                _TAG,
                f"RAM at {percent:.0f}% but a boost ran {self.cooldown - remaining:.0f}s ago — "
                f"holding off for another {remaining / 60:.0f} min.",
            )
            return "cooldown"

        with self._lock:
            # Re-check inside the lock so a manual check_once() racing the
            # polling thread can't double-fire the UI automation.
            if self.cooldown_remaining() > 0:
                return "cooldown"
            log(_TAG, f"RAM at {percent:.0f}% (> {self.threshold:.0f}%) — triggering PC Boost.")
            try:
                result = self._boost()
            except Exception as e:
                self.last_boost_at = time.monotonic()
                log(_TAG, f"PC Boost raised: {e!r}")
                return "boost-failed"
            # pc_boost() reports outcome as prose, not a status code; only the
            # success path says the boost was triggered.
            self.last_boost_at = time.monotonic()
            if "triggered" in result.lower():
                self.boost_count += 1
                log(_TAG, f"PC Boost done: {result}")
                return "boosted"
            log(_TAG, f"PC Boost did not run: {result}")
            return "boost-failed"

    def boost_now(self, skip_cooldown: bool = True) -> str:
        """Manual / IoT-triggered boost — no time limit when asked.
        skip_cooldown=True lets Era boost on demand regardless of timer."""
        with self._lock:
            saved = self.cooldown
            if skip_cooldown:
                self.cooldown = 0.0
            try:
                result = self.check_once()
            finally:
                if skip_cooldown:
                    self.cooldown = saved
            return result + ":manual"


_watchdog: RamWatchdog | None = None
_singleton_lock = threading.Lock()


def get_ram_watchdog() -> RamWatchdog | None:
    """The process-wide watchdog (auto-boost >87%, efficient 60s poll)."""
    return _watchdog


def start_ram_watchdog() -> RamWatchdog | None:
    """Start the process-wide watchdog (87% auto-boost, 60s poll, efficiency mode)."""
    global _watchdog
    with _singleton_lock:
        if _watchdog is not None and _watchdog.running:
            return _watchdog
        watchdog = RamWatchdog()
        if not watchdog.start():
            return None
        _watchdog = watchdog
        return _watchdog


def stop_ram_watchdog() -> None:
    global _watchdog
    with _singleton_lock:
        if _watchdog is not None:
            _watchdog.stop()
        _watchdog = None
