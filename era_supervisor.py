#!/usr/bin/env python3
"""era_supervisor.py - keep Era One alive.

Plain English:
  This is a tiny watchdog that sits OUTSIDE Era. It starts `main.py`, and if Era
  ever dies (crash, freeze-kill, Task-Manager kill), it writes a note to a log and
  starts Era again a few seconds later. If Era crashes over and over in a short
  time (a real bug, not a fluke), the watchdog STOPS and tells you loudly instead
  of spinning forever.

  It touches nothing inside Era - it only wraps it. Standard-library only, so it
  needs no extra installs.

Reboot on demand ("reboot Era One"):
  Era writes %USERPROFILE%\\.era\\era.relaunch and exits. The watchdog relaunches
  her after ~1s instead of the crash delay, and the exit does not count toward the
  crash-loop limit. If Era is too stuck to exit on her own, the watchdog kills her
  once the flag is older than ERA_RELAUNCH_GRACE seconds.

Frozen, not crashed:
  Era touches %USERPROFILE%\\.era\\era.heartbeat from inside her event loop. Once
  the first beat arrives, a heartbeat older than ERA_HANG_TIMEOUT seconds means the
  loop is stuck: the watchdog kills the process tree and restarts her (counted as
  a crash). The check pauses after the machine sleeps, so waking up never reads
  as a hang. ERA_HANG_TIMEOUT=0 disables it.

Silent by default (localhost HUD, 2026-09-26):
  Launched in a console window of its OWN (the logon Scheduled Task runs
  python.exe), the watchdog re-spawns itself windowless (CREATE_NO_WINDOW) and
  lets that window close, so normal use shows no terminal; Era's output goes to
  logs/eraone.log instead (core/stdio_log.py). The ERA ONE launcher starts it
  windowless directly. Pass --debug (the "ERA ONE Debug" shortcut does) to keep
  the console and watch the logs live. A console shared with a shell you typed
  into is never left.

Single instance:
  Holds the named mutex Local\\EraOne.Supervisor, so a second watchdog (the
  launcher and the logon task racing) exits at once instead of starting a second
  Era. If main.py exits with code 86 it found another backend already running
  (app/hud/instance.py EXIT_ALREADY_RUNNING); the watchdog then stands down.

Every restart is recorded in logs/era_lifecycle.jsonl (append-only audit trail;
core/era_relaunch.py writes the same file from inside Era).

How to stop Era on purpose (so the watchdog does NOT revive it):
  - Stop/disable the Windows Scheduled Task:  .\\register_era_supervisor.ps1 -Action stop
    (that script installs/removes the task that auto-starts this supervisor), OR
  - Create the stop file:  %USERPROFILE%\\.era\\era.stop
    (delete it to allow supervision again). The watchdog checks for it each cycle.

Environment overrides (optional):
  ERA_PYTHON       Python used to launch main.py (default: the Python running this).
  ERA_LOCK_FILE    Same var era_dispatch.py uses; its ".lockdir" is cleared on launch.
  ERA_STATUS_FILE  Same var era_dispatch.py uses; a stale one is cleared on launch.
  ERA_HANG_TIMEOUT    Seconds without a heartbeat before Era counts as frozen (180).
  ERA_RELAUNCH_GRACE  Seconds Era gets to exit after asking for a relaunch (25).
"""


from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.win_subprocess import NO_WINDOW

# --- Where things live -------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# ERA_MAIN lets a test harness point the watchdog at a stand-in script; defaults
# to the real Era entry point.
MAIN_PY = Path(os.getenv("ERA_MAIN", str(BASE_DIR / "main.py")))
# ERA_LOG_DIR: same override core/logger.py honours, so a test run never writes here.
LOG_DIR = Path(os.environ.get("ERA_LOG_DIR") or str(BASE_DIR / "logs"))
CRASH_LOG = LOG_DIR / "era_crash.log"

# The Python that runs Era. Default = the same interpreter running this watchdog,
# so "whatever starts the supervisor also starts Era" - no interpreter mismatch.
PYTHON = os.getenv("ERA_PYTHON", sys.executable)

