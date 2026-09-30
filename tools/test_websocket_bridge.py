"""
Integration test for the WebSocket bridge between Python backend and React frontend.

Tests WebSocketServer standalone (no GUI required): start, connect, broadcast,
command receive, mute toggle, and state/log broadcast round-trips.
"""

import asyncio
import json
import os
import sys
import threading
import time

__test__ = False

HOST = "127.0.0.1"
PORT = 18765  # Different from production 8765 to avoid collisions


def _import_ws_server() -> type:
    """WebSocketServer lives in app/websocket_server.py (main.py only re-exports
    it), so it imports directly — no GUI, audio or engine stubs needed."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, project_root)
    from app.websocket_server import WebSocketServer

    return WebSocketServer


def test_websocket_bridge():
    print("=" * 60)
    print("WebSocket Bridge Integration Test")
    print("=" * 60)

    # Import WebSocketServer in isolation
    WebSocketServer = _import_ws_server()
    print("[OK] WebSocketServer imported without GUI")

    # Track callbacks from server
    received_commands = []

    server = WebSocketServer(host=HOST, port=PORT)
    server._on_command = lambda text: received_commands.append(text)
    server.start()
    assert server.ready.wait(10) and server.bound_port, server.bind_error
    print(f"[OK] WebSocketServer started on {HOST}:{PORT}")

    # Test client
    async def client_test():
        import websockets

        async def recv(ws, mtype):
            """Next message of this type; greetings/metrics in between are skipped."""
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if m.get("type") == mtype:
                    return m

        # The server refuses sockets without the per-process session token.
        uri = f"ws://{HOST}:{PORT}/ws?token={server.session_token}"
        async with websockets.connect(uri) as ws:
            # 1. Receive broadcast
            server.broadcast({"type": "state", "state": "LISTENING", "ts": time.time()})
            data1 = await recv(ws, "state")
            assert data1["type"] == "state", f"Expected state, got {data1}"
            assert data1["state"] == "LISTENING"
            print("[PASS] broadcast state -> client")

            # 2. Broadcast log message
            server.broadcast({"type": "log", "text": "hello world", "ts": time.time()})
            data2 = await recv(ws, "log")
            assert data2["type"] == "log"
            assert data2["text"] == "hello world"
            print("[PASS] broadcast log -> client")

            # 3. Broadcast mute
            server.broadcast({"type": "mute", "muted": True, "ts": time.time()})
            data3 = await recv(ws, "mute")
            assert data3["type"] == "mute"
            assert data3["muted"] is True
            print("[PASS] broadcast mute -> client")

            # 4. Send command from client to server
            await ws.send(json.dumps({"type": "command", "text": "hello era one"}))
            await asyncio.sleep(0.3)
            assert "hello era one" in received_commands, f"Missing command in {received_commands}"
            print("[PASS] client command -> server callback")

            # 5. Send mute command
            await ws.send(json.dumps({"type": "mute", "muted": True}))
            await asyncio.sleep(0.3)
            assert "__mute__" in received_commands, f"Missing __mute__ in {received_commands}"
            print("[PASS] client mute -> server callback")

            # 6. Send unmute command
            await ws.send(json.dumps({"type": "mute", "muted": False}))
            await asyncio.sleep(0.3)
            assert "__unmute__" in received_commands, f"Missing __unmute__ in {received_commands}"
            print("[PASS] client unmute -> server callback")

            # 7. Multiple clients receive broadcast
            async with websockets.connect(uri) as ws2:
                server.broadcast({"type": "state", "state": "STANDBY", "ts": time.time()})
                data4 = await recv(ws2, "state")
                assert data4["state"] == "STANDBY"
                # First client also gets it
                data5 = await recv(ws, "state")
                assert data5["state"] == "STANDBY"
                print("[PASS] broadcast reaches all connected clients")

            # 8. Disconnected client doesn't cause errors
            server.broadcast({"type": "state", "state": "OFFLINE", "ts": time.time()})
            data6 = await recv(ws, "state")
            assert data6["state"] == "OFFLINE"
            print("[PASS] broadcast still works after client disconnect")

        return True

    result = asyncio.run(client_test())
    assert result

    print("=" * 60)
    print("ALL 8 TESTS PASSED")
    print("=" * 60)
    return True


def test_state_mapping():
    """Verify the states used in main.py match what useEra expects."""
    states_from_python = {"LISTENING", "SPEAKING", "THINKING", "STANDBY"}

    import os
    import re

    main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    with open(main_path, encoding="utf-8") as f:
        content = f.read()
    found = set(re.findall(r'set_state\("(\w+)"\)', content))
    missing_react = found - states_from_python
    extra_react = states_from_python - found
    if missing_react:
        print(f"[WARN] States sent by Python but unknown to useEra: {missing_react}")
    if extra_react:
        print(f"[WARN] States expected by useEra but never sent from Python: {extra_react}")
    assert not missing_react, f"Unknown states: {missing_react}"
    print(f"[OK] All Python states ({sorted(found)}) are handled by useEra")
    return True


if __name__ == "__main__":
    ok1 = test_websocket_bridge()
    ok2 = test_state_mapping()
    sys.exit(0 if (ok1 and ok2) else 1)
