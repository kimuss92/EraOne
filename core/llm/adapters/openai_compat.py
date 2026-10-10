"""OpenAI-compatible chat completions — the cloud brain of the cascade mode.

WHY THIS EXISTS
  The cascade voice mode (app/era_live/cascade.py) replaces the Gemini Live
  session with  mic -> local STT -> THIS -> Fish/Kokoro -> speaker. There is no
  long-lived session to drop: every turn is one short streamed HTTP request, so
  a failure costs that turn, never the conversation.

  Written for CodeCraft (an OpenAI-compatible gateway), but nothing here is
  CodeCraft-specific: any /v1/chat/completions endpoint with SSE streaming and
  function tools works.

CONFIG (never hardcoded; the api key is never logged)
  base_url  env CODECRAFT_BASE_URL  -> config/api_keys.json codecraft.base_url
  model     env CODECRAFT_MODEL     -> config/api_keys.json codecraft.model
  api_key   env CODECRAFT_API_KEY   -> config/api_keys.json codecraft.api_key
  (a top-level "CODECRAFT_API_KEY" etc. in config/api_keys.json also works)
  The value "placeholder" (or empty) counts as NOT configured, so shipping the
  defaults changes nothing until the real values are filled in.

WHAT IT DOES AND DOES NOT DO
  * Streams text deltas and assembles streamed tool calls into ToolCall objects.
  * Never EXECUTES a tool (see the adapters package contract) — LocalBrain hands
    each call to EraLive._execute_tool, the same gate Gemini's calls take.
  * Retries with exponential backoff (429 / 5xx / network) — but only BEFORE the
    first byte arrives. A retry after text was spoken would say it twice.
  * Translates LocalBrain's Ollama-dialect tool history into OpenAI's (ids on
    tool calls, JSON-string arguments, tool_call_id on results).
  * stdlib only (urllib), like ollama_chat — no new dependency.
"""

from __future__ import annotations

import json
import os
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

PLACEHOLDER = "placeholder"
USER_AGENT = "EraOne/1.0"
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 2.0, 4.0)
_RETRY_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


# ── config ──────────────────────────────────────────────────────────────────


def codecraft_config() -> dict[str, str]:
    """env -> config/api_keys.json `codecraft` block -> placeholder."""
    try:
        from core.utils import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    block = cfg.get("codecraft") if isinstance(cfg.get("codecraft"), dict) else {}

    def pick(key: str, env: str) -> str:
        # Also accepts the env-style name as a top-level api_keys.json key
        # ("CODECRAFT_API_KEY": "..."), which is how the user first filled it in.
        val = (
            os.environ.get(env, "").strip()
            or str(block.get(key) or "").strip()
            or str(cfg.get(env) or "").strip()
        )
        # "<your model here>"-style template text copied verbatim is not a value.
        return PLACEHOLDER if not val or val.startswith("<") else val

    return {
        "base_url": pick("base_url", "CODECRAFT_BASE_URL"),
        "model": pick("model", "CODECRAFT_MODEL"),
        "api_key": pick("api_key", "CODECRAFT_API_KEY"),
    }


def is_configured(cfg: dict[str, str] | None = None) -> tuple[bool, str]:
    """(ready, reason). Never reveals the key itself."""
    cfg = cfg or codecraft_config()
    missing = [
        k for k in ("base_url", "model", "api_key") if cfg.get(k, PLACEHOLDER) == PLACEHOLDER
    ]
    if missing:
        names = ", ".join(f"CODECRAFT_{k.upper()}" for k in missing)
        return False, f"CodeCraft not configured (set {names})"
    if not cfg["base_url"].startswith(("https://", "http://")):
        return False, "CODECRAFT_BASE_URL must start with https://"
    return True, ""


# ── message translation (Ollama dialect -> OpenAI) ──────────────────────────


