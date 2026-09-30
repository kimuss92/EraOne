"""Tests for the window-level UI behaviour in ui.py — zoom and window flags.

WHAT THESE COVER
----------------
Two things that were broken in ways nothing else would catch:

  * The TASKBAR minimize. A bare frameless window has no WS_MINIMIZEBOX, so
    Windows does not consider it minimizable and click-to-minimize on the
    taskbar silently does nothing — while the in-app button keeps working, which
    is exactly why it looked fine until you tried the taskbar.

  * Zoom persistence. Ctrl+wheel already zoomed, but the level was forgotten on
    every launch, so a layout the user had deliberately set came back wrong.

The zoom helpers are module-level and Qt-free on purpose: this file must not
construct a QApplication, because a widget test would need a display and would
be the slowest thing in the suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ui as ui_module  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_prefs(monkeypatch, tmp_path):
    """Never read or write the real runtime/ui_prefs.json."""
    monkeypatch.setattr(ui_module, "_ZOOM_PREFS_PATH", tmp_path / "ui_prefs.json")
    return tmp_path / "ui_prefs.json"


# ── the default ───────────────────────────────────────────────────────────


def test_the_default_zoom_gives_the_centre_column_room():
    """Nothing is CLIPPED at 100% — the layout is responsive. What zooming out
    buys is room: measured at 980x700, the sphere's column is 436px at 100% and
    608px at 85%, because the rails have fixed widths."""
    assert ui_module._DEFAULT_ZOOM == 0.85


def test_a_missing_prefs_file_uses_the_default():
    assert ui_module._load_zoom() == ui_module._DEFAULT_ZOOM


def test_the_default_is_inside_the_allowed_range():
    assert ui_module._MIN_ZOOM <= ui_module._DEFAULT_ZOOM <= ui_module._MAX_ZOOM


# ── persistence ───────────────────────────────────────────────────────────


def test_a_saved_zoom_survives_a_restart(isolated_prefs):
    """The whole point: a level set with Ctrl+wheel must still be there next
    launch, or the user re-sets it every single time."""
    ui_module._save_zoom(0.7)
    assert ui_module._load_zoom() == 0.7


def test_the_prefs_file_is_valid_json(isolated_prefs):
    ui_module._save_zoom(0.75)
    assert json.loads(isolated_prefs.read_text(encoding="utf-8"))["zoom"] == 0.75


def test_saving_preserves_other_keys(isolated_prefs):
    """This file is named for UI prefs generally, so a future setting must not
    be wiped by a zoom change."""
    isolated_prefs.parent.mkdir(parents=True, exist_ok=True)
    isolated_prefs.write_text(json.dumps({"theme": "dark", "zoom": 1.0}), encoding="utf-8")
    ui_module._save_zoom(0.8)
    data = json.loads(isolated_prefs.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["zoom"] == 0.8


def test_a_corrupt_prefs_file_falls_back_to_the_default(isolated_prefs):
    """A broken preference must cost the preference, not the window."""
    isolated_prefs.parent.mkdir(parents=True, exist_ok=True)
    isolated_prefs.write_text("{not json", encoding="utf-8")
    assert ui_module._load_zoom() == ui_module._DEFAULT_ZOOM


def test_a_non_object_prefs_file_falls_back(isolated_prefs):
    isolated_prefs.parent.mkdir(parents=True, exist_ok=True)
    isolated_prefs.write_text('"a string"', encoding="utf-8")
    assert ui_module._load_zoom() == ui_module._DEFAULT_ZOOM


def test_an_unwritable_prefs_path_does_not_raise(monkeypatch, tmp_path):
    """Failing to persist must never take down the window."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(ui_module, "_ZOOM_PREFS_PATH", blocker / "ui_prefs.json")
    ui_module._save_zoom(0.9)  # must not raise


@pytest.mark.parametrize("stored", [0.0, -1.0, 12.0, 100.0])
def test_an_out_of_range_stored_zoom_is_rejected(isolated_prefs, stored):
    """A zoom of 0 would render nothing at all, and there would be no obvious
    way for the user to recover the window."""
    isolated_prefs.parent.mkdir(parents=True, exist_ok=True)
    isolated_prefs.write_text(json.dumps({"zoom": stored}), encoding="utf-8")
    assert ui_module._load_zoom() == ui_module._DEFAULT_ZOOM


def test_a_non_numeric_stored_zoom_is_rejected(isolated_prefs):
    isolated_prefs.parent.mkdir(parents=True, exist_ok=True)
    isolated_prefs.write_text(json.dumps({"zoom": "big"}), encoding="utf-8")
    assert ui_module._load_zoom() == ui_module._DEFAULT_ZOOM


def test_the_range_is_usable_in_both_directions():
    assert ui_module._MIN_ZOOM < ui_module._DEFAULT_ZOOM < ui_module._MAX_ZOOM
    assert 0 < ui_module._ZOOM_STEP <= 0.25


# ── the window flags ──────────────────────────────────────────────────────


def test_the_window_asks_for_a_minimize_button():
    """WITHOUT this hint the taskbar's click-to-minimize is a silent no-op on a
    frameless window — the in-app button still works, so the bug hides.

    Asserted against the SOURCE rather than a live widget: constructing the
    window needs a QApplication and a display, which would make this the
    slowest test in the suite for a one-line property.
    """
    source = (Path(__file__).resolve().parents[1] / "ui.py").read_text(encoding="utf-8")
    flags_call = source[source.index("self.setWindowFlags(") : source.index("# No WA_Translucent")]
    assert "FramelessWindowHint" in flags_call, "the frameless look was dropped"
    assert "WindowMinimizeButtonHint" in flags_call, "taskbar minimize will not work"


def test_ctrl_wheel_is_intercepted_rather_than_left_to_the_web_view():
    """QWebEngineView zooms on Ctrl+wheel by itself, but forgets the level. The
    interception is what makes it persist."""
    source = (Path(__file__).resolve().parents[1] / "ui.py").read_text(encoding="utf-8")
    assert "QEvent.Type.Wheel" in source
    assert "ControlModifier" in source
    assert "_apply_zoom" in source


def test_the_zoom_is_reapplied_after_a_page_load():
    """QWebEngineView resets zoom on navigation, so a factor set before the load
    is silently discarded."""
    source = (Path(__file__).resolve().parents[1] / "ui.py").read_text(encoding="utf-8")
    assert "loadFinished.connect" in source
