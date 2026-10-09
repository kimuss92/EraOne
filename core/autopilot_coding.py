"""Bounded, cancellable Claude Code investigation for the Autopilot lane."""

from __future__ import annotations

import asyncio
import os

from core import claude_headless
from core.llm.registry import resolve
from core.llm.spend import from_registry
from core.utils import env_float


async def plan(text: str) -> claude_headless.Reply:
    if not claude_headless.available():
        return claude_headless.Reply(False, reason="Claude Code is unavailable or disabled")
    entry = resolve("era.deep")
    if entry.provider != "claude_cli":
        return claude_headless.Reply(False, reason="era.deep must select Claude Code")
    ledger = from_registry()
    affordable, _ = ledger.can_afford(1.0)
    if not affordable:
        return claude_headless.Reply(False, reason="the coding budget is exhausted")
    # --tools restricts availability; --allowedTools merely suppresses prompts.
    # Disable MCP discovery so a configured external tool cannot enter this lane.
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        os.getenv("ERA_AUTOPILOT_CODING_MODEL", entry.model),
        "--permission-mode",
        "plan",
        "--tools",
        "Read,Grep,Glob",
        "--allowedTools",
        "Read,Grep,Glob",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--max-turns",
        "8",
        "--max-budget-usd",
        "1.0",
    ]
    prompt = (
        "Investigate this Era One coding request in the current repository. "
        "Read source only. Do not edit files, execute code, invoke other agents, "
        "or read credentials, config/api_keys.json, memory, or model weights. "
        "Return findings and a concrete repair plan with repository-relative paths. "
        "Do not claim any fixes have been applied. Request:\n" + text
    )
    env = dict(os.environ)
    # An explicit terminal profile takes precedence over the shipped machine paths.
    config_dir = env.get("CLAUDE_CONFIG_DIR") or entry.provider_config.get("config_dir")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    process = None
    communication = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(claude_headless.REPO_ROOT),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communication = asyncio.create_task(process.communicate(prompt.encode("utf-8")))
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communication),
            timeout=env_float("ERA_AUTOPILOT_CODING_TIMEOUT_S", 300, minimum=1, maximum=600),
        )
        if process.returncode != 0:
            return claude_headless.Reply(False, reason="Claude Code exited unsuccessfully")
        reply = claude_headless._parse(
            stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        )
        if reply.ok:
            ledger.record(reply.cost_usd, alias="era.deep")
        return reply
    except TimeoutError:
        return claude_headless.Reply(False, reason="Claude Code exceeded its time limit")
    except OSError:
        return claude_headless.Reply(False, reason="Claude Code could not be started")
    finally:
        # This also runs on CancelledError: abort/shutdown cannot leave the CLI live.
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if communication is not None and not communication.done():
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)
