"""New settings reuse HUD authentication and protect credential values."""

import json

from tools.test_hud_server import get, post, server  # noqa: F401


def test_new_settings_and_companion_require_hud_auth(server):
    for path in ("/api/empower", "/api/companion", "/api/launcher"):
        assert get(server, path)[0] == 401
    for path in ("/api/empower/settings", "/api/empower/test", "/api/companion", "/api/launcher"):
        assert post(server, path, {})[0] == 401


def test_service_snapshot_cannot_leak_a_key(server, monkeypatch):
    from core import empower_config

    monkeypatch.setattr(empower_config, "read", lambda: {"gemini_api_key": "PRIVATE_TEST_SECRET"})
    code, body = get(server, "/api/empower", token=server.session_token)
    assert code == 200
    assert b"PRIVATE_TEST_SECRET" not in body
    fields = json.loads(body)["fields"]
    key = next(f for f in fields if f["key"] == "gemini_api_key")
    assert key["configured"] and key["value"] == ""


def test_launcher_request_persists_visibility_and_uses_current_session(server, monkeypatch):
    from app.hud import floating_launcher, settings

    launches = []
    monkeypatch.setattr(floating_launcher, "ensure_started", lambda *args: launches.append(args))
    code, body = post(server, "/api/launcher", {"action": "show"}, server.session_token)
    assert code == 200 and body["accepted"]
    assert settings.load()["floating_icon"] and settings.load()["desktop_tray"]
    assert launches == [(server.bound_port, server.session_token)]
    code, body = post(server, "/api/launcher", {"action": "hide"}, server.session_token)
    assert code == 200 and not settings.load()["floating_icon"]
    settings.update({"desktop_tray": False})


def test_arbitrary_settings_and_launcher_actions_rejected(server):
    assert (
        post(server, "/api/empower/settings", {"execute": "anything"}, server.session_token)[0]
        == 400
    )
    assert post(server, "/api/launcher", {"action": "execute"}, server.session_token)[0] == 400


def test_companion_default_is_stopped(server):
    code, body = get(server, "/api/companion", token=server.session_token)
    assert code == 200 and not json.loads(body)["running"]


def test_launcher_poll_does_not_probe_windows_startup(server, monkeypatch):
    from app.hud import shortcuts

    def unexpected_probe():
        raise AssertionError("Launcher polling must not enumerate scheduled tasks")

    monkeypatch.setattr(shortcuts, "autostart_status", unexpected_probe)
    code, body = get(server, "/api/launcher", token=server.session_token)
    assert code == 200
    assert set(json.loads(body)) == {
        "floating_icon",
        "desktop_tray",
        "open_hud_on_start",
        "floating_magnet",
    }


def test_launcher_magnet_toggle_persists_without_starting_anything(server, monkeypatch):
    from app.hud import floating_launcher, settings

    launches = []
    monkeypatch.setattr(floating_launcher, "ensure_started", lambda *args: launches.append(args))
    code, body = post(server, "/api/launcher", {"action": "magnet_off"}, server.session_token)
    assert code == 200 and body["floating_magnet"] is False
    assert settings.load()["floating_magnet"] is False
    code, body = post(server, "/api/launcher", {"action": "magnet_on"}, server.session_token)
    assert code == 200 and settings.load()["floating_magnet"] is True
    assert launches == []


def test_closing_the_last_workspace_brings_the_floating_icon_back(server, monkeypatch):
    import asyncio

    from app.hud import browser, floating_launcher, settings

    launches = []
    monkeypatch.delenv("ERA_NO_BROWSER", raising=False)
    monkeypatch.setattr(floating_launcher, "ensure_started", lambda *args: launches.append(args))
    monkeypatch.setattr(browser, "find_hud_window", lambda *a: None)
    settings.update({"floating_icon": False, "desktop_tray": False})
    asyncio.run(server._restore_launcher_after_close(grace=0))
    assert settings.load()["floating_icon"] and settings.load()["desktop_tray"]
    assert launches == [(server.bound_port, server.session_token)]

    # A workspace window still open (page reload, second window): leave it alone.
    launches.clear()
    settings.update({"floating_icon": False})
    monkeypatch.setattr(browser, "find_hud_window", lambda *a: 123)
    asyncio.run(server._restore_launcher_after_close(grace=0))
    assert not settings.load()["floating_icon"] and launches == []
    settings.update({"desktop_tray": False})
