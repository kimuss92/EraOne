"""Claude as Era's SPOKEN brain, over the subscription `claude -p` CLI.

WHY THIS EXISTS (2026-10-03)
  Era's voice is one Gemini Live preview websocket. On 2026-10-03 it logged 16
  disconnects by 16:10 (1011 "service unavailable" / "internal error") and, in
  Karim's screen recording, sat connected and listening while never answering.
  The local loop (app/era_live/local_fallback.py) already hears, thinks and
  speaks without Gemini — but its brain was an Ollama model on the GPU. This is
  a brain for that loop that runs on Karim's Claude SUBSCRIPTION: no API key,
  no VRAM (he needs the GPU free for Adobe), and two accounts to fail over to.

WHY IT IS NOT JUST ClaudeCliProvider
  1. LocalBrain hands the provider NATIVE tool definitions, which
     ClaudeCliProvider refuses on purpose (see its docstring: tools inside the
     CLI would execute OUTSIDE Era's safety gates). This adapter never enables
     them. It renders Era's tool catalog as TEXT and asks for a JSON-only reply;
     LocalBrain parses that with core/local_tool_calls.parse_text_tool_calls,
     which drops unknown names and confirmation tools, and runs it through the
     same `_execute_tool` gate Gemini's calls use. The CLI has no tool surface
     at all (`--tools ""` + `--allowedTools ""`).
  2. LocalBrain passes conversation history in an Ollama-specific side channel
     (`provider_options["ollama"]["extra_messages"]`). ClaudeCliProvider would
     ignore it and Claude would answer every turn with no memory of the last.
  3. Cost and latency. Measured 2026-10-03 from an empty cwd, Haiku:

        plain `claude -p`                       4.1 s   70,508 tokens loaded
        + --strict-mcp-config etc.              4.7 s   39,624 tokens loaded
        + --tools "" --setting-sources ""
          + --system-prompt (this adapter)      4.3 s      740 tokens, $0.001

     The default Claude Code harness (plugins, skills, hooks, MCP) is what costs
     tokens; none of it is wanted for answering one spoken sentence.

Everything else — scratch cwd, account failover, error classification,
telemetry — is inherited from ClaudeCliProvider unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Iterator
from typing import Any

from core.llm.adapters.claude_cli import GATEWAY_ENV_VARS, ClaudeCliProvider
from core.llm.registry import ModelEntry
from core.llm.types import LlmError, LlmMessage, LlmRequest, StreamEvent

# Replaces Claude Code's own system prompt (which is what loads tokens). Single
# line, plain ASCII: it is a command-line argument on Windows.
_CLI_SYSTEM = (
    "You are the reasoning engine inside a voice assistant. "
    "Follow the instructions in the user message exactly."
)

# Flags that strip the Claude Code harness. Each was measured; see the docstring.
_LEAN_FLAGS = (
    "--tools",
    "",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--system-prompt",
    _CLI_SYSTEM,
)

_TOOL_PROTOCOL = (
    "TOOL CALLS. The tools you may call are listed below as name(arguments) - purpose. "
    "To call one, your ENTIRE reply must be a single JSON object and nothing else, in "
    'this exact shape: {"name": "<tool>", "arguments": {...}}. No prose before or after '
    "it, no markdown fences. To call several, a JSON array of such objects. Do NOT say "
    "the action is done in the same reply: you will be shown the tool result next and "
    "may then answer in words. If no tool is needed, reply in plain spoken sentences "
    "and never output JSON."
)


def _arg_summary(parameters: dict[str, Any]) -> str:
    props = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])
    return ", ".join(f"{k}{'*' if k in required else ''}" for k in props)


def render_tool_catalog(tools: tuple[dict[str, Any], ...]) -> str:
    """Era's tools as short text lines. `*` marks a required argument."""
    lines = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        desc = " ".join(str(fn.get("description") or "").split())
        lines.append(f"- {fn['name']}({_arg_summary(fn.get('parameters') or {})}) - {desc}")
    return "\n".join(lines)


def render_history(messages: list[dict[str, Any]]) -> str:
    """LocalBrain's Ollama-dialect turns as a plain transcript, tool calls and
    results included — dropping the calls is what taught a model to fake 'Done'."""
    out = []
    for m in messages:
        role, content = m.get("role"), str(m.get("content") or "").strip()
        if role == "user":
            out.append(f"User: {content}")
        elif role == "assistant":
            calls = [
                {"name": c["function"].get("name"), "arguments": c["function"].get("arguments")}
                for c in (m.get("tool_calls") or [])
                if isinstance(c, dict) and isinstance(c.get("function"), dict)
            ]
            if calls:
                out.append(f"You (tool call): {json.dumps(calls, default=str)}")
            elif content:
                out.append(f"You: {content}")
        elif role == "tool":
            out.append(f"Tool result ({m.get('tool_name', '?')}): {content}")
    return "\n".join(out)


