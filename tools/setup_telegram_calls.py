"""Open local Telegram account setup using its isolated optional environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Delegate interactively without putting credentials in command arguments."""
    root = Path(__file__).resolve().parents[1]
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        print("Windows local app-data is unavailable.")
        return 2
    interpreter = Path(local) / "EraOne" / "telegram-calls" / ".venv" / "Scripts" / "python.exe"
    if not interpreter.is_file():
        print("The optional Telegram call environment is not installed.")
        return 2
    return subprocess.call([str(interpreter), "-m", "core.telegram_call_setup"], cwd=root)


if __name__ == "__main__":
    sys.exit(main())
