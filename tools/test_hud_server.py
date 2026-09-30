"""Localhost HUD server: one port for page + REST + WebSocket, and the security
properties that keep a web page from driving Era (token, Host, Origin)."""

import asyncio
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
import pytest

from app.hud.events import hud
from app.hud.ui_base import HeadlessUI
from app.websocket_server import WebSocketServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import os

    os.environ["ERA_HUD_SETTINGS"] = str(tmp_path_factory.mktemp("hud") / "settings.json")
    srv = WebSocketServer(port=_free_port())
    srv.commands = []  # type: ignore[attr-defined]
    srv._on_command = srv.commands.append
    srv._on_interrupt = lambda: srv.commands.append("__interrupt__")
    srv.engine_provider = lambda: object()
    srv.start()
    assert srv.ready.wait(10) and srv.bound_port, srv.bind_error
    yield srv


def get(srv, path, token=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.bound_port}{path}", headers=headers or {})
    if token:
        req.add_header("X-EraOne-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(srv, path, body, token=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.bound_port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("X-EraOne-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_health_is_public_and_identifies_eraone(server):
    status, body = get(server, "/api/health")
    data = json.loads(body)
    assert status == 200 and data["app"] == "eraone" and data["status"] == "ready"
    assert "token" not in json.dumps(data).lower()


def test_api_needs_the_session_token(server):
    assert get(server, "/api/status")[0] == 401
    assert get(server, "/api/status", token="wrong")[0] == 401
    assert get(server, "/api/status", token=server.session_token)[0] == 200


def test_schedule_is_token_guarded_and_read_only(server):
    assert get(server, "/api/schedule")[0] == 401
    status, body = get(server, "/api/schedule", token=server.session_token)
    data = json.loads(body)
    assert status == 200 and "tasks" in data and "running" in data
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.bound_port}/api/schedule",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-EraOne-Token": server.session_token},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 405  # no write path exists


def test_dns_rebinding_host_is_refused(server):
    assert (
        get(server, "/api/health", headers={"Host": f"evil.example:{server.bound_port}"})[0] == 403
    )


def test_cross_origin_calls_are_refused_even_with_a_token(server):
    status, _ = get(server, "/api/status", server.session_token, {"Origin": "https://evil.example"})
    assert status == 403


def test_the_page_carries_the_token_and_a_strict_csp(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.bound_port}/")
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode()
        csp = r.headers.get("Content-Security-Policy", "")
    assert f'content="{server.session_token}"' in html
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp


def test_capabilities_come_from_the_real_registry(server):
    status, body = get(server, "/api/capabilities", token=server.session_token)
    data = json.loads(body)
    from app.tool_declarations import TOOL_DECLARATIONS

    assert status == 200 and data["count"] == len(TOOL_DECLARATIONS)
    cal = [i for i in data["integrations"] if i["id"] == "calendar"][0]
    assert cal["connected"] is False  # no calendar integration exists; never faked


def test_message_endpoint_validates_and_reaches_the_engine(server):
    assert post(server, "/api/message", {"text": ""}, server.session_token)[0] == 400
    assert post(server, "/api/message", {"text": "x" * 9000}, server.session_token)[0] == 413
    status, data = post(server, "/api/message", {"text": "pause spotify"}, server.session_token)
    assert status == 200 and data["accepted"] is True
    import time

    time.sleep(0.2)
    assert "pause spotify" in server.commands


def test_shutdown_requires_explicit_confirmation(server):
    assert post(server, "/api/shutdown", {}, server.session_token)[0] == 400


def test_settings_reject_bad_values(server):
    assert (
        post(server, "/api/settings", {"weather_city": "<script>"}, server.session_token)[0] == 400
    )
    assert post(server, "/api/settings", {"nope": 1}, server.session_token)[0] == 400
    status, data = post(server, "/api/settings", {"hud_sounds": True}, server.session_token)
    assert status == 200 and data["hud_sounds"] is True


def test_reveal_only_opens_paths_from_the_backend_card_store(server):
    status, _ = post(server, "/api/reveal", {"cardId": "does-not-exist"}, server.session_token)
    assert status == 404


