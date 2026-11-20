"""core/era_relaunch.py — "reboot Era One": quit and come straight back.

Plain English:
  When Era lags or half-breaks, the user says "reboot Era One". Era then asks the
  external watchdog (era_supervisor.py) for an immediate relaunch and exits. The
  watchdog sees the relaunch flag, skips its crash delay, does not count the exit
  as a crash, and starts a fresh Era about a second later.

  The same watchdog also restarts Era when she crashes, and — through the
  heartbeat below — when she is still running but frozen.

Files (all under ~/.era, or $ERA_HOME):
  era.relaunch    written by Era: "relaunch me now". Consumed by the supervisor.
  era.stop        the user's "stay down" flag. A reboot order removes it, since
                  the user just asked for Era to come back.
  era.heartbeat   touched every few seconds FROM Era's event loop. If the loop
                  freezes, the file goes stale and the supervisor restarts Era.

Audit trail: logs/era_lifecycle.jsonl (append-only, never truncated on launch —
unlike era.log, which starts empty every run and would lose the very event it
should record). era_supervisor.py writes the same file with its own copy of
`record_lifecycle`, because the watchdog must keep working even when Era's own
code is broken; tools/test_era_relaunch.py pins the two to the same path.

Standard library only, for the same reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_PY = _REPO_ROOT / "era_supervisor.py"
LIFECYCLE_LOG_NAME = "era_lifecycle.jsonl"
HEARTBEAT_INTERVAL_S = 10.0
READY_ACK_MAX_AGE_S = 300.0

# Words allowed BEFORE the command: a greeting or the wake word, nothing else.
# Anything more ("don't reboot Era One", "why did you reboot Era One") is not the
# command — the phrase must be the whole utterance.
_PREFIX_WORDS = ("hey", "ok", "okay", "era", "era one", "please")
_ERA_ONE = r"era[\s-]*(?:one|1|won)|era1"
# What speech-to-text and typing really produced on 2026-11-05: "Tribute era one",
# "Tributi erawan", "Tribut i rawan", "rebot era one". The model understood each one and
# called the right tool; the transcript is a separate, worse guess. Safe to accept only
# because is_reboot_command matches the WHOLE utterance, so "a tribute to Era" stays out.
_MISHEARD = rf"(?:rebot|re\s+boot|tribut\w*)\s+(?:i\s+)?(?:{_ERA_ONE}|erawan|era\s+wan|rawan|era)"
_CMD = rf"(?:(?:re-?boot|restart)\s+(?:{_ERA_ONE}|era|yourself)|{_MISHEARD})"


def era_home() -> Path:
    return Path(os.getenv("ERA_HOME", str(Path.home() / ".era")))


def relaunch_flag() -> Path:
    return era_home() / "era.relaunch"


def stop_flag() -> Path:
    return era_home() / "era.stop"


def heartbeat_file() -> Path:
    return era_home() / "era.heartbeat"


def ready_ack_file() -> Path:
    """One-shot receipt that an owner-requested relaunch reached readiness."""
    return era_home() / "era.relaunch.ready"


def arm_ready_ack(source: str, *, muted: bool = False) -> bool:
    """Persist a short-lived marker for a successful user-requested restart."""
    try:
        path = ready_ack_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"requested_at": time.time(), "source": source, "pid": os.getpid(), "muted": muted}
            ),
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        print(f"[RELAUNCH] could not arm ready acknowledgment: {exc}")
        return False


def consume_ready_ack(now: float | None = None, current_pid: int | None = None) -> bool | str:
    """Consume once; return 'muted' to suppress speech after a muted reboot."""
    path = ready_ack_file()
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if not isinstance(marker, dict):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        age = (time.time() if now is None else now) - float(marker["requested_at"])
    except (KeyError, TypeError, ValueError):
        age = READY_ACK_MAX_AGE_S + 1
    if not -30.0 <= age <= READY_ACK_MAX_AGE_S:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if marker.get("source") not in {"voice", "typed", "hud", "telegram"}:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if marker.get("pid") == (os.getpid() if current_pid is None else current_pid):
        # A reconnect in the old process is not evidence that the supervisor
        # completed the requested restart; leave the marker for its successor.
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return "muted" if marker.get("muted") is True else True


def lifecycle_log() -> Path:
    env_dir = os.environ.get("ERA_LOG_DIR")
    base = Path(env_dir) if env_dir else _REPO_ROOT / "logs"
    return base / LIFECYCLE_LOG_NAME


def is_reboot_command(text: str, wake_words: Iterable[str] = ()) -> bool:
    """True only when the WHOLE utterance is "reboot Era One".

    Tolerates what speech-to-text does to it ("Era 1", "era-one", a trailing
    period) and an optional leading greeting or wake word ("Shazam, reboot Era
    One."). Nothing else may surround it.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    words = [w.strip().lower() for w in (*_PREFIX_WORDS, *wake_words) if w and w.strip()]
    prefix = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
    pattern = rf"^\W*(?:(?:{prefix})\W+)*(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?{_CMD}(?:\s+(?:now|please))?\W*$"
    return re.match(pattern, t) is not None


def record_lifecycle(event: str, **fields: Any) -> None:
    """Append one audit record to logs/era_lifecycle.jsonl. Never raises."""
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, "pid": os.getpid()}
    rec.update({k: v for k, v in fields.items() if v is not None})
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        rec["test_run"] = True
    try:
        path = lifecycle_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        print(f"[RELAUNCH] could not write lifecycle log: {e}")


