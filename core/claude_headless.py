"""core/claude_headless.py — ask Claude Code a question without touching the GUI.

WHY THIS EXISTS ALONGSIDE core/claude_bridge.py, AND NOT INSTEAD OF IT
  The GUI bridge focuses the Claude window, pastes, presses Enter, then polls the
  session transcript because the app has no delivery confirmation. Every step is
  a way to fail: an unverified `ctrl+n`, the clipboard, a foreground check, a
  30 s locate timeout. `claude -p` removes all of it — the answer arrives on
  stdout.

  It is not a replacement, and the measurements are why (2026-09-29):

    headless, fresh      cache_creation 63,516 tokens   total_cost_usd 0.637
    headless, --resume   cache_read 63,516 + create 27,505   total_cost_usd 0.944

  Every invocation reloads the project context — CLAUDE.md, hooks, plugin skills,
  MCP servers — so a one-word question costs about **$0.64**, and `--resume` cost
  MORE rather than amortising it. `--bare` would strip that context but declares
  auth "strictly ANTHROPIC_API_KEY or apiKeyHelper", and this project runs on the
  subscription CLI with no API key.

  So the GUI path keeps two advantages that are not fragility: it pastes into a
  session that is already warm, and Karim can SEE it. This module is for the case
  where Era needs an ANSWER and nothing needs to be visible or approved.

CONTAINMENT — this is the part that must not be loosened
  core/safety_guard.py already says a message to Claude Code "is an instruction
  to an agent that can act on this machine OUTSIDE Era's gates", and
  core/claude_error_router.py sends on that path with no user confirmation. A
  headless agent is invisible, so it runs READ-ONLY:

    --permission-mode plan   no edits, no commands; it plans and answers
    --disallowedTools ...    the mutating and network tools denied by name

  Both, not either: plan mode is the policy, the denylist is the belt. Neither is
  configurable from a tool argument, because the model choosing its own
  containment is not containment.

TWO THINGS THE CLI DOES THAT BREAK NAIVE CODE, both measured
  * The default model alias FAILS: `[claude-code:unrecognized_model]
    {"model":"auto/best-coding"}` with `terminal_reason: "api_error"`. An
    explicit --model is required, so MODEL is pinned here.
  * It writes diagnostics to STDERR that corrupt a merged `2>&1` JSON parse.
    stdout and stderr are captured separately and only stdout is parsed.

DEGRADE, NEVER RAISE
  Every failure returns a Reply with ok=False and a reason Era can say out loud.
  The caller falls back to the GUI path, so an unavailable CLI costs a slower
  answer, never a dead end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pinned because the default alias returns api_error — see the module docstring.
MODEL = os.environ.get("ERA_CLAUDE_HEADLESS_MODEL", "claude-sonnet-5-5")
TIMEOUT_S = float(os.environ.get("ERA_CLAUDE_HEADLESS_TIMEOUT_S", "300"))

# Denied by name, on top of plan mode. Write/Edit/Bash are the ones that could
# act on the machine; WebFetch/WebSearch would let a question pull in text that
# then steers the same agent, which is the injection shape safety_guard warns
# about; Task would spawn subagents this containment does not cover.
DENIED_TOOLS = (
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "KillShell",
)

# A single-word answer is ~$0.64, so an accidental loop is expensive. Callers are
# expected to be gated already (user words + confirmation); this is the backstop.
MAX_PROMPT_CHARS = 30_000


@dataclass
class Reply:
    ok: bool
    text: str = ""
    reason: str = ""
    session_id: str = ""
    cost_usd: float = 0.0

    @property
    def spoken_failure(self) -> str:
        return f"I could not reach Claude Code without its window: {self.reason}"


def available() -> bool:
    """Is this transport usable right now? Probed, never assumed.

    NEVER UNDER PYTEST, and that is the important half. Every call costs about
    $0.64 and needs a login, so a suite that reached the real CLI would spend
    money on each run and fail wherever nobody is signed in. The gate caught this
    for real: routing `ask` here broke `test_tool_ask_then_read_reply`, which
    exercises the GUI path — and it broke it by trying to invoke `claude`. This
    repo has been burned by the same shape three times (tests reaching the live
    browser, the real vault, the real Era lock), and `core/claude_error_router.py`
    already refuses to send under pytest for the same reason.

    Tests that mean to exercise this patch `available`/`ask` directly.
    """
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if os.environ.get("ERA_CLAUDE_HEADLESS", "").strip() == "0":
        return False
    return shutil.which("claude") is not None


def _command(prompt: str) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        MODEL,
        "--output-format",
        "json",
        "--permission-mode",
        "plan",
        "--disallowedTools",
        " ".join(DENIED_TOOLS),
    ]


def ask(prompt: str, *, timeout_s: float | None = None) -> Reply:
    """Put one question to a contained, read-only Claude and return its answer.

    Never raises. A failure is a Reply with ok=False whose `reason` is safe to
    say out loud.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return Reply(False, reason="there was no question to ask")
    if len(prompt) > MAX_PROMPT_CHARS:
        return Reply(False, reason=f"the question is too long ({len(prompt)} characters)")
    if not available():
        return Reply(False, reason="the claude command is not on PATH")

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            _command(prompt),
            cwd=str(REPO_ROOT),
            capture_output=True,  # stdout and stderr kept APART on purpose
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s if timeout_s is not None else TIMEOUT_S,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return Reply(False, reason="it did not answer in time")
    except OSError as e:
        return Reply(False, reason=f"the command could not be started ({e})")

    if proc.returncode != 0:
        detail = _first_line(proc.stderr) or f"exit code {proc.returncode}"
        return Reply(False, reason=f"it exited with an error ({detail})")
    return _parse(proc.stdout, proc.stderr)


def _first_line(text: str, limit: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _parse(stdout: str, stderr: str) -> Reply:
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        # Diagnostics on stdout, or a truncated stream. Say so rather than
        # handing the model a fragment of JSON as if it were an answer.
        return Reply(
            False, reason=f"its answer was not readable ({_first_line(stdout) or 'empty'})"
        )
    if not isinstance(payload, dict):
        return Reply(False, reason="its answer was not in the expected shape")

    session_id = str(payload.get("session_id") or "")
    cost = payload.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0

    if payload.get("is_error") or payload.get("terminal_reason") == "api_error":
        reason = _first_line(str(payload.get("result") or "")) or _first_line(stderr) or "API error"
        return Reply(False, reason=reason, session_id=session_id, cost_usd=cost_usd)

    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        return Reply(
            False, reason="it answered with nothing", session_id=session_id, cost_usd=cost_usd
        )
    return Reply(True, text=text.strip(), session_id=session_id, cost_usd=cost_usd)
