"""No helper program Era runs may flash a console window on Karim's screen.

THE DEFECT THESE TESTS PIN (reported 2026-10-03)
  A PowerShell window blinked on screen every time Era did a task. Two separate
  causes, both pinned here:

  1. core/era_relaunch.py:spawn_supervisor used DETACHED_PROCESS. That leaves the
     supervisor with NO CONSOLE, the supervisor hands Era its stdio, so Era had
     none either — and from then on every console helper Era ran (powershell,
     taskkill, ffmpeg, the claude CLI) had to create its own console, each with a
     visible window. `test_the_relaunch_path_never_detaches` fails if that flag
     comes back.

  2. Most call sites did not pass CREATE_NO_WINDOW themselves. That was survivable
     only while the whole process tree happened to inherit a windowless console —
     i.e. it depended on how Era was started. `test_every_runtime_spawn_is_windowless`
     makes each call site carry its own guarantee.

WHY A SOURCE SCAN AND NOT A BEHAVIOURAL TEST
  The thing under test only exists on Windows, inside the kernel's process
  creation. There is no way to observe "a window did not appear" from a Linux
  CI container, and a mock would only assert that the mock was called. So the
  property asserted is the one that is actually checkable anywhere: the flag is
  present at the point where the process is created. Whether that suppresses the
  window is a Windows fact, documented in core/win_subprocess.py and confirmed on
  the real machine — not something these tests claim to prove.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from core.win_subprocess import NEW_GROUP, NO_WINDOW, no_window

REPO = Path(__file__).resolve().parents[1]

# Where Era's own runtime lives. tools/, .claude/ and setup.py are developer
# surfaces: there a console window is wanted, not a bug.
RUNTIME_ROOTS = ("actions", "core", "app", "agent", "memory", "src")
RUNTIME_FILES = ("era_dispatch.py", "music_generate.py", "era_supervisor.py")

SPAWNERS = {"run", "Popen", "call", "check_output", "check_call"}

# Commands that exist only on macOS/Linux cannot open a Windows console window.
POSIX_ONLY = {
    "osascript",
    "pactl",
    "xdg-open",
    "xdg-settings",
    "gsettings",
    "feh",
    "qdbus",
    "xfconf-query",
    "brightnessctl",
    "wmctrl",
    "xdotool",
    "launchctl",
    "crontab",
    "at",
    "systemd-run",
    "pmset",
    "networksetup",
    "open",
    "defaults",
    "pgrep",
    "systemctl",
    "nmcli",
    "amixer",
    "notify-send",
}

# Deliberate exceptions, as {file: (how many unguarded spawns, why)}.
#
# The COUNT is the point. A file-level "trust me" would let a genuinely new
# window-flashing call slip in beside a legitimate one, so the number is pinned:
# add a spawn here and the test fails, remove one and the test fails too, which
# is how a stale exception gets deleted instead of inherited.
ALLOWED_VISIBLE = {
    # `ERA ONE Debug` exists so Karim can watch the backend log live in a real
    # console. Hiding it would delete the feature.
    "app/hud/launcher.py": (
        2,
        "the --debug console IS the feature: CREATE_NEW_CONSOLE "
        "for the visible supervisor, and the blocking --debug call",
    ),
    # explorer.exe is a GUI program (Windows ignores the flag) and is handed a
    # string command line rather than argv, because Explorer splits its own
    # command line on commas.
    "app/hud/api.py": (2, "explorer is a GUI program, string command line"),
    # The watchdog hands Era its own stdio on purpose, so `--debug` shows Era's
    # output in the console the user is already looking at.
    "era_supervisor.py": (1, "Era inherits the watchdog's stdio by design"),
}


def _runtime_sources() -> list[Path]:
    out: list[Path] = [REPO / f for f in RUNTIME_FILES]
    for root in RUNTIME_ROOTS:
        out += sorted((REPO / root).rglob("*.py"))
    return [
        p
        for p in out
        if p.exists()
        and not p.name.startswith("test_")
        and "backups" not in p.as_posix()
        and "__pycache__" not in p.as_posix()
    ]


def _first_literal_arg(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.List) and call.args[0].elts:
        first = call.args[0].elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def _subprocess_constants(path: Path) -> set[str]:
    """Names read off the `subprocess` module in real code — comments excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and getattr(n.value, "id", "") == "subprocess"
    }


# Every spelling of "no window" a call site may legitimately use.
_NO_WINDOW_NAMES = ("CREATE_NO_WINDOW", "NO_WINDOW", "no_window")


