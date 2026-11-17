"""Opt-in mobile browser companion, isolated from the privileged localhost HUD."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from pathlib import Path

from aiohttp import web

from app.hud.events import hud
from app.hud.instance import era_home


class Companion:
    def __init__(self, server, storage: Path | None = None) -> None:
        self.server = server
        self.storage = storage or era_home() / "companion_devices.json"
        self.runner: web.AppRunner | None = None
        self.host = ""
        self.port = 0
        self.offer = ""
        self.expires = 0.0
        self.devices: dict = {}
        self.attempts: list[float] = []
        self.messages: dict[str, list[float]] = {}
        if self.storage.exists():
            raw = json.loads(self.storage.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Invalid companion device store")
            self.devices = raw

    def _save(self) -> None:
        self.storage.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.storage.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.devices, indent=2), encoding="utf-8")
        tmp.replace(self.storage)

    def snapshot(self) -> dict:
        return {
            "running": self.runner is not None,
            "url": f"http://{self.host}:{self.port}/" if self.runner else "",
            "devices": [
                {k: row[k] for k in ("id", "name", "created_at")} for row in self.devices.values()
            ],
            "capabilities": ["chat", "conversation", "shared text"],
            "limitations": "Browser companion over a trusted local network. No iPhone cellular call interception, SMS reading, background location or remote approval buttons. HTTP traffic is not encrypted; use only a network you trust.",
        }

    async def start(self, host: str, port: int) -> dict:
        address = ipaddress.ip_address(host)
        if (
            address.version != 4
            or not address.is_private
            or address.is_unspecified
            or address.is_multicast
        ):
            raise ValueError("Bind to a specific private IPv4 address on this PC")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Use a port between 1024 and 65535")
        if self.runner:
            raise ValueError("Stop the companion before changing its address")
        self.host, self.port = host, port
        app = web.Application(middlewares=[self.guard], client_max_size=32768)
        app.router.add_get("/", self.page)
        app.router.add_post("/pair", self.pair)
        app.router.add_post("/message", self.message)
        app.router.add_get("/conversation", self.conversation)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        return self.snapshot()

    async def stop(self) -> dict:
        if self.runner:
            await self.runner.cleanup()
        self.runner = None
        self.offer = ""
        return self.snapshot()

    def pairing_offer(self) -> dict:
        if not self.runner:
            raise ValueError("Start the companion first")
        self.offer = secrets.token_urlsafe(24)
        self.expires = time.monotonic() + 120
        return {"url": f"http://{self.host}:{self.port}/#pair={self.offer}", "expires_in": 120}

    def revoke(self, device_id: str) -> dict:
        if device_id not in self.devices:
            raise ValueError("Unknown device")
        del self.devices[device_id]
        self._save()
        return self.snapshot()

    @web.middleware
    async def guard(self, request: web.Request, handler):
        expected = f"{self.host}:{self.port}"
        if (
            request.host != expected
            or request.headers.get("Origin", f"http://{expected}") != f"http://{expected}"
        ):
            raise web.HTTPForbidden()
        if request.path not in {"/", "/pair"}:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            digest = hashlib.sha256(token.encode()).hexdigest()
            device = next(
                (d for d in self.devices.values() if hmac.compare_digest(d["digest"], digest)), None
            )
            if not token or device is None:
                raise web.HTTPUnauthorized()
            request["device"] = device["id"]
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @staticmethod
    async def body(request: web.Request) -> dict:
        try:
            value = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="JSON required") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="Object required")
        return value

    async def pair(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        self.attempts = [t for t in self.attempts if now - t < 60]
        if len(self.attempts) >= 8:
            raise web.HTTPTooManyRequests()
        self.attempts.append(now)
        body = await self.body(request)
        supplied = body.get("code", "")
        if (
            not isinstance(supplied, str)
            or not self.offer
            or now > self.expires
            or not hmac.compare_digest(supplied, self.offer)
        ):
            raise web.HTTPUnauthorized()
        name = body.get("name", "Phone")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise web.HTTPBadRequest(text="Device name required")
        if len(self.devices) >= 20:
            raise web.HTTPConflict(text="Revoke an old device before pairing")
        self.offer = ""  # one use, including concurrent submissions
        token, device_id = secrets.token_urlsafe(32), secrets.token_hex(8)
        self.devices[device_id] = {
            "id": device_id,
            "name": name.strip(),
            "created_at": time.time(),
            "digest": hashlib.sha256(token.encode()).hexdigest(),
        }
        self._save()
        return web.json_response({"id": device_id, "token": token})

    async def message(self, request: web.Request) -> web.Response:
        body = await self.body(request)
        text = body.get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 8000
            or text.strip().startswith("__")
        ):
            raise web.HTTPBadRequest(text="Send 1–8000 characters of text")
        now = time.monotonic()
        device_id = request["device"]
        recent = [t for t in self.messages.get(device_id, []) if now - t < 10]
        if len(recent) >= 6:
            raise web.HTTPTooManyRequests()
        self.messages[device_id] = [*recent, now]
        if self.server.engine() is None or not self.server._on_command:
            raise web.HTTPServiceUnavailable(text="Era is not ready")
        # Same owner input boundary; the phone never invokes tools directly.
        await asyncio.to_thread(self.server._on_command, text.strip())
        return web.json_response({"accepted": True})

    async def conversation(self, _request: web.Request) -> web.Response:
        rows = hud.conversation(60)
        return web.json_response(
            {
                "entries": [
                    {k: row.get(k) for k in ("role", "text", "ts")}
                    for row in rows
                    if row.get("role") in {"user", "assistant", "era", "agent"}
                ]
            }
        )

    async def page(self, _request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="apple-mobile-web-app-capable" content="yes"><title>Era Connect</title>
<style>body{background:#06121b;color:#e1f5ff;font:16px system-ui;max-width:760px;margin:auto;padding:24px}h1{color:#67e8f9}input,textarea,button{box-sizing:border-box;font:inherit;border:1px solid #246079;border-radius:12px;padding:12px;background:#0b2433;color:inherit}input,textarea{width:100%;margin:8px 0}button{cursor:pointer}article{white-space:pre-wrap;border-bottom:1px solid #173442;padding:14px 0}small{color:#99b8ca}#status{min-height:24px;color:#67e8f9}textarea{min-height:100px}nav{display:flex;gap:12px}</style>
<h1>ERA · CONNECT</h1><small>Your desktop assistant, from your phone. On iPhone, use Safari → Share → Add to Home Screen.</small>
<section id="pairing"><h2>Pair this device</h2><input id="name" placeholder="My iPhone" maxlength="60"><input id="code" placeholder="Pairing token from desktop"><button id="pair">Pair device</button></section>
<section id="chat" hidden><div id="messages" aria-live="polite"></div><form id="form"><textarea id="text" maxlength="8000" placeholder="Ask Era or paste text to share" aria-label="Message"></textarea><nav><button>Send to Era</button><button type="button" id="forget">Forget this browser</button></nav></form></section><p id="status" role="status"></p>
<script>
let token=localStorage.getItem('era-companion')||'';const by=id=>document.getElementById(id);const pairCode=new URLSearchParams(location.hash.slice(1)).get('pair');if(pairCode){by('code').value=pairCode;history.replaceState(null,'',location.pathname)}
function view(){by('pairing').hidden=!!token;by('chat').hidden=!token}
async function call(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:body?JSON.stringify(body):undefined});if(!r.ok){if(r.status===401){token='';localStorage.removeItem('era-companion');view()}throw Error(r.status===401?'Pairing expired or device revoked.':'Request failed ('+r.status+').')}return r.json()}
by('pair').onclick=async()=>{try{const r=await call('/pair',{code:by('code').value,name:by('name').value||'My phone'});token=r.token;localStorage.setItem('era-companion',token);by('code').value='';view();by('status').textContent='Paired. Desktop approvals remain on your PC.';refresh()}catch(e){by('status').textContent=e.message}};
by('form').onsubmit=async e=>{e.preventDefault();const b=by('form').querySelector('button');b.disabled=true;try{await call('/message',{text:by('text').value});by('text').value='';by('status').textContent='Sent. Waiting for Era.'}catch(e){by('status').textContent=e.message}finally{b.disabled=false}};
by('forget').onclick=()=>{localStorage.removeItem('era-companion');token='';view();by('status').textContent='Forgotten here. Revoke this device in desktop Settings to invalidate its credential.'};
async function refresh(){if(!token||document.hidden)return;try{const r=await call('/conversation');by('messages').replaceChildren(...r.entries.map(x=>{const a=document.createElement('article');a.textContent=(x.role||'Era')+': '+(x.text||'');return a}))}catch(e){by('status').textContent=e.message}}
view();refresh();setInterval(refresh,4000);
</script></html>"""
