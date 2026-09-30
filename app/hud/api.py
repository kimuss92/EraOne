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
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from app.hud import schedule_view, settings, shortcuts, status
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
        r.add_get("/api/schedule", self.schedule)
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

    async def schedule(self, _r: web.Request):
        return web.json_response(await asyncio.to_thread(schedule_view.snapshot), dumps=_dumps)

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
        """Open (or reveal) a file or folder from a context card, a source row,
        or a path shown in HUD text (notices, approvals, Era's replies…).

        Every route goes through ONE validator (_checked_path): local drive
        only, canonicalised (trailing dots/spaces, 8.3 names, symlinks and
        junctions resolve to the real target, and every check runs on that),
        no alternate data streams. A raw ``path`` must also equal, token for
        token, a path the HUD itself published — never text the user or a
        Telegram sender typed. mode "open" opens a folder in Explorer and only
        an allowlisted inert DOCUMENT with its app; everything else, including
        every unknown type, is only revealed, never run."""
        body = await _json_body(r)
        raw = _stored_path(str(body.get("cardId") or ""), str(body.get("sourceId") or ""))
        if not raw and isinstance(body.get("path"), str):
            raw = await asyncio.to_thread(_published_path, body["path"])
        target = await asyncio.to_thread(_checked_path, raw)
        if target is None:
            return web.json_response({"error": "file not found"}, status=404)
        opened = body.get("mode") == "open" and (_safe_folder(target) or _safe_to_open(target))
        if sys.platform == "win32":
            # Quoted by hand: Explorer splits its OWN command line on commas, and
            # list2cmdline only quotes on whitespace. '"' cannot occur in a
            # Windows file name, so the quotes cannot be broken out of.
            if opened and target.is_dir():
                subprocess.Popen(f'explorer "{target}"')
            elif opened:
                await asyncio.to_thread(os.startfile, str(target))  # noqa: S606
            else:
                subprocess.Popen(f'explorer /select,"{target}"')
        return web.json_response({"accepted": True, "action": "open" if opened else "reveal"})


# The ONLY types "open" hands to their default app: inert documents and media.
# Anything else (scripts, installers, disk images, themes, .pyz, .diagcab,
# macro documents, HTML/SVG, unknown types…) is revealed in its folder instead.
_OPENABLE = frozenset(
    ".txt .md .log .csv .tsv .json .pdf .png .jpg .jpeg .gif .webp .bmp .tif .tiff .heic "
    ".mp3 .wav .m4a .flac .ogg .aac .mp4 .mkv .mov .avi .webm .srt .docx .xlsx .pptx".split()
)

# A local drive path only: no relative paths, no UNC/network shares, no \\?\ or \\.\.
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")

# The HUD's own path tokeniser (keep in sync with ui_src/src/components/PathText.tsx).
_PATH_TOKEN = re.compile(
    r"""(["'`])([A-Za-z]:[\\/][^"'`<>|\r\n]*?)\1|([A-Za-z]:[\\/][^\s"'`<>|]*)"""
)
_TRAILING = re.compile(r"[.,;:!?)\]}]+$")


def _safe_to_open(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _OPENABLE


def _safe_folder(path: Path) -> bool:
    # "name.{CLSID}" folders open as shell namespace objects, not folders.
    return path.is_dir() and "{" not in path.name


def _checked_path(raw: str) -> Path | None:
    """The canonical, existing, local target of ``raw`` — or None. Never raises."""
    raw = (raw or "").strip()
    if not raw or len(raw) > 1024 or not _DRIVE_PATH.match(raw) or "\0" in raw:
        return None
    if ":" in raw[2:]:  # alternate data stream (file.txt:stream, ::$DATA)
        return None
    for part in re.split(r"[\\/]+", raw[3:]):
        # Windows silently drops trailing dots/spaces ("evil.cmd." IS evil.cmd).
        if part in (".", "..") or part.endswith((".", " ")):
            return None
    try:
        real = os.path.realpath(raw, strict=True)
    except (OSError, ValueError):
        return None
    if not _DRIVE_PATH.match(real):  # a junction/link out to a share or device
        return None
    return Path(real)


def _norm(p: str) -> str:
    return p.replace("/", "\\").casefold()


def _published_path(raw: str) -> str:
    """``raw`` if it is exactly one of the paths the HUD showed, else ""."""
    raw = (raw or "").strip()
    if len(raw) > 1024 or not _DRIVE_PATH.match(raw):
        return ""
    want = _norm(raw)
    return raw if want in _published_paths() else ""


def _published_paths() -> set[str]:
    """Every path token in what the HUD currently displays — minus anything a
    person typed (role "user": the HUD owner or a Telegram sender), so a path
    someone sends Era is never one click from opening. Never raises."""
    found: set[str] = set()

    def tokens(text: str) -> None:
        for m in _PATH_TOKEN.finditer(text):
            path = m.group(2) or _TRAILING.sub("", m.group(3))
            if len(path) >= 4:
                found.add(_norm(path))

    def walk(v: Any) -> None:
        if isinstance(v, str):
            tokens(v)
        elif isinstance(v, dict):
            if v.get("role") == "user":
                return
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    sources = (
        lambda: hud.conversation(400),
        hud.activity,
        lambda: hud.recent_events(300),
        hud.approval_public,
        hud.failures,
        schedule_view.snapshot,
    )
    for src in sources:
        try:
            walk(src())
        except Exception:
            pass
    return found


def _stored_path(card_id: str, source_id: str) -> str:
    act = hud.activity()
    if card_id:
        for c in act["cards"]:
            if c.get("id") == card_id:
                return str((c.get("payload") or {}).get("path") or "")
    if source_id:
        for s in act["sources"]:
            if s.get("id") == source_id and s.get("type") == "file":
                return str(s.get("subtitle") or "")
    return ""


def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw is not None else default
    except ValueError:
        return default


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
