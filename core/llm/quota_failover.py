"""Gemini quota failover: exhausted key -> spare Gemini key -> CodeCraft -> Claude -> local.

(CodeCraft, step 3a, added 2026-09-24 — see core/llm/codecraft_failover.py.)

(Step 4, the local Ollama alias, and the offline path — network down goes
straight to local — were added 2026-09-23 for 24/7 operation; see _try_local.)

WHY IT PATCHES THE SDK INSTEAD OF core.ai_provider
  Gemini is called from ~20 sites. Only some go through core.ai_provider; the
  rest (code_helper, dev_agent, file_processor, flight_finder, desktop,
  computer_settings, design_studio, youtube_video, ...) build their own
  genai.Client and call `client.models.generate_content` directly. A fix in
  ai_provider alone would leave most tools dead-ending on RESOURCE_EXHAUSTED.
  Wrapping `google.genai.models.Models.generate_content` once covers every
  client in the process — including ones created later — with no call-site edits.

ORDER OF RECOVERY, AND WHY
  1. Another Gemini key. Quota is billed per key (see core/utils.get_api_keys),
     so a spare key restores the SAME model for free and at Gemini latency.
  2. Claude over the tool-free `claude -p` adapter (routing.quota_failover.alias,
     era.cheap by default). Costs real money (~$0.016 floor) and ~2.5s, so it is
     bounded by the shared daily budget AND a per-day call cap.
  Gemini stays primary: an exhausted (key, model) pair is only latched for
  `latch_seconds`, after which Gemini is tried first again.

WHAT IS NEVER FAILED OVER — the original quota error is re-raised instead
  * Non-text input (images, files, audio). The Claude path has no vision; a
    reply to a screenshot it cannot see would be a confident fabrication.
  * Requests carrying `tools` (google_search grounding, function calling). A
    Claude answer to a grounded search would be invented "results", and tools on
    the Claude path would move execution outside Era's safety gates.
  * `cached_content` requests, whose real prompt lives server-side.
  * Anything once the budget or call cap is spent, or the flag is off.
  Re-raising the ORIGINAL exception keeps every caller's existing quota handling
  (e.g. ai_provider's loud [QUOTA] line) exactly as it was.

The Live voice session is untouched: it is realtime audio, which Claude cannot
serve. Its quota recovery remains key rotation + cooldown in reconnect_mixin.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

_ORIGINAL: Any = None
_INSTALL_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()

# (api_key, model) -> monotonic time until which that pair is presumed exhausted.
_LATCH: dict[tuple[str, str], float] = {}
# api_key -> genai.Client. The CLIENT must be held, not just `.models`: a Client
# nobody references is garbage-collected and closes its HTTP pool, after which
# every call raises "Cannot send a request, as the client has been closed".
_ROTATION_CLIENTS: dict[str, Any] = {}
_CLAUDE_CALLS: dict[str, int] = {}  # UTC day -> failover calls made

# Injectable seams for tests: nothing here should spawn a CLI or read real keys.
_claude_provider_factory: Any = None
_local_provider_factory: Any = None
_keys_source: Any = None
_ledger_factory: Any = None

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "alias": "era.cheap",
    "max_per_day": 80,
    "timeout_s": 45.0,
    "latch_seconds": 300,
    "rotate_keys": True,
}


class NotEligible(Exception):
    """The request cannot be faithfully served by the Claude text path."""


# ── policy ────────────────────────────────────────────────────────────────────


def policy() -> dict[str, Any]:
    try:
        from core.llm.router import quota_failover_policy

        return quota_failover_policy()
    except Exception:
        return dict(_DEFAULTS)  # fails closed: enabled False


def _log(message: str) -> None:
    line = f"[quota_failover] {message}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"))


def is_quota_error(exc: BaseException) -> bool:
    try:
        from core.llm.adapters.gemini_generate import classify_gemini_error

        return classify_gemini_error(exc) == "quota"
    except Exception:
        msg = str(exc)
        return "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


# ── request translation ───────────────────────────────────────────────────────


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


_NON_TEXT_PART_FIELDS = (
    "inline_data",
    "file_data",
    "function_call",
    "function_response",
    "executable_code",
    "code_execution_result",
)


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict) or type(part).__name__ == "Part":
        for field in _NON_TEXT_PART_FIELDS:
            if _get(part, field):
                raise NotEligible(f"non-text part ({field})")
        text = _get(part, "text")
        if isinstance(text, str):
            return text
        raise NotEligible("part without text")
    raise NotEligible(f"unsupported content type {type(part).__name__}")


def _content_turns(contents: Any) -> list[tuple[str, str]]:
    """Flatten Gemini `contents` into (role, text) turns, or raise NotEligible."""
    if contents is None:
        return []
    if isinstance(contents, str):
        return [("user", contents)]
    if isinstance(contents, (list, tuple)):
        turns: list[tuple[str, str]] = []
        for item in contents:
            turns.extend(_content_turns(item))
        return turns
    parts = _get(contents, "parts")
    if parts is not None and (isinstance(contents, dict) or type(contents).__name__ == "Content"):
        role = str(_get(contents, "role") or "user")
        text = "\n".join(t for t in (_part_text(p) for p in parts) if t)
        return [(role, text)]
    return [("user", _part_text(contents))]


def _system_text(value: Any) -> str:
    if not value:
        return ""
    return "\n\n".join(text for _role, text in _content_turns(value) if text)


def _json_schema(config: Any) -> dict | None:
    """None when plain text is wanted; a schema (possibly {}) when JSON is."""
    schema = _get(config, "response_json_schema") or _get(config, "response_schema")
    mime = str(_get(config, "response_mime_type") or "")
    if schema is None and mime != "application/json":
        return None
    if isinstance(schema, dict):
        return schema
    for dumper in ("model_json_schema", "model_dump"):
        fn = getattr(schema, dumper, None)
        if callable(fn):
            try:
                out = fn(exclude_none=True) if dumper == "model_dump" else fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    return {}  # JSON of any shape; the adapter still validates it parses


def build_claude_request(*, contents: Any, config: Any, alias: str, timeout_s: float):
    """Translate a generate_content call into an LlmRequest, or raise NotEligible."""
    from core.llm.types import LlmMessage, LlmRequest, PromptBlock

    if config is not None:
        if _get(config, "tools"):
            raise NotEligible("request uses tools (search grounding / function calling)")
        if _get(config, "cached_content"):
            raise NotEligible("request uses server-side cached content")

    turns = [(role, text) for role, text in _content_turns(contents) if text.strip()]
    if not turns:
        raise NotEligible("no text to send")
    messages = tuple(
        LlmMessage.text("assistant" if role == "model" else "user", text) for role, text in turns
    )
    system_text = _system_text(_get(config, "system_instruction")) if config is not None else ""
    return LlmRequest(
        alias=alias,
        messages=messages,
        system=(PromptBlock(text=system_text),) if system_text else (),
        json_schema=_json_schema(config) if config is not None else None,
        timeout_s=float(timeout_s),
        request_id=f"quota-failover-{int(time.time() * 1000)}",
    )


def to_gemini_response(text: str, model_id: str) -> Any:
    """A real GenerateContentResponse, so `.text` and
    `.candidates[0].content.parts[i].text` both work for existing callers."""
    from google.genai import types as gtypes

    return gtypes.GenerateContentResponse(
        candidates=[
            gtypes.Candidate(
                content=gtypes.Content(role="model", parts=[gtypes.Part(text=text)]),
                finish_reason=gtypes.FinishReason.STOP,
            )
        ],
        model_version=model_id,
    )


# ── recovery steps ────────────────────────────────────────────────────────────


def _latched(key: str, model: str) -> bool:
    with _STATE_LOCK:
        until = _LATCH.get((key, model))
        if until is None:
            return False
        if time.monotonic() >= until:
            _LATCH.pop((key, model), None)
            return False
        return True


def _latch(key: str, model: str, seconds: float) -> None:
    if key and seconds > 0:
        with _STATE_LOCK:
            _LATCH[(key, model)] = time.monotonic() + seconds


def _models_for_key(key: str) -> Any:
    with _STATE_LOCK:
        client = _ROTATION_CLIENTS.get(key)
        if client is None:
            from google import genai

            client = genai.Client(api_key=key)
            _ROTATION_CLIENTS[key] = client
        return client.models


def _spare_keys(current: str) -> list[str]:
    try:
        if _keys_source is not None:
            keys = list(_keys_source())
        else:
            from core.utils import get_api_keys

            keys = get_api_keys()
    except Exception:
        return []
    return [k for k in keys if k and k != current]


def _try_claude(pol: dict[str, Any], *, model: str, contents: Any, config: Any) -> Any:
    """Serve the request through Claude, or raise (NotEligible / anything)."""
    request = build_claude_request(
        contents=contents, config=config, alias=pol["alias"], timeout_s=pol["timeout_s"]
    )

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    with _STATE_LOCK:
        if _CLAUDE_CALLS.get(day, 0) >= int(pol["max_per_day"]):
            raise NotEligible(f"daily failover cap reached ({pol['max_per_day']} calls)")

    if _ledger_factory is not None:
        ledger = _ledger_factory()
    else:
        from core.llm.spend import from_registry

        ledger = from_registry()
    estimate = 0.0
    try:
        from core.llm import registry

        estimate = registry.resolve(pol["alias"]).call_overhead_usd
    except Exception:
        pass
    affordable, why_not = ledger.can_afford(estimate)
    if not affordable:
        raise NotEligible(why_not)

    if _claude_provider_factory is not None:
        provider = _claude_provider_factory()
    else:
        from core.llm.adapters.claude_cli import ClaudeCliProvider

        provider = ClaudeCliProvider()

    with _STATE_LOCK:
        _CLAUDE_CALLS[day] = _CLAUDE_CALLS.get(day, 0) + 1
        for stale in [d for d in _CLAUDE_CALLS if d != day]:
            _CLAUDE_CALLS.pop(stale, None)

    response = provider.generate(request)
    try:
        ledger.record(response.usage.est_cost_usd, alias=pol["alias"])
    except Exception:
        pass
    text = response.text or ""
    if request.json_schema is None and not text.strip():
        raise RuntimeError("Claude returned an empty reply")
    _log(f"{model} quota exhausted -> served by {response.model_id} ({pol['alias']})")
    return to_gemini_response(text, response.model_id)


# Markers of "the network is down", as opposed to a bug or a bad request. Kept
# narrow on purpose: classify_gemini_error files anything unknown as
# "transient", and failing a programming error over to a local answer would
# hide it behind a plausible reply.
_OFFLINE_MARKERS = (
    "getaddrinfo",
    "Name or service not known",
    "nodename nor servname",
    "No address associated",
    "Temporary failure in name resolution",
    "ConnectError",
    "ConnectTimeout",
    "Connection refused",
    "actively refused",
    "Network is unreachable",
    "[Errno 11001]",
    "[WinError 10051]",
    "[WinError 10060]",
    "[WinError 10061]",
)


def is_offline_error(exc: BaseException) -> bool:
    """True when the request never reached Google because there is no network."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    name = type(exc).__name__
    if name in ("ConnectError", "ConnectTimeout", "ReadTimeout"):
        return True
    msg = f"{name}: {exc}"
    return any(m in msg for m in _OFFLINE_MARKERS)


