"""Create the ERA ONE shortcuts (Desktop + Start Menu).

    python scripts/install_shortcuts.py              both locations
    python scripts/install_shortcuts.py --no-start-menu

Run it with the interpreter Era runs on: the shortcuts point at that
interpreter's pythonw.exe (silent) and python.exe (Debug). Idempotent —
re-running overwrites the same shortcuts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.hud import shortcuts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-desktop", action="store_true")
    ap.add_argument("--no-start-menu", action="store_true")
    opts = ap.parse_args()
    made = shortcuts.install(desktop=not opts.no_desktop, start_menu=not opts.no_start_menu)
    for p in made:
        print(f"created {p}")
    print(f"target  {shortcuts.pythonw_path()} {shortcuts.launcher_args()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