# Era's runtime dir + the exact lock/status paths era_dispatch.py uses, so we
# clean up the same files it would leave behind after a mid-job crash.
ERA_HOME = Path(os.getenv("ERA_HOME", str(Path.home() / ".era")))
LOCK_PATH = Path(os.getenv("ERA_LOCK_FILE", str(ERA_HOME / "era.lock")))
LOCK_DIR = Path(str(LOCK_PATH) + ".lockdir")  # FileLock's atomic mutex dir
STATUS_PATH = Path(os.getenv("ERA_STATUS_FILE", str(ERA_HOME / "era_status.json")))
STOP_FLAG = ERA_HOME / "era.stop"  # you create this to stop
HALT_FLAG = ERA_HOME / "era_supervisor_halted.flag"  # we create this on crash-loop
RELAUNCH_FLAG = ERA_HOME / "era.relaunch"  # Era creates this: "relaunch me now"
HEARTBEAT = ERA_HOME / "era.heartbeat"  # Era touches this from her event loop
# Same file core/era_relaunch.py appends to (tools/test_era_relaunch.py pins it).
LIFECYCLE_LOG = LOG_DIR / "era_lifecycle.jsonl"

# --- Crash-loop policy (overridable via env for testing) ---------------------
RESTART_DELAY_S = float(os.getenv("ERA_RESTART_DELAY", "5"))  # delay before relaunch
LOOP_WINDOW_S = float(os.getenv("ERA_LOOP_WINDOW", "120"))  # rolling window
MAX_RESTARTS = int(os.getenv("ERA_MAX_RESTARTS", "5"))  # >this in window => halt
RELAUNCH_DELAY_S = float(os.getenv("ERA_RELAUNCH_DELAY", "1"))  # user-asked reboot
HANG_TIMEOUT_S = float(os.getenv("ERA_HANG_TIMEOUT", "180"))  # 0 disables hang check
RELAUNCH_GRACE_S = float(os.getenv("ERA_RELAUNCH_GRACE", "25"))  # then force-kill
POLL_S = float(os.getenv("ERA_POLL", "1"))

# Must match app/hud/instance.py (not imported: the watchdog never imports Era).
# An overridden ERA_HOME (tests, stand-ins) gets its own name, so a test
# watchdog never stands down just because the real one is running.
SUPERVISOR_MUTEX = "Local\\EraOne.Supervisor" + (
    "." + hashlib.sha1(str(ERA_HOME).lower().encode()).hexdigest()[:10]
    if os.getenv("ERA_HOME")
    else ""
)
EXIT_ALREADY_RUNNING = 86

# --- Logging: rotating file + console ---------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("era_supervisor")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
_fileh = RotatingFileHandler(CRASH_LOG, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
_fileh.setFormatter(_fmt)
_streamh = logging.StreamHandler(sys.stdout)
_streamh.setFormatter(_fmt)
log.addHandler(_fileh)
log.addHandler(_streamh)


def banner(lines: list[str]) -> None:
    """Print an unmissable box to the console AND the log."""
    width = max((len(x) for x in lines), default=0) + 4
    bar = "=" * width
    box = [bar, *[f"= {x.ljust(width - 4)} =" for x in lines], bar]
    print("\n".join(box), flush=True)  # clean, unmissable console box
    log.critical("HALT: %s", " | ".join(lines))  # one line into the crash log


def record_lifecycle(event: str, **fields) -> None:
    """Append one audit record to logs/era_lifecycle.jsonl. Never raises.
    A copy of core.era_relaunch.record_lifecycle: the watchdog must not import
    Era's code, which may be exactly what is broken."""
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, "pid": os.getpid()}
    rec.update({k: v for k, v in fields.items() if v is not None})
    try:
        LIFECYCLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LIFECYCLE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError as e:
        log.error("Could not write lifecycle log: %s", e)


def read_relaunch_request() -> dict | None:
    """The relaunch order Era left, or None. Unreadable content still counts:
    the flag's existence is the order, the JSON is only detail for the audit."""
    if not RELAUNCH_FLAG.exists():
        return None
    try:
        data = json.loads(RELAUNCH_FLAG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def consume_relaunch_request() -> None:
    try:
        RELAUNCH_FLAG.unlink()
    except OSError:
        pass


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill Era and everything she started (browser helpers, audio workers)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        creationflags=NO_WINDOW,
    )
    else:
        proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED|SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT = still running
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_pid(pid: int, timeout_s: float = 60) -> None:
    """Started by Era herself (she had no supervisor): let her exit first."""
    deadline = time.time() + timeout_s
    while pid_alive(pid) and time.time() < deadline:
        time.sleep(0.5)


