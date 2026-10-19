"""app/hud/window_frame: only allowlisted actions, sane insets, HUD window only."""

import sys

import pytest

from app.hud import window_frame

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows window regions")


def test_unknown_action_refused_before_touching_any_window(monkeypatch):
    def boom():
        raise AssertionError("must validate the action first")

    monkeypatch.setattr(window_frame, "_hud_window", boom)
    with pytest.raises(window_frame.WindowError, match="unknown action"):
        window_frame.apply({"action": "explode"})


@pytest.mark.parametrize("top", [-1, 201, 3.5, "31", True, None])
def test_bad_insets_refused(monkeypatch, top):
    monkeypatch.setattr(window_frame, "_hud_window", lambda: (object(), 1))
    with pytest.raises(window_frame.WindowError, match="inset"):
        window_frame.apply({"action": "frame", "top": top, "side": 7})


def test_refuses_when_hud_not_in_front(monkeypatch):
    import ctypes

    monkeypatch.setattr(ctypes.windll.user32, "GetForegroundWindow", lambda: 0)
    with pytest.raises(window_frame.WindowError, match="no window"):
        window_frame.apply({"action": "minimize"})
