"""WebSocket bridge server (extracted from main.py).

Async server that broadcasts Era's state and system metrics to the React
frontend. Self-contained; re-imported by main.py.
"""

import asyncio
import json
import threading
from typing import Any

import websockets

from core.system_monitor import SystemMonitor
from core.vad import get_vad


class WebSocketServer:
    """Async WebSocket server that broadcasts era state to the React frontend."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._clients: set[websockets.WebSocketServerProtocol] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_command: Any = None
        self._on_window_control: Any = None
        self._on_mic_toggle: Any = None
        # Optional callable returning the current backend browser-mic state (bool),
        # sent to each client on connect so its indicator starts from truth rather
        # than a local default — otherwise a fresh tab can show "Off" while the
        # backend mic is live.
        self._mic_state_provider: Any = None
        self._on_file_upload: Any = None
        # Called with the alias the user clicked in the model switcher ("" =
        # automatic). Separate from _on_command on purpose: a click is already
        # an unambiguous alias, and pushing it back through the typed path would
        # make it depend on a regex that only understands spoken phrasings.
        self._on_model_select: Any = None
        # Called (no args) when a client connects, so the backend can re-publish
        # state a fresh window has no way to ask for — the active model and the
        # switcher's options. Without it a reopened UI shows "resolving…" until
        # something happens to route a request.
        self._on_client_connect: Any = None
        self._system_monitor = SystemMonitor(interval=2.0)
        self._vad = get_vad()
        self._metrics_task: asyncio.Task | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._system_monitor.start()
        self._vad.start()
        self._metrics_task = asyncio.create_task(self._push_metrics_loop())
        # Default max_size (1 MiB) is too small for base64-encoded file
        # uploads (images/PDFs) — raise it so file_upload messages aren't
        # silently dropped by the connection closing.
        async with websockets.serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=30,
            max_size=32 * 1024 * 1024,
        ):
            await asyncio.Future()

    async def _push_metrics_loop(self) -> None:
        """Periodically push system metrics to all connected clients."""
        while True:
            await asyncio.sleep(2.0)
            if self._clients:
                metrics = self._system_monitor.get_latest()
                if metrics:
                    self.broadcast({"type": "system_metrics", "data": metrics})

    async def _handler(self, websocket: websockets.WebSocketServerProtocol) -> None:
        self._clients.add(websocket)
        # Sync this client's mic indicator to the authoritative backend state on
        # connect, so it can never open showing "Off" while the mic is actually live.
        if self._mic_state_provider is not None:
            try:
                await websocket.send(
                    json.dumps({"type": "browser_mic", "active": bool(self._mic_state_provider())})
                )
            except Exception:
                pass
        # Re-publish model state for this window. Threaded because it reaches
        # into the registry and the broadcaster, and the connect handshake must
        # not wait on either.
        if self._on_client_connect is not None:
            try:
                threading.Thread(target=self._on_client_connect, daemon=True).start()
            except Exception:
                pass
        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")
                    if msg_type == "command" and self._on_command:
                        threading.Thread(
                            target=self._on_command,
                            args=(data.get("text", ""),),
                            daemon=True,
                        ).start()
                    elif msg_type == "mute" and self._on_command:
                        cmd = "__mute__" if data.get("muted") else "__unmute__"
                        threading.Thread(
                            target=self._on_command,
                            args=(cmd,),
                            daemon=True,
                        ).start()
                    elif msg_type == "window_control" and self._on_window_control:
                        action = data.get("action", "")
                        threading.Thread(
                            target=self._on_window_control,
                            args=(action,),
                            daemon=True,
                        ).start()
                    elif msg_type == "mic_toggle" and self._on_mic_toggle:
                        # Pass an explicit desired state through when the UI sends
                        # one ("active"); otherwise fall back to a flip (back-compat).
                        _mic_active = data.get("active", None)
                        threading.Thread(
                            target=self._on_mic_toggle,
                            args=(_mic_active,),
                            daemon=True,
                        ).start()
                    elif msg_type == "file_upload" and self._on_file_upload:
                        threading.Thread(
                            target=self._on_file_upload,
                            args=(
                                data.get("name", ""),
                                data.get("mime", ""),
                                data.get("data", ""),
                            ),
                            daemon=True,
                        ).start()
                    elif msg_type == "model_select" and self._on_model_select:
                        threading.Thread(
                            target=self._on_model_select,
                            args=(str(data.get("alias", "") or ""),),
                            daemon=True,
                        ).start()
                    elif msg_type == "mic_volume":
                        # Frontend sends mic volume for VAD processing
                        volume = data.get("volume", 0.0)
                        self._vad.update_mic_volume(volume)
                        # Send VAD state back to all clients
                        is_active = self._vad.is_speaking()
                        self.broadcast({"type": "user_voice_active", "active": is_active})
                except json.JSONDecodeError:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    def broadcast(self, data: dict) -> None:
        if not self._loop or not self._clients:
            return
        msg = json.dumps(data)
        for client in list(self._clients):
            try:
                asyncio.run_coroutine_threadsafe(client.send(msg), self._loop)
            except Exception:
                self._clients.discard(client)