def heartbeat_age(since: float) -> float | None:
    """Seconds since Era's last beat in THIS run, or None before her first one."""
    try:
        mtime = HEARTBEAT.stat().st_mtime
    except OSError:
        return None
    if mtime < since:
        return None  # a beat from a previous run does not arm the check
    return time.time() - mtime


def watch(proc: subprocess.Popen, started: float) -> tuple[int, str]:
    """Wait for Era to exit; kill her if she freezes or ignores her own relaunch.
    Returns (exit code, why) where why is "exit", "hang" or "relaunch_forced"."""
    last_tick = time.monotonic()
    armed_after = 0.0  # monotonic time before which the hang check stays off
    while True:
        code = proc.poll()
        if code is not None:
            return code, "exit"
        now_mono = time.monotonic()
        if now_mono - last_tick > max(30.0, POLL_S * 10):
            # We ourselves were suspended (laptop sleep): every heartbeat is old
            # for a reason that is not Era's fault. Give her a full timeout.
            armed_after = now_mono + HANG_TIMEOUT_S
            log.info("Supervisor resumed after %.0fs gap; hang check paused.", now_mono - last_tick)
        last_tick = now_mono

        req = read_relaunch_request()
        if req is not None:
            asked = float(req.get("requested_at") or 0) or time.time()
            if asked >= started and time.time() - asked > RELAUNCH_GRACE_S:
                log.warning(
                    "Era asked for a relaunch %.0fs ago but is still running - killing it.",
                    time.time() - asked,
                )
                kill_tree(proc)
                return (proc.returncode if proc.returncode is not None else -1), "relaunch_forced"

        if HANG_TIMEOUT_S > 0 and now_mono >= armed_after:
            age = heartbeat_age(started)
            if age is not None and age > HANG_TIMEOUT_S:
                log.error("Era's heartbeat is %.0fs old - she is frozen. Killing her.", age)
                record_lifecycle("hang.detected", heartbeat_age_s=round(age), era_pid=proc.pid)
                kill_tree(proc)
                return (proc.returncode if proc.returncode is not None else -1), "hang"
        time.sleep(POLL_S)


def clear_stale_locks() -> None:
    """Remove any lock/status left by era_dispatch.py after a crash. If Era died
    while a CM job held the shared lock, that lock is stale - the process holding
    it is gone - so it's safe (and necessary) to clear before restarting."""
    ERA_HOME.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK_DIR.exists():
            # FileLock's lockdir usually holds an owner.json, so a bare rmdir()
            # fails with WinError 145 ("directory not empty") and the stale lock
            # survives the restart. rmtree removes the dir and its contents.
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            if LOCK_DIR.exists():
                log.error("Could not fully clear stale lock: %s", LOCK_DIR)
            else:
                log.warning("Cleared stale dispatch lock: %s", LOCK_DIR)
    except OSError as e:
        log.error("Could not clear stale lock %s: %s", LOCK_DIR, e)
    try:
        if STATUS_PATH.exists():
            STATUS_PATH.unlink()
            log.warning("Cleared stale job-status file: %s", STATUS_PATH)
    except OSError as e:
        log.error("Could not clear stale status %s: %s", STATUS_PATH, e)


def preflight() -> bool:
    """Fatal-config checks. Returns False (and halts) if we can't possibly run."""
    if not MAIN_PY.exists():
        write_halt(f"main.py not found at {MAIN_PY}")
        return False
    if not Path(PYTHON).exists() and os.path.sep in PYTHON:
        write_halt(f"Python interpreter not found: {PYTHON}")
        return False
    return True


