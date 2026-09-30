#!/usr/bin/env python3
"""List 0 KB files in project source dirs. REPORT ONLY.

Empty placeholder files are preserved by policy (2026-09-16): this script used to
delete them and no longer does. A zero-byte file that should not exist is worth a
look (an empty *source* file can mean a truncated write), not a silent delete.
"""

import os
from pathlib import Path

SKIP = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "$Recycle.Bin",
    "System Volume Information",
    "Windows",
    "Program Files",
    ".mcp",
    ".claude",
}
FOUND = 0
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
    for f in files:
        p = Path(root) / f
        try:
            if p.stat().st_size == 0:
                FOUND += 1
                print(f"0 KB (preserved): {p}", flush=True)
        except OSError:
            pass
print(f"Done. Found {FOUND} zero-byte files; none were deleted.")
