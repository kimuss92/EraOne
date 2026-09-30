"""
omniroute.py — keep Era's model gateway (OmniRoute) running.

The user environment routes every Claude Code run — Era's headless CM and Meta
Ads dispatches included — through OmniRoute (ANTHROPIC_BASE_URL, default
http://127.0.0.1:20128). Nothing started it with Era: a dispatch sent while the
gateway was down hung or failed with "connection refused". So Era now starts it
herself — the supervisor before every launch, and era_dispatch before every
headless run (cheap: one port probe when it is already up).

Launch method mirrors D:\\Z.O.R.O\\run-voice-line.ps1 (verified 2026-09-24):
  omniroute.cmd serve --daemon --no-open
with OMNIROUTE_SERVER_HOST=127.0.0.1 (its default binds 0.0.0.0 with no key)
and PORT/OMNIROUTE_PORT pinned — OmniRoute obeys an inherited PORT and once came
up on a launcher's 8777 instead of 20128.

Degrade, never raise: a gateway that cannot be started costs a clear log line,
never Era's launch or a job.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PORT = 20128
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "omniroute-start.log"


def gateway_port(env=None) -> int | None:
    """The local port Claude Code is pointed at, or None when ANTHROPIC_BASE_URL
    is unset or remote — then there is no local gateway for Era to manage."""
    env = os.environ if env is None else env
    url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
        return None
    return parsed.port or DEFAULT_PORT


def is_up(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _omniroute_cmd() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    cmd = Path(appdata) / "npm" / "omniroute.cmd"
    return cmd if cmd.exists() else None


def ensure_running(wait_s: float = 90, env=None) -> tuple[bool, str]:
    """Start OmniRoute if Era's Claude runs need it and it is down.

    Returns (ok, detail). ok is True when no local gateway is configured, when it
    was already up, or when it came up within `wait_s`.
    """
    try:
        port = gateway_port(env)
        if port is None:
            return True, "no local gateway configured"
        if is_up(port):
            return True, f"already up on :{port}"
        cmd = _omniroute_cmd()
        if cmd is None:
            return False, "OmniRoute is not installed (%APPDATA%\\npm\\omniroute.cmd missing)"
        child_env = {
            **os.environ,
            "OMNIROUTE_SERVER_HOST": "127.0.0.1",
            "PORT": str(port),
            "OMNIROUTE_PORT": str(port),
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "w", encoding="utf-8", errors="replace") as out:
            # --daemon: returns once the server is spawned; OmniRoute supervises
            # and restarts it itself. CREATE_NO_WINDOW keeps a console from
            # flashing up when the supervisor runs hidden at logon.
            subprocess.run(
                ["cmd.exe", "/c", str(cmd), "serve", "--daemon", "--no-open"],
                env=child_env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=wait_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if is_up(port):
                return True, f"started on :{port}"
            time.sleep(1)
        return False, f"started but :{port} never answered — see {_LOG_PATH}"
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        return False, f"could not start OmniRoute: {exc}"


if __name__ == "__main__":
    print(ensure_running())