@pytest.fixture
def opener(monkeypatch, tmp_path):
    """Stored cards/sources/conversation + recorded launches (nothing really opens).

    Built on the canonical tmp dir: the endpoint resolves 8.3 short names
    (TEMP often is one) to the long form before acting."""
    import os

    from app.hud import api

    base = Path(os.path.realpath(tmp_path))
    doc, script, folder = base / "notes.txt", base / "run.bat", base / "ABC"
    pyz, cab, cmd = base / "tool.pyz", base / "fix.diagcab", base / "evil.cmd"
    for f in (doc, script, pyz, cab, cmd):
        f.write_text("x")
    folder.mkdir()
    store = {
        "cards": [
            {"id": f"c-{p.name}", "payload": {"path": str(p)}}
            for p in (doc, script, folder, pyz, cab)
        ]
        + [
            {"id": "c-dot", "payload": {"path": f"{cmd}."}},
            {"id": "c-unc", "payload": {"path": r"\\evil.example\s\invoice.pdf"}},
        ],
        "sources": [
            {"id": "s-doc", "type": "file", "subtitle": str(doc)},
            {"id": "s-rel", "type": "file", "subtitle": "desktop"},
            {"id": "s-web", "type": "web", "subtitle": str(doc)},
        ],
    }
    said = base / "said-by-user.txt"
    said.write_text("x")
    convo = [
        {"role": "era", "text": f'Saved "{doc}". Also see {folder}\\..\\notes.txt'},
        {"role": "user", "text": f"open {said}"},
    ]
    calls: list = []
    monkeypatch.setattr(api.hud, "activity", lambda: store)
    monkeypatch.setattr(api.hud, "conversation", lambda limit=400: convo)
    monkeypatch.setattr(api.os, "startfile", lambda p: calls.append(("start", p)), raising=False)
    monkeypatch.setattr(api.subprocess, "Popen", lambda args: calls.append(("popen", args)))
    return calls, doc, script, folder


def _reveal(server, body):
    return post(server, "/api/reveal", body, server.session_token)


def test_open_mode_opens_a_document_with_its_app(server, opener):
    calls, doc, _, _ = opener
    status, data = _reveal(server, {"cardId": "c-notes.txt", "mode": "open"})
    assert status == 200 and data["action"] == "open"
    assert calls == [("start", str(doc))]


def test_open_mode_never_runs_code_it_only_reveals_it(server, opener):
    calls, _, script, _ = opener
    status, data = _reveal(server, {"cardId": "c-run.bat", "mode": "open"})
    assert status == 200 and data["action"] == "reveal"
    assert calls == [("popen", f'explorer /select,"{script}"')]


def test_open_is_an_allowlist_unknown_runnable_types_are_only_revealed(server, opener):
    calls, *_ = opener
    for card in ("c-tool.pyz", "c-fix.diagcab"):
        _, data = _reveal(server, {"cardId": card, "mode": "open"})
        assert data["action"] == "reveal"
    assert all(kind == "popen" for kind, _ in calls) and len(calls) == 2


def test_trailing_dot_name_is_refused_not_run(server, opener):
    # Windows drops the dot: "evil.cmd." IS evil.cmd, and startfile would run it.
    calls, *_ = opener
    status, _ = _reveal(server, {"cardId": "c-dot", "mode": "open"})
    assert status == 404 and calls == []


def test_unc_card_paths_are_refused(server, opener):
    calls, *_ = opener
    status, _ = _reveal(server, {"cardId": "c-unc", "mode": "open"})
    assert status == 404 and calls == []


def test_open_mode_opens_a_folder_in_explorer(server, opener):
    calls, _, _, folder = opener
    _reveal(server, {"cardId": "c-ABC", "mode": "open"})
    assert calls == [("popen", f'explorer "{folder}"')]


def test_default_mode_still_only_reveals(server, opener):
    calls, doc, _, _ = opener
    _reveal(server, {"cardId": "c-notes.txt"})
    assert calls == [("popen", f'explorer /select,"{doc}"')]


def test_source_rows_open_only_absolute_file_paths(server, opener):
    calls, doc, _, _ = opener
    ok, _ = _reveal(server, {"sourceId": "s-doc", "mode": "open"})
    rel, _ = _reveal(server, {"sourceId": "s-rel", "mode": "open"})
    web, _ = _reveal(server, {"sourceId": "s-web", "mode": "open"})
    assert (ok, rel, web) == (200, 404, 404)
    assert calls == [("start", str(doc))]