def trim_to_tool_json(text: str) -> str:
    """If the reply OPENS with a tool-call JSON, drop whatever follows it.

    Observed live 2026-10-03: Haiku answered "pause the music" with
    `{"name": "spotify_control", ...}` and then, in the same reply, "Music paused,
    sir." Era's parser (core/local_tool_calls.parse_text_tool_calls) accepts only
    a reply that is NOTHING but the call — a deliberate rule so that JSON quoted
    from an email cannot run — so the call was refused and Era said she could not
    do it. The trailing words are also a claim made BEFORE the tool ran, so they
    must not be spoken either way.

    The rule stays strict: only a reply that STARTS with JSON is touched, so
    prose that merely contains or quotes JSON still reaches the parser unchanged
    and is still refused.
    """
    stripped = (text or "").strip()
    body = stripped
    for fence in ("```json", "```"):
        if body.startswith(fence):
            body = body[len(fence) :].lstrip()
            break
    if not body.startswith(("{", "[")):
        return text
    try:
        _obj, end = json.JSONDecoder().raw_decode(body)
    except ValueError:
        return text
    return body[:end]


class ClaudeVoiceProvider(ClaudeCliProvider):
    """`claude -p` as a text-protocol brain for the local voice loop."""

    # Registry aliases such as era.cheap belong to provider "claude_cli"; this
    # class is a behaviour variant of it, not a new provider.
    name = "claude_cli"

    def _build_command(self, entry: ModelEntry) -> list[str]:
        return [*super()._build_command(entry), *_LEAN_FLAGS]

    def _build_env(self, entry: ModelEntry, account: Any = None) -> dict[str, str]:
        """The subscription login, not the gateway: a user environment that points
        Claude Code at OmniRoute makes `claude -p` exit 1 with no output (60 s
        timeout before this strip, 4-7 s after). ERA_CLAUDE_KEEP_API_ENV=1 opts out."""
        env = super()._build_env(entry, account)
        if os.environ.get("ERA_CLAUDE_KEEP_API_ENV", "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            for name in GATEWAY_ENV_VARS:
                env.pop(name, None)
        return env

    @staticmethod
    def _to_text_request(request: LlmRequest) -> LlmRequest:
        """One tool-free request carrying everything as text."""
        opts = (request.provider_options or {}).get("ollama") or {}
        history = render_history(list(opts.get("extra_messages") or []))
        # LocalBrain carries the conversation in the side channel above, but any
        # other caller uses `messages`. Dropping them silently meant a request
        # like "reply with exactly: ok" arrived as an empty prompt.
        plain = "\n".join(
            f"{'User' if m.role == 'user' else 'You'}: {m.as_text}"
            for m in request.messages
            if m.role != "system" and m.as_text
        )
        history = "\n".join(p for p in (history, plain) if p)
        parts = [request.system_text()]
        catalog = render_tool_catalog(request.tools)
        if catalog:
            parts += [_TOOL_PROTOCOL, "TOOLS:\n" + catalog]
        parts += [
            "CONVERSATION SO FAR:\n" + history if history else "",
            "Write your next reply as the assistant now.",
        ]
        prompt = "\n\n".join(p for p in parts if p).strip()
        return dataclasses.replace(
            request,
            system=(),
            tools=(),
            messages=(LlmMessage.text("user", prompt),),
            provider_options={},
        )

    def generate(self, request: LlmRequest):  # noqa: ANN201 - matches the base
        response = super().generate(self._to_text_request(request))
        response.text = trim_to_tool_json(response.text)
        return response

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        """Failures become an ERROR EVENT, not an exception.

        LocalBrain pumps this generator on a worker thread and only understands
        error events. A raised LlmError there was an uncaught thread exception
        that ended the stream with no words — Era heard the user and said
        nothing (2026-10-03, the CLI exited 1 because of an inherited API key).
        """
        try:
            yield from super().stream(request)
        except LlmError as exc:
            yield StreamEvent(kind="error", error=exc)
        except Exception as exc:  # a provider bug must still be a spoken failure
            yield StreamEvent(
                kind="error",
                error=LlmError("transient", f"{type(exc).__name__}: {exc}", provider=self.name),
            )
