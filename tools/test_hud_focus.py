"""Voice navigation requests are allowlisted, ephemeral, and view-only."""

import asyncio
import json

import aiohttp
import pytest

from app.hud.events import HudBus
from core import autopilot, visual_engine
from tools.test_hud_server import post, server  # noqa: F401 - shared isolated server fixture


@pytest.mark.parametrize(
    "phrase,view",
    [
        ("Era, show me system telemetry", "telemetry"),
        ("show active tasks", "activity"),
        ("open memory", "memory"),
        ("show context", "context"),
        ("show my schedule", "schedule"),
        ("show diagnostics", "diagnostics"),
        ("show tools", "tools"),
        ("show overview", "overview"),
        ("focus telemetry panel", "telemetry"),
    ],
)
def test_voice_command_emits_view_without_scene_write(monkeypatch, tmp_path, phrase, view):
    from app.hud import events

    monkeypatch.setenv("ERA_AUTOPILOT", "1")
    bus = HudBus()
    sent = []
    bus.attach(sent.append)
    monkeypatch.setattr(events, "hud", bus)
    monkeypatch.setattr(visual_engine, "PATH", tmp_path / "scene.json")
    decision = autopilot.classify(phrase)
    assert decision.tool == "visual_engine"
    assert decision.lane == autopilot.LOCAL
    result = visual_engine.apply(json.loads(decision.args["command"]))
    assert result["success"] is True and result["view"] == view
    assert sent[-1]["event"]["type"] == "hud.view.focus"
    assert sent[-1]["event"]["metadata"] == {"view": view}
    assert not visual_engine.PATH.exists()


@pytest.mark.parametrize("view", [None, [], {}, "shell", "telemetry; shutdown", ""])
def test_invalid_view_never_emits(view):
    bus = HudBus()
    sent = []
    bus.attach(sent.append)
    with pytest.raises(ValueError):
        bus.focus_view(view)
    assert sent == []


def test_focus_cannot_smuggle_a_layout_or_widget():
    with pytest.raises(ValueError):
        visual_engine.apply({"action": "focus", "view": "tools", "layout": {"left": False}})


@pytest.mark.parametrize(
    "phrase",
    [
        "don't show telemetry",
        "show telemetry and delete files",
        "she said show telemetry",
        "show me system telemetry tomorrow",
        "what happens if I show telemetry",
    ],
)
def test_ambiguous_or_compound_request_is_not_partially_executed(monkeypatch, phrase):
    monkeypatch.setenv("ERA_AUTOPILOT", "1")
    assert autopilot.classify(phrase).rule != "hud_focus"


def test_focus_crosses_authenticated_api_and_websocket(server):
    from app.hud.events import hud

    async def check():
        hud.attach(server.broadcast)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(
                    f"http://127.0.0.1:{server.bound_port}/ws?token={server.session_token}"
                ) as socket:
                    code, body = await asyncio.to_thread(
                        post,
                        server,
                        "/api/visual",
                        {"action": "focus", "view": "telemetry"},
                        server.session_token,
                    )
                    assert code == 200 and body["view"] == "telemetry"
                    while True:
                        message = await socket.receive_json(timeout=3)
                        if message.get("event", {}).get("type") == "hud.view.focus":
                            assert message["event"]["metadata"] == {"view": "telemetry"}
                            break
            assert server.commands == []
            assert post(server, "/api/visual", {"action": "focus", "view": "tools"})[0] == 401
        finally:
            hud.attach(None)

    asyncio.run(check())
