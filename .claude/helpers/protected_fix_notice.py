#!/usr/bin/env python
"""PreToolUse notice — shows the protected-fix rules for a file about to be edited.

Reads the Claude Code hook JSON from stdin. When the target file is covered by an
entry in docs/PROTECTED_FIXES.md, it returns that entry's rule and guard tests as
additionalContext so the agent sees them BEFORE changing the file.

It NEVER blocks: every path exits 0, including a missing registry, a parse error
or an unexpected exception. (A hook that exits 2 on its own failure blocks every
edit in the repo — see memory note era-hooks-run-through-git-bash.)
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not path:
            return 0
        # CLAUDE_PROJECT_DIR can arrive as a Git Bash path (/c/Users/...) that
        # Windows Python cannot open, so fall back to this file's own location.
        candidates = [os.environ.get("CLAUDE_PROJECT_DIR"), Path(__file__).resolve().parents[2]]
        root = next(
            (
                Path(c)
                for c in candidates
                if c and (Path(c) / "docs" / "PROTECTED_FIXES.md").is_file()
            ),
            None,
        )
        if root is None:
            return 0
        sys.path.insert(0, str(root))
        from tools import protected_fixes

        hits = protected_fixes.for_files([path])
        if not hits:
            return 0
        text = (
            "PROTECTED FIX — this file carries fixes the owner does not want broken again "
            "(docs/PROTECTED_FIXES.md). Your edit must keep every RULE below true, must not "
            "delete/skip/weaken the guard tests, and must be checked with "
            "`python tools/protected_fixes.py run` (or era_gate) before you report done.\n\n"
            + "\n\n".join(e.describe() for e in hits)
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": text,
                    }
                }
            )
        )
    except Exception as exc:  # advisory only: never block an edit because the notice failed
        sys.stderr.write(f"[protected_fix_notice] skipped: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
