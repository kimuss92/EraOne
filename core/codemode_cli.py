"""Pinned, tool-free Claude subscription sessions; no gateway or account fallback."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from core.llm.adapters.claude_cli import GATEWAY_ENV_VARS, classify_cli_result
from core.llm.types import LlmError
from core.win_subprocess import NO_WINDOW

MAX_OUTPUT = 500_000


def subscription_env(profile: str) -> dict:
    env = dict(os.environ)
    for name in (
        *GATEWAY_ENV_VARS,
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SIMPLE",
    ):
        env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = profile
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


async def stop_tree(proc) -> None:
    if proc.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
            try:
                await asyncio.wait_for(killer.wait(), 5)
            except TimeoutError:
                killer.kill()
                await killer.wait()
        else:
            import signal

            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    await proc.wait()


def reply_prefix(raw: str) -> str:
    """Decode only complete JSON string characters; never speak candidate source."""
    match = re.match(r'^\s*\{\s*"reply"\s*:\s*"', raw)
    if not match:
        return ""
    out, index = [], match.end()
    while index < len(raw):
        char = raw[index]
        if char == '"':
            break
        if char == "\\":
            if index + 1 >= len(raw):
                break
            count = 6 if raw[index + 1] == "u" else 2
            if index + count > len(raw):
                break
            try:
                char = json.loads('"' + raw[index : index + count] + '"')
            except ValueError:
                break
            # Wait for a low surrogate before emitting the pair.
            if len(char) == 1 and 0xD800 <= ord(char) <= 0xDBFF:
                if index + 12 > len(raw):
                    break
                try:
                    char = json.loads('"' + raw[index : index + 12] + '"')
                except ValueError:
                    break
                count = 12
            index += count
        else:
            index += 1
        out.append(char)
        if len(out) >= 10000:
            break
    return "".join(out)


class ClaudeSession:
    def __init__(self, profile: str, session_id: str, directory: Path, *, model="sonnet"):
        self.profile, self.session_id, self.directory = profile, session_id, directory
        self.model, self.resumable = model, False
        self.binary = shutil.which(os.getenv("CLAUDE_BIN", "claude"))
        if not self.binary:
            raise LlmError("unsupported", "Claude CLI is missing", provider="codemode")

    async def _spawn(self, arguments):
        self.directory.mkdir(parents=True, exist_ok=True)
        return await asyncio.create_subprocess_exec(
            self.binary,
            *arguments,
            cwd=self.directory,
            env=subscription_env(self.profile),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_OUTPUT + 1,
            creationflags=NO_WINDOW,
            **({"start_new_session": True} if os.name != "nt" else {}),
        )

    async def authenticate(self) -> None:
        proc = await self._spawn(["auth", "status", "--json"])
        try:
            # Status is local and contains metadata, never credential contents.
            raw = await asyncio.wait_for(proc.stdout.read(20001), 10)
            if len(raw) > 20000:
                raise ValueError("Oversized authentication status")
            await asyncio.wait_for(proc.wait(), 5)
            status = json.loads(raw)
            if (
                proc.returncode
                or not status.get("loggedIn")
                or status.get("authMethod") != "claude.ai"
            ):
                raise ValueError("Selected Claude profile needs a Claude subscription login")
            actual = status.get("configDirectory")
            if actual and Path(actual).resolve() != Path(self.profile).resolve():
                raise ValueError("Claude profile mismatch; open a new CodeMode session")
        except (ValueError, TimeoutError) as exc:
            raise LlmError("auth", str(exc), provider="codemode") from exc
        finally:
            await stop_tree(proc)

    def command(self):
        return [
            "-p",
            "--safe-mode",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--allowedTools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            '{"disableAllHooks":true}',
            "--model",
            self.model,
            *(
                ["--resume", self.session_id]
                if self.resumable
                else ["--session-id", self.session_id]
            ),
        ]

    async def generate(self, prompt: str, on_text, *, wall_s=180.0, idle_s=45.0):
        if len(prompt.encode("utf-8")) > 150000:
            raise LlmError(
                "context_overflow", "CodeMode context exceeds its limit", provider="codemode"
            )
        await self.authenticate()
        proc = await self._spawn(self.command())
        deadline, total, raw, spoken, result = time.monotonic() + wall_s, 0, "", "", None

        async def errors():
            count = 0
            while chunk := await proc.stderr.read(4096):
                count += len(chunk)
                if count > 100000:
                    raise ValueError("Oversized Claude error stream")

        drain = asyncio.create_task(errors())
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), 10)
            proc.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("CodeMode generation deadline reached")
                line = await asyncio.wait_for(proc.stdout.readline(), min(idle_s, remaining))
                if not line:
                    break
                total += len(line)
                if total > MAX_OUTPUT:
                    raise ValueError("Oversized Claude response")
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError("Invalid Claude stream frame")
                sid = frame.get("session_id")
                if sid and sid != self.session_id:
                    raise ValueError("Claude session mismatch")
                if sid == self.session_id:
                    self.resumable = True
                kind = frame.get("type")
                if kind == "stream_event":
                    event = frame.get("event", {})
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        raw += delta.get("text", "")
                elif kind == "assistant" and not raw:
                    raw = "".join(
                        item.get("text", "")
                        for item in frame.get("message", {}).get("content", [])
                        if item.get("type") == "text"
                    )
                elif kind == "result":
                    result = frame
                    failure = classify_cli_result(frame)
                    if failure:
                        raise LlmError(*failure, provider="codemode")
                    if not raw:
                        raw = frame.get("result", "")
                text = reply_prefix(raw)
                if not text.startswith(spoken):
                    raise ValueError("Claude revised already streamed text")
                if len(text) > len(spoken):
                    await on_text(text[len(spoken) :])
                    spoken = text
            await asyncio.wait_for(proc.wait(), min(5, max(0.1, deadline - time.monotonic())))
            await drain
            if proc.returncode or result is None:
                raise ValueError("Claude ended without a successful final result")
            answer = json.loads(raw)
            if (
                not isinstance(answer, dict)
                or not isinstance(answer.get("reply"), str)
                or len(answer["reply"]) > 10000
                or (
                    answer.get("candidate") is not None
                    and not isinstance(answer["candidate"], dict)
                )
                or (
                    answer.get("read_requests") is not None
                    and (
                        not isinstance(answer["read_requests"], list)
                        or len(answer["read_requests"]) > 4
                    )
                )
                or (answer.get("read_requests") and answer.get("candidate") is not None)
            ):
                raise ValueError("Claude returned an invalid CodeMode answer")
            if answer["reply"] != spoken:
                if not answer["reply"].startswith(spoken):
                    raise ValueError("Claude final answer disagrees with streamed text")
                await on_text(answer["reply"][len(spoken) :])
            self.resumable = True
            return answer
        except TimeoutError as exc:
            raise LlmError(
                "timeout", "CodeMode timed out; retry explicitly", provider="codemode"
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise LlmError("invalid_response", str(exc), provider="codemode") from exc
        finally:
            await stop_tree(proc)
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
