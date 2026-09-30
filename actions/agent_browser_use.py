"""Agent-browser CLI integration for Era One.

Delegates complex multi-step web tasks to the agent-browser CLI
(https://github.com/vercel-labs/agent-browser).

Connects to the user's existing browser via CDP --auto-connect.
Always opens a new tab — never touches existing Netflix/YouTube tabs.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

# vercel-labs/agent-browser has never had a run/exec/execute/start command: it is
# a per-action CLI (open, click, snapshot...) and its ONLY natural-language entry
# point is `chat <message>`. The old probe list could never succeed, which is why
# upgrading the package did not fix it. `-q` prints only the AI's text answer.
_CHAT_ARGS: list[str] = ["--auto-connect", "-q", "chat"]


class _NoGatewayKey(RuntimeError):
    """`chat` refused to start: no Vercel AI Gateway key configured."""


def _find_agent_browser() -> str:
    path = shutil.which("agent-browser") or shutil.which("npx")
    if not path:
        raise FileNotFoundError(
            "agent-browser CLI not found on PATH. Install it with: npm install -g agent-browser"
        )
    return path


async def _run_agent_browser(query: str, timeout: int = 120) -> str:
    exe = _find_agent_browser()
    if "npx" in exe:
        cmd = [exe, "agent-browser", *_CHAT_ARGS, query]
    else:
        cmd = [exe, *_CHAT_ARGS, query]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        raise TimeoutError(
            f"agent-browser timed out after {timeout}s. "
            "The task may still be running in your browser."
        ) from e

    result = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode == 0:
        return result or err or "Task completed (no output returned)."

    # `chat` is powered by the Vercel AI Gateway; without a key it refuses before
    # touching the browser. Say so plainly instead of blaming the install.
    if "AI_GATEWAY_API_KEY" in f"{result}\n{err}":
        raise _NoGatewayKey(
            "agent-browser's `chat` mode needs a Vercel AI Gateway key "
            "(AI_GATEWAY_API_KEY, https://vercel.com/ai-gateway)."
        )

    raise RuntimeError(
        f"agent-browser exited with code {proc.returncode}.\n{(err or result)[:500]}"
    )


def agent_browser(parameters: dict[str, Any] | None, **kwargs) -> str:
    query = (parameters or {}).get("query", "")
    if not query:
        return "Error: 'query' parameter is required."

    try:
        result = asyncio.run(_run_agent_browser(query))
        return result
    except _NoGatewayKey as e:
        # Degrade instead of failing: research-style queries (the scheduled
        # evening_movie_search) are fully answerable by Era's own web search.
        # Once the key is set, `chat` takes over again with no code change.
        from actions.web_search import web_search

        found = web_search({"query": query})
        return f"{found}\n\n(Answered via web_search — {e})"
    except FileNotFoundError as e:
        return f"Error: {e}"
    except TimeoutError as e:
        return f"Timeout: {e}"
    except RuntimeError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"
