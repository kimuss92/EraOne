"""era_observe: the report must flag real faults and stay quiet about normal life."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tools import era_observe

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _log(tmp_path, rows, crash_lines=()):
    lines = []
    for offset_s, event, extra in rows:
        stamp = (T0 + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines.append(json.dumps({"ts": stamp, "level": "INFO", "event": event, **extra}))
    (tmp_path / "era.log").write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "era_crash.log").write_text("\n".join(crash_lines), encoding="utf-8")
    return tmp_path


def _report(tmp_path, since="1h"):
    events = era_observe.load_events(tmp_path)
    until = events[-1]["_t"]
    since_t = era_observe.parse_since(since, until, events)
    return era_observe.build_report(events, since_t, until, tmp_path)


def test_quiet_session_is_clean(tmp_path):
    _log(
        tmp_path,
        [
            (0, "process.start", {}),
            (10, "turn.latency", {"response_ms": 800, "total_ms": 4000, "interrupted": False}),
            (20, "turn.latency", {"response_ms": 1200, "total_ms": 6000, "interrupted": False}),
            (30, "engine.network.blip", {}),
        ],
    )
    report = _report(tmp_path)
    assert report["hard"] == {}
    assert report["notes"] == {"engine.network.blip": 1, "starts_without_relaunch_request": 1}
    assert report["latency_ms"]["response_ms"]["n"] == 2
    assert era_observe.main(["--log-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "event",
    ["audio.output.write_failed", "awareness.devices.observer_failed", "tool.duplicate.in_flight"],
)
def test_wrong_in_any_amount_events_fail_the_run(tmp_path, event):
    _log(tmp_path, [(0, "process.start", {}), (5, event, {})])
    assert _report(tmp_path)["hard"] == {event: 1}
    assert era_observe.main(["--log-dir", str(tmp_path)]) == 1


def test_expected_refusals_are_not_hard_but_slow_failures_are_called_out(tmp_path):
    fail = {"tool": "file_controller", "action": "open", "error_type": "ActionError"}
    _log(
        tmp_path,
        [
            (0, "tool.execution.failed", {**fail, "duration_ms": 15882}),
            (1, "tool.execution.failed", {"tool": "scheduled_task", "error_type": "TaskError"}),
        ],
    )
    report = _report(tmp_path)
    assert report["hard"] == {}
    assert report["slow_tool_failures"] == ["file_controller.open 15882ms"]
    assert report["tool_failures_unexpected"] == ["file_controller.open: ActionError"]


def test_start_after_a_requested_relaunch_is_planned(tmp_path):
    _log(
        tmp_path,
        [(0, "era.relaunch.requested", {}), (20, "process.start", {}), (900, "turn.latency", {})],
    )
    assert "starts_without_relaunch_request" not in _report(tmp_path)["notes"]


def test_forced_kill_in_the_supervisor_log_is_hard(tmp_path):
    local = (T0 + timedelta(seconds=5)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    _log(
        tmp_path,
        [(0, "process.start", {}), (60, "turn.latency", {})],
        crash_lines=[
            f"{local},100  WARNING   Era asked for a relaunch 25s ago but is still running - "
            "killing it.",
            f"{local},200  INFO      Era relaunch requested by user - exit code 0 after 9s.",
        ],
    )
    faults = _report(tmp_path)["hard"]["supervisor_faults"]
    assert len(faults) == 1 and "killing it" in faults[0]


def test_window_excludes_older_events(tmp_path):
    _log(tmp_path, [(0, "audio.output.write_failed", {}), (4 * 3600, "turn.latency", {})])
    assert _report(tmp_path, since="1h")["hard"] == {}
    assert _report(tmp_path, since="6h")["hard"] == {"audio.output.write_failed": 1}


def test_last_start_window_begins_at_the_latest_process_start(tmp_path):
    _log(
        tmp_path,
        [
            (0, "process.start", {}),
            (10, "tool.duplicate.in_flight", {}),
            (600, "process.start", {}),
        ],
    )
    assert _report(tmp_path, since="last-start")["hard"] == {}


def test_unreadable_log_exits_2(tmp_path):
    assert era_observe.main(["--log-dir", str(tmp_path)]) == 2
