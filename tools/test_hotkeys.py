"""Hotkeys send what was asked, or say they could not; dangerous chords stay gated.

Regression for 2026-11-04: pyautogui drops key names it does not know, so
"control+c" pressed a bare "c" and still answered "Hotkey: control+c". The guard
compared raw text, so "alt + f4" was never gated. No test sends a real key: every
test hands the code a recording stand-in for pyautogui.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from actions import computer_control as cc
from actions import computer_settings as cs
from agent.executor import _jit_before_mutation
from app.era_live.tool_dispatch import ToolDispatchMixin
from core import hotkeys, vision_context
from core.pending_confirmation import PendingConfirmationStore
from core.safety_guard import HIGH, classify_action
from core.tool_result import looks_like_failure
from core.vision_types import VisionResult


@pytest.fixture
def keyboard(monkeypatch):
    """Records chords and presses; refuses to reach the real keyboard."""
    sent = SimpleNamespace(chords=[], presses=[])
    fake = SimpleNamespace(
        hotkey=lambda *keys: sent.chords.append(keys),
        press=lambda key: sent.presses.append(key),
    )
    monkeypatch.setattr(cc, "pyautogui", fake)
    monkeypatch.setattr(cs, "pyautogui", fake)
    monkeypatch.setattr("core.utils.is_windows", lambda: True)
    monkeypatch.setattr(hotkeys, "_focused_window_line", lambda: "\nFocused window when sent: App")
    return sent


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+c", ["ctrl", "c"]),
        ("Ctrl + Shift + Esc", ["ctrl", "shift", "esc"]),
        ("control+c", ["ctrl", "c"]),
        ("Windows+Shift+R", ["win", "shift", "r"]),
        ("windows key + r", ["win", "r"]),
        ("win shift r", ["win", "shift", "r"]),
        ("ctrl+page down", ["ctrl", "pagedown"]),
        ("page down", ["pagedown"]),
        ("ctrl++", ["ctrl", "+"]),
        ("ctrl+plus", ["ctrl", "+"]),
        (["ctrl", "c"], ["ctrl", "c"]),
        ("", []),
        (None, []),
    ],
)
def test_parse_keys(raw, expected):
    assert hotkeys.parse_keys(raw) == expected


@pytest.mark.parametrize("spelling", ["control+c", "CTRL + C", "ctrl c"])
def test_every_spelling_sends_the_full_chord(keyboard, spelling):
    out = cc.computer_control({"action": "hotkey", "keys": spelling})

    assert keyboard.chords == [("ctrl", "c")]
    assert out.splitlines()[0] == "Hotkey sent: ctrl+c"


def test_screen_recording_chord_reaches_the_keyboard_unchanged(keyboard):
    out = cc.computer_control({"action": "hotkey", "keys": "win+shift+r"})

    assert keyboard.chords == [("win", "shift", "r")]
    assert not looks_like_failure(out)


def test_result_names_the_window_without_a_title_faking_a_failure(keyboard, monkeypatch):
    monkeypatch.setattr(
        hotkeys, "_focused_window_line", lambda: "\nFocused window when sent: Error log"
    )

    out = cc.computer_control({"action": "hotkey", "keys": "ctrl+s"})

    assert "Error log" in out
    assert not looks_like_failure(out)


@pytest.mark.parametrize("keys", ["ctrl+banana", "cmd+c", "banana"])
def test_unknown_key_name_presses_nothing_and_reports_failure(keyboard, keys):
    out = cc.computer_control({"action": "hotkey", "keys": keys})

    assert keyboard.chords == []
    assert looks_like_failure(out)
    assert "Nothing was pressed" in out


def test_empty_hotkey_is_a_failure(keyboard):
    out = cc.computer_control({"action": "hotkey"})

    assert keyboard.chords == []
    assert looks_like_failure(out)


def test_mouse_in_a_corner_is_reported_not_raised(keyboard, monkeypatch):
    class FailSafeException(Exception):
        pass

    def trip(*_keys):
        raise FailSafeException("corner")

    fake = SimpleNamespace(hotkey=trip, press=trip)
    monkeypatch.setattr(cs, "pyautogui", fake)

    for out in (
        cs.computer_settings({"action": "hotkey", "keys": "ctrl+c"}),
        cs.computer_settings({"action": "press", "key": "enter"}),
    ):
        assert looks_like_failure(out)
        assert "corner" in out


def test_ctrl_alt_del_says_it_cannot_be_sent(keyboard):
    out = hotkeys.send_hotkey("ctrl+alt+del", cc.pyautogui)

    assert keyboard.chords == []
    assert looks_like_failure(out)


def test_computer_settings_hotkey_uses_the_same_rules(keyboard):
    good = cs.computer_settings({"action": "hotkey", "keys": "control+shift+t"})
    bad = cs.computer_settings({"action": "hotkey", "keys": "ctrl+banana"})

    assert keyboard.chords == [("ctrl", "shift", "t")]
    assert good.splitlines()[0] == "Hotkey sent: ctrl+shift+t"
    assert looks_like_failure(bad)


def test_press_refuses_a_chord_and_an_unknown_key(keyboard):
    assert looks_like_failure(cc.computer_control({"action": "press", "key": "ctrl+c"}))
    assert looks_like_failure(cc.computer_control({"action": "press", "key": "banana"}))
    assert keyboard.presses == []

    assert cc.computer_control({"action": "press", "key": "Page Down"}) == "Pressed: pagedown"
    assert keyboard.presses == ["pagedown"]


def test_hotkey_to_a_window_focuses_it_first(keyboard, monkeypatch):
    focused = []
    monkeypatch.setattr(cc, "_focus_window", focused.append)
    monkeypatch.setattr(cc, "get_active_window", lambda: {"title": "Report - Notepad"})

    out = cc.computer_control({"action": "hotkey", "keys": "ctrl+s", "title": "notepad"})

    assert focused == ["notepad"]
    assert keyboard.chords == [("ctrl", "s")]
    assert not looks_like_failure(out)


def test_hotkey_to_a_window_that_stays_behind_presses_nothing(keyboard, monkeypatch):
    monkeypatch.setattr(cc, "_focus_window", lambda _title: "Focused window: notepad")
    monkeypatch.setattr(cc, "get_active_window", lambda: {"title": "Bank - Chrome"})

    out = cc.computer_control({"action": "hotkey", "keys": "ctrl+s", "title": "notepad"})

    assert keyboard.chords == []
    assert looks_like_failure(out)
    assert "Bank - Chrome" in out


def test_focus_window_title_never_reaches_the_script_text(monkeypatch):
    runs = []
    monkeypatch.setattr(cc, "get_os", lambda: "windows")
    monkeypatch.setattr(cc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cc.subprocess, "run", lambda args, **kw: runs.append((args, kw)))
    title = 'x"); Remove-Item C:\\important; ("'

    cc._focus_window(title)

    args, kwargs = runs[0]
    assert "Remove-Item" not in " ".join(args)
    assert kwargs["env"]["ERA_FOCUS_TITLE"] == title


@pytest.mark.parametrize(
    "keys",
    [
        "alt+f4",
        "alt + f4",
        "F4+Alt",
        "ctrl+w",
        "control+w",
        "W + CTRL",
        "win+r",
        "windows+r",
        "win r",
        "win+d",
        "ctrl+shift+esc",
        "ctrl+alt+del",
    ],
)
@pytest.mark.parametrize("tool", ["computer_control", "computer_settings"])
def test_dangerous_chords_are_gated_in_any_spelling_on_both_tools(tool, keys):
    verdict = classify_action(tool, {"action": "hotkey", "keys": keys})

    assert verdict["risk_level"] == HIGH
    assert verdict["requires_confirmation"] is True


@pytest.mark.parametrize(
    "keys",
    [
        "win+shift+r",
        "win+shift+s",
        "win+v",
        "win+.",
        "win+e",
        "win+tab",
        "alt+tab",
        "win+ctrl+left",
        "win+shift+right",
        "win+up",
        "ctrl+c",
        "ctrl+s",
        "ctrl+shift+t",
        "f11",
        "enter",
    ],
)
def test_useful_shortcuts_run_without_a_confirmation(keys):
    verdict = classify_action("computer_control", {"action": "hotkey", "keys": keys})

    assert verdict["requires_confirmation"] is False


@pytest.mark.parametrize("keys", ["winleft+r", "winright+d", "altleft+f4", "altright+f4"])
def test_left_and_right_modifier_names_are_the_same_dangerous_chord(keys):
    assert (
        classify_action("computer_control", {"action": "hotkey", "keys": keys})["risk_level"]
        == HIGH
    )


@pytest.mark.parametrize("keys", ["ctrlleft+w", "ctrlright+w", "ctrl+shift+escape"])
def test_duplicate_key_names_are_the_same_dangerous_chord(keys):
    assert (
        classify_action("computer_control", {"action": "hotkey", "keys": keys})["risk_level"]
        == HIGH
    )


@pytest.mark.parametrize(
    "keys",
    ["win+r+a", "alt+f4+shift", "ctrl+w+x", "win+x", "win+s", "ctrl+enter", "shift+delete"],
)
@pytest.mark.parametrize("tool", ["computer_control", "computer_settings"])
def test_chords_off_the_routine_list_wait_for_a_yes(tool, keys):
    assert (
        classify_action(tool, {"action": "hotkey", "keys": keys})["requires_confirmation"] is True
    )


@pytest.mark.parametrize("key", ["win", "winleft", "Windows"])
def test_a_lone_win_key_waits_for_a_yes(key):
    verdict = classify_action("computer_control", {"action": "press", "key": key})

    assert verdict["requires_confirmation"] is True


def test_a_harmless_field_cannot_hide_the_chord_that_gets_sent(keyboard):
    smuggled = {"action": "hotkey", "value": "ctrl+c", "keys": "alt+f4"}

    assert classify_action("computer_control", smuggled)["risk_level"] == HIGH
    assert hotkeys.resolve_keys(smuggled) == "alt+f4"


def test_keys_given_only_as_value_are_sent_and_gated(keyboard):
    params = {"action": "hotkey", "value": "win+e"}

    assert classify_action("computer_control", params)["requires_confirmation"] is False
    cc.computer_control(params)
    assert keyboard.chords == [("win", "e")]
    assert (
        classify_action("computer_control", {"action": "hotkey", "value": "win+r"})["risk_level"]
        == HIGH
    )


@pytest.mark.parametrize("action", ["open_run", "task_manager", "close_app", "Open-Run"])
def test_actions_that_press_gated_chords_are_gated_too(action):
    assert classify_action("computer_settings", {"action": action})["requires_confirmation"] is True


def test_a_name_with_no_key_on_this_os_is_refused(keyboard):
    out = cc.computer_control({"action": "hotkey", "keys": "command+c"})

    assert keyboard.chords == []
    assert looks_like_failure(out)


def test_hotkey_with_a_list_of_keys_does_not_crash_the_guard():
    verdict = classify_action("computer_control", {"action": "hotkey", "keys": ["alt", "f4"]})

    assert verdict["risk_level"] == HIGH


# ── The screen check before a key goes down ──────────────────────────────────


@pytest.fixture
def screen_checks(monkeypatch):
    """Counts screen analyses, with the cooldown out of the way."""
    calls = []

    def analyze(**kwargs):
        calls.append(kwargs)
        return VisionResult(ok=True, screen_state="desktop")

    monkeypatch.setattr("core.vision_service.analyze_screen", analyze)
    monkeypatch.setattr(vision_context, "_LAST_JIT_EXECUTION_TIME", 0.0)
    yield calls
    vision_context.clear_latest_vision_result()


@pytest.mark.parametrize(
    "keys", ["win+shift+r", "Windows + Shift + R", "control+c", "ctrl+s", ["alt", "tab"]]
)
def test_a_routine_shortcut_does_not_wait_on_the_screen_check(screen_checks, keys):
    params = {"action": "hotkey", "keys": keys}

    asyncio.run(vision_context.jit_vision_capture("computer_control", "hotkey", params))

    assert screen_checks == []


@pytest.mark.parametrize(
    "params",
    [
        {"action": "hotkey", "keys": "alt+f4"},
        {"action": "hotkey", "keys": "win+x"},
        {"action": "hotkey", "keys": "ctrl+enter"},
        {"action": "hotkey", "keys": "enter"},
        {"action": "hotkey", "keys": ""},
        {"action": "hotkey", "value": "ctrl+c", "keys": "alt+f4"},
        {"action": "press", "key": "enter"},
        {"action": "press", "keys": "ctrl+c"},
    ],
)
def test_everything_else_still_gets_the_screen_check(screen_checks, params):
    action = params["action"]

    asyncio.run(vision_context.jit_vision_capture("computer_control", action, params))

    assert len(screen_checks) == 1


def test_browser_hotkeys_keep_the_screen_check(screen_checks):
    params = {"action": "hotkey", "keys": "ctrl+c"}

    asyncio.run(vision_context.jit_vision_capture("browser_control", "hotkey", params))

    assert len(screen_checks) == 1


def test_a_skipped_check_does_not_use_up_the_cooldown(screen_checks):
    routine = {"action": "hotkey", "keys": "ctrl+c"}

    asyncio.run(vision_context.jit_vision_capture("computer_control", "hotkey", routine))
    asyncio.run(vision_context.jit_vision_capture("computer_control", "click", {}))

    assert len(screen_checks) == 1


def test_the_agent_executor_passes_the_keys_to_the_check(screen_checks):
    routine = {"action": "hotkey", "keys": "ctrl+c"}
    other = {"action": "hotkey", "keys": "win+x"}

    asyncio.run(_jit_before_mutation("computer_control", "hotkey", routine))
    assert screen_checks == []

    asyncio.run(_jit_before_mutation("computer_control", "hotkey", other))
    assert len(screen_checks) == 1


@pytest.mark.parametrize(("keys", "checks"), [("ctrl+c", 0), ("win+x", 1)])
def test_the_live_dispatcher_passes_the_keys_to_the_check(screen_checks, keys, checks):
    era = SimpleNamespace(
        _last_user_text="",
        _inflight_user_text="",
        _inflight_started_at=0.0,
        _recent_user_texts=[],
        _pending_store=PendingConfirmationStore(),
        speak=lambda *a, **k: None,
    )

    async def raw(_tool_name, _args, fc=None):
        return "Hotkey sent."

    era._execute_tool_raw = raw
    era._execute_tool_result = ToolDispatchMixin._execute_tool_result.__get__(era)
    call = SimpleNamespace(name="computer_control", args={"action": "hotkey", "keys": keys})

    asyncio.run(era._execute_tool_result(call))

    assert len(screen_checks) == checks
