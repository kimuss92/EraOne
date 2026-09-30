#!/usr/bin/env python
"""PostToolUse formatter — auto-formats edited Python files and checks syntax.

Reads the Claude Code hook JSON from stdin. For a .py file that was just
written/edited it runs `ruff format` (best-effort, silent) then `py_compile`.
If the file no longer compiles, the syntax error is surfaced back to Claude
via stderr + exit 2 so it can be fixed immediately.
"""

import json
import os
import subprocess
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not path or not path.lower().endswith(".py") or not os.path.isfile(path):
        return 0

    # 1) Auto-format (best-effort; never blocks).
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", path],
        capture_output=True,
    )

    # 2) Syntax check — surface failures back to Claude.
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(f"Syntax error after edit in {path}:\n{result.stderr}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
