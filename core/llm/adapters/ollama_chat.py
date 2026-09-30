"""Local LLM over Ollama's /api/chat — the brain that needs no cloud.

WHY THIS EXISTS
  Era's reasoning used to depend entirely on Gemini: the Live session for voice,
  generate_content for tools, and Claude only as a paid quota spill-over. When
  every Gemini key ran dry she fell silent for 15 min – 2 h. This adapter is the
  floor underneath all of that: free, local, and reachable with the network down.

WHAT IT DOES AND DOES NOT DO
  * generate() and stream() over /api/chat with real token streaming.
  * Tool CALLING: Era's tool specs are offered in Ollama's function format and
    the model's picks come back as ToolCall objects. It never EXECUTES them —
    the caller routes each one through the same gate Gemini's calls take
    (EraLive._execute_tool -> classify_action -> pending_confirmation), exactly
    as the adapters package contract requires.
  * stdlib only (urllib), in the style of core/text_ollama.py — no new deps.

TOOL SCHEMAS ARE SLIMMED ON PURPOSE
  Era's 51 declarations are ~17k tokens verbatim. A 7B model on an 8 GB laptop
  GPU pays for every one of them on a cold prompt, so descriptions are trimmed
  to their first sentence (measured: ~8.4k tokens). Names, types, enums and
  `required` are untouched, so every tool stays callable with valid arguments.
  The block is deterministic, which keeps it a stable prefix Ollama can reuse.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

from core.llm.types import (
    Capability,
    LlmError,
    LlmRequest,
    LlmResponse,
    StreamEvent,
    ToolCall,
    Usage,
)
from core.text_ollama import SHARED_NUM_CTX

DEFAULT_MODEL = "gemma4:e4b"  # config/llm.yaml era.local is authoritative
# slim tools (~8.4k) + compact prompt + a short history. Shared with Hermes so
# the resident model is never reloaded between the two (see text_ollama).
DEFAULT_NUM_CTX = SHARED_NUM_CTX


def base_url() -> str:
    try:
        from core.vision_config import resolve_ollama_base_url

        return resolve_ollama_base_url()
    except Exception:
        return "http://127.0.0.1:11434"


# ── tool schema slimming ────────────────────────────────────────────────────

_SENTENCE = re.compile(r"(.+?[.!?])(\s|$)")


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join(text.split())
    m = _SENTENCE.match(text)
    return (m.group(1) if m else text)[:limit]


def _slim(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (_first_sentence(v, 70) if k == "description" and isinstance(v, str) else _slim(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_slim(x) for x in node]
    return node


def to_ollama_tools(specs: list[Any]) -> list[dict[str, Any]]:
    """ToolSpec list -> Ollama/OpenAI function-tool list, slimmed."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": _first_sentence(s.description or "", 180),
                "parameters": _slim(s.parameters or {"type": "object", "properties": {}}),
            },
        }
        for s in specs
    ]


# ── message translation ─────────────────────────────────────────────────────


def to_ollama_messages(request: LlmRequest) -> list[dict[str, Any]]:
    """LlmRequest -> /api/chat messages. System blocks first, in order.

    Tool round-trips ride in ``provider_options["ollama"]["extra_messages"]``:
    the assistant tool_calls turn and the role="tool" results, already in
    Ollama's dialect. Keeping them there (rather than widening LlmMessage) is
    what the adapters contract asks of provider-specific shapes.
    """
    out: list[dict[str, Any]] = []
    system = request.system_text()
    if system:
        out.append({"role": "system", "content": system})
    for msg in request.messages:
        role = msg.role if msg.role in ("user", "assistant", "system", "tool") else "user"
        out.append({"role": role, "content": msg.as_text})
    extra = (request.provider_options.get("ollama") or {}).get("extra_messages") or []
    out.extend(extra)
    return out


def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # some templates return a JSON string
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append(ToolCall(id=f"local-{uuid.uuid4().hex[:10]}", name=name, args=dict(args)))
    return calls


def _classify(exc: BaseException) -> LlmError:
    if isinstance(exc, LlmError):
        return exc
    if isinstance(exc, TimeoutError) or "timed out" in str(exc):
        return LlmError("timeout", str(exc), provider="ollama")
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
        return LlmError("unsupported", f"model not pulled: {exc}", provider="ollama")
    return LlmError("transient", f"Ollama unreachable at {base_url()}: {exc}", provider="ollama")


# ── provider ────────────────────────────────────────────────────────────────


