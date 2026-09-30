"""Launcher helpers, single-instance mutexes, HUD settings and the console log
tee. Nothing here starts Era or opens a browser window."""

import json
import os
import sys

import pytest

from app.hud import instance, launcher, settings
from core import stdio_log


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    import socket

    monkeypatch.setenv("ERA_HOME", str(tmp_path / "era"))
    monkeypatch.setenv("ERA_HUD_SETTINGS", str(tmp_path / "hud_settings.json"))
    monkeypatch.setenv("ERA_NO_BROWSER", "1")
    # A port nothing listens on: the launcher must never find the LIVE Era
    # (which answers "ready" on 8765 while the suite runs on this machine).
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("ERAONE_PORT", str(s.getsockname()[1]))
    return tmp_path


def test_mutex_names_are_scoped_to_an_overridden_home():
    name = instance.backend_mutex_name()
    assert name.startswith("Local\\EraOne.Backend.") and len(name) > len("Local\\EraOne.Backend.")
    import era_supervisor

    assert instance.EXIT_ALREADY_RUNNING == era_supervisor.EXIT_ALREADY_RUNNING == 86


@pytest.mark.skipif(sys.platform != "win32", reason="named mutexes are Windows-only")
def test_a_second_owner_is_refused_and_released_on_close():
    a = instance.NamedMutex(instance.backend_mutex_name())
    b = instance.NamedMutex(instance.backend_mutex_name())
    assert a.acquire() is True
    assert instance.mutex_exists(instance.backend_mutex_name())
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_backend_info_round_trip_holds_no_token(tmp_path):
    instance.write_backend_info(port=8765, ui_mode="browser")
    info = instance.read_backend_info()
    assert info["port"] == 8765 and info["pid"] == os.getpid()
    assert "token" not in json.dumps(info).lower()
    instance.clear_backend_info(pid=os.getpid() + 1)  # not ours: kept
    assert instance.read_backend_info() is not None
    instance.clear_backend_info(pid=os.getpid())
    assert instance.read_backend_info() is None


def test_startup_errors_are_reported_to_the_launcher():
    instance.write_startup_error("hud_server", "port 8765 is already in use")
    err = instance.read_startup_error(newer_than=0)
    assert err["reason"] == "hud_server"
    assert instance.read_startup_error(newer_than=err["at"] + 10) is None


def test_health_distinguishes_eraone_from_a_foreign_listener():
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Foreign(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"app": "something-else"}')

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Foreign)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert launcher.health(httpd.server_address[1]) == "foreign"
    finally:
        httpd.shutdown()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert launcher.health(closed, timeout=0.3) is None


def test_launch_clears_an_intentional_stop_flag():
    flag = instance.era_home() / "era.stop"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stopped")
    launcher.clear_stop_flag()
    assert not flag.exists()


def test_halt_flag_becomes_a_readable_startup_error():
    flag = instance.era_home() / "era_supervisor_halted.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("Reason: 6 restarts within 120s")
    with pytest.raises(launcher.LaunchError) as e:
        launcher.wait_ready(lambda _s: None, started_at=0.0, proc=None)
    assert "crashes" in e.value.reason and "6 restarts" in e.value.detail


def test_settings_defaults_validation_and_persistence():
    assert settings.load()["weather_city"] == "Tunis"
    saved = settings.update({"weather_city": "  Sfax  ", "hud_sounds": True})
    assert saved["weather_city"] == "Sfax" and settings.load()["hud_sounds"] is True
    for bad in ({"weather_city": "<img src=x>"}, {"hud_sounds": "yes"}, {"unknown": 1}, {}):
        with pytest.raises(ValueError):
            settings.update(bad)


def test_stdio_log_masks_secrets_and_classifies_lines(tmp_path):
    w = stdio_log.RotatingWriter(tmp_path / "eraone.log")
    e = stdio_log.RotatingWriter(tmp_path / "eraone-error.log")
    tee = stdio_log.TeeStream(None, w, e, is_err=False)
    tee.write("[era] [TOOL] web_search key=AIzaSyA1234567890abcdefghijklmnopqrstuvw\n")
    tee.write("[era] [ERROR] Mic: device lost\n")
    tee.write("partial ")
    tee.write("line joined\n")
    main = (tmp_path / "eraone.log").read_text(encoding="utf-8")
    err = (tmp_path / "eraone-error.log").read_text(encoding="utf-8")
    assert "AIzaSyA1234567890" not in main and "AIza***" in main
    assert "| era/TOOL" in main and "| INFO" in main
    assert "partial line joined" in main
    assert "device lost" in err and "web_search" not in err


def test_stdio_log_rotates_without_raising(tmp_path):
    w = stdio_log.RotatingWriter(tmp_path / "x.log", max_bytes=200, backups=2)
    for i in range(50):
        w.write(f"line {i:04d} " + "x" * 40 + "\n")
    assert (tmp_path / "x.log.1").exists()
    assert (tmp_path / "x.log").stat().st_size < 400
