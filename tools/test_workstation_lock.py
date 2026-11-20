"""Locking the PC: real lock call, and success only when Windows confirms it.

Regression for 2026-11-04: "lock my screen" and "press Windows+L" both injected
Win+L, answered "Done: lock_screen." / "Hotkey: win+l" (trace: VERIFIED), and the
PC stayed unlocked. Nothing here locks the machine: the autouse fixture replaces
the LockWorkStation call and the session flag for every test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from actions import computer_control as cc
from actions import computer_settings as cs
from core import workstation_lock as wl
from core.action_verifiers import verify_action_result
from core.tool_result import ToolResult


@pytest.fixture(autouse=True)
def fake_windows(monkeypatch):
    """Scriptable stand-in for Windows; the real lock is unreachable from these tests."""
    fake = SimpleNamespace(calls=0, accepted=True, code=0, lock_takes_effect=True, locked=False)

    def fake_call():
        fake.calls += 1
        if fake.accepted and fake.lock_takes_effect:
            fake.locked = True
        return fake.accepted, fake.code

    monkeypatch.setattr(wl, "_call_lock_workstation", fake_call)
    monkeypatch.setattr(wl, "session_is_locked", lambda: fake.locked)
    monkeypatch.setattr(wl, "LOCK_TIMEOUT_S", 0.0)
    monkeypatch.setattr(wl, "_POLL_S", 0.0)
    monkeypatch.setattr(cs, "is_windows", lambda: True)
    monkeypatch.setattr("core.utils.is_windows", lambda: True)
    monkeypatch.setattr("core.hotkeys.lock_workstation", wl.lock_workstation)
    return fake


@pytest.fixture
def no_injection(monkeypatch):
    """Fail if anything falls back to pressing keys."""

    def refuse(*keys):
        raise AssertionError(f"lock path injected keys: {keys}")

    monkeypatch.setattr(cs, "pyautogui", SimpleNamespace(hotkey=refuse, press=refuse))
    monkeypatch.setattr(cc, "pyautogui", SimpleNamespace(hotkey=refuse, press=refuse))


def _classified(message: str) -> ToolResult:
    """Run a tool message through the same judges the dispatcher uses."""
    return verify_action_result(
        "computer_settings",
        ToolResult.from_any(message, tool_name="computer_settings"),
        {"action": "lock_screen"},
    )


def test_lock_screen_action_calls_windows_and_reports_confirmed_lock(fake_windows, no_injection):
    out = cs.computer_settings({"action": "lock_screen"})

    assert fake_windows.calls == 1
    assert fake_windows.locked is True
    assert out != "Done: lock_screen."
    assert "locked" in out.lower()
    result = _classified(out)
    assert result.success is True
    assert result.verified is True


def test_accepted_call_that_never_locks_is_a_failure_not_done(fake_windows, no_injection):
    fake_windows.lock_takes_effect = False

    out = wl.lock_workstation(timeout=0)

    assert "NOT locked" in out
    assert not _classified(out).success


def test_windows_refusing_the_call_is_a_failure(fake_windows, no_injection):
    fake_windows.accepted = False
    fake_windows.code = 5

    out = cs.computer_settings({"action": "lock_screen"})

    assert "code 5" in out
    result = _classified(out)
    assert not result.success
    assert result.error_type


def test_unreadable_session_state_never_claims_success(fake_windows, monkeypatch, no_injection):
    monkeypatch.setattr(wl, "session_is_locked", lambda: None)

    out = wl.lock_workstation(timeout=0)

    assert fake_windows.calls == 1
    assert not _classified(out).success


def test_already_locked_does_not_call_again(fake_windows, no_injection):
    fake_windows.locked = True

    out = cs.computer_settings({"action": "lock_screen"})

    assert fake_windows.calls == 0
    assert _classified(out).success


@pytest.mark.parametrize("keys", ["win+l", "WIN + L", "windows+l", "l+win", "winleft+l"])
def test_win_l_hotkey_through_computer_control_really_locks(fake_windows, no_injection, keys):
    out = cc.computer_control({"action": "hotkey", "keys": keys})

    assert fake_windows.calls == 1
    assert not out.startswith("Hotkey")
    assert "locked" in out.lower()


def test_win_l_hotkey_through_computer_settings_really_locks(fake_windows, no_injection):
    out = cs.computer_settings({"action": "hotkey", "keys": "win+l"})

    assert fake_windows.calls == 1
    assert not out.startswith("Hotkey")
    assert "locked" in out.lower()


@pytest.mark.parametrize(
    "keys",
    [("ctrl", "l"), ("win", "d"), ("win", "shift", "l"), ("l",), ("win",)],
)
def test_other_chords_are_not_mistaken_for_lock(keys):
    assert wl.is_lock_chord(keys) is False


def test_other_hotkeys_still_go_to_the_keyboard(fake_windows, monkeypatch):
    sent = []
    monkeypatch.setattr(cc, "pyautogui", SimpleNamespace(hotkey=lambda *k: sent.append(k)))

    out = cc._hotkey("ctrl", "c")

    assert sent == [("ctrl", "c")]
    assert out.splitlines()[0] == "Hotkey sent: ctrl+c"
    assert fake_windows.calls == 0
