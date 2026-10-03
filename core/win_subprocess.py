"""One definition of "run a helper program without flashing a window at Karim".

THE SYMPTOM THIS EXISTS FOR
  A PowerShell window blinking on screen every time Era does a task
  (reported 2026-10-03). Nothing was wrong with the PowerShell calls
  themselves — the flash is Windows creating a console for them.

THE WINDOWS RULE, EXACTLY
  A console program needs a console. When its parent already has one, the
  child attaches to it silently. When the parent has NO console, Windows
  creates a brand-new one for the child — and that one has a WINDOW, which
  appears, then vanishes when the program exits. That blink is the bug.

  CREATE_NO_WINDOW says "give this process a console with no window". The
  process runs normally, output still pipes back, and children attach to
  that invisible console instead of making their own. So the flag suppresses
  the flash for the whole subtree, not just the one call.

  Hiding the window afterwards is NOT an equivalent fix: with Windows
  Terminal as the default console host (the Windows 11 default) the window
  belongs to another process, so ShowWindow(SW_HIDE) does nothing. The flag
  has to be set when the process is created. Recorded in era_supervisor.py
  (respawn_windowless) the first time this was learned, 2026-09-26.

  DETACHED_PROCESS is the trap: it also means "no console window", but it
  gets there by giving the process NO CONSOLE AT ALL — so every console
  helper it later spawns creates its own, with a window. It is the opposite
  of what Era wants. core/era_relaunch.py used it until 2026-10-03.

WHY A CONSTANT AND NOT A WRAPPER
  `creationflags=NO_WINDOW` reads at each call site as what it is, and keeps
  every existing kwarg (timeout, capture_output, encoding) under the plain
  subprocess API. tools/test_no_window.py walks the source and fails if a
  runtime call site forgets it, so the rule is enforced rather than
  remembered.

OFF WINDOWS
  Both constants are 0, and subprocess accepts creationflags=0 on POSIX.
  So the same call site is correct on Linux and macOS with no branch.
"""

from __future__ import annotations

import subprocess

# CREATE_NO_WINDOW (0x08000000) — a real console, no window. 0 off Windows.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# CREATE_NEW_PROCESS_GROUP (0x00000200) — Ctrl-C in our console does not reach
# the child. Used for processes meant to outlive the one that started them.
NEW_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def no_window(extra: int = 0) -> int:
    """NO_WINDOW combined with other creation flags (priority class, group)."""
    return NO_WINDOW | int(extra)