def owns_visible_console() -> bool:
    """True if this process sits alone in a console WINDOW that was created for
    it (Task Scheduler running python.exe). A console shared with a shell the user
    typed into has other processes attached; a windowless console has no hwnd;
    captured output (tests) is not a tty. None of those count."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        if not sys.stdout or not sys.stdout.isatty():
            return False
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleWindow():
            return False
        pids = (ctypes.c_uint * 4)()
        return k32.GetConsoleProcessList(pids, 4) == 1
    except Exception:
        return False


def respawn_windowless(argv: list[str]) -> int | None:
    """Re-run this watchdog with CREATE_NO_WINDOW and return its pid.

    A windowless console, not a hidden window: hiding does nothing when Windows
    Terminal is the default console host, and a CREATE_NO_WINDOW process still
    has a real console that every child inherits — so Era's own powershell/cmd
    helpers never pop a window either (verified 2026-09-26)."""
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *argv],
            cwd=str(BASE_DIR),
            env={**os.environ, "ERA_SUPERVISOR_DETACHED": "1"},
            creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return child.pid
    except OSError as e:
        log.error("Could not detach from the console (%s); staying visible.", e)
        return None


def release_single_instance() -> None:
    global _mutex_handle
    if _mutex_handle and os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
    _mutex_handle = None


_mutex_handle = None


def acquire_single_instance() -> bool:
    """Take Local\\EraOne.Supervisor; False if another watchdog holds it."""
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, SUPERVISOR_MUTEX)
    if not handle:
        return True  # cannot tell; never block Era over a mutex API failure
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def ensure_gateway() -> None:
    """Start OmniRoute (Era's model gateway) if it is down. Every CM/ads run goes
    through it, and until 2026-09-25 only ZORO's launcher started it — so Era's
    jobs worked only while ZORO happened to be open. Never blocks Era's launch."""
    try:
        from core import omniroute

        ok, detail = omniroute.ensure_running()
    except Exception as e:  # noqa: BLE001 - the gateway must never stop Era launching
        ok, detail = False, f"gateway check failed: {e}"
    (log.info if ok else log.error)("OmniRoute: %s", detail)


def write_halt(reason: str) -> None:
    ERA_HOME.mkdir(parents=True, exist_ok=True)
    try:
        HALT_FLAG.write_text(
            f"Era supervisor halted at {datetime.now().isoformat(timespec='seconds')}\n"
            f"Reason: {reason}\n\n"
            f"Fix the problem, DELETE this file, then restart the supervisor\n"
            f"(or its Scheduled Task).\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.error("Could not write halt flag: %s", e)
    banner(
        [
            "ERA SUPERVISOR HALTED",
            reason,
            f"Flag file: {HALT_FLAG}",
            "Delete that file and restart the supervisor after fixing it.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keep Era One alive.")
    ap.add_argument("--wait-pid", type=int, default=0, help="wait for this PID to exit first")
    ap.add_argument("--debug", action="store_true", help="keep the console visible (live logs)")
    opts = ap.parse_args(argv)
    if not acquire_single_instance():
        log.info("Another Era supervisor is already running - this one exits.")
        return 0
    if (
        not opts.debug
        and os.getenv("ERA_SUPERVISOR_DETACHED") != "1"
        and os.getenv("ERA_SUPERVISOR_VISIBLE") != "1"
        and owns_visible_console()
    ):
        # Started in a console of its own (the logon task): continue windowless
        # and let this visible console close. Output goes to logs/ instead.
        release_single_instance()
        pid = respawn_windowless(list(argv if argv is not None else sys.argv[1:]))
        if pid is not None:
            log.info("Continuing without a console window (supervisor pid %s).", pid)
            return 0
        acquire_single_instance()
    log.info("Era supervisor starting. python=%s  main=%s  cwd=%s", PYTHON, MAIN_PY, BASE_DIR)
    if opts.wait_pid:
        log.info("Waiting for the previous Era (pid %s) to exit...", opts.wait_pid)
        wait_for_pid(opts.wait_pid)
    # A leftover halt flag from a previous run should not silently keep us down,
    # but we DO surface it so you know the last stop was a crash-loop halt.
    if HALT_FLAG.exists():
        log.warning(
            "Previous run left a halt flag (%s); clearing it and starting fresh.", HALT_FLAG
        )
        try:
            HALT_FLAG.unlink()
        except OSError:
            pass
    if not preflight():
        return 1

    restarts: deque[float] = deque()  # timestamps of recent (re)launches
    launch_reason = "initial"  # tells Era whether to open the HUD by itself
    while True:
        if STOP_FLAG.exists():
            log.info("Stop flag present (%s) - supervisor exiting, Era stays down.", STOP_FLAG)
            return 0

        clear_stale_locks()
        # A relaunch order left over from a previous run is spent: this launch IS it.
        consume_relaunch_request()
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass
        ensure_gateway()
        started = time.time()
        log.info("Launching Era...")
        # Tells Era a supervisor will bring her back (core/era_relaunch.py).
        env = {
            # Keep the local model resident between turns (a cold start is 3-8 s on a
            # shared GPU). A default only: a value already in the environment wins.
            # NOTE this is Era's process. The Ollama SERVER reads its own copy at its
            # own start, and Era's ollama adapter already sends keep_alive="30m" on
            # every request, which is what actually holds the model warm.
            "OLLAMA_KEEP_ALIVE": "30m",
            **os.environ,
            "ERA_SUPERVISED": "1",
            "ERA_SUPERVISOR_PID": str(os.getpid()),
            "ERA_LAUNCH_REASON": launch_reason,
            "PYTHONUNBUFFERED": "1",
        }
        try:
            # stdio inherited: when run in a console you see Era's output live.
            proc = subprocess.Popen([PYTHON, str(MAIN_PY)], cwd=str(BASE_DIR), env=env, creationflags=NO_WINDOW)
        except OSError as e:
            write_halt(f"Failed to launch Era: {e}")
            return 1
        try:
            code, why = watch(proc, started)
        except KeyboardInterrupt:
            log.info("Ctrl-C received - supervisor exiting (Era was stopped too).")
            if proc.poll() is None:
                kill_tree(proc)
            return 0

        ran_for = time.time() - started
        if code == EXIT_ALREADY_RUNNING and why == "exit":
            log.info("Era found another backend already running - supervisor stands down.")
            record_lifecycle("supervisor.stand_down", reason="backend_already_running")
            return 0
        req = read_relaunch_request()
        if req is not None and not STOP_FLAG.exists():
            # User-ordered reboot: back at once, and not counted as a crash.
            consume_relaunch_request()
            log.info(
                "Era relaunch requested by user (%s, via %s) - exit code %s after %.0fs. "
                "Relaunching in %ss.",
                req.get("reason", "reboot"),
                req.get("source", "?"),
                code,
                ran_for,
                RELAUNCH_DELAY_S,
            )
            record_lifecycle(
                "relaunch.performed",
                reason=req.get("reason"),
                source=req.get("source"),
                requested_at=req.get("iso"),
                exit_code=code,
                forced=(why == "relaunch_forced"),
                ran_for_s=round(ran_for),
            )
            time.sleep(RELAUNCH_DELAY_S)
            launch_reason = "relaunch"
            continue

        if not STOP_FLAG.exists():
            kind = "hang" if why == "hang" else ("crash" if code else "clean_exit")
            record_lifecycle(f"restart.{kind}", exit_code=code, ran_for_s=round(ran_for))
        if why == "hang":
            log.warning("Era was frozen after %.0fs - treating as a crash.", ran_for)
        elif code == 0:
            log.info(
                "Era exited cleanly (code 0) after %.0fs. Relaunching to stay available.", ran_for
            )
        else:
            log.warning("Era exited with code %s after %.0fs - treating as a crash.", code, ran_for)

        # Crash-loop guard: prune restarts older than the window, then check count.
        now = time.time()
        restarts.append(now)
        while restarts and now - restarts[0] > LOOP_WINDOW_S:
            restarts.popleft()
        if len(restarts) > MAX_RESTARTS:
            write_halt(
                f"{len(restarts)} restarts within {LOOP_WINDOW_S}s "
                f"(limit {MAX_RESTARTS}). Era is crash-looping - not restarting."
            )
            record_lifecycle("supervisor.halted", restarts=len(restarts), window_s=LOOP_WINDOW_S)
            return 1

        # A clean stop requested during the run should win over the delay.
        if STOP_FLAG.exists():
            log.info("Stop flag present after exit - supervisor exiting.")
            return 0
        log.info(
            "Restarting Era in %ss  (%d restart(s) in the last %ss).",
            RESTART_DELAY_S,
            len(restarts),
            LOOP_WINDOW_S,
        )
        time.sleep(RESTART_DELAY_S)
        launch_reason = "restart"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
