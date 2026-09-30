"""Which of the local model's tool calls may run — the security-sensitive half
of core/local_brain.py, kept separate so it can be read and tested on its own.

Everything here answers one question: did the MODEL decide to act, and is it
allowed to? Text that merely contains JSON, a call to a tool that does not
exist, and a model approving its own risky action are all refused. See the
2026-09-23 security review notes in docs/LOCAL_FALLBACK.md.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

_ASK_YES = "That needs your confirmation, sir. Shall I proceed?"


# Some local models (qwen2.5-coder among them — measured 2026-09-23) answer a
# tool request with the call written out as JSON TEXT instead of Ollama's
# structured tool_calls. Such text is never spoken (contains_tool_text holds it
# back), and it is only EXECUTED when the whole reply is nothing but the call.
_TOOLISH_START = ("{", "[", "```", "<tool_call>")
_FENCE = re.compile(r"```(?:json)?|</?tool_call>", re.IGNORECASE)

# Tools that answer a pending confirmation. The model must never be the one to
# say yes: these run only as the sole call of a turn's first pass, i.e. as the
# direct response to a fresh user utterance (the fallback mixin also checks
# that utterance was an approval), and never via the text channel.
CONFIRM_TOOLS = frozenset({"confirm_pending_confirmation", "cancel_pending_confirmation"})


def contains_tool_text(text: str) -> bool:
    """True once the reply shows JSON anywhere. Such a reply is never spoken —
    whether it is also executed is parse_text_tool_calls' much stricter call."""
    return (
        text.lstrip().startswith(_TOOLISH_START)
        or '{"' in text
        or "{'" in text
        or "<tool_call>" in text
    )


def parse_text_tool_calls(text: str, known: set[str]) -> list[Any]:
    """JSON-text tool calls -> ToolCall list, or [] unless the reply is ONLY
    tool-call JSON (fences allowed, no prose around it).

    Security review 2026-09-23: accepting JSON anywhere in a reply let a model
    that QUOTES a tool result (an attacker's email containing
    {"name": "file_controller", ...}) execute it silently. A structured call is
    the model deciding to act; quoted text is not. Unknown names and the
    confirmation tools are dropped.
    """
    from core.llm.types import ToolCall

    if not text.lstrip().startswith(_TOOLISH_START):
        return []
    body = _FENCE.sub(" ", text).strip()
    decoder = json.JSONDecoder()
    found: list[Any] = []
    i = 0
    while i < len(body):
        if body[i].isspace() or body[i] == ",":
            i += 1
            continue
        try:
            obj, i = decoder.raw_decode(body, i)
        except json.JSONDecodeError:
            return []  # prose (or junk) around the JSON: not a pure call
        found.extend(obj if isinstance(obj, list) else [obj])
    calls = []
    for obj in found:
        if not isinstance(obj, dict):
            continue
        fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        name = str(fn.get("name") or "").strip()
        args = fn.get("arguments", fn.get("parameters", {})) or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name in known and name not in CONFIRM_TOOLS and isinstance(args, dict):
            calls.append(ToolCall(id=f"local-{uuid.uuid4().hex[:10]}", name=name, args=args))
    return calls


def admit_calls(calls: list[Any], known: set[str], *, fresh: bool) -> tuple[list[Any], list[Any]]:
    """(admitted, refused). `fresh` = no tool result has been fed back this turn.

    Unknown names are refused (the structured path is filtered like the text
    one). A confirmation tool is admitted only when fresh AND alone: a pass
    that both requests a risky action and confirms it, or a later round that
    confirms what an earlier round armed, is the model approving itself.
    """
    admitted, refused = [], []
    for c in calls:
        if c.name not in known:
            refused.append(c)
        elif c.name in CONFIRM_TOOLS and not (fresh and len(calls) == 1):
            refused.append(c)
        else:
            admitted.append(c)
    return admitted, refused


def needs_confirmation(payload: dict[str, Any]) -> bool:
    return payload.get("error_type") == "ConfirmationRequired" or bool(
        payload.get("confirmation_id") and not payload.get("success")
    )


def _spoken_question(payload: dict[str, Any]) -> str:
    """The question Era asks when an action needs a yes. Once the preview has
    been read aloud, suggested_confirmation_question is an instruction TO THE
    MODEL ("do not repeat the steps..."), so the plain question is used."""
    from core.confirmation_preview import ANNOUNCED_KEY

    data = payload.get("data")
    if isinstance(data, dict) and data.get(ANNOUNCED_KEY):
        return _ASK_YES
    return str(payload.get("suggested_confirmation_question") or _ASK_YES)


def _history_tool_content(content: str) -> str:
    """What a tool result looks like once it is HISTORY: status only.

    Raw results (email bodies, web pages) kept for eight turns are standing
    prompt-injection material that can ride on the user's later words.
    """
    try:
        payload = json.loads(content)
        return json.dumps(
            {"success": payload.get("success"), "error_type": payload.get("error_type")}
        )
    except Exception:
        return "{}"
