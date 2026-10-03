"""Windows shortcuts for the launcher, and the opt-in "start with Windows" switch.

Shortcuts point at ``pythonw.exe -m app.hud.launcher`` — pythonw has no console,
so double-clicking never flashes a terminal. The Debug shortcut deliberately
uses ``python.exe`` so its console (and the live logs) stay visible.

Autostart uses a Startup-folder shortcut: no admin rights, trivially reversible.
The older ``EraOneSupervisor`` scheduled task (register_era_supervisor.ps1) is
reported as-is; it is the user's existing choice and is never silently changed.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "ERA ONE"
DEBUG_NAME = "ERA ONE Debug"
TASK_NAME = "EraOneSupervisor"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def icon_path() -> Path:
    return repo_root() / "assets" / "eraone.ico"


def _sibling(name: str) -> Path:
    exe = Path(sys.executable)
    cand = exe.with_name(name)
    return cand if cand.exists() else exe


def pythonw_path() -> Path:
    return _sibling("pythonw.exe")


def python_path() -> Path:
    return _sibling("python.exe")


def launcher_args(*, debug: bool = False, background: bool = False) -> str:
    args = "-m app.hud.launcher"
    if debug:
        args += " --debug"
    if background:
        args += " --background"
    return args


def desktop_dir() -> Path:
    try:
        import win32com.client  # type: ignore[import-not-found]

        return Path(win32com.client.Dispatch("WScript.Shell").SpecialFolders("Desktop"))
    except Exception:
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def programs_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_dir() -> Path:
    return programs_dir() / "Startup"


def create_shortcut(
    lnk: Path, target: Path, args: str, workdir: Path, description: str, icon: Path | None = None
) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    icon_loc = f"{icon},0" if icon and icon.exists() else ""
    try:
        import win32com.client  # type: ignore[import-not-found]

        sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(lnk))
        sc.TargetPath = str(target)
        sc.Arguments = args
        sc.WorkingDirectory = str(workdir)
        sc.Description = description
        if icon_loc:
            sc.IconLocation = icon_loc
        sc.save()
        return
    except ImportError:
        pass
    # PowerShell fallback. -EncodedCommand sidesteps every quoting hazard in paths.
    q = lambda s: str(s).replace("'", "''")  # noqa: E731
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + q(lnk) + "');"
        "$s.TargetPath='" + q(target) + "';$s.Arguments='" + q(args) + "';"
        "$s.WorkingDirectory='"
        + q(workdir)
        + "';$s.Description='"
        + q(description)
        + "';"
        + ("$s.IconLocation='" + q(icon_loc) + "';" if icon_loc else "")
        + "$s.Save()"
    )
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        check=True,
        capture_output=True,
        creationflags=_NO_WINDOW,
        timeout=30,
    )


def install(*, desktop: bool = True, start_menu: bool = True) -> list[Path]:
    """Create the ERA ONE (silent) and ERA ONE Debug shortcuts."""
    root, icon = repo_root(), icon_path()
    made: list[Path] = []
    targets = []
    if desktop:
        targets.append(desktop_dir())
    if start_menu:
        targets.append(programs_dir() / APP_NAME)
    for folder in targets:
        normal = folder / f"{APP_NAME}.lnk"
        create_shortcut(
            normal, pythonw_path(), launcher_args(), root, "Start ERA ONE and open the HUD", icon
        )
        debug = folder / f"{DEBUG_NAME}.lnk"
        create_shortcut(
            debug,
            python_path(),
            launcher_args(debug=True),
            root,
            "ERA ONE with live backend logs",
            icon,
        )
        made += [normal, debug]
    return made


# ── autostart ──────────────────────────────────────────────────────────────

_task_cache: dict = {"at": 0.0, "state": None}


def scheduled_task_state() -> str | None:
    """State of the legacy EraOneSupervisor task (Running/Ready/Disabled) or None."""
    now = time.monotonic()
    if now - _task_cache["at"] < 60 and _task_cache["at"]:
        return _task_cache["state"]
    state = None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue).State",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        state = (out.stdout or "").strip() or None
    except Exception:
        state = None
    _task_cache.update(at=now, state=state)
    return state


def autostart_status() -> dict:
    if not os.environ.get("APPDATA"):
        return {"startup_shortcut": False, "scheduled_task": None, "enabled": False}
    lnk = startup_dir() / f"{APP_NAME}.lnk"
    task = scheduled_task_state()
    return {
        "startup_shortcut": lnk.exists(),
        "scheduled_task": task,
        "enabled": lnk.exists() or (task is not None and task != "Disabled"),
    }


def set_autostart(enabled: bool) -> dict:
    lnk = startup_dir() / f"{APP_NAME}.lnk"
    if enabled:
        create_shortcut(
            lnk,
            pythonw_path(),
            launcher_args(background=True),
            repo_root(),
            "Start ERA ONE silently at logon",
            icon_path(),
        )
    else:
        try:
            lnk.unlink()
        except FileNotFoundError:
            pass
    return autostart_status()