class OllamaChatProvider:
    """LLMProvider over Ollama. One instance can serve many aliases."""

    name = "ollama"

    def __init__(self, model: str | None = None, *, num_ctx: int = DEFAULT_NUM_CTX) -> None:
        self._model_override = model
        self._num_ctx = num_ctx
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # -- resolution ----------------------------------------------------------
    def _model_for(self, alias: str) -> str:
        if self._model_override:
            return self._model_override
        try:
            from core.llm import registry

            entry = registry.resolve(alias)
            if entry.provider == "ollama":
                return entry.model
        except Exception:
            pass
        return DEFAULT_MODEL

    def capabilities(self, alias: str) -> Capability:
        ok, why = ping()
        return Capability(
            tools=True,
            json_schema=True,
            streaming=True,
            cost_reporting=True,
            available=ok,
            unavailable_reason=why,
        )

    def _body(self, request: LlmRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model_for(request.alias),
            "messages": to_ollama_messages(request),
            "stream": stream,
            "keep_alive": "30m",
            "options": {"num_ctx": self._num_ctx, "num_predict": request.max_output_tokens},
            # Reasoning models (gemma4, qwen3) otherwise write a hidden chain of
            # thought before every reply: measured 9-27 s a turn on gemma4:e4b.
            # A spoken assistant cannot afford it. Ignored by other models.
            "think": False,
        }
        if request.tools:
            body["tools"] = list(request.tools)
        if request.json_schema is not None:
            body["format"] = request.json_schema or "json"
        return body

    def _open(self, body: dict[str, Any], timeout_s: float):
        req = urllib.request.Request(
            url=f"{base_url().rstrip('/')}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout_s)

    # -- LLMProvider ---------------------------------------------------------
    def generate(self, request: LlmRequest) -> LlmResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage = Usage()
        for ev in self.stream(request):
            if ev.kind == "text_delta":
                text_parts.append(ev.text)
            elif ev.kind == "tool_call" and ev.tool_call is not None:
                calls.append(ev.tool_call)
            elif ev.kind == "usage" and ev.usage is not None:
                usage = ev.usage
            elif ev.kind == "error" and ev.error is not None:
                raise ev.error
        return LlmResponse(
            request_id=request.request_id,
            provider=self.name,
            model_id=self._model_for(request.alias),
            alias=request.alias,
            text="".join(text_parts),
            tool_calls=tuple(calls),
            stop_reason="tool_use" if calls else "end_turn",
            usage=usage,
        )

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        started = time.perf_counter()
        first_ms: int | None = None
        try:
            resp = self._open(self._body(request, stream=True), request.timeout_s)
        except Exception as e:
            yield StreamEvent(kind="error", error=_classify(e))
            return
        try:
            for raw_line in resp:
                if request.request_id and request.request_id in self._cancelled:
                    yield StreamEvent(kind="turn_complete", detail={"stop_reason": "cancelled"})
                    return
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    yield StreamEvent(
                        kind="error",
                        error=LlmError("transient", str(chunk["error"]), provider="ollama"),
                    )
                    return
                message = chunk.get("message") or {}
                delta = message.get("content") or ""
                if delta:
                    if first_ms is None:
                        first_ms = int((time.perf_counter() - started) * 1000)
                    yield StreamEvent(kind="text_delta", text=delta)
                for call in _parse_tool_calls(message):
                    if first_ms is None:
                        first_ms = int((time.perf_counter() - started) * 1000)
                    yield StreamEvent(kind="tool_call", tool_call=call)
                if chunk.get("done"):
                    yield StreamEvent(
                        kind="usage",
                        usage=Usage(
                            input_tokens=int(chunk.get("prompt_eval_count") or 0),
                            output_tokens=int(chunk.get("eval_count") or 0),
                            reported_cost=True,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            first_token_ms=first_ms,
                        ),
                    )
                    yield StreamEvent(kind="turn_complete")
                    return
        except Exception as e:
            yield StreamEvent(kind="error", error=_classify(e))
        finally:
            try:
                resp.close()
            except Exception:
                pass
            if request.request_id:
                with self._lock:
                    self._cancelled.discard(request.request_id)

    def cancel(self, request_id: str) -> None:
        if request_id:
            with self._lock:
                self._cancelled.add(request_id)


# ── helpers ─────────────────────────────────────────────────────────────────


def ping(timeout_s: float = 2.0) -> tuple[bool, str]:
    """(reachable, reason). Cheap: lists local models, loads nothing."""
    try:
        with urllib.request.urlopen(f"{base_url().rstrip('/')}/api/tags", timeout=timeout_s):
            return True, ""
    except Exception as e:
        return False, f"Ollama is not reachable at {base_url()} ({type(e).__name__})"
