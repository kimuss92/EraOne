"""actions/premiere_control.py — voice/text bridge to the Adobe Premiere Pro MCP server.

Flow: transcribed command -> OpenAI tool-calling loop -> Premiere MCP tools -> spoken ack.

Hardening (vs. the first version):
  - Paths and model are config-driven (config/api_keys.json) with the old defaults,
    and the MCP server script is checked BEFORE launching node.
  - The tool loop is bounded (MAX_TOOL_ROUNDS) and the whole run has a hard timeout,
    so a stuck MCP server can never hang Era's background task forever.
  - Conversation history is trimmed at user-message boundaries so it can't grow
    unbounded across a long editing session (and never orphans a tool result).
"""

import asyncio
import json
import os
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from openai import AsyncOpenAI

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

_DEFAULT_SERVER = r"D:\AI_Projects\Adobe_Premiere_Pro_MCP\dist\index.js"
_DEFAULT_TEMP_DIR = r"D:\temp\premiere-mcp-bridge"
_DEFAULT_MODEL = "gpt-4o-mini"
MAX_TOOL_ROUNDS = 8  # LLM->tools->LLM cycles per command before we stop
MAX_HISTORY_ENTRIES = 30  # trim threshold; trimming only cuts at a user-message boundary
RUN_TIMEOUT_S = 180  # hard cap for one whole voice command

# Multi-turn memory so "now make it black and white" works after "cut clip on V1".
conversation_history: list = []


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _trim_history() -> None:
    """Drop the oldest turns once the history is too long. Only cut at a 'user'
    entry so an assistant tool_calls message is never separated from its tool
    results (OpenAI rejects such a sequence)."""
    global conversation_history
    if len(conversation_history) <= MAX_HISTORY_ENTRIES:
        return
    h = conversation_history[-MAX_HISTORY_ENTRIES:]
    while h and not (isinstance(h[0], dict) and h[0].get("role") == "user"):
        h.pop(0)
    conversation_history = h


async def execute_premiere_voice_command(user_voice_text, era_instance=None):
    """Run one natural-language Premiere editing command end-to-end.
    Never raises — always returns a human-readable result string."""
    if not _MCP_AVAILABLE:
        return "Premiere voice bridge is not initialized or missing dependencies."

    config = _load_config()
    api_key = config.get("OPENAI_API_KEY")
    if not api_key:
        return "API Key missing."

    server_script = config.get("premiere_mcp_server", _DEFAULT_SERVER)
    if not Path(server_script).exists():
        return (
            f"Premiere MCP server not found at {server_script}. "
            "Set 'premiere_mcp_server' in config/api_keys.json to the dist/index.js path."
        )

    try:
        return await asyncio.wait_for(
            _run(user_voice_text, config, api_key, server_script, era_instance),
            timeout=float(config.get("premiere_timeout_s", RUN_TIMEOUT_S)),
        )
    except TimeoutError:
        msg = "[Premiere Control Error] timed out waiting for Premiere — is it running?"
        print(msg)
        return msg
    except Exception as e:
        msg = f"[Premiere Control Error] {e}"
        print(msg)
        return msg


async def _run(user_voice_text, config, api_key, server_script, era_instance):
    openai_client = AsyncOpenAI(api_key=api_key)
    model = config.get("premiere_model", _DEFAULT_MODEL)
    temp_dir = config.get("premiere_temp_dir", _DEFAULT_TEMP_DIR)

    server_params = StdioServerParameters(
        command="node",
        args=[server_script],
        env={**os.environ, "PREMIERE_TEMP_DIR": temp_dir},
    )

    _trim_history()
    conversation_history.append({"role": "user", "content": user_voice_text})

    # Context managers keep the MCP transport inside one task scope (avoids
    # anyio TaskGroup cross-contamination errors on teardown).
    async with stdio_client(server_params) as (transport_read, transport_write):
        async with ClientSession(transport_read, transport_write) as mcp_session:
            await mcp_session.initialize()

            tools_result = await mcp_session.list_tools()
            formatted_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema,
                    },
                }
                for t in tools_result.tools
            ]

            response = await openai_client.chat.completions.create(
                model=model,
                messages=conversation_history,
                tools=formatted_tools,
            )

            rounds = 0
            while response.choices[0].message.tool_calls:
                rounds += 1
                if rounds > MAX_TOOL_ROUNDS:
                    print(f"[Premiere] stopped after {MAX_TOOL_ROUNDS} tool rounds (loop guard)")
                    break
                assistant_message = response.choices[0].message
                conversation_history.append(assistant_message)

                for tool_call in assistant_message.tool_calls:
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    print(f"\n 🎬 [Sending to Premiere: {tool_call.function.name}]")
                    print(f"    ↳ Args: {tool_args}")

                    tool_result = await mcp_session.call_tool(tool_call.function.name, tool_args)

                    if tool_result.content and len(tool_result.content) > 0:
                        result_text = tool_result.content[0].text
                        is_error = getattr(tool_result, "isError", False)
                    else:
                        result_text = "Success"
                        is_error = False

                    if is_error or "error" in result_text.lower() or "false" in result_text.lower():
                        print(f" ❌ [Adobe Rejected]: {result_text}")
                    else:
                        print(f" ✅ [Adobe Accepted]: {result_text[:150]}...")

                    conversation_history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": result_text,
                        }
                    )

                response = await openai_client.chat.completions.create(
                    model=model,
                    messages=conversation_history,
                    tools=formatted_tools,
                )

            final_message = response.choices[0].message
            conversation_history.append(final_message)

            if era_instance:
                era_instance.speak("Timeline updated, sir.")

            try:
                main_module = __import__("main")
                await main_module.send_era_notification_async(
                    "Premiere Voice Automation",
                    f"Executed: {user_voice_text}",
                )
            except Exception:
                pass

            return final_message.content
