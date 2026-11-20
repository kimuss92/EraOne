"""PF-066 guards: the conversation window holds while talking, "stay silent" is
final, and the HUD "i" key and "hud.telemetry" panel work.

Live 2026-11-20: inactivity mode came on in the middle of a conversation, "stay
silent" sometimes did nothing, and Era asked the HUD to hide "hud.telemetry"
(rejected as an unknown panel although "telemetry" is a declared alias).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from core import attention
from core.attention import AttentionGate, ListeningState, is_silence_order


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _window(monkeypatch, tmp_path):
    cfg = tmp_path / "voice.json"
    cfg.write_text(json.dumps({"listening": {}}))
    monkeypatch.setattr(attention, "_VOICE_JSON", cfg)
    monkeypatch.setattr(attention, "_names", lambda: {"era"})
    monkeypatch.setattr("core.session.window_seconds", lambda: 60.0)


def _speech() -> bytes:
    t = np.arange(1024) / 16000
    return (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()


def test_a_long_reply_to_an_owner_turn_does_not_let_the_window_lapse():
    clock = Clock()
    state = ListeningState(clock=clock)
    state.note_owner_turn()  # accepted inside the open window
    for _ in range(12):  # Era talks for 4 minutes, checked every 20 s
        clock.t += 20
        state.hold_while_speaking()
    assert state.window_open()


def test_holding_while_speaking_never_reopens_a_closed_window():
    clock = Clock()
    state = ListeningState(clock=clock)
    state.note_owner_turn()
    clock.t += 100
    assert not state.window_open()
    state.hold_while_speaking()
    assert not state.window_open()


def test_a_scheduled_report_with_no_owner_turn_does_not_hold_the_window():
    clock = Clock()
    state = ListeningState(clock=clock)
    clock.t += 50
    state.hold_while_speaking()  # no owner turn, no explicit address
    clock.t += 20
    assert not state.window_open()


def test_stay_silent_is_final_until_her_name_is_heard():
    clock = Clock()
    state = ListeningState(clock=clock)
    state.note_activity(explicit=True)
    state.close_session()
    assert state.forced_closed() and not state.window_open()
    state.note_activity()  # a passing, unnamed utterance must not reopen it
    state.hold_while_speaking()
    assert not state.window_open()
    state.note_activity(explicit=True)  # "Era, ..."
    assert state.window_open() and not state.forced_closed()


def test_a_long_owner_utterance_outlives_the_window_it_started_in():
    clock = Clock()
    state = ListeningState(clock=clock, media_active=lambda: False)
    gate = AttentionGate(state, None)
    frame = _speech()
    dur = (len(frame) // 2) / 16000.0
    gate.feed(frame)  # window open: PASSING starts
    for _ in range(int(150 / dur)):  # 150 s of continuous speech, window is 60 s
        clock.t += dur
        out = gate.feed(frame)
        assert out == [frame], "speech was silenced mid-utterance"


@pytest.mark.parametrize(
    "text",
    [
        "stay silent",
        "Era, remain silent",
        "can you stay silent",
        "Era, could you please be quiet",
        "I want you to remain silent",
        "just be quiet",
        "stop talking",
        "Era stop talking now",
        "tais-toi",
        "Era, silent",
    ],
)
def test_natural_silence_orders_are_recognised(text):
    assert is_silence_order(text)


@pytest.mark.parametrize(
    "text",
    [
        "stay silent when I'm on a call",
        "why did you stay silent",
        "can you stay silent about the price",
        "stop talking to him",
        "I will stop talking soon",
        "silent night",
    ],
)
def test_sentences_that_only_mention_silence_are_not_orders(text):
    assert not is_silence_order(text)


def test_busy_never_keeps_the_mic_open_after_stay_silent():
    from app.era_live.audio import AudioIOMixin

    class Eng:
        _pending_store = None
        session_manager = None  # would raise if consulted

    eng = Eng()
    eng._listening = ListeningState(clock=Clock())
    eng._listening.close_session()
    assert AudioIOMixin._busy_reason(eng) is None


@pytest.mark.parametrize(
    "panel", ["hud.telemetry", "telemetry", "System Telemetry", "hud.system", "hud_system"]
)
def test_telemetry_resolves_to_the_system_panel(panel):
    from core.visual_engine import _resolve_panel_id

    assert _resolve_panel_id(panel) == "hud.system"


def test_an_invented_panel_is_still_rejected():
    from core.visual_engine import _panel, _resolve_panel_id

    assert _resolve_panel_id("hud.mystery") is None
    with pytest.raises(ValueError, match="Unknown HUD panel"):
        _panel({"action": "panel", "panel_id": "hud.mystery", "visible": False})


def test_the_hud_has_an_i_shortcut_and_the_backend_commands():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    shortcuts = (root / "ui_src/src/lib/shortcuts.ts").read_text(encoding="utf-8")
    assert "id: 'inactivity', key: 'I'" in shortcuts
    main = (root / "main.py").read_text(encoding="utf-8")
    assert '"__inactivity__"' in main and "_hud_set_inactivity" in main
    api = (root / "app/hud/api.py").read_text(encoding="utf-8")
    assert "/api/inactivity" in api


def test_reading_a_log_file_is_not_reported_as_a_failed_call():
    from core.tool_result import ToolResult, looks_like_failure

    log = "2026-09-26 04:07:48.375 | ERROR   | stderr | Exception in callback x\nmore"
    assert not looks_like_failure(log)
    assert ToolResult.from_any(log, tool_name="file_controller").success
    assert looks_like_failure("Path not found: C:\\x")


def test_an_unregistered_app_is_found_by_its_window_title(monkeypatch):
    """PF-068: Neat Download Manager runs as NeatDM.exe; Era said it did not open."""
    from core import live_app_state as las

    monkeypatch.setattr(las, "_identity", lambda app: (app.lower(), {"neat download manager.exe"}))
    monkeypatch.setattr(las, "_names_from_window_titles", lambda app: {"neatdm.exe"})
    monkeypatch.setattr(
        las, "_processes", lambda names: [42] if "neatdm.exe" in names else []
    )
    monkeypatch.setattr(las, "_windows", lambda names: [{"hwnd": 1, "pid": 42, "state": "visible"}])
    assert las.collect_live_state("Neat Download Manager")["process_running"] is False  # a close never uses it
    las._DISCOVER.set(True)
    state = las.collect_live_state("Neat Download Manager")
    las._DISCOVER.set(False)
    assert state["process_running"] is True and state["pids"] == [42]
    assert las._matches(state, ("window", True))


def test_premiere_pro_uses_its_real_exe_and_is_found_by_process_stem(monkeypatch):
    """PF-068: Premiere's exe is "Adobe Premiere Pro.exe", not "Premiere.exe"."""
    from actions.open_app import MASTER_APP_REGISTRY as opened
    from actions.window_control import MASTER_APP_REGISTRY as focused
    from core import live_app_state as las
    from types import SimpleNamespace as NS

    for reg in (opened, focused):
        assert reg["premiere pro"]["exe"] == "Adobe Premiere Pro.exe"
    monkeypatch.setattr(
        las.process_awareness,
        "list_processes",
        lambda refresh=False: [NS(name="Adobe Premiere Pro.exe", pid=7), NS(name="x.exe", pid=8)],
    )
    assert las._names_from_process_stems("Premiere Pro 2026") == {"adobe premiere pro.exe"}
    assert las._names_from_process_stems("Adobe Premiere Pro") == {"adobe premiere pro.exe"}
    assert las._names_from_process_stems("Pro") == set()  # too short to be a match


def test_any_app_uses_the_exe_its_launcher_resolves_not_a_guess(monkeypatch):
    """PF-068: the verifier asks the launcher's own resolver for the real exe."""
    from actions import open_app as oa
    from core import live_app_state as las

    oa._EXE_HINTS.clear()
    monkeypatch.setattr(oa, "get_executable_from_registry", lambda a: None)
    monkeypatch.setattr(oa, "scan_start_menu_shortcuts", lambda a: "C:\\sm\\Neat.lnk")
    monkeypatch.setattr(oa, "inspect_shortcut", lambda p: ("C:\\Neat\\NeatDM.exe", ""))
    assert oa.known_exe_names("Neat Download Manager") == {"neatdm.exe"}
    assert "neatdm.exe" in las._identity("Neat Download Manager")[1]

    oa._EXE_HINTS.clear()  # an updater/bootstrapper is never the app's process
    monkeypatch.setattr(oa, "inspect_shortcut", lambda p: ("C:\\x\\Update.exe", ""))
    assert oa.known_exe_names("Some Electron App") == frozenset()
    oa._EXE_HINTS.clear()