def request_relaunch(reason: str, source: str, **extra: Any) -> bool:
    """Leave the supervisor a relaunch order. Returns True if the flag was written.

    Removes era.stop first: the user asked for Era to come BACK, so a leftover
    "stay down" flag from an earlier shutdown must not strand her.
    """
    home = era_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        if stop_flag().exists():
            stop_flag().unlink()
            print(f"[RELAUNCH] removed {stop_flag()} so the supervisor brings Era back.")
        relaunch_flag().write_text(
            json.dumps(
                {
                    "requested_at": time.time(),
                    "iso": datetime.now().isoformat(timespec="seconds"),
                    "reason": reason,
                    "source": source,
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        ok = True
    except Exception as e:
        print(f"[RELAUNCH] could not write relaunch flag: {e}")
        ok = False
    record_lifecycle(
        "relaunch.requested",
        reason=reason,
        source=source,
        flag_written=ok,
        supervised=is_supervised(),
        **extra,
    )
    return ok


def is_supervised() -> bool:
    """Is era_supervisor.py our parent? The env var covers supervisors from this
    version on; the parent walk covers one started before it (still running old
    code, so it never set the variable)."""
    if os.environ.get("ERA_SUPERVISED") == "1":
        return True
    try:
        import psutil

        for parent in psutil.Process().parents():
            try:
                if any("era_supervisor" in part for part in parent.cmdline()):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def spawn_supervisor(wait_pid: int) -> bool:
    """Era was started by hand, so nothing would bring her back after she exits.
    Start a detached supervisor that waits for this process to die first (two
    Eras at once would fight over the mic and the dispatch lock)."""
    if not SUPERVISOR_PY.exists():
        return False
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [sys.executable, str(SUPERVISOR_PY), "--wait-pid", str(wait_pid)],
            cwd=str(_REPO_ROOT),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        record_lifecycle("relaunch.supervisor_spawned", wait_pid=wait_pid)
        return True
    except Exception as e:
        record_lifecycle("relaunch.supervisor_spawn_failed", error=f"{type(e).__name__}: {e}")
        return False


def _terminate_self(code: int) -> None:
    """End this process NOW, without Windows' ExitProcess teardown.

    `os._exit` still runs every DLL's detach routine; an audio or GPU driver
    blocked there kept Era alive past the supervisor's grace (20:53 2026-11-15:
    exit code already 0, process still up 26 s later, force-killed). Elsewhere
    the os._exit below is the whole exit. Inert inside a pytest test, where a
    patched os._exit is the observable exit and this would end the runner.
    """
    if os.name == "nt" and "PYTEST_CURRENT_TEST" not in os.environ:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.TerminateProcess(k32.GetCurrentProcess(), int(code) & 0xFFFFFFFF)


def hard_exit(code: int = 0) -> None:
    """Leave the process on purpose: flush what Era wrote, then stop for good.

    Never returns. Used by the one exit path (tool_dispatch._exit_after_speaking)
    so the supervisor never has to force-kill a relaunch that already said
    goodbye (PF-024).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        import logging

        for handler in logging.getLogger().handlers:
            handler.flush()
    except Exception:
        pass
    try:
        _terminate_self(code)
    except Exception:
        pass
    os._exit(code)


def _beat() -> None:
    try:
        path = heartbeat_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time():.0f} {os.getpid()}", encoding="utf-8")
    except Exception:
        pass


def start_heartbeat(
    get_loop: Callable[[], Any], interval: float = HEARTBEAT_INTERVAL_S
) -> threading.Thread:
    """Beat from INSIDE Era's event loop, scheduled from a helper thread.

    Writing from the thread itself would prove only that the thread is alive: a
    frozen event loop — the thing that makes Era deaf and mute — would keep
    beating. Scheduling the write onto the loop means a stuck loop stops the
    beats, and the supervisor notices. No loop yet (still starting) means no
    beat; the supervisor only arms its hang check after the first one.
    """

    def _run() -> None:
        while True:
            try:
                loop = get_loop()
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(_beat)
            except Exception:
                pass  # a loop closed between the check and the call
            time.sleep(interval)

    t = threading.Thread(target=_run, daemon=True, name="EraHeartbeat")
    t.start()
    return t
