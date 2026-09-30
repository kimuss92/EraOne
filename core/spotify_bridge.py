# core/spotify_bridge.py
"""Async HTTP client for the Spicetify `era-bridge` extension.

The extension (%APPDATA%\\spicetify\\Extensions\\era-bridge\\extension.js) runs
an HTTP server inside the Spotify renderer on 127.0.0.1:8766 and drives Spotify
through Spicetify's own logged-in session — so this whole lane needs NO Spotify
API key and NO OAuth flow.

This module is the Era side: one dispatch entry point (`handle_action`) that the
live tool dispatcher calls, plus a `sync()` wrapper for non-async callers.

Two transports, transparent to every public function (they all go through
`_request`):
  * SERVER mode — the extension hosts an HTTP server on :8766 and Era is the
    client (httpx). Used when the Spotify renderer has Node `require`.
  * POLL mode   — the renderer has no `require`, so it cannot host a server. Era
    then hosts a small relay on :8767 (aiohttp) and the extension polls it with
    fetch, running each command via CosmosAsync. Started lazily, only when the
    direct server is unreachable but the extension reaches out.

Cold start: if neither transport is live, Spotify is launched through the repo's
own launcher (actions/spotify_control._launch_spotify_desktop) and both paths are
polled until one answers or the timeout elapses.

Ports 8766 (server) / 8767 (relay). The HUD owns 8765 and this file never uses it.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import httpx

from core.utils import load_config

# --- config: env-free, read from config/api_keys.json "spotify_bridge" block ---
# Defaults stand alone, so the lane works whether or not that block exists (the
# api_keys.json file is write-protected by a guard hook, so the block is optional).
#   enabled    — MASTER SWITCH. Default false: the bridge is DORMANT and every
#                Spotify intent goes to the window-automation path
#                (actions/spotify_control.py). Set true in config/api_keys.json's
#                "spotify_bridge" block only once a Spicetify-compatible Spotify
#                build actually loads the era-bridge extension.
#   port       — the extension's own HTTP server (SERVER mode); Era is the client.
#   relay_port — Era's relay for POLL mode, used only when the Spotify renderer has
#                no Node `require` and the extension cannot host a server. There the
#                extension reaches OUT with fetch, so Era hosts and the extension
#                polls. 8767, distinct from 8766 so the two modes never fight over a
#                port. The HUD's 8765 is never used here.
_DEFAULTS = {
    "enabled": False,
    "host": "127.0.0.1",
    "port": 8766,
    "relay_port": 8767,
    "launch_timeout_s": 20,
}


def is_enabled() -> bool:
    """Master switch for the keyless bridge. False (default) means dormant: the
    live dispatch path must use window-automation, never this module's network I/O."""
    return bool(_cfg().get("enabled"))


def _cfg() -> dict:
    block = {}
    try:
        block = load_config().get("spotify_bridge") or {}
    except Exception:
        block = {}
    return {**_DEFAULTS, **{k: v for k, v in block.items() if v is not None}}


def _base_url() -> str:
    c = _cfg()
    return f"http://{c['host']}:{c['port']}"


_TOKEN_PATH = Path(os.path.expandvars(r"%APPDATA%\spicetify\era-bridge.token"))


class SpotifyBridgeDown(Exception):
    """The era-bridge server did not answer (Spotify not running / extension off)."""


def _read_token() -> str:
    try:
        return _TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _headers(token: str) -> dict:
    # Origin matches the extension's allow-list; the two Era headers gate the port.
    return {
        "X-Era-Bridge": "1",
        "X-Era-Token": token,
        "Origin": "http://127.0.0.1:8766",
        "Content-Type": "application/json",
    }


