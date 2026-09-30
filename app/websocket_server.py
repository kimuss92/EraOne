"""Localhost HUD server: the HUD page, its REST API and its WebSocket on ONE port.

Replaces the bare `websockets` server so the browser HUD can be served from the
same origin it talks to (http://127.0.0.1:<port>/ + ws://…/ws): no CORS, one
port to health-check, one port conflict to handle. aiohttp was already a
dependency (main.py uses it for Telegram pushes). The public surface main.py
wires — the `_on_*` callbacks, `broadcast()`, `start()` — is unchanged.

Runs on its OWN thread and event loop, separate from the engine's, so a busy
or blocked engine can never freeze the HUD.

Security (the backend acts on the real machine, so a web page must never be
able to drive it):
  * binds 127.0.0.1 only;
  * Host header must be 127.0.0.1/localhost/[::1] on the bound port, which
    defeats DNS-rebinding;
  * every WebSocket and every /api/* call except /api/health needs the
    per-process session token. The token is injected into the served HUD page,
    which a cross-origin page cannot read, and is never written to disk;
  * a browser Origin, when present, must be this server's own origin (the legacy
    Qt window loads from file://, whose Origin is "null"; it carries the token).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from core.system_monitor import SystemMonitor
from core.vad import get_vad

_ROOT = Path(__file__).resolve().parents[1]
DIST_INDEX = _ROOT / "ui" / "dist" / "index.html"
ASSETS = _ROOT / "assets"
MAX_MSG = 32 * 1024 * 1024  # base64 file uploads ride the socket

_MISSING_BUILD = (
    "<!doctype html><html><head><title>ERA ONE</title></head>"
    "<body style='background:#03070c;color:#7fb8d4;font:14px monospace;display:flex;"
    "align-items:center;justify-content:center;height:100vh;margin:0'>"
    "<div>HUD build not found. Run <code>npm run build</code> in <code>ui_src</code>.</div>"
    "</body></html>"
)


class WebSocketServer:
    """HTTP + WebSocket bridge between the engine and the HUD frontend."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, port_fallback: int = 0):
        self.host = host
        self.port = port
        self.port_fallback = max(0, int(port_fallback))
        self._clients: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Engine callbacks (wired by main.py). Each runs on its own thread so a
        # slow engine call never stalls the socket.
        self._on_command: Any = None
        self._on_window_control: Any = None
        self._on_mic_toggle: Any = None
        self._mic_state_provider: Any = None
        self._on_file_upload: Any = None
        self._on_model_select: Any = None
        self._on_client_connect: Any = None
        self._on_interrupt: Any = None
        self._on_shutdown: Any = None
        self._on_restart: Any = None
        self.engine_provider: Any = lambda: None
        self.meta: dict = {}
        self.session_token = secrets.token_urlsafe(24)
        self.bound_port: int | None = None
        self.bind_error: str | None = None
        self.ready = threading.Event()
        self.started_at = time.time()
        self._system_monitor = SystemMonitor(interval=2.0)
        self._vad = get_vad()
        self._index_cache: tuple[float, str] = (0.0, "")
        self._runner: web.AppRunner | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="HudServer", daemon=True)
        self._thread.start()

    @staticmethod
    def _loop_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """A HUD client vanishing (window closed, a launcher's health probe) makes
        the Windows Proactor loop print a ConnectionResetError traceback. That is
        expected here and says nothing about Era; anything else is reported."""
        if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
            return
        loop.default_exception_handler(context)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.set_exception_handler(self._loop_exception)
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # port taken, bad config: report, never crash Era
            self.bind_error = f"{type(e).__name__}: {e}"
            self.ready.set()
            return
        self._loop.run_forever()

    async def _serve(self) -> None:
        from app.hud.api import HudApi

        self._system_monitor.start()
        self._vad.start()
        app = web.Application(middlewares=[self._guard], client_max_size=MAX_MSG)
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/", self._root)
        app.router.add_get("/favicon.ico", self._favicon)
        app.router.add_get("/icon-{size}.png", self._icon)
        app.router.add_get("/manifest.webmanifest", self._manifest)
        HudApi(self).register(app)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self.bound_port = await self._bind()
        self.ready.set()
        asyncio.get_running_loop().create_task(self._push_metrics_loop())

    async def _port_in_use(self, port: int) -> bool:
        """Something already answers there. Probed by connecting, because on
        Windows a 127.0.0.1 bind can succeed next to another process's 0.0.0.0
        listener — the bind alone would not reveal the conflict."""
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(self.host, port), 0.25)
        except (OSError, TimeoutError):
            return False
        w.close()
        return True

    async def _bind(self) -> int:
        last: Exception | None = None
        for port in range(self.port, self.port + self.port_fallback + 1):
            if await self._port_in_use(port):
                last = OSError(f"port {port} is already in use by another process")
                continue
            site = web.TCPSite(self._runner, self.host, port)
            try:
                await site.start()
                return port
            except OSError as e:
                last = e
        raise last or OSError("no port available")

    def client_count(self) -> int:
        return len(self._clients)

    def engine(self) -> Any:
        try:
            return self.engine_provider()
        except Exception:
            return None

    # ── security ─────────────────────────────────────────────────────────

    def origins(self) -> set[str]:
        p = self.bound_port
        return {f"http://127.0.0.1:{p}", f"http://localhost:{p}", f"http://[::1]:{p}"}

    def _host_ok(self, host: str) -> bool:
        p = self.bound_port
        return host.lower() in {f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"}

    def token_ok(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(str(token), self.session_token)

    def _ws_allowed(self, request: web.Request) -> bool:
        if not self.token_ok(request.query.get("token", "")):
            return False
        origin = request.headers.get("Origin")
        return origin is None or origin in self.origins() or origin in ("null", "file://")

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if not self._host_ok(request.headers.get("Host", "")):
            return web.Response(status=403, text="forbidden host")
        path = request.path
        if path.startswith("/api/") and path != "/api/health":
            if not self.token_ok(request.headers.get("X-EraOne-Token", "")):
                return web.json_response({"error": "unauthorized"}, status=401)
            origin = request.headers.get("Origin")
            if origin and origin not in self.origins():
                return web.json_response({"error": "forbidden origin"}, status=403)
        resp = await handler(request)
        if not resp.prepared:
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            if path.startswith("/api/"):
                resp.headers["Cache-Control"] = "no-store"
        return resp

    # ── static ───────────────────────────────────────────────────────────

    def _index_html(self) -> str:
        try:
            mtime = DIST_INDEX.stat().st_mtime
        except OSError:
            return _MISSING_BUILD
        if mtime != self._index_cache[0]:
            self._index_cache = (mtime, DIST_INDEX.read_text(encoding="utf-8"))
        html = self._index_cache[1]
        meta = f'<meta name="eraone-token" content="{self.session_token}">'
        return html.replace("<head>", "<head>" + meta, 1)

    async def _root(self, request: web.Request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._ws_handler(request)  # legacy ws://host:port (Qt window)
        p = self.bound_port
        csp = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            f"font-src 'self' data:; connect-src 'self' ws://127.0.0.1:{p} ws://localhost:{p}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return web.Response(
            text=self._index_html(),
            content_type="text/html",
            headers={"Cache-Control": "no-store", "Content-Security-Policy": csp},
        )

    async def _favicon(self, _request: web.Request):
        ico = ASSETS / "eraone.ico"
        return web.FileResponse(ico) if ico.exists() else web.Response(status=404)

    async def _icon(self, request: web.Request):
        size = request.match_info.get("size", "")
        png = ASSETS / f"eraone-{size}.png"
        if size not in ("192", "512") or not png.exists():
            return web.Response(status=404)
        return web.FileResponse(png)

    async def _manifest(self, _request: web.Request):
        return web.json_response(
            {
                "name": "ERA ONE",
                "short_name": "ERA ONE",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#03070c",
                "theme_color": "#03070c",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            },
            content_type="application/manifest+json",
        )

    # ── websocket ────────────────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request):
        if not self._ws_allowed(request):
            return web.Response(status=403, text="forbidden")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MSG)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            await self._greet(ws)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
        return ws

    async def _greet(self, ws: web.WebSocketResponse) -> None:
        """Bring a fresh window up to date: it cannot ask for state it missed."""
        from app.hud.events import hud
        from app.hud.status import os_name

        try:
            if self._mic_state_provider is not None:
                active = bool(self._mic_state_provider())
                await ws.send_str(json.dumps({"type": "browser_mic", "active": active}))
            await ws.send_str(json.dumps({"type": "snapshot", "data": hud.snapshot()}, default=str))
            metrics = self._system_monitor.get_latest()
            if metrics:
                metrics["os"] = os_name()
                await ws.send_str(json.dumps({"type": "system_metrics", "data": metrics}))
        except Exception:
            pass
        if self._on_client_connect is not None:
            threading.Thread(target=self._on_client_connect, daemon=True).start()

    def _spawn(self, fn: Any, *args: Any) -> None:
        if fn is not None:
            threading.Thread(target=fn, args=args, daemon=True).start()

    async def _dispatch(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        t = data.get("type", "")
        if t == "command":
            self._spawn(self._on_command, str(data.get("text", ""))[:8000])
        elif t == "mute":
            self._spawn(self._on_command, "__mute__" if data.get("muted") else "__unmute__")
        elif t == "window_control":
            self._spawn(self._on_window_control, str(data.get("action", "")))
        elif t == "mic_toggle":
            self._spawn(self._on_mic_toggle, data.get("active", None))
        elif t == "file_upload":
            self._spawn(
                self._on_file_upload,
                str(data.get("name", "")),
                str(data.get("mime", "")),
                str(data.get("data", "")),
            )
        elif t == "model_select":
            self._spawn(self._on_model_select, str(data.get("alias", "") or ""))
        elif t == "interrupt":
            self._spawn(self._on_interrupt)
        elif t == "ping":
            try:
                await ws.send_str(json.dumps({"type": "pong", "t": data.get("t")}))
            except Exception:
                pass
        elif t == "mic_volume":
            # Legacy Qt UI only: its browser-side meter feeds the display VAD.
            self._vad.update_mic_volume(float(data.get("volume", 0.0) or 0.0))
            self.broadcast({"type": "user_voice_active", "active": self._vad.is_speaking()})

    # ── broadcast ────────────────────────────────────────────────────────

    def broadcast(self, data: dict) -> None:
        """Thread-safe fan-out: one hop onto the server loop per message."""
        loop = self._loop
        if loop is None or not self._clients:
            return
        try:
            msg = json.dumps(data, default=str)
            loop.call_soon_threadsafe(self._fanout, msg)
        except (RuntimeError, TypeError, ValueError):
            pass

    def _fanout(self, msg: str) -> None:
        for ws in list(self._clients):
            if ws.closed:
                self._clients.discard(ws)
                continue
            asyncio.ensure_future(self._safe_send(ws, msg))

    async def _safe_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except Exception:
            self._clients.discard(ws)

    async def _push_metrics_loop(self) -> None:
        from app.hud.status import os_name

        while True:
            await asyncio.sleep(2.0)
            if self._clients:
                metrics = self._system_monitor.get_latest()
                if metrics:
                    metrics["os"] = os_name()
                    self.broadcast({"type": "system_metrics", "data": metrics})

    def system_metrics(self) -> dict:
        return self._system_monitor.get_latest()
