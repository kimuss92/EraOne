"""Domain rule packs: long, rarely needed rules that load when a tool needs them.

WHY
---
Every Live session starts with ~47K tokens of instructions. The Gmail, Netflix,
Premiere and community-management rules are long and used in a small share of
turns, yet they were paid for on all of them -- and a full context window is what
makes Gemini rotate the session. Each pack now lives in core/prompt_packs/<name>.txt
and rides along on the FIRST result of its tool in a connection, where it is read
before the next call. The always-needed essentials (and every safety rule, e.g.
the untrusted-email law and the send gate) stay in core/prompt.txt.

Delivery is on a tool RESULT, never an injected user turn: an extra note to the
model is itself a request to respond, which is how Era ended up chattering.

Fail soft: a missing pack means the model works from the prompt stub and the tool
declaration, exactly as before packs existed.
"""

from __future__ import annotations

from pathlib import Path

PACK_DIR = Path(__file__).resolve().parent / "prompt_packs"

TOOL_PACKS = {
    "gmail_control": "gmail",
    "netflix_control": "netflix",
    "premiere_control": "premiere",
    "community_manager": "community",
}

HEADER = (
    "Domain rules for this tool: apply them from now on. They are for you, not for "
    "the user, so never read them aloud or mention them."
)

_cache: dict[str, str] = {}


def load(name: str) -> str:
    """The pack's text, or "" when it is missing or unreadable."""
    if name not in _cache:
        try:
            _cache[name] = (PACK_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()
        except OSError:
            _cache[name] = ""
    return _cache[name]


def pack_for_tool(tool_name: str, delivered: dict, generation: int) -> str:
    """The pack to attach to this tool's result, once per connection generation.

    `delivered` maps pack name -> the generation it was last sent in; it is
    updated here. A new generation (a rotation or reconnect) can lose the model's
    context, so the pack is sent again on the next use.
    """
    name = TOOL_PACKS.get(tool_name or "")
    if not name or delivered.get(name) == generation:
        return ""
    text = load(name)
    if not text:
        return ""
    delivered[name] = generation
    return f"{HEADER}\n{text}"