def to_openai_messages(request: LlmRequest) -> list[dict[str, Any]]:
    """LlmRequest -> chat.completions messages.

    LocalBrain keeps its turn history in Ollama's dialect under
    provider_options["ollama"]["extra_messages"]. OpenAI is stricter:
      * every assistant tool call needs an id, and arguments are a JSON STRING;
      * every tool result must name the tool_call_id it answers;
      * every tool call must be answered before the next non-tool message.
    LocalBrain stops running calls after an armed confirmation, so the last
    rule is enforced here by answering skipped calls with a "not run" result.
    Orphan tool results (no open call) are dropped rather than sent to a 400.
    """
    out: list[dict[str, Any]] = []
    system = request.system_text()
    if system:
        out.append({"role": "system", "content": system})
    for msg in request.messages:
        role = msg.role if msg.role in ("user", "assistant", "system") else "user"
        out.append({"role": role, "content": msg.as_text})

    extra = (request.provider_options.get("ollama") or {}).get("extra_messages") or []
    open_ids: list[str] = []

    def close_open() -> None:
        while open_ids:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": open_ids.pop(0),
                    "content": json.dumps({"success": False, "error": "Not run."}),
                }
            )

    for m in extra:
        role = m.get("role")
        if role == "tool":
            if not open_ids:
                continue
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": open_ids.pop(0),
                    "content": str(m.get("content") or ""),
                }
            )
            continue
        close_open()
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for raw in m["tool_calls"]:
                fn = raw.get("function") or {}
                args = fn.get("arguments") or {}
                call_id = str(raw.get("id") or f"call_{uuid.uuid4().hex[:12]}")
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(fn.get("name") or ""),
                            "arguments": args if isinstance(args, str) else json.dumps(args),
                        },
                    }
                )
                open_ids.append(call_id)
            out.append(
                {"role": "assistant", "content": m.get("content") or "", "tool_calls": calls}
            )
        else:
            out.append(
                {
                    "role": role if role in ("user", "assistant", "system") else "user",
                    "content": str(m.get("content") or ""),
                }
            )
    close_open()
    return out


# ── SSE stream assembly ─────────────────────────────────────────────────────


class _ToolAssembler:
    """Streamed tool calls arrive as fragments keyed by index; join them."""

    def __init__(self) -> None:
        self._parts: dict[int, dict[str, str]] = {}

    def feed(self, deltas: list[dict[str, Any]]) -> None:
        for d in deltas:
            idx = int(d.get("index") or 0)
            slot = self._parts.setdefault(idx, {"id": "", "name": "", "args": ""})
            if d.get("id"):
                slot["id"] = str(d["id"])
            fn = d.get("function") or {}
            if fn.get("name"):
                slot["name"] += str(fn["name"])
            if fn.get("arguments"):
                slot["args"] += str(fn["arguments"])

    def calls(self) -> list[ToolCall]:
        out = []
        for idx in sorted(self._parts):
            slot = self._parts[idx]
            name = slot["name"].strip()
            if not name:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                args = {}
            out.append(
                ToolCall(
                    id=slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    name=name,
                    args=args if isinstance(args, dict) else {},
                )
            )
        self._parts.clear()
        return out


def iter_sse(lines: Iterator[bytes]) -> Iterator[dict[str, Any]]:
    """Decoded JSON payloads of an SSE body; stops at [DONE]."""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


# ── errors ──────────────────────────────────────────────────────────────────


