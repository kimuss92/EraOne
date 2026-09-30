"""ERA ONE launcher — the one thing the desktop shortcut runs.

    pythonw -m app.hud.launcher               normal: silent, opens/focuses the HUD
    python  -m app.hud.launcher --debug       same backend + HUD, live logs here
    pythonw -m app.hud.launcher --background  start silently (logon), no window

It holds no AI and no state. Its whole job:

  1. is a backend already up?  -> /api/health on the port the backend reported
  2. if not, start the supervisor WINDOWLESS (CREATE_NO_WINDOW: a real console
     with no window, which every child inherits — so nothing Era spawns pops a
     terminal either)
  3. wait for REAL readiness (health says "ready"), showing actual stages if it
     takes a moment — never a fixed sleep
  4. open the HUD as an app window, or focus the one already open
  5. if startup fails, say why, with Retry / Open Logs / Diagnostics

Double-clicking twice is harmless: the backend and supervisor each hold a named
mutex, so a second launch only ever finds the first one and focuses its HUD.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.hud import browser, instance, settings, shortcuts

REPO = Path(__file__).resolve().parents[2]
_NO_WINDOW = 0x08000000
_NEW_GROUP = 0x00000200
_NEW_CONSOLE = 0x00000010
READY_TIMEOUT_S = 180.0


class LaunchError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def log_dir() -> Path:
    return Path(os.environ.get("ERA_LOG_DIR") or (REPO / "logs"))


def log(msg: str) -> None:
    path = log_dir() / "launcher.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            tail = path.read_bytes()[-200_000:]
            path.write_bytes(tail)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")
    except OSError:
        pass
    if sys.stdout is not None and os.environ.get("ERA_LAUNCHER_ECHO") == "1":
        print(f"[launcher] {msg}", flush=True)


def configured_port() -> int:
    try:
        port = int(os.environ.get("ERAONE_PORT", "") or 8765)
    except ValueError:
        return 8765
    return port if 1024 <= port <= 65535 else 8765


def health(port: int, timeout: float = 0.6) -> dict | str | None:
    """The backend's health dict; "foreign" if something else answers; None if
    nothing listens."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return "foreign"
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == "eraone" else "foreign"


def locate_backend() -> tuple[int | None, dict | None, bool]:
    """(port, health, foreign_on_configured_port)."""
    ports: list[int] = []
    info = instance.read_backend_info()
    if info and instance.pid_alive(int(info.get("pid") or 0)):
        ports.append(int(info.get("port") or 0))
    ports.append(configured_port())
    foreign = False
    for p in dict.fromkeys(x for x in ports if x):
        h = health(p)
        if isinstance(h, dict):
            return p, h, False
        if h == "foreign" and p == configured_port():
            foreign = True
    return None, None, foreign


def supervisor_running() -> bool:
    return instance.mutex_exists(instance.supervisor_mutex_name())


def backend_running() -> bool:
    return instance.mutex_exists(instance.backend_mutex_name())


def clear_stop_flag() -> None:
    """An explicit launch means "wake": drop an intentional-shutdown flag."""
    flag = instance.era_home() / "era.stop"
    if flag.exists():
        try:
            flag.unlink()
            log(f"cleared stop flag {flag}")
        except OSError as e:
            log(f"could not clear stop flag: {e}")


