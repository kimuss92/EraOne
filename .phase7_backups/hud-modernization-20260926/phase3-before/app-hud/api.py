"""REST endpoints for the localhost HUD.

Reads come from the HUD bus and app.hud.status; writes go through the SAME
engine callbacks the WebSocket uses (main.py wires them), so there is exactly
one path into the engine per action. Anything blocking (file reads, psutil,
network) runs on a worker thread, never on the server loop.

Auth is enforced by the server middleware (session token + origin check) for
everything except /api/health, which only says whether Era is up.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from app.hud import settings, shortcuts, status
from app.hud.events import hud

_MAX_TEXT = 8000


async def _json_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception as e:
        raise web.HTTPBadRequest(text="body must be JSON") from e
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="body must be a JSON object")
    return data


class HudApi:
    def __init__(self, server: Any) -> None:
        self.s = server

    def register(self, app: web.Application) -> None:
        r = app.router
        r.add_get("/api/health", self.health)
        r.add_get("/api/status", self.get_status)
        r.add_get("/api/system", self.system)
        r.add_get("/api/capabilities", self.capabilities)
        r.add_get("/api/conversation", self.conversation)
        r.add_get("/api/activity", self.activity)
        r.add_get("/api/events", self.events)
        r.add_get("/api/memory", self.memory)
        r.add_get("/api/memory/search", self.memory_search)
        r.add_get("/api/settings", self.get_settings)
        r.add_post("/api/settings", self.post_settings)
        r.add_get("/api/weather", self.weather)
        r.add_get("/api/diagnostics", self.diagnostics)
        r.add_post("/api/message", self.message)
        r.add_post("/api/interrupt", self.interrupt)
        r.add_post("/api/voice/start", self.voice_start)
        r.add_post("/api/voice/stop", self.voice_stop)
        r.add_post("/api/standby", self.standby)
        r.add_post("/api/shutdown", self.shutdown)
        r.add_post("/api/restart", self.restart)
        r.add_post("/api/reveal", self.reveal)

    def _call(self, fn: Any, *args: Any) -> bool:
        if fn is None:
            return False
        threading.Thread(target=fn, args=args, daemon=True).start()
        return True

    def _need_engine(self) -> None:
        if self.s.engine() is None:
            raise web.HTTPServiceUnavailable(text="ERA ONE is still starting")

    # ── reads ────────────────────────────────────────────────────────────

    async def health(self, _r: web.Request):
        return web.json_response(status.health(self.s, self.s.engine()))

    async def get_status(self, _r: web.Request):
        data = await asyncio.to_thread(status.status, self.s, self.s.engine())
        return web.json_response(data, dumps=_dumps)

    async def system(self, _r: web.Request):
        return web.json_response({"metrics": self.s.system_metrics(), "os": status.os_name()})

    async def capabilities(self, _r: web.Request):
        return web.json_response(await asyncio.to_thread(status.capabilities), dumps=_dumps)

    async def conversation(self, r: web.Request):
        limit = _int(r.query.get("limit"), 400, 1, 400)
        return web.json_response({"entries": hud.conversation(limit)}, dumps=_dumps)

    async def activity(self, _r: web.Request):
        return web.json_response(hud.activity(), dumps=_dumps)

    async def events(self, r: web.Request):
        limit = _int(r.query.get("limit"), 100, 1, 300)
        return web.json_response({"events": hud.recent_events(limit)}, dumps=_dumps)

    async def memory(self, _r: web.Request):
        return web.json_response(await asyncio.to_thread(status.memory_overview), dumps=_dumps)

    async def memory_search(self, r: web.Request):
        q = (r.query.get("q") or "").strip()
        if not q:
            return web.json_response({"results": []})
        rows = await asyncio.to_thread(status.memory_search, q)
        return web.json_response({"query": q[:200], "results": rows}, dumps=_dumps)

    async def get_settings(self, _r: web.Request):
        data = settings.load()
        data["autostart"] = await asyncio.to_thread(shortcuts.autostart_status)
        return web.json_response(data)

    async def post_settings(self, r: web.Request):
        body = await _json_body(r)
        autostart = body.pop("autostart", None)
        try:
            data = settings.update(body) if body else settings.load()
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if autostart is not None:
            if not isinstance(autostart, bool):
                return web.json_response({"error": "autostart must be true or false"}, status=400)
            data["autostart"] = await asyncio.to_thread(shortcuts.set_autostart, autostart)
        else:
            data["autostart"] = await asyncio.to_thread(shortcuts.autostart_status)
        return web.json_response(data)

    async def weather(self, _r: web.Request):
        city = settings.load()["weather_city"]
        return web.json_response(await asyncio.to_thread(status.weather, city), dumps=_dumps)

    async def diagnostics(self, _r: web.Request):
        data = await asyncio.to_thread(status.diagnostics, self.s, self.s.engine())
        return web.json_response(data, dumps=_dumps)

    # ── actions (same callbacks as the WebSocket) ────────────────────────

    async def message(self, r: web.Request):
        body = await _json_body(r)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return web.json_response({"error": "text is required"}, status=400)
        if len(text) > _MAX_TEXT:
            return web.json_response({"error": "text too long"}, status=413)
        self._need_engine()
        return web.json_response({"accepted": self._call(self.s._on_command, text.strip())})

    async def interrupt(self, _r: web.Request):
        self._need_engine()
        return web.json_response({"accepted": self._call(self.s._on_interrupt)})

    async def voice_start(self, _r: web.Request):
        self._need_engine()
        return web.json_response({"accepted": self._call(self.s._on_mic_toggle, True)})

    async def voice_stop(self, _r: web.Request):
        self._need_engine()
        return web.json_response({"accepted": self._call(self.s._on_mic_toggle, False)})

    async def standby(self, r: web.Request):
        body = await _json_body(r)
        on = body.get("on")
        if not isinstance(on, bool):
            return web.json_response({"error": "on must be true or false"}, status=400)
        self._need_engine()
        cmd = "__standby__" if on else "__unstandby__"
        return web.json_response({"accepted": self._call(self.s._on_command, cmd)})

    async def shutdown(self, r: web.Request):
        body = await _json_body(r)
        if body.get("confirm") is not True:
            return web.json_response({"error": "confirm must be true"}, status=400)
        return web.json_response({"accepted": self._call(self.s._on_shutdown)})

    async def restart(self, r: web.Request):
        body = await _json_body(r)
        if body.get("confirm") is not True:
            return web.json_response({"error": "confirm must be true"}, status=400)
        return web.json_response({"accepted": self._call(self.s._on_restart)})

    async def reveal(self, r: web.Request):
        """Show a file from a context card in Explorer. The path comes from the
        backend's own card store, never from the request — and it is REVEALED
        (selected in its folder), never executed."""
        body = await _json_body(r)
        card_id = str(body.get("cardId") or "")
        path = ""
        for c in hud.activity()["cards"]:
            if c.get("id") == card_id:
                path = str((c.get("payload") or {}).get("path") or "")
                break
        if not path or not Path(path).exists():
            return web.json_response({"error": "file not found"}, status=404)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{Path(path)}"])
        return web.json_response({"accepted": True})


def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw is not None else default
    except ValueError:
        return default


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
