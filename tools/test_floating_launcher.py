"""Exercise the real Qt menu/event loop in an isolated process, with no live Era."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "scenario", ["menu", "exit", "shutdown", "restart", "offline", "reject", "cancel"]
)
def test_floating_menu_and_process_exit(tmp_path, scenario):
    pytest.importorskip("PyQt6")
    env = {
        **os.environ,
        "ERA_HOME": str(tmp_path / "home"),
        "ERA_HUD_SETTINGS": str(tmp_path / "settings.json"),
        "ERA_NO_BROWSER": "1",
        "QT_QPA_PLATFORM": "offscreen",
    }
    result = subprocess.run(
        [sys.executable, "-m", "tools.test_floating_launcher", "--qt-scenario", scenario],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO PASSED" in result.stdout


def test_snap_to_edge_picks_the_nearest_edge_and_stays_on_screen():
    from app.hud.floating_launcher import MAGNET_MARGIN as M
    from app.hud.floating_launcher import ORB_SIZE as S
    from app.hud.floating_launcher import snap_to_edge

    area = (0, 0, 1919, 1039)  # inclusive Qt geometry of a 1920x1040 work area
    assert snap_to_edge(30, 500, area) == (M, 500)  # near left
    assert snap_to_edge(1850, 500, area) == (1920 - S - M, 500)  # near right
    assert snap_to_edge(900, 20, area) == (900, M)  # near top
    assert snap_to_edge(900, 1000, area) == (900, 1040 - S - M)  # near bottom
    assert snap_to_edge(-500, -500, area) == (M, M)  # dragged off screen: pulled back
    assert snap_to_edge(10, 500, (1920, 0, 3839, 1079)) == (1920 + M, 500)  # second monitor


def test_position_round_trip(tmp_path, monkeypatch):
    from app.hud import floating_launcher

    monkeypatch.setenv("ERA_HUD_SETTINGS", str(tmp_path / "hud_settings.json"))
    assert floating_launcher.load_position() is None
    floating_launcher.save_position(100, 200)
    assert floating_launcher.load_position() == (100, 200)
    (tmp_path / "floating_launcher_pos.json").write_text("garbage", encoding="utf-8")
    assert floating_launcher.load_position() is None


def _qt_scenario(scenario):
    import io
    import json
    import time
    import urllib.request

    from PyQt6.QtCore import QPoint, Qt, QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QPushButton, QSystemTrayIcon

    from app.hud import browser, floating_launcher, settings

    settings.update({"floating_icon": True, "desktop_tray": True})
    state = {"window": None, "online": scenario != "offline"}
    requests, opened, closed, errors, failures = [], [], [], [], []

    def open_hud(url, **kwargs):
        opened.append((url, kwargs))
        state["window"] = 123

    def close_hud():
        closed.append(state["window"])
        state["window"] = None
        return 1

    browser.open_hud = open_hud
    browser.close_hud_windows = close_hud
    browser.find_hud_window = lambda: state["window"]
    QMessageBox.question = lambda *args: (
        QMessageBox.StandardButton.No if scenario == "cancel" else QMessageBox.StandardButton.Yes
    )
    QSystemTrayIcon.showMessage = lambda self, title, message: errors.append(message)

    def urlopen(req, timeout):
        assert req.get_header("X-eraone-token") == "isolated-test-token"
        assert timeout == 3
        if not state["online"]:
            raise ConnectionError("test backend stopped")
        path = req.full_url.removeprefix("http://127.0.0.1:1")
        body = json.loads(req.data) if req.data else None
        requests.append((path, body))
        if path == "/api/settings":
            result = settings.update(body)
        elif path == "/api/launcher":
            result = settings.load()
        else:
            assert path in {"/api/shutdown", "/api/restart"}
            assert body == {"confirm": True}
            result = {"accepted": scenario != "reject"}
            if result["accepted"]:
                state["online"] = False
        return io.BytesIO(json.dumps(result).encode())

    urllib.request.urlopen = urlopen
    sys.stdin = io.StringIO('{"port": 1, "token": "isolated-test-token"}\n')
    started = time.monotonic()
    step = 0
    since = started

    def tick():
        nonlocal step, since
        app = QApplication.instance()
        try:
            if time.monotonic() - started > 12:
                raise AssertionError(f"launcher failed to exit: {scenario}, step={step}")
            orb = next(w for w in app.topLevelWidgets() if isinstance(w, QPushButton))
            menu = next(w for w in app.topLevelWidgets() if isinstance(w, QMenu))
            actions = {a.text(): a for a in menu.actions() if not a.isSeparator()}
            assert set(actions) == {
                "Open App",
                "Open Workspace",
                "Close Workspace",
                "Show Workspace On Startup",
                "Show Floating Icon",
                "Hide Floating Icon",
                "Magnet To Screen Edge",
                "Restart",
                "Quit",
                "Exit Launcher Only",
            }
            assert orb.width() == orb.height() == 48

            if scenario == "offline":
                return  # three failed polls must end the real event loop
            if step == 0:
                if not orb.isVisible():
                    return
                if screenshot := os.environ.get("ERA_LAUNCHER_QA_SCREENSHOT"):
                    assert orb.grab().save(screenshot)
                if scenario == "cancel":
                    actions["Quit"].trigger()
                    actions["Restart"].trigger()
                    actions["Exit Launcher Only"].trigger()
                    return
                if scenario != "menu":
                    actions[
                        {
                            "exit": "Exit Launcher Only",
                            "shutdown": "Quit",
                            "restart": "Restart",
                            "reject": "Quit",
                        }[scenario]
                    ].trigger()
                    step = 1
                    return
                # The normal window close stays protected; explicit Exit must work anyway.
                assert orb.close() is False
                assert orb.isVisible()
                QTest.mouseClick(orb, Qt.MouseButton.LeftButton)
                step = 1
            elif scenario == "reject":
                if errors:
                    assert "did not accept" in errors[-1]
                    assert orb.isVisible() and state["online"]
                    actions["Exit Launcher Only"].trigger()
            elif scenario != "menu":
                return
            elif step == 1 and len(opened) == 1:
                actions["Open App"].trigger()
                actions["Open Workspace"].trigger()
                step = 2
            elif step == 2 and len(opened) == 3:
                # Dragging moves the orb without launching another workspace.
                origin = orb.pos()
                QTest.mousePress(orb, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
                QTest.mouseMove(orb, QPoint(30, 30))
                QTest.mouseRelease(orb, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
                assert orb.pos() != origin and len(opened) == 3
                assert floating_launcher.load_position() is not None  # the drop is remembered
                actions["Close Workspace"].trigger()
                actions["Show Workspace On Startup"].trigger()
                step = 3
            elif step == 3 and closed and not settings.load()["open_hud_on_start"]:
                assert not actions["Show Workspace On Startup"].isChecked()
                actions["Hide Floating Icon"].trigger()
                step, since = 4, time.monotonic()
            elif step == 4 and time.monotonic() - since > 3:
                assert not orb.isVisible(), "Hide was undone by polling without an open workspace"
                assert actions["Hide Floating Icon"].isChecked()
                assert not actions["Show Floating Icon"].isChecked()
                actions["Show Floating Icon"].trigger()
                actions["Show Workspace On Startup"].trigger()
                step = 5
            elif step == 5 and orb.isVisible() and settings.load()["open_hud_on_start"]:
                assert actions["Show Workspace On Startup"].isChecked()
                assert actions["Show Floating Icon"].isChecked()
                assert actions["Magnet To Screen Edge"].isChecked()
                actions["Exit Launcher Only"].trigger()
        except Exception as exc:
            failures.append(repr(exc))
            app.exit(1)

    driver = QTimer()
    driver.timeout.connect(tick)
    # Start after main() constructs QApplication; the scheduled callback is on Qt's thread.
    original_app = QApplication

    def make_app(args):
        app = original_app(args)
        driver.start(50)
        return app

    import PyQt6.QtWidgets

    PyQt6.QtWidgets.QApplication = make_app
    code = floating_launcher.main()
    assert not failures, failures
    assert code == 0
    if scenario in {"menu", "exit", "cancel"}:
        assert state["online"]
        assert not any(p in {"/api/shutdown", "/api/restart"} for p, _ in requests)
    if scenario in {"shutdown", "restart"}:
        assert ("/api/" + scenario, {"confirm": True}) in requests
    print("SCENARIO PASSED", scenario)


if __name__ == "__main__":
    _qt_scenario(sys.argv[2])
