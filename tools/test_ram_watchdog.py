# tools/test_ram_watchdog.py
from __future__ import annotations

import time

from core.ram_watchdog import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD_PERCENT,
    RamWatchdog,
)


def _watchdog(percent, *, result="PC Boost triggered — cleaning, sir.", **kwargs):
    """A watchdog whose RAM reading and boost call are both fakes."""
    calls: list[float] = []

    def boost() -> str:
        calls.append(time.monotonic())
        return result

    wd = RamWatchdog(
        threshold=kwargs.pop("threshold", 70.0),
        interval=kwargs.pop("interval", 300.0),
        cooldown=kwargs.pop("cooldown", 1800.0),
        sampler=percent if callable(percent) else (lambda: percent),
        boost=boost,
        **kwargs,
    )
    return wd, calls


# ── Defaults ──


def test_defaults_match_the_spec():
    # Retuned to "efficiency mode" (see core/ram_watchdog.py): trigger higher so
    # a machine merely running warm is left alone, but poll more often so real
    # pressure is caught quickly.
    assert DEFAULT_THRESHOLD_PERCENT == 87.0
    assert DEFAULT_INTERVAL_SECONDS == 60.0
    assert DEFAULT_COOLDOWN_SECONDS > 0


# ── Threshold ──


def test_below_threshold_does_not_boost():
    wd, calls = _watchdog(42.0)
    assert wd.check_once() == "below-threshold"
    assert calls == []


def test_exactly_at_threshold_does_not_boost():
    wd, calls = _watchdog(70.0)
    assert wd.check_once() == "below-threshold"
    assert calls == []


def test_above_threshold_boosts_without_asking():
    wd, calls = _watchdog(70.1)
    assert wd.check_once() == "boosted"
    assert len(calls) == 1
    assert wd.boost_count == 1


def test_unreadable_ram_is_not_treated_as_pressure():
    wd, calls = _watchdog(None)
    assert wd.check_once() == "no-reading"
    assert calls == []


# ── Cooldown ──


def test_second_check_inside_cooldown_is_suppressed():
    wd, calls = _watchdog(95.0)
    assert wd.check_once() == "boosted"
    assert wd.check_once() == "cooldown"
    assert wd.check_once() == "cooldown"
    assert len(calls) == 1


def test_boost_resumes_after_cooldown_expires():
    wd, calls = _watchdog(95.0, cooldown=0.05)
    assert wd.check_once() == "boosted"
    assert wd.check_once() == "cooldown"
    time.sleep(0.06)
    assert wd.check_once() == "boosted"
    assert len(calls) == 2


def test_cooldown_remaining_counts_down():
    wd, _ = _watchdog(95.0, cooldown=60.0)
    assert wd.cooldown_remaining() == 0.0
    wd.check_once()
    assert 0 < wd.cooldown_remaining() <= 60.0


# ── Failure handling ──


def test_failed_boost_still_starts_the_cooldown():
    # A machine without PC Manager must not retry the launch every interval.
    wd, calls = _watchdog(95.0, result="PC Manager did not open. Is it installed?")
    assert wd.check_once() == "boost-failed"
    assert wd.check_once() == "cooldown"
    assert len(calls) == 1


def test_raising_boost_is_contained_and_cools_down():
    def boom() -> str:
        raise RuntimeError("COM blew up")

    wd = RamWatchdog(threshold=70.0, cooldown=60.0, sampler=lambda: 95.0, boost=boom)
    assert wd.check_once() == "boost-failed"
    assert wd.check_once() == "cooldown"


# ── Env overrides ──


def test_env_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("ERA_RAM_BOOST_THRESHOLD", "85")
    monkeypatch.setenv("ERA_RAM_BOOST_INTERVAL", "60")
    monkeypatch.setenv("ERA_RAM_BOOST_COOLDOWN", "120")
    wd = RamWatchdog()
    assert (wd.threshold, wd.interval, wd.cooldown) == (85.0, 60.0, 120.0)


def test_garbage_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("ERA_RAM_BOOST_THRESHOLD", "seventy")
    monkeypatch.setenv("ERA_RAM_BOOST_INTERVAL", "0")  # would spin the thread
    wd = RamWatchdog()
    assert wd.threshold == DEFAULT_THRESHOLD_PERCENT
    assert wd.interval == DEFAULT_INTERVAL_SECONDS


def test_watchdog_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ERA_RAM_WATCHDOG", "0")
    wd, calls = _watchdog(95.0)
    assert wd.start() is False
    assert wd.running is False
    assert calls == []


# ── Thread lifecycle ──


def test_thread_samples_and_boosts_on_its_own(monkeypatch):
    monkeypatch.delenv("ERA_RAM_WATCHDOG", raising=False)
    monkeypatch.setattr("core.ram_watchdog.is_windows", lambda: True)
    wd, calls = _watchdog(95.0, interval=0.05, cooldown=0.0)
    assert wd.start() is True
    try:
        deadline = time.monotonic() + 3.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls, "watchdog thread never triggered a boost"
        assert wd.last_percent == 95.0
    finally:
        wd.stop()


def test_stop_ends_the_thread(monkeypatch):
    monkeypatch.delenv("ERA_RAM_WATCHDOG", raising=False)
    monkeypatch.setattr("core.ram_watchdog.is_windows", lambda: True)
    wd, _ = _watchdog(10.0, interval=0.05)
    assert wd.start() is True
    wd.stop()
    deadline = time.monotonic() + 2.0
    while wd._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not wd._thread.is_alive()
    assert wd.running is False


def test_status_reports_configuration():
    wd, _ = _watchdog(10.0)
    status = wd.status()
    assert status["threshold_percent"] == 70.0
    assert status["interval_seconds"] == 300.0
    assert status["running"] is False
    assert status["boost_count"] == 0
