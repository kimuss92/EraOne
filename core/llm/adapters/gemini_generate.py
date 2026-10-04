"""Gemini generate/JSON as an LLMProvider — a wrapper AROUND existing behaviour.

DIRECTION OF DEPENDENCY, AND WHY
  This module calls core.ai_provider. Not the reverse.

  core.ai_provider is imported without a try/except by six modules
  (design_studio, self_diagnosis, self_improve, agent/{planner,executor,
  error_handler}) and by lane_router, which routes every utterance. Inverting
  the dependency — making that file delegate here — would put a brand-new code
  path underneath Era's live routing on day one of the migration.

  Wrapping instead means those seven callers are untouched byte for byte, the
  adapter is exercised only by whatever opts into it, and the risk of adopting
  the abstraction is bounded by who calls it rather than by who imports it.
  tools/test_ai_provider_contract.py pins the wrapped behaviour so a later
  inversion is a verified change rather than a hopeful one.

WHAT THIS ADDS
  Token accounting. core.ai_provider throws the response object away and returns
  a string, so Era has never counted a Gemini token. Going through
  GeminiModel.generate_content (its public class) keeps the response, so
  usage_metadata can be read: prompt/candidates/cached counts. Cost stays 0.0
  with reported_cost=False unless config/llm.yaml carries prices for the alias,
  because inventing a per-token price would be worse than admitting we have none.

WHAT THIS DELIBERATELY DOES NOT DO
  Streaming. Gemini's generate path here is a single blocking call, so stream()
  emits one text_delta then turn_complete. The protocol documents that callers
  may not depend on chunk granularity — only on event order. Real token
  streaming belongs to the Live session (a RealtimeVoiceProvider), not here.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

from core.llm import registry
from core.llm.types import (
    Capability,
    LlmError,
    LlmRequest,
    LlmResponse,
    StreamEvent,
    Usage,
)

_QUOTA_MARKERS = ("429", "RESOURCE_EXHAUSTED", "quota")
_AUTH_MARKERS = ("401", "403", "API key not valid", "PERMISSION_DENIED", "UNAUTHENTICATED")
_TRANSIENT_MARKERS = ("500", "502", "503", "504", "UNAVAILABLE", "DEADLINE_EXCEEDED")
_CONTEXT_MARKERS = ("token count", "exceeds the maximum", "context length")


def classify_gemini_error(exc: BaseException) -> str:
    """Map a google-genai exception onto Era's ErrorKind vocabulary.

    Substring matching on the message, because google-genai raises a small set
    of generic exception types and puts the useful signal in the text. The
    existing quota detection in core/ai_provider.py works the same way, and it
    is what turned an invisible session-long outage into one loud line.

    Order matters: quota before rate_limit, since a 429 that says
    RESOURCE_EXHAUSTED is an exhausted daily allowance (cool down, rotate keys)
    rather than a per-minute burst (retry shortly).
    """
    msg = str(exc)
    if any(m in msg for m in _QUOTA_MARKERS):
        return "quota" if "RESOURCE_EXHAUSTED" in msg or "quota" in msg else "rate_limit"
    if any(m in msg for m in _AUTH_MARKERS):
        return "auth"
    if any(m in msg for m in _CONTEXT_MARKERS):
        return "context_overflow"
    if any(m in msg for m in _TRANSIENT_MARKERS):
        return "transient"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "transient"


def _usage_from(response: Any, entry: registry.ModelEntry, started: float) -> Usage:
    """Read usage_metadata if the SDK provided it. Never raises: a missing or
    renamed field must cost a metric, not the call."""
    prompt = candidates = cached = 0
    try:
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            prompt = int(getattr(meta, "prompt_token_count", 0) or 0)
            candidates = int(getattr(meta, "candidates_token_count", 0) or 0)
            cached = int(getattr(meta, "cached_content_token_count", 0) or 0)
    except Exception:
        pass

    price = entry.price or {}
    cost = 0.0
    reported = False
    if price:
        cost = (prompt / 1_000_000) * float(price.get("input_per_mtok", 0.0)) + (
            candidates / 1_000_000
        ) * float(price.get("output_per_mtok", 0.0))
        reported = True

    return Usage(
        input_tokens=prompt,
        output_tokens=candidates,
        cache_read_tokens=cached,
        est_cost_usd=round(cost, 6),
        reported_cost=reported,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _strip_code_fences(text: str) -> str:
    """The model returns ```json blocks even when asked for raw JSON. Same
    treatment core/ai_provider.generate_json applies, kept identical on purpose
    so both paths parse the same replies."""
    return re.sub(r"```(?:json)?", "", text or "").strip().rstrip("`").strip()


class GeminiGenerateProvider:
    """LLMProvider over Gemini's generateContent path."""

    name = "gemini"

    def __init__(self, ai_provider_module: Any = None) -> None:
        # Injectable so tests can drive the adapter without google-genai
        # installed or a key present. Defaults to the real module.
        if ai_provider_module is None:
            from core import ai_provider as ai_provider_module  # noqa: PLC0415
        self._ai = ai_provider_module

    # ── capabilities ────────────────────────────────────────────────────────

    def capabilities(self, alias: str) -> Capability:
        entry = registry.resolve(alias)
        if entry.provider != self.name:
            raise LlmError(
                "unsupported",
                f"{alias} belongs to provider {entry.provider!r}, not {self.name!r}",
                provider=self.name,
                alias=alias,
            )
        return entry.capabilities

    def _entry(self, request: LlmRequest) -> registry.ModelEntry:
        try:
            entry = registry.resolve(request.alias)
        except registry.RegistryError as e:
            raise LlmError("unsupported", str(e), provider=self.name, alias=request.alias) from e
        if entry.provider != self.name:
            raise LlmError(
                "unsupported",
                f"{request.alias} is a {entry.provider} alias, not {self.name}",
                provider=self.name,
                alias=request.alias,
            )
        ok, reason = entry.capabilities.satisfies(
            json_schema=request.json_schema is not None,
            tools=bool(request.tools),
        )
        if not ok:
            raise LlmError(
                "unsupported",
                f"{request.alias} cannot serve this request — {reason}",
                provider=self.name,
                alias=request.alias,
            )
        return entry

    # ── generate ────────────────────────────────────────────────────────────

    def generate(self, request: LlmRequest) -> LlmResponse:
        entry = self._entry(request)
        prompt = self._prompt_from(request)
        system = request.system_text() or None
        want_json = request.json_schema is not None

        started = time.perf_counter()
        try:
            model = self._ai.GeminiModel(entry.model, system)
            response = model.generate_content(
                prompt,
                response_mime_type="application/json" if want_json else None,
            )
        except Exception as e:
            raise LlmError(
                classify_gemini_error(e),
                str(e),
                provider=self.name,
                alias=request.alias,
            ) from e

        text = (getattr(response, "text", "") or "").strip()
        if want_json:
            text = _strip_code_fences(text)
            # Validate here rather than returning something the caller will
            # discover is unparseable later. invalid_response is retryable, so
            # the gateway can reprompt once before giving up.
            try:
                json.loads(text)
            except (ValueError, TypeError) as e:
                raise LlmError(
                    "invalid_response",
                    f"expected JSON, got: {text[:200]}",
                    provider=self.name,
                    alias=request.alias,
                ) from e

        return LlmResponse(
            request_id=request.request_id,
            provider=self.name,
            model_id=entry.model,
            alias=request.alias,
            text=text,
            stop_reason="end_turn",
            usage=_usage_from(response, entry, started),
            raw=response,
        )

    # ── stream ──────────────────────────────────────────────────────────────

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        """One text_delta then turn_complete. See the module docstring: this
        path does not stream, and pretending otherwise by chunking the finished
        string would be a lie that a latency metric would then believe."""
        response = self.generate(request)
        if response.text:
            yield StreamEvent(kind="text_delta", text=response.text)
        yield StreamEvent(kind="usage", usage=response.usage)
        yield StreamEvent(kind="turn_complete", detail={"stop_reason": response.stop_reason})

    def cancel(self, request_id: str) -> None:
        """No-op: generateContent is a blocking call with nothing to signal.
        Safe to call for any id, per the protocol — cancellation races
        completion by nature."""
        return None

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _prompt_from(request: LlmRequest) -> str:
        """Flatten messages into the single string this Gemini path takes.

        The system prompt is passed separately (system_instruction), so only
        conversational turns are joined here. Roles are labelled when there is
        more than one turn: a bare concatenation of a multi-turn exchange reads
        to the model as one run-on user message.
        """
        turns = [m for m in request.messages if m.role != "system"]
        if not turns:
            return ""
        if len(turns) == 1:
            return turns[0].as_text
        return "\n\n".join(f"{m.role}: {m.as_text}" for m in turns)
