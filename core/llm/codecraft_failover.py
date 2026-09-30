"""CodeCraft as a text step in the Gemini quota failover (core/llm/quota_failover.py).

Order there:  spare Gemini key -> CODECRAFT (this) -> Claude CLI -> local Ollama.

WHY BEFORE CLAUDE
  CodeCraft is a flat subscription (100M tokens/month) that is already paid for,
  while every Claude CLI call costs ~$0.058 against the daily budget. Measured
  2026-09-24: claude-sonnet-5 via CodeCraft answered a small prompt in ~3 s,
  about the same speed as Claude CLI. Too slow for the live voice, fine for text.

SAME ELIGIBILITY AS CLAUDE
  build_claude_request raises NotEligible for images/files/audio, tools or
  search grounding, and cached content, so those still re-raise the original
  quota error. JSON requests are asked for plain JSON and verified here; a
  reply that does not parse raises, and the chain moves on to Claude.

Kill switch: routing.quota_failover.codecraft: false (config/llm.yaml).
"""

from __future__ import annotations

import json
import re
from typing import Any

# Injectable seam for tests: nothing here may make a real network call.
_provider_factory: Any = None

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def available() -> tuple[bool, str]:
    from core.llm.adapters.openai_compat import is_configured

    return is_configured()


def _json_instruction(schema: dict) -> str:
    shape = f" matching this JSON schema:\n{json.dumps(schema)}" if schema else ""
    return f"Reply with ONLY valid JSON{shape}\nNo prose, no markdown fences."


def try_codecraft(*, contents: Any, config: Any, model: str, timeout_s: float) -> Any:
    """Serve one generate_content call through CodeCraft, or raise."""
    from dataclasses import replace

    from core.llm.quota_failover import NotEligible, _log, build_claude_request, to_gemini_response
    from core.llm.types import PromptBlock

    ok, why = available()
    if not ok:
        raise NotEligible(why)
    request = build_claude_request(
        contents=contents, config=config, alias="era.codecraft", timeout_s=timeout_s
    )
    if request.json_schema is not None:
        request = replace(
            request,
            system=(*request.system, PromptBlock(text=_json_instruction(request.json_schema))),
        )

    if _provider_factory is not None:
        provider = _provider_factory()
    else:
        from core.llm.adapters.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider()
    response = provider.generate(request)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("CodeCraft returned an empty reply")
    if request.json_schema is not None:
        text = _FENCE.sub("", text).strip()
        json.loads(text)  # raises -> Claude gets its turn
    _log(f"{model} quota exhausted -> served by CodeCraft {response.model_id}")
    return to_gemini_response(text, f"codecraft/{response.model_id}")
