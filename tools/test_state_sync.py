"""Tests for the event-driven Windows state synchronizer.

All process events are synthetic. These tests do not inspect the host process
list, initialize COM, or access Windows devices.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from windows import state_sync

SOURCE_PATH = Path(__file__).resolve().parents[1] / "windows" / "state_sync.py"


def _row(pid: int, name: str | None):
    return SimpleNamespace(ProcessId=pid, Name=name)


def _event(kind: str, pid: int, name: str = ""):
    return SimpleNamespace(
        Path_=SimpleNamespace(Class=kind),
        ProcessID=pid,
        ProcessName=name,
    )


def test_initial_inventory_is_normalized_without_polling_live_processes():
    monitor = state_sync._OpenAppEventMonitor()
    monitor._replace_inventory(
        [
            _row(10, "Editor.exe"),
            _row(20, "Browser.exe"),
            _row(0, "System Idle Process"),
            _row(30, ""),
        ]
    )

    assert monitor.snapshot() == {
        "editor.exe": {"pid": 10, "name": "Editor.exe"},
        "browser.exe": {"pid": 20, "name": "Browser.exe"},
    }


def test_start_and_stop_events_update_inventory_by_pid():
    monitor = state_sync._OpenAppEventMonitor()
    monitor._replace_inventory([_row(10, "Editor.exe")])

    assert monitor._apply_event(_event("Win32_ProcessStartTrace", 20, "Browser.exe"))
    assert monitor.snapshot()["browser.exe"] == {"pid": 20, "name": "Browser.exe"}

    assert monitor._apply_event(_event("Win32_ProcessStopTrace", 10))
    assert "editor.exe" not in monitor.snapshot()
    assert not monitor._apply_event(_event("Win32_ProcessStopTrace", 999))


def test_duplicate_executable_survives_one_instance_stopping():
    monitor = state_sync._OpenAppEventMonitor()
    monitor._replace_inventory([_row(10, "Editor.exe"), _row(11, "Editor.exe")])

    assert monitor._apply_event(_event("Win32_ProcessStopTrace", 10))
    assert monitor.snapshot() == {
        "editor.exe": {"pid": 11, "name": "Editor.exe"},
    }


def test_invalid_events_do_not_corrupt_inventory():
    monitor = state_sync._OpenAppEventMonitor()
    monitor._replace_inventory([_row(10, "Editor.exe")])

    assert not monitor._apply_event(SimpleNamespace())
    assert not monitor._apply_event(_event("Win32_ProcessStartTrace", -1, "Bad.exe"))
    assert not monitor._apply_event(_event("UnexpectedEvent", 10, "Changed.exe"))
    assert monitor.snapshot() == {
        "editor.exe": {"pid": 10, "name": "Editor.exe"},
    }


def test_source_uses_wmi_events_and_one_second_main_sleep():
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    process_iter_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "process_iter"
    ]
    sleep_values = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sleep"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, (int, float))
    ]

    assert not process_iter_calls
    assert "SELECT * FROM Win32_ProcessTrace" in source
    assert "ExecNotificationQuery" in source
    assert 1.0 in sleep_values
    assert 0.05 not in sleep_values
