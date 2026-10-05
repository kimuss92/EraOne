#!/usr/bin/env python
"""PreToolUse guard — blocks Read/Grep/Write/Edit on secrets and machine-written state.

Reads the Claude Code hook JSON from stdin. Exits 2 (with a reason on stderr)
to block the call when the target path is a protected file; exits 0 otherwise.

Protected:
  - config/api_keys.json      (live API keys) and ANY copy of it: every file whose name
                              starts with `api_keys` (api_keys.json.bak, api_keys.json~, ...)
                              except the tracked placeholder api_keys.example.json
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
    # A copy of the key file holds the same secrets under a name no exact match would catch.
    if base.startswith("api_keys") and base != "api_keys.example.json":
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
            "Claude never reads or edits it; do that manually outside Claude if truly needed.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
