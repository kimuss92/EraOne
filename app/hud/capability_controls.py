"""Capability configuration and companion lifecycle on the guarded HUD API."""

from __future__ import annotations

import asyncio
import base64
import io
import socket

from aiohttp import web


class EmpowerApi:
    def __init__(self, server):
        self.server = server
        self.companion = None

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/empower", self.get)
        app.router.add_post("/api/empower/settings", self.settings)
        app.router.add_post("/api/empower/test", self.test)
        app.router.add_get("/api/companion", self.companion_status)
        app.router.add_post("/api/companion", self.companion_action)
        app.router.add_post("/api/launcher", self.launcher)
        app.router.add_get("/api/launcher", self.launcher_status)
        app.on_cleanup.append(self.cleanup)

    async def cleanup(self, _app) -> None:
        if self.companion:
            await self.companion.stop()

    async def get(self, _request):
        from core.empower_config import snapshot

        return web.json_response(await asyncio.to_thread(snapshot))

    async def settings(self, request):
        from app.hud.api import _json_body
        from core.empower_config import update

        try:
            return web.json_response(await asyncio.to_thread(update, await _json_body(request)))
        except (ValueError, OSError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def test(self, request):
        from app.hud.api import _json_body
        from core.empower_config import test_connection

        try:
            body = await _json_body(request)
            return web.json_response(await asyncio.to_thread(test_connection, body.get("service")))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    def _companion(self):
        if self.companion is None:
            from app.hud.companion import Companion

            self.companion = Companion(self.server)
        return self.companion

    async def companion_status(self, _request):
        snapshot = self._companion().snapshot()
        snapshot["addresses"] = await asyncio.to_thread(local_addresses)
        return web.json_response(snapshot)

    async def companion_action(self, request):
        from app.hud.api import _json_body

        body = await _json_body(request)
        try:
            companion = self._companion()
            action = body.get("action")
            if action == "start":
                result = await companion.start(
                    body.get("host", "127.0.0.1"), body.get("port", 8766)
                )
            elif action == "stop":
                result = await companion.stop()
            elif action == "pair":
                import qrcode

                result = companion.pairing_offer()
                stream = io.BytesIO()
                qrcode.make(result["url"]).save(stream, format="PNG")
                result["qr"] = (
                    "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
                )
            elif action == "revoke":
                result = companion.revoke(body.get("id", ""))
            else:
                raise ValueError("Use start, stop, pair or revoke")
            return web.json_response(result)
        except (ValueError, OSError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def launcher_status(self, _request):
        from app.hud import settings

        current = await asyncio.to_thread(settings.load)
        return web.json_response(
            {
                key: current[key]
                for key in ("floating_icon", "desktop_tray", "open_hud_on_start", "floating_magnet")
            }
        )

    async def launcher(self, request):
        from app.hud import floating_launcher, settings
        from app.hud.api import _json_body

        body = await _json_body(request)
        action = body.get("action")
        if action in {"magnet_on", "magnet_off"}:
            current = await asyncio.to_thread(
                settings.update, {"floating_magnet": action == "magnet_on"}
            )
            return web.json_response(
                {"accepted": True, "floating_magnet": current["floating_magnet"]}
            )
        if action not in {"show", "hide"}:
            return web.json_response(
                {"error": "Use show, hide, magnet_on or magnet_off"}, status=400
            )
        await asyncio.to_thread(
            settings.update, {"floating_icon": action == "show", "desktop_tray": True}
        )
        await asyncio.to_thread(
            floating_launcher.ensure_started, self.server.bound_port, self.server.session_token
        )
        return web.json_response({"accepted": True})


def local_addresses() -> list[str]:
    import psutil

    addresses = {
        entry.address
        for entries in psutil.net_if_addrs().values()
        for entry in entries
        if entry.family == socket.AF_INET and not entry.address.startswith("169.254.")
    }
    return sorted(addresses - {"127.0.0.1"}) or ["127.0.0.1"]
