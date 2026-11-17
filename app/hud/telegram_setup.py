"""Authenticated HUD gateway to the isolated Telegram setup environment."""

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web


class TelegramSetupApi:
    def __init__(self):
        self.lock = asyncio.Lock()

    def register(self, app):
        app.router.add_post("/api/telegram/setup", self.handle)

    async def handle(self, request):
        if request.content_length and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid setup request."}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "Invalid setup request."}, status=400)
        if self.lock.locked():
            return web.json_response({"error": "A setup step is still running."}, status=409)
        interpreter = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "EraOne/telegram-calls/.venv/Scripts/python.exe"
        )
        if not interpreter.is_file():
            return web.json_response(
                {"error": "Telegram calling components are not installed."}, status=503
            )
        async with self.lock:
            proc = await asyncio.create_subprocess_exec(
                str(interpreter),
                "-m",
                "core.telegram_setup_wizard",
                cwd=str(Path(__file__).resolve().parents[2]),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
            )
            try:
                output, _ = await asyncio.wait_for(proc.communicate(json.dumps(data).encode()), 90)
                result = json.loads(output)
                return web.json_response(result, status=400 if result.get("error") else 200)
            except asyncio.CancelledError:
                raise
            except Exception:
                return web.json_response(
                    {"error": "Setup timed out or stopped. Reopen setup to resume."}, status=503
                )
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