def test_raw_path_opens_only_an_exact_published_path(server, opener, tmp_path):
    calls, doc, _, folder = opener
    base = doc.parent
    secret = base / "never-shown.txt"
    secret.write_text("x")
    codes = [
        _reveal(server, {"path": str(doc), "mode": "open"})[0],  # Era said it
        _reveal(server, {"path": str(secret), "mode": "open"})[0],  # never shown
        _reveal(server, {"path": str(base / "said-by-user.txt"), "mode": "open"})[0],  # typed
        _reveal(server, {"path": str(base), "mode": "open"})[0],  # a prefix of a shown path
        _reveal(server, {"path": f"{folder}\\..\\notes.txt", "mode": "open"})[0],  # traversal
        _reveal(server, {"path": r"\\evil\share\x.txt", "mode": "open"})[0],
        _reveal(server, {"path": "notes.txt", "mode": "open"})[0],
    ]
    assert codes == [200, 404, 404, 404, 404, 404, 404]
    assert calls == [("start", str(doc))]


def test_checked_path_rejects_windows_name_tricks(tmp_path):
    from app.hud import api

    f = tmp_path / "a.txt"
    f.write_text("x")
    assert api._checked_path(str(f)) is not None
    for bad in (f"{f}.", f"{f} .", f"{f}::$DATA", f"{f}:stream", r"\\?\C:\x", r"\\srv\s\a.txt"):
        assert api._checked_path(bad) is None, bad


def test_websocket_auth_snapshot_events_and_commands(server):
    async def run():
        base = f"http://127.0.0.1:{server.bound_port}"
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                async with s.ws_connect(base + "/ws"):
                    pass
            with pytest.raises(aiohttp.WSServerHandshakeError):
                async with s.ws_connect(
                    base + f"/ws?token={server.session_token}",
                    headers={"Origin": "https://evil.example"},
                ):
                    pass
            async with s.ws_connect(base + f"/ws?token={server.session_token}") as ws:
                first = await ws.receive_json(timeout=5)
                assert first["type"] == "snapshot" and "state" in first["data"]
                hud.attach(server.broadcast)
                hud.tool_started("t-ws", "web_search", {"query": "x"})
                seen = []
                for _ in range(8):
                    m = await ws.receive_json(timeout=5)
                    seen.append(m.get("event", {}).get("type"))
                    if "tool.started" in seen:
                        break
                assert "tool.started" in seen
                await ws.send_json({"type": "interrupt"})
                await ws.send_json({"type": "ping", "t": 7})
                while True:
                    m = await ws.receive_json(timeout=5)
                    if m["type"] == "pong":
                        assert m["t"] == 7
                        break
        await asyncio.sleep(0.2)
        assert "__interrupt__" in server.commands

    asyncio.run(run())


def test_port_conflict_falls_back_to_a_free_port():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        srv = WebSocketServer(port=busy, port_fallback=5)
        srv.start()
        assert srv.ready.wait(10)
        assert srv.bound_port not in (None, busy)
    finally:
        blocker.close()


def test_port_conflict_without_fallback_is_reported_not_raised():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        srv = WebSocketServer(port=blocker.getsockname()[1], port_fallback=0)
        srv.start()
        assert srv.ready.wait(10)
        assert srv.bound_port is None and "in use" in (srv.bind_error or "")
    finally:
        blocker.close()


def test_headless_ui_speaks_the_engine_api_and_feeds_the_bus():
    ui = HeadlessUI()
    sent = []
    ui.set_ws_broadcast(sent.append)
    ui.set_state("SPEAKING")
    ui.write_log("era: hello")
    ui.set_mute(True)
    assert ui.muted is True
    ui.set_amplitude(float("nan"), [float("inf"), 0.5])
    amp = [m for m in sent if m["type"] == "amplitude"][0]
    assert amp["value"] == 0.0 and amp["bands"] == [0.0, 0.5]  # stays valid JSON
    assert {m["type"] for m in sent} >= {"state", "log", "mute", "amplitude"}
    ui.window_control("minimize")  # the browser owns its window: a no-op, never an error