class BridgeError(Exception):
    """The bridge answered with a 4xx/5xx (e.g. 501 unsupported, 409 nothing playing)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# Which transport reached the extension last. Set the first time one works so we
# stop paying for the dead path on every later call. Reset to None on hard failure.
_MODE: str | None = None


async def _request_direct(method: str, path: str, *, params=None, json=None) -> dict:
    """SERVER mode: HTTP straight to the extension on :port. Retries once on
    connection refusal; re-reads the token once on 401 (it rotates when Spotify
    restarts). Raises SpotifyBridgeDown if unreachable. Never logs the token."""
    token = _read_token()
    url = _base_url() + path
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.request(
                    method, url, params=params, json=json, headers=_headers(token)
                )
            if resp.status_code == 401 and attempt == 0:
                token = _read_token()  # rotated on Spotify restart — re-read once
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code >= 400:
                raise BridgeError(resp.status_code, data.get("error") or f"HTTP {resp.status_code}")
            return data if isinstance(data, dict) else {"result": data}
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
    raise SpotifyBridgeDown(str(last_exc) if last_exc else "bridge unreachable")


async def _request(method: str, path: str, *, params=None, json=None) -> dict:
    """Mode-aware call: SERVER mode (direct HTTP) when the extension hosts a
    server, else POLL mode (Era's relay) when it can only reach out. The public
    API funcs below all go through here, so they work identically in both modes."""
    global _MODE
    if _MODE != "relay":
        try:
            return await _request_direct(method, path, params=params, json=json)
        except SpotifyBridgeDown:
            if not _ext_online():
                raise  # nothing hosting AND nothing polling in — genuinely down
            _MODE = "relay"
    return await _request_relay(method, path, params=params, json=json)


# --- POLL-mode relay (Era hosts; a require-less extension polls it) ------------
# Only ever started when the direct server is unreachable but the extension can
# reach out. aiohttp is already a project dependency (the HUD uses it).
_relay_runner = None  # aiohttp AppRunner, started once
_relay_lock = asyncio.Lock()
_cmd_queue: asyncio.Queue | None = None
_pending: dict[str, asyncio.Future] = {}
_ext_last_poll: float = 0.0  # monotonic time of the extension's last contact


def _ext_online() -> bool:
    """True if the poll-mode extension has contacted the relay recently."""
    return _ext_last_poll > 0 and (time.monotonic() - _ext_last_poll) < 12.0


async def _ensure_relay() -> None:
    """Start the relay HTTP server on relay_port, once. Idempotent; never raises
    out (a relay we cannot start just means POLL mode stays unavailable)."""
    global _relay_runner, _cmd_queue
    async with _relay_lock:
        if _relay_runner is not None:
            return
        try:
            from aiohttp import web
        except Exception:
            return
        _cmd_queue = asyncio.Queue()
        c = _cfg()

        def _guard(request) -> bool:
            return request.headers.get("X-Era-Bridge") == "1"

        async def _hello(request):
            global _ext_last_poll
            if not _guard(request):
                return web.json_response({"error": "missing bridge header"}, status=403)
            _ext_last_poll = time.monotonic()
            return web.json_response({"ok": True})

        async def _poll(request):
            global _ext_last_poll
            if not _guard(request):
                return web.json_response({"error": "missing bridge header"}, status=403)
            _ext_last_poll = time.monotonic()
            try:
                cmd = await asyncio.wait_for(_cmd_queue.get(), timeout=25.0)
            except TimeoutError:
                return web.Response(status=204)  # long-poll idle; extension re-polls
            return web.json_response(cmd)

        async def _result(request):
            if not _guard(request):
                return web.json_response({"error": "missing bridge header"}, status=403)
            try:
                data = await request.json()
            except Exception:
                data = {}
            fut = _pending.get(str(data.get("id")))
            if fut is not None and not fut.done():
                fut.set_result({"status": data.get("status", 200), "body": data.get("body") or {}})
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/__relay/hello", _hello)
        app.router.add_get("/__relay/poll", _poll)
        app.router.add_post("/__relay/result", _result)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, c["host"], int(c["relay_port"]))
        await site.start()
        _relay_runner = runner


async def _request_relay(method: str, path: str, *, params=None, json=None) -> dict:
    """POLL mode: enqueue the command for the polling extension and await its
    result. Raises SpotifyBridgeDown if the extension never answers in time."""
    await _ensure_relay()
    if _cmd_queue is None:
        raise SpotifyBridgeDown("relay unavailable")
    merged = dict(params or {})
    merged.update(json or {})
    cmd_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _pending[cmd_id] = fut
    await _cmd_queue.put({"id": cmd_id, "method": method, "path": path, "params": merged})
    try:
        res = await asyncio.wait_for(fut, timeout=10.0)
    except TimeoutError as exc:
        raise SpotifyBridgeDown("extension did not answer the relay") from exc
    finally:
        _pending.pop(cmd_id, None)
    status = int(res.get("status", 200))
    body = res.get("body") or {}
    if status >= 400:
        raise BridgeError(status, (body or {}).get("error") or f"HTTP {status}")
    return body if isinstance(body, dict) else {"result": body}


# --- public async API ---------------------------------------------------------
async def is_up() -> bool:
    """Up if the extension either hosts a reachable server (SERVER mode) or has
    recently polled Era's relay (POLL mode)."""
    try:
        data = await _request_direct("GET", "/health")
        if data.get("ok"):
            return True
    except (SpotifyBridgeDown, BridgeError):
        pass
    return _ext_online()


async def state() -> dict:
    return await _request("GET", "/state")


async def play() -> dict:
    return await _request("POST", "/transport/play")


async def pause() -> dict:
    return await _request("POST", "/transport/pause")


async def toggle() -> dict:
    return await _request("POST", "/transport/toggle")


async def next() -> dict:
    return await _request("POST", "/transport/next")


async def prev() -> dict:
    return await _request("POST", "/transport/prev")


async def seek(ms: int) -> dict:
    return await _request("POST", "/transport/seek", json={"position_ms": int(ms)})


async def get_volume() -> int:
    data = await _request("GET", "/volume")
    return int(data.get("percent") or 0)


async def set_volume(percent: int) -> dict:
    return await _request("POST", "/volume", json={"percent": int(percent)})


async def shuffle(on: bool) -> dict:
    return await _request("POST", "/shuffle", json={"state": bool(on)})


async def repeat(mode: str) -> dict:
    return await _request("POST", "/repeat", json={"mode": str(mode)})


async def like_current() -> dict:
    return await _request("POST", "/like")


async def unlike_current() -> dict:
    return await _request("POST", "/unlike")


async def like_uri(uri: str) -> dict:
    return await _request("POST", "/like-uri", json={"uri": uri})


async def unlike_uri(uri: str) -> dict:
    return await _request("POST", "/unlike-uri", json={"uri": uri})


async def liked(limit: int = 50, offset: int = 0) -> dict:
    return await _request("GET", "/liked", params={"limit": limit, "offset": offset})


async def new_releases(limit: int = 20, country: str = "US") -> dict:
    return await _request("GET", "/new-releases", params={"limit": limit, "country": country})


async def search(q: str, type: str = "track", limit: int = 20) -> dict:
    return await _request("GET", "/search", params={"q": q, "type": type, "limit": limit})


async def queue(uri: str) -> dict:
    return await _request("POST", "/queue", json={"uri": uri})


async def play_uri(uri: str) -> dict:
    return await _request("POST", "/play-uri", json={"uri": uri})


async def play_context(context_uri: str, offset_uri: str | None = None) -> dict:
    body = {"context_uri": context_uri}
    if offset_uri:
        body["offset_uri"] = offset_uri
    return await _request("POST", "/play-context", json=body)


async def play_first_match(q: str) -> dict:
    """Search + play in one. Returns {ok, uri, name, artists} or raises BridgeError."""
    found = await search(q, type="track", limit=1)
    items = ((found.get("tracks") or {}).get("items")) or []
    if not items:
        return {"ok": False, "error": f"no track found for {q!r}"}
    track = items[0]
    await play_uri(track["uri"])
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    return {"ok": True, "uri": track["uri"], "name": track.get("name"), "artists": artists}


# --- cold start ----------------------------------------------------------------
async def ensure_up(launch_timeout_s: int | None = None) -> bool:
    """True if the bridge answers /health, launching Spotify first if needed.

    Reuses the repo's own Spotify launcher rather than spawning a raw process;
    the supervisor only supervises main.py, so it is not the launch path here.
    """
    if await is_up():
        return True

    # Start the relay first, so a require-less extension has somewhere to poll the
    # instant Spotify finishes loading (POLL mode). Harmless if SERVER mode wins.
    await _ensure_relay()

    try:
        from actions.spotify_control import _launch_spotify_desktop

        await asyncio.get_running_loop().run_in_executor(None, _launch_spotify_desktop)
    except Exception:
        pass  # launch failure just means the poll below will time out cleanly

    deadline = asyncio.get_running_loop().time() + float(
        launch_timeout_s if launch_timeout_s is not None else _cfg()["launch_timeout_s"]
    )
    while asyncio.get_running_loop().time() < deadline:
        if await is_up():
            return True
        await asyncio.sleep(0.5)
    return False


def sync(coro):
    """Run a bridge coroutine from a synchronous caller.

    Uses a fresh loop; raises RuntimeError if a loop is already running in this
    thread (call the coroutine directly there instead)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "spotify_bridge.sync() called from a running event loop; await the coroutine"
    )


# --- one dispatch entry point for the live tool router -------------------------
# Returns a short spoken-style string. Never raises: a down bridge or an
# unsupported action becomes a clean sentence, matching how spotify_control reports.
_NUM_KEYS = ("percent", "seconds", "limit", "offset", "step")


def _spoken_now_playing(st: dict) -> str:
    name = st.get("name")
    artists = ", ".join(st.get("artists") or [])
    if not name:
        if st.get("is_playing"):
            return "Spotify is playing, sir."
        return "Spotify is running but nothing is playing, sir."
    who = f" by {artists}" if artists else ""
    verb = "Now playing" if st.get("is_playing") else "Paused on"
    return f"{verb} '{name}'{who}, sir."


async def handle_action(args: dict) -> str:
    action = str((args or {}).get("action", "")).lower().strip()
    if not action:
        return "No Spotify action was given, sir."

    if not await ensure_up():
        return "Spotify is not running and did not answer in time, sir."

    try:
        if action in ("play", "start"):
            query = args.get("query") or args.get("value") or args.get("q")
            if args.get("uri"):
                await play_uri(args["uri"])
                return "Playing that on Spotify, sir."
            if args.get("context_uri"):
                await play_context(args["context_uri"], args.get("offset_uri"))
                return "Playing that on Spotify, sir."
            if query:
                res = await play_first_match(str(query))
                if not res.get("ok"):
                    return f"I couldn't find {query} on Spotify, sir."
                who = f" by {res['artists']}" if res.get("artists") else ""
                return f"Playing '{res.get('name')}'{who}, sir."
            await play()
            return "Spotify is playing, sir."

        if action == "pause":
            await pause()
            return "Spotify paused, sir."
        if action in ("toggle", "play_pause"):
            await toggle()
            return "Toggled Spotify playback, sir."
        if action in ("next", "skip"):
            await next()
            return "Skipping to the next track, sir."
        if action in ("prev", "previous", "back"):
            await prev()
            return "Going to the previous track, sir."
        if action == "seek":
            secs = float(args.get("seconds") or 0)
            await seek(int(secs * 1000))
            return f"Jumped to {int(secs)} seconds, sir."

        if action == "set_volume":
            pct = int(args.get("percent") or 0)
            await set_volume(pct)
            return f"Spotify volume set to {pct} percent, sir."
        if action == "get_volume":
            return f"Spotify volume is {await get_volume()} percent, sir."
        if action in ("volume_up", "volume_down"):
            step = int(args.get("step") or 10)
            cur = await get_volume()
            new = cur + step if action == "volume_up" else cur - step
            new = max(0, min(100, new))
            await set_volume(new)
            return (
                f"Spotify volume {'up' if action == 'volume_up' else 'down'} to {new} percent, sir."
            )
        if action == "mute":
            await set_volume(0)
            return "Spotify muted, sir."

        if action == "shuffle":
            on = bool(args.get("state", True))
            await shuffle(on)
            return f"Shuffle {'on' if on else 'off'}, sir."
        if action == "repeat":
            mode = str(args.get("mode") or "off")
            await repeat(mode)
            return f"Repeat set to {mode}, sir."

        if action == "like":
            await like_current()
            return "Added the current track to your Liked Songs, sir."
        if action == "unlike":
            await unlike_current()
            return "Removed the current track from your Liked Songs, sir."

        if action == "liked_list":
            data = await liked(int(args.get("limit") or 20), int(args.get("offset") or 0))
            total = data.get("total")
            n = len(data.get("items") or [])
            return f"You have {total} liked songs; I pulled {n} of them, sir."
        if action == "new_releases":
            data = await new_releases(
                int(args.get("limit") or 20), str(args.get("country") or "US")
            )
            n = len(data.get("albums") or [])
            return f"I found {n} new releases, sir."
        if action == "search":
            q = str(args.get("q") or args.get("query") or "").strip()
            if not q:
                return "What should I search for on Spotify, sir?"
            data = await search(q, str(args.get("type") or "track"), int(args.get("limit") or 20))
            items = ((data.get("tracks") or {}).get("items")) or []
            if not items:
                return f"No Spotify results for {q}, sir."
            top = items[0]
            who = ", ".join(a["name"] for a in top.get("artists", []))
            return f"Top result for {q}: '{top.get('name')}'{(' by ' + who) if who else ''}, sir."
        if action == "queue":
            q = str(args.get("query") or args.get("q") or "").strip()
            if args.get("uri"):
                await queue(args["uri"])
                return "Added that to the Spotify queue, sir."
            if not q:
                return "What should I queue on Spotify, sir?"
            found = await search(q, type="track", limit=1)
            items = ((found.get("tracks") or {}).get("items")) or []
            if not items:
                return f"I couldn't find {q} to queue, sir."
            await queue(items[0]["uri"])
            return f"Queued '{items[0].get('name')}', sir."

        if action in ("playing_now", "now_playing", "status", "current_track"):
            return _spoken_now_playing(await state())

        return f"I don't have a Spotify bridge action called '{action}', sir."

    except SpotifyBridgeDown:
        return "The Spotify bridge stopped responding, sir."
    except BridgeError as exc:
        if exc.status == 501:
            return "This Spicetify version doesn't support that Spotify action, sir."
        if exc.status == 409:
            return "Nothing is playing on Spotify right now, sir."
        return f"Spotify rejected that: {exc.message}, sir."
    except Exception as exc:  # noqa: BLE001 - a control action must degrade to a sentence
        return f"That Spotify action failed: {exc}, sir."