def _no_window_aliases(tree: ast.Module) -> set[str]:
    """Names that resolve to a no-window flag, following assignment chains.

    Call sites do not all pass the constant directly: one builds
    `_LOW_PRIORITY = BELOW_NORMAL_PRIORITY_CLASS | _NO_WINDOW`, another computes a
    local `flags` inside an `if os.name == "nt"` branch. Both are correct, and a
    checker that only recognises the literal constant would report them as
    offenders — so it would get loosened to something that waves everything
    through. Resolving the chain to a fixed point keeps the check strict instead.
    """
    assignments: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        source = ast.unparse(value)
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.append((target.id, source))

    known = set(_NO_WINDOW_NAMES)
    while True:
        grown = {name for name, src in assignments if any(k in src for k in known)}
        if grown <= known:
            return known
        known |= grown


def _unguarded_spawns(path: Path) -> list[str]:
    """Call sites in `path` that create a process without asking for no window."""
    text = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(text)
    known = _no_window_aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        if not (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)):
            continue
        if getattr(func.value, "id", "") != "subprocess" or func.attr not in SPAWNERS:
            continue
        if _first_literal_arg(node) in POSIX_ONLY:
            continue
        flags = next((k for k in node.keywords if k.arg == "creationflags"), None)
        if flags is None:
            found.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno} (no creationflags)")
            continue
        # A bare number does not count: 0x08000000 in a diff teaches nobody, and
        # it raises ValueError the moment the same line runs on Linux.
        source = ast.unparse(flags.value)
        if not any(name in source for name in known):
            found.append(
                f"{path.relative_to(REPO).as_posix()}:{node.lineno} "
                f"(creationflags={source} does not resolve to a no-window constant)"
            )
    return found


def test_every_runtime_spawn_is_windowless() -> None:
    """Every process Era starts carries its own no-window guarantee.

    Not "the process tree happens to inherit one" — that depended on how Era was
    launched, which is exactly how the flashing window got in.
    """
    offenders: list[str] = []
    allowed_seen: dict[str, int] = dict.fromkeys(ALLOWED_VISIBLE, 0)
    for path in _runtime_sources():
        rel = path.relative_to(REPO).as_posix()
        found = _unguarded_spawns(path)
        if rel in ALLOWED_VISIBLE:
            allowed_seen[rel] = len(found)
            continue
        offenders += found
    assert offenders == [], (
        "these call sites can flash a console window on Windows:\n  "
        + "\n  ".join(offenders)
        + "\n\nPass creationflags=NO_WINDOW (from core.win_subprocess). If a visible "
        "console really is the point, add the site to ALLOWED_VISIBLE with the reason."
    )
    for rel, (expected, why) in ALLOWED_VISIBLE.items():
        assert allowed_seen[rel] == expected, (
            f"{rel} is allowed {expected} spawn(s) without the no-window flag "
            f"({why}) but has {allowed_seen[rel]}. If a NEW one was added it must "
            f"pass creationflags=NO_WINDOW; if one was removed, drop the exception."
        )


def test_the_relaunch_path_never_detaches() -> None:
    """DETACHED_PROCESS is the trap that caused the flashing window.

    It means "no console at all", so every console helper started underneath has
    to make its own — with a window. CREATE_NO_WINDOW detaches just as completely
    and hands down a windowless console instead.
    """
    # The docstrings in that file explain the DETACHED_PROCESS trap by name, so a
    # plain substring search would match the explanation. Read the code instead.
    used = _subprocess_constants(REPO / "core" / "era_relaunch.py")
    assert "DETACHED_PROCESS" not in used, (
        "core/era_relaunch.py is back to DETACHED_PROCESS. That strips the console "
        "from the supervisor and therefore from Era, and every PowerShell call Era "
        "makes after a restart blinks a window. Use CREATE_NO_WINDOW."
    )
    assert "CREATE_NO_WINDOW" in used


def test_the_relaunched_supervisor_keeps_its_own_process_group() -> None:
    """Detaching is still required: Ctrl-C in Era's console must not kill the
    watchdog that is waiting to bring her back."""
    assert "CREATE_NEW_PROCESS_GROUP" in _subprocess_constants(REPO / "core" / "era_relaunch.py")


def test_flags_are_inert_off_windows() -> None:
    """The same call site has to be correct on Linux and macOS with no branch.

    subprocess rejects a non-zero creationflags on POSIX, so the constants must
    collapse to 0 there — otherwise every patched call site raises ValueError.
    """
    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # on Windows
        assert NO_WINDOW == 0x08000000
        assert NEW_GROUP == 0x00000200
    else:
        assert NO_WINDOW == 0
        assert NEW_GROUP == 0
    assert no_window() == NO_WINDOW
    assert no_window(NEW_GROUP) == NO_WINDOW | NEW_GROUP


def test_a_posix_run_accepts_the_constant() -> None:
    """Proof, not assumption: the patched shape actually runs on this platform."""
    done = subprocess.run(["echo", "era"], capture_output=True, text=True, creationflags=NO_WINDOW)
    assert done.returncode == 0