def start_supervisor(*, background: bool) -> subprocess.Popen:
    clear_stop_flag()
    env = dict(os.environ)
    env.pop("ERA_SUPERVISOR_VISIBLE", None)
    if not background:
        env["ERA_NO_AUTO_HUD"] = "1"  # this launcher opens the HUD itself
    cmd = [str(shortcuts.python_path()), str(REPO / "era_supervisor.py")]
    log(f"starting supervisor windowless: {cmd}")
    return subprocess.Popen(
        cmd,
        cwd=str(REPO),
        env=env,
        creationflags=_NO_WINDOW | _NEW_GROUP,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _halt_reason(since: float) -> str | None:
    flag = instance.era_home() / "era_supervisor_halted.flag"
    try:
        if flag.exists() and flag.stat().st_mtime >= since - 1:
            return flag.read_text(encoding="utf-8", errors="replace").strip()[:600]
    except OSError:
        pass
    return None


def wait_ready(progress, *, started_at: float, proc: subprocess.Popen | None) -> tuple[int, dict]:
    deadline = started_at + READY_TIMEOUT_S
    foreign_since: float | None = None
    while True:
        port, h, foreign = locate_backend()
        sup, back = supervisor_running(), backend_running()
        progress(
            [
                ("SUPERVISOR", "online" if sup else ("starting" if proc else "waiting")),
                ("BACKEND", "online" if back else "waiting"),
                ("LOCAL SERVER", f"online :{port}" if h else "waiting"),
                ("CORE", (h or {}).get("agent", "waiting")),
                ("VOICE", (h or {}).get("voice", "waiting")),
                ("MODEL", (h or {}).get("model", "waiting")),
            ]
        )
        if h and h.get("status") == "ready":
            return int(port), h
        now = time.time()
        halted = _halt_reason(started_at)
        if halted:
            raise LaunchError("The supervisor stopped after repeated crashes.", halted)
        err = instance.read_startup_error(newer_than=started_at - 1)
        if err and not h:
            raise LaunchError(
                "The backend started but its local server could not.", str(err.get("detail", ""))
            )
        if foreign and not back:
            foreign_since = foreign_since or now
            if now - foreign_since > 25:
                raise LaunchError(
                    f"Port {configured_port()} is used by another application.",
                    "Close it, or set ERAONE_PORT to a free port.",
                )
        if proc is not None and proc.poll() is not None and not sup and not back:
            raise LaunchError(
                "The backend exited during startup.",
                f"Supervisor exit code {proc.returncode}. See logs/era_crash.log.",
            )
        if now > deadline:
            raise LaunchError(
                "Timed out waiting for ERA ONE to become ready.",
                "It may still be starting. See logs/eraone.log.",
            )
        time.sleep(0.3)


def open_hud(port: int, h: dict) -> str:
    how = browser.open_hud(
        f"http://127.0.0.1:{port}/",
        prefer_app=settings.load()["app_mode"],
        hud_clients=int(h.get("hud_clients") or 0),
    )
    log(f"HUD {how} on port {port}")
    return how


def _open_logs() -> None:
    try:
        os.startfile(str(log_dir()))  # type: ignore[attr-defined]
    except OSError as e:
        log(f"could not open logs: {e}")


def _open_diagnostics() -> None:
    subprocess.Popen(
        [str(shortcuts.python_path()), "-m", "app.hud.launcher", "--debug"],
        cwd=str(REPO),
        creationflags=_NEW_CONSOLE,
    )


def run(*, background: bool = False) -> int:
    from app.hud import launcher_ui

    while True:
        port, h, _ = locate_backend()
        if h and h.get("status") == "ready":
            log(f"backend already running (pid {h.get('pid')}, port {port})")
            if not background:
                open_hud(port, h)
            return 0
        started_at = time.time()
        proc = None
        if not supervisor_running() and not backend_running():
            proc = start_supervisor(background=background)
        else:
            log("ERA ONE is already starting; waiting for it")
        if background:
            return 0
        try:
            port, h = launcher_ui.run_with_splash(
                lambda progress, t0=started_at, sup=proc: wait_ready(
                    progress, started_at=t0, proc=sup
                )
            )
        except LaunchError as e:
            log(f"startup failed: {e.reason} | {e.detail}")
            choice = launcher_ui.error_dialog(e.reason, e.detail)
            if choice == "retry":
                continue
            if choice == "logs":
                _open_logs()
            elif choice == "diagnostics":
                _open_diagnostics()
            return 1
        open_hud(port, h)
        return 0


def _tail(path: Path, stop: threading.Event) -> None:
    pos = path.stat().st_size if path.exists() else 0
    while not stop.is_set():
        try:
            size = path.stat().st_size
            if size < pos:
                pos = 0  # rotated
            if size > pos:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                sys.stdout.write(chunk)
                sys.stdout.flush()
        except OSError:
            pass
        stop.wait(0.5)


def run_debug() -> int:
    """Same backend, same HUD — plus the logs, live, in this console."""
    os.environ["ERA_LAUNCHER_ECHO"] = "1"
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("ERA ONE Debug")
    except Exception:
        pass
    print("ERA ONE — debug console\n")
    port, h, _ = locate_backend()
    if h or supervisor_running() or backend_running():
        print(
            "ERA ONE is already running — attaching to its log (Ctrl+C detaches; Era keeps running).\n"
        )

        def _open_when_ready() -> None:
            try:
                p, hh = wait_ready(lambda _s: None, started_at=time.time(), proc=None)
                open_hud(p, hh)
                print(f"\n[launcher] HUD: http://127.0.0.1:{p}/  (pid {hh.get('pid')})\n")
            except LaunchError as e:
                print(f"\n[launcher] {e.reason} {e.detail}\n")

        threading.Thread(target=_open_when_ready, daemon=True).start()
        stop = threading.Event()
        try:
            _tail(log_dir() / "eraone.log", stop)
        except KeyboardInterrupt:
            stop.set()
        return 0

    print("Starting ERA ONE in this console. Closing this window stops this ERA ONE session.\n")
    clear_stop_flag()
    started_at = time.time()

    def _open_when_ready() -> None:
        try:
            p, hh = wait_ready(lambda _s: None, started_at=started_at, proc=None)
            open_hud(p, hh)
            print(f"\n[launcher] ERA ONE ready — HUD: http://127.0.0.1:{p}/\n", flush=True)
        except LaunchError as e:
            print(f"\n[launcher] {e.reason} {e.detail}\n", flush=True)

    threading.Thread(target=_open_when_ready, daemon=True).start()
    env = {**os.environ, "ERA_NO_AUTO_HUD": "1", "ERA_SUPERVISOR_VISIBLE": "1"}
    try:
        return subprocess.call(
            [sys.executable, str(REPO / "era_supervisor.py"), "--debug"], cwd=str(REPO), env=env
        )
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Start ERA ONE and open its HUD.")
    ap.add_argument("--debug", action="store_true", help="show live backend logs")
    ap.add_argument("--background", action="store_true", help="start silently, open nothing")
    opts = ap.parse_args(argv)
    os.chdir(REPO)
    if opts.debug:
        return run_debug()
    return run(background=opts.background)


if __name__ == "__main__":
    sys.exit(main())