def _classify(exc: BaseException) -> LlmError:
    if isinstance(exc, LlmError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        retry_after = None
        try:
            retry_after = float(exc.headers.get("Retry-After") or 0) or None
        except Exception:
            pass
        if code in (401, 403):
            return LlmError(
                "auth", f"CodeCraft rejected the key (HTTP {code})", provider="codecraft"
            )
        if code == 429:
            return LlmError(
                "rate_limit",
                "CodeCraft rate limit (HTTP 429)",
                provider="codecraft",
                retry_after_s=retry_after,
            )
        if code in (400, 404, 422):
            return LlmError(
                "unsupported", f"CodeCraft refused the request (HTTP {code})", provider="codecraft"
            )
        return LlmError(
            "transient", f"CodeCraft HTTP {code}", provider="codecraft", retry_after_s=retry_after
        )
    if isinstance(exc, TimeoutError) or "timed out" in str(exc):
        return LlmError("timeout", str(exc), provider="codecraft")
    return LlmError(
        "transient", f"CodeCraft unreachable: {type(exc).__name__}", provider="codecraft"
    )


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRY_CODES
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


# ── provider ────────────────────────────────────────────────────────────────


class OpenAICompatProvider:
    """LLMProvider over an OpenAI-compatible endpoint. Thread-safe cancel."""

    name = "codecraft"

    def __init__(self, config: dict[str, str] | None = None, *, sleep=time.sleep) -> None:
        self._fixed = config
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._sleep = sleep

    def _cfg(self) -> dict[str, str]:
        return self._fixed or codecraft_config()

    def capabilities(self, alias: str) -> Capability:
        ok, why = is_configured(self._cfg())
        return Capability(tools=True, streaming=True, available=ok, unavailable_reason=why)

    def _body(self, request: LlmRequest, cfg: dict[str, str]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": cfg["model"],
            "messages": to_openai_messages(request),
            "stream": True,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            body["tools"] = list(request.tools)
        return body

    def _open(self, body: dict[str, Any], cfg: dict[str, str], timeout_s: float):
        req = urllib.request.Request(
            url=f"{cfg['base_url'].rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {cfg['api_key']}",
                # CodeCraft sits behind Cloudflare, which answers urllib's
                # default "Python-urllib/x" agent with 403 error 1010 (measured
                # 2026-09-24). Any honest non-default agent passes.
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout_s)

    def _open_with_retry(self, request: LlmRequest, cfg: dict[str, str]):
        body = self._body(request, cfg)
        last: BaseException | None = None
        for attempt in range(MAX_ATTEMPTS):
            if request.request_id in self._cancelled:
                return None
            try:
                return self._open(body, cfg, request.timeout_s)
            except Exception as e:
                last = e
                if not _should_retry(e) or attempt == MAX_ATTEMPTS - 1:
                    break
                err = _classify(e)
                wait = min(err.retry_after_s or BACKOFF_S[attempt], 10.0)
                print(f"[CODECRAFT] {err.kind}; retry {attempt + 2}/{MAX_ATTEMPTS} in {wait:.0f}s")
                self._sleep(wait)
        raise _classify(last) if last else LlmError("transient", "no attempt made")

    def generate(self, request: LlmRequest) -> LlmResponse:
        text, calls, usage = [], [], Usage()
        for ev in self.stream(request):
            if ev.kind == "text_delta":
                text.append(ev.text)
            elif ev.kind == "tool_call" and ev.tool_call is not None:
                calls.append(ev.tool_call)
            elif ev.kind == "usage" and ev.usage is not None:
                usage = ev.usage
            elif ev.kind == "error" and ev.error is not None:
                raise ev.error
        return LlmResponse(
            request_id=request.request_id,
            provider=self.name,
            model_id=self._cfg()["model"],
            alias=request.alias,
            text="".join(text),
            tool_calls=tuple(calls),
            stop_reason="tool_use" if calls else "end_turn",
            usage=usage,
        )

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        cfg = self._cfg()
        ok, why = is_configured(cfg)
        if not ok:
            yield StreamEvent(kind="error", error=LlmError("unsupported", why, provider=self.name))
            return
        started = time.perf_counter()
        first_ms: int | None = None
        try:
            resp = self._open_with_retry(request, cfg)
        except Exception as e:
            yield StreamEvent(kind="error", error=_classify(e))
            return
        if resp is None:
            yield StreamEvent(kind="turn_complete", detail={"stop_reason": "cancelled"})
            return
        tools = _ToolAssembler()
        usage = Usage()
        try:
            for chunk in iter_sse(resp):
                if request.request_id and request.request_id in self._cancelled:
                    yield StreamEvent(kind="turn_complete", detail={"stop_reason": "cancelled"})
                    return
                if chunk.get("error"):
                    msg = str((chunk["error"] or {}).get("message") or chunk["error"])[:200]
                    yield StreamEvent(
                        kind="error", error=LlmError("transient", msg, provider=self.name)
                    )
                    return
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = Usage(
                        input_tokens=int(u.get("prompt_tokens") or 0),
                        output_tokens=int(u.get("completion_tokens") or 0),
                    )
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    if text:
                        if first_ms is None:
                            first_ms = int((time.perf_counter() - started) * 1000)
                        yield StreamEvent(kind="text_delta", text=text)
                    if delta.get("tool_calls"):
                        if first_ms is None:
                            first_ms = int((time.perf_counter() - started) * 1000)
                        tools.feed(delta["tool_calls"])
            for call in tools.calls():
                yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(
                kind="usage",
                usage=Usage(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    first_token_ms=first_ms,
                ),
            )
            yield StreamEvent(kind="turn_complete")
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


class FirstThatAnswers:
    """primary, then fallback — but only when the primary failed BEFORE saying
    anything. Used for the optional routing.cascade.fallback_alias (Ollama)."""

    def __init__(self, primary: Any, fallback: Any, label: str = "") -> None:
        self._primary = primary
        self._fallback = fallback
        self._label = label

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        produced = False
        for ev in self._primary.stream(request):
            if ev.kind == "error" and not produced:
                print(f"[CASCADE] Brain failed ({ev.error}); falling back to {self._label}.")
                yield from self._fallback.stream(request)
                return
            if ev.kind in ("text_delta", "tool_call"):
                produced = True
            yield ev

    def cancel(self, request_id: str) -> None:
        for p in (self._primary, self._fallback):
            try:
                p.cancel(request_id)
            except Exception:
                pass