def _try_local(pol: dict[str, Any], *, model: str, contents: Any, config: Any) -> Any:
    """Serve the request with the local Ollama alias, or raise.

    Same eligibility as Claude (build_claude_request raises NotEligible for
    images, tools/grounding and cached content): a local model answering a
    screenshot it cannot see would be the same confident fabrication.
    """
    alias = str(pol.get("local_alias") or "").strip()
    if not alias:
        raise NotEligible("no local alias configured")
    request = build_claude_request(contents=contents, config=config, alias=alias, timeout_s=120.0)
    if _local_provider_factory is not None:
        provider = _local_provider_factory()
    else:
        from core.llm.adapters.ollama_chat import OllamaChatProvider

        provider = OllamaChatProvider()
    response = provider.generate(request)
    text = response.text or ""
    if request.json_schema is None and not text.strip():
        raise RuntimeError("local model returned an empty reply")
    _log(f"{model} unavailable -> served locally by {response.model_id} ({alias})")
    return to_gemini_response(text, response.model_id)


def generate_with_failover(self: Any, *, model: str, contents: Any, config: Any = None) -> Any:
    """Drop-in replacement for Models.generate_content."""
    pol = policy()
    if not pol.get("enabled"):
        return _ORIGINAL(self, model=model, contents=contents, config=config)

    current_key = str(getattr(getattr(self, "_api_client", None), "api_key", "") or "")
    latch_s = float(pol.get("latch_seconds") or 0)
    quota_exc: BaseException | None = None

    # 1. The caller's own client, unless that key/model pair is known-exhausted.
    if not (current_key and _latched(current_key, model)):
        try:
            return _ORIGINAL(self, model=model, contents=contents, config=config)
        except Exception as e:
            if pol.get("offline_failover") and is_offline_error(e):
                # No network: spare keys and Claude are unreachable too.
                try:
                    return _try_local(pol, model=model, contents=contents, config=config)
                except Exception as local_err:
                    _log(f"{model} offline; local failover unavailable: {local_err}")
                raise
            if not is_quota_error(e):
                raise
            quota_exc = e
            _latch(current_key, model, latch_s)

    # 2. Spare Gemini keys: same model, same request, no extra cost.
    if pol.get("rotate_keys", True):
        for key in _spare_keys(current_key):
            if _latched(key, model):
                continue
            try:
                result = _ORIGINAL(
                    _models_for_key(key), model=model, contents=contents, config=config
                )
                _log(f"{model} quota exhausted on active key -> served by a spare Gemini key")
                return result
            except Exception as e:
                if not is_quota_error(e):
                    raise
                quota_exc = quota_exc or e
                _latch(key, model, latch_s)

    if quota_exc is None:
        # Every key was latched, so Gemini was never asked this time.
        quota_exc = RuntimeError(f"429 RESOURCE_EXHAUSTED: all Gemini keys latched for {model}")

    # 3a. CodeCraft: already-paid subscription, so tried before paid Claude.
    if pol.get("codecraft"):
        from core.llm.codecraft_failover import try_codecraft

        try:
            return try_codecraft(
                contents=contents, config=config, model=model, timeout_s=pol["timeout_s"]
            )
        except NotEligible as why:
            _log(f"{model} quota exhausted; CodeCraft skipped: {why}")
        except Exception as e:
            _log(f"{model} quota exhausted; CodeCraft failed: {type(e).__name__}: {str(e)[:200]}")

    # 3b. Claude.
    try:
        return _try_claude(pol, model=model, contents=contents, config=config)
    except NotEligible as why:
        _log(f"{model} quota exhausted; Claude failover skipped: {why}")
    except Exception as e:
        _log(f"{model} quota exhausted; Claude failover failed: {type(e).__name__}: {str(e)[:200]}")

    # 4. Local model: free, no network, no daily cap.
    try:
        return _try_local(pol, model=model, contents=contents, config=config)
    except NotEligible as why:
        _log(f"{model} quota exhausted; local failover skipped: {why}")
    except Exception as e:
        _log(f"{model} quota exhausted; local failover failed: {type(e).__name__}: {str(e)[:200]}")
    raise quota_exc


