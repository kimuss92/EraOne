"""The key-file guard: every copy of config/api_keys.json is protected, the placeholder is not.

Only path STRINGS are used; the real key file is never opened. The hook is run as Claude Code
runs it (a subprocess fed the hook JSON on stdin), because that is the contract that matters.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "helpers" / "guard_protected_paths.py"

BLOCKED = [
    "config/api_keys.json",
    "config/api_keys.json.bak",
    "config/api_keys.json~",
    "config/api_keys_old.json",
    r"C:\Users\nesri\Desktop\api_keys.json.bak",
    "C:/Users/nesri/OneDrive/Documents/EraOne/config/../config/API_KEYS.JSON",
    ".env",
    ".env.local",
    "memory/long_term.json",
]
ALLOWED = ["config/api_keys.example.json", "config/llm.yaml", "core/api.py", "crew/router.py"]


def run(tool: str, path: str) -> int:
    key = "path" if tool == "Grep" else "file_path"
    payload = json.dumps({"tool_name": tool, "tool_input": {key: path}})
    done = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, timeout=30
    )
    return done.returncode


@pytest.mark.parametrize("tool", ["Read", "Grep", "Write", "Edit"])
@pytest.mark.parametrize("path", BLOCKED)
def test_every_copy_of_the_key_file_is_blocked_for_every_tool(tool, path):
    assert run(tool, path) == 2


@pytest.mark.parametrize("path", ALLOWED)
def test_the_placeholder_and_ordinary_files_stay_usable(path):
    assert run("Read", path) == 0


def test_unreadable_hook_input_never_blocks():
    done = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0


def test_the_guard_is_registered_for_read_and_grep_without_the_edit_hooks():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    group = next(g for g in settings["hooks"]["PreToolUse"] if g["matcher"] == "Read|Grep")
    commands = [h["command"] for h in group["hooks"]]
    assert commands and all("guard_protected_paths.py" in c for c in commands), (
        "the Read group runs the guard and nothing else (not the claude-flow pre-edit hook)"
    )
    deny = settings["permissions"]["deny"]
    for tool in ("Read", "Write", "Edit"):
        assert f"{tool}(./config/api_keys.json*)" in deny


@pytest.mark.parametrize(
    "path",
    ["config/api_keys.json", "config/api_keys.json.bak", "config/api_keys.old.json"],
)
def test_git_ignores_every_copy_but_keeps_the_example_tracked(path):
    def ignored(p: str) -> bool:
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", p], cwd=ROOT, capture_output=True, timeout=30
            ).returncode
            == 0
        )

    assert ignored(path)
    assert not ignored("config/api_keys.example.json")
