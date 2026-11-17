"""HUD reports the live listening window without changing it."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.hud.status import listening_mode
from core.attention import ListeningState


def test_hud_tracks_expiry_and_wake_without_reanchoring():
    now = [0.0]
    state = ListeningState(clock=lambda: now[0], window_s=120)
    engine = SimpleNamespace(_listening=state, _attention_keep_open=lambda: False)
    assert listening_mode(engine) == "active"
    now[0] = 121
    assert listening_mode(engine) == "inactive"
    assert listening_mode(engine) == "inactive"
    assert not state.window_open()
    state.note_activity(explicit=True)
    assert listening_mode(engine) == "active"


@pytest.mark.parametrize(
    "flags,expected",
    [
        ({"_standby_active": True}, "standby"),
        ({"_mic_muted": True}, "muted"),
        ({"mic_effective_active": lambda: False}, "off"),
        ({"_attention_keep_open": lambda: True}, "active"),
    ],
)
def test_hud_does_not_confuse_privacy_or_busy_states_with_inactivity(flags, expected):
    state = ListeningState(window_s=120)
    state.close_session()
    args = {"_listening": state, "_attention_keep_open": lambda: False}
    args.update(flags)
    assert listening_mode(SimpleNamespace(**args)) == expected


def test_missing_engine_or_clock_is_unknown_and_never_created():
    assert listening_mode(None) == "unavailable"
    engine = SimpleNamespace()
    assert listening_mode(engine) == "unavailable"
    assert not hasattr(engine, "_listening")


def test_failed_read_is_unavailable():
    def broken():
        raise RuntimeError("clock unavailable")

    engine = SimpleNamespace(_listening=SimpleNamespace(window_open=broken))
    assert listening_mode(engine) == "unavailable"


def test_inactivity_cue_requires_a_live_entry_transition_only():
    root = Path(__file__).resolve().parents[1]
    transport = (root / "ui_src/src/lib/transport.ts").read_text(encoding="utf-8")
    live_handler = transport.split("case 'listening_mode':", 1)[1].split("case 'event':", 1)[0]
    snapshot_handler = transport.split("case 'snapshot':", 1)[1].split("case 'amplitude':", 1)[0]
    assert "previous === 'active' && mode === 'inactive'" in live_handler
    assert "cue('inactivity')" in live_handler
    assert "cue(" not in snapshot_handler


def test_inactivity_indicator_uses_theme_tokens_and_static_accessible_markup():
    root = Path(__file__).resolve().parents[1]
    component = (root / "ui_src/src/components/CoreStage.tsx").read_text(encoding="utf-8")
    styles = (root / "ui_src/src/eclipse.css").read_text(encoding="utf-8")
    assert 'role="status"' in component and 'aria-label="Inactivity mode.' in component
    assert 'className="era-inactivity"' in component
    assert "var(--warn)" in styles and "var(--panel-solid)" in styles
    assert "var(--accent-soft)" in styles and "var(--text-2)" in styles