# ── installation ──────────────────────────────────────────────────────────────


def install(*, force: bool = False) -> bool:
    """Wrap Models.generate_content once per process. Idempotent.

    Skipped under pytest unless forced: a test that trips a fake quota error on
    a real SDK object must never spawn a paid Claude call.
    """
    global _ORIGINAL
    if not force and ("pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST")):
        return False
    with _INSTALL_LOCK:
        try:
            from google.genai import models as genai_models
        except Exception:
            return False
        current = genai_models.Models.generate_content
        if getattr(current, "_era_quota_failover", False):
            return True
        _ORIGINAL = current

        def generate_content(self, *, model, contents, config=None):
            return generate_with_failover(self, model=model, contents=contents, config=config)

        generate_content._era_quota_failover = True  # type: ignore[attr-defined]
        generate_content.__doc__ = current.__doc__
        genai_models.Models.generate_content = generate_content
        return True


def uninstall() -> None:
    """Restore the SDK method. For tests."""
    global _ORIGINAL
    with _INSTALL_LOCK:
        if _ORIGINAL is None:
            return
        from google.genai import models as genai_models

        genai_models.Models.generate_content = _ORIGINAL
        _ORIGINAL = None
    with _STATE_LOCK:
        _LATCH.clear()
        _ROTATION_CLIENTS.clear()
        _CLAUDE_CALLS.clear()


def status() -> dict[str, Any]:
    with _STATE_LOCK:
        return {
            "installed": _ORIGINAL is not None,
            "latched_pairs": len(_LATCH),
            "claude_calls_today": dict(_CLAUDE_CALLS),
            "policy": policy(),
        }


if __name__ == "__main__":  # python -m core.llm.quota_failover
    print(json.dumps(status(), indent=2, default=str))
