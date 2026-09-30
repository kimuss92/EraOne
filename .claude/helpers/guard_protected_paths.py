#!/usr/bin/env python
"""PreToolUse guard — blocks Write/Edit to secrets and machine-written state.

Reads the Claude Code hook JSON from stdin. Exits 2 (with a reason on stderr)
to block the edit when the target path is a protected file; exits 0 otherwise.

Protected:
  - config/api_keys.json      (live API keys)
  - config/media_host.json    (Cloudinary/Bunny credentials, if stored there)
  - any .env / .env.* file    (secrets)
  - memory/long_term.json     (agent-written long-term memory)
  - memory/runtime_intent.json
  - memory/feedback_memory.jsonl
"""

import json
import os
import sys

PROTECTED_SUFFIXES = (
    "config/api_keys.json",
    "config/media_host.json",
    "memory/long_term.json",
    "memory/runtime_intent.json",
    "memory/feedback_memory.jsonl",
)


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower().rstrip("/")


def _is_protected(path: str) -> bool:
    np = _norm(path)
    base = os.path.basename(np)
    if base == ".env" or base.startswith(".env."):
        return True
    return any(np.endswith(s) for s in PROTECTED_SUFFIXES)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if path and _is_protected(path):
        sys.stderr.write(
            f"BLOCKED: '{path}' is a protected secrets/state file. "
            "Edit it manually outside Claude if a change is truly needed.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
