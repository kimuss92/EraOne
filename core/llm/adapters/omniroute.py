"""OmniRoute lane — Era's reasoning calls through the `era-gateway` combo.

WHY (Karim, 2026-10-09)
  "Keep OmniRoute with the combo that includes many models, so Era stays always
  working even if I hit the usage limit." The combo (OmniRoute on
  127.0.0.1:20128) puts the Claude subscription first and falls through to other
  providers by itself, so one usage limit no longer stops Era's reasoning.

WHERE IT SITS
  ClaudeCliProvider.generate() tries this lane FIRST and falls back to the
  subscription `claude -p` accounts on ANY failure here (gateway down, 5xx,
  timeout, bad key). The fallback is logged, never silent. That covers every
  claude_cli alias: autonomous development, planning, Gemini quota failover and
  the Claude voice path.

WHAT IT SENDS
  Anthropic Messages API, `model: era-gateway`, text in / text out. No tools are
  ever sent: as on the CLI path, the model reasons and Era's gated layer acts.

THE KEY
  ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY. The process env is used only when its
  base URL is loopback; otherwise both come from the Windows user env
  (HKCU\\Environment). Inside the Claude desktop app the process env points at
  api.anthropic.com, and sending the OmniRoute key there is a leak (it happened
  once, 2026-10-09). The key is never logged.

OFF SWITCHES
  config/llm.yaml `routing.omniroute.enabled: false`, or ERA_OMNIROUTE=0. Never
  active under pytest: a test must not reach a real gateway.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from core.llm import registry
from core.llm.types import LlmError, LlmRequest, LlmResponse, Usage
from core.logger import log_event, setup_logger

omniroute_logger = setup_logger("era_one.llm.omniroute")

NAME = "omniroute"
DEFAULT_COMBO = "era-gateway"
DEFAULT_BASE = "http://127.0.0.1:20128"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_ANTHROPIC_VERSION = "2023-06-01"

# http(url, headers, body, timeout) -> (status, headers_dict, parsed_json_or_None)
Http = Callable[[str, dict, bytes, float], "tuple[int, dict, Any]"]


def _policy() -> dict[str, Any]:
    try:
        section = registry.routing_policy().get("omniroute")
    except Exception:  # noqa: BLE001 - a broken config disables the lane, never Era
        return {}
    return section if isinstance(section, dict) else {}


def _windows_user_env(name: str) -> str:
    """A variable as the Windows USER environment holds it (what a new terminal sees)."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip()
    except OSError:
        return ""


def _loopback_base(raw: str) -> str:
    """The gateway base URL, or "" when it is not a loopback address."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in _LOOPBACK:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def endpoint() -> tuple[str, str]:
    """(base_url, key) for the gateway; ("", "") when no loopback gateway is configured.

    A process env whose base is NOT loopback is ignored entirely, key included:
    that key belongs to whatever the base points at, never to OmniRoute.
    """
    proc_base = _loopback_base(os.environ.get("ANTHROPIC_BASE_URL", "").strip())
    if proc_base:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        return proc_base, key or _windows_user_env("ANTHROPIC_API_KEY")
    user_base = _windows_user_env("ANTHROPIC_BASE_URL") or DEFAULT_BASE
    base = _loopback_base(user_base)
    return (base, _windows_user_env("ANTHROPIC_API_KEY")) if base else ("", "")


def enabled() -> bool:
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if os.environ.get("ERA_OMNIROUTE", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(_policy().get("enabled", False))


def _urllib_http(url: str, headers: dict, body: bytes, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            status, hdrs, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        status, hdrs, raw = exc.code, dict(exc.headers or {}), exc.read()
    try:
        return status, hdrs, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return status, hdrs, None


def _system(request: LlmRequest) -> str:
    system = request.system_text()
    if request.json_schema is not None:
        system = (
            system + "\n\nRespond with a single JSON object and nothing else — no prose, no "
            "markdown fences. It must validate against this JSON Schema:\n"
            + json.dumps(request.json_schema, indent=2)
        ).strip()
    return system


def _messages(request: LlmRequest) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for message in request.messages:
        if message.role == "system" or not message.as_text:
            continue
        role = "assistant" if message.role == "assistant" else "user"
        if out and out[-1]["role"] == role:  # the API wants alternating turns
            out[-1]["content"] += "\n\n" + message.as_text
        else:
            out.append({"role": role, "content": message.as_text})
    return out


def _fail(status: int, body: Any) -> LlmError:
    err = body.get("error") if isinstance(body, dict) else None
    detail = err.get("message") if isinstance(err, dict) else ""
    kind = "auth" if status in (401, 403) else "rate_limit" if status == 429 else "transient"
    return LlmError(kind, f"OmniRoute HTTP {status} {str(detail)[:160]}".strip(), provider=NAME)


def try_first(request: LlmRequest) -> LlmResponse | None:
    """The gateway's answer, or None so the caller falls back (logged, never silent)."""
    if not enabled():
        return None
    try:
        return generate(request)
    except LlmError as exc:
        log_event(
            omniroute_logger,
            "llm.omniroute.fallback",
            "WARNING",
            alias=request.alias,
            kind=exc.kind,
            reason=exc.message[:200],
        )
        return None


def generate(request: LlmRequest, *, http: Http | None = None) -> LlmResponse:
    """One call to the era-gateway combo. Raises LlmError on any failure."""
    base, key = endpoint()
    if not base:
        raise LlmError("unsupported", "no loopback OmniRoute gateway configured", provider=NAME)
    if not key:
        raise LlmError("auth", "no OmniRoute key in ANTHROPIC_API_KEY", provider=NAME)
    messages = _messages(request)
    if not messages:
        raise LlmError("unsupported", "nothing to ask", provider=NAME, alias=request.alias)
    combo = str(_policy().get("combo") or DEFAULT_COMBO)
    payload: dict[str, Any] = {
        "model": combo,
        "max_tokens": int(request.max_output_tokens or 4096),
        "messages": messages,
    }
    system = _system(request)
    if system:
        payload["system"] = system
    timeout = float(_policy().get("timeout_s") or request.timeout_s or 60.0)
    headers = {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    started = time.perf_counter()
    try:
        status, resp_headers, body = (http or _urllib_http)(
            f"{base}/v1/messages", headers, json.dumps(payload).encode("utf-8"), timeout
        )
    except TimeoutError as exc:
        raise LlmError(
            "timeout", f"OmniRoute gave no answer in {timeout:.0f}s", provider=NAME
        ) from exc
    except Exception as exc:  # noqa: BLE001 - refused, reset, DNS: the gateway is down
        raise LlmError(
            "transient", f"OmniRoute unreachable ({type(exc).__name__})", provider=NAME
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    if status != 200 or not isinstance(body, dict):
        raise _fail(status, body)
    text = "".join(
        str(c.get("text") or "")
        for c in body.get("content") or []
        if isinstance(c, dict) and c.get("type", "text") == "text"
    ).strip()
    if request.json_schema is not None:
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
        try:
            json.loads(text)
        except (ValueError, TypeError) as exc:
            raise LlmError(
                "invalid_response", f"expected JSON, got: {text[:200]}", provider=NAME
            ) from exc
    lower = {str(k).lower(): v for k, v in (resp_headers or {}).items()}
    model = str(lower.get("x-omniroute-model") or body.get("model") or combo)
    member = str(lower.get("x-omniroute-provider") or "")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    # Master prompt §3.2: record which combo member answered, and how fast.
    log_event(
        omniroute_logger,
        "llm.omniroute.answered",
        "INFO",
        combo=combo,
        model=model,
        member_provider=member,
        alias=request.alias,
        latency_ms=latency_ms,
    )
    return LlmResponse(
        request_id=request.request_id or f"omniroute-{int(time.time() * 1000)}",
        provider=NAME,
        model_id=model,
        alias=request.alias,
        text=text,
        stop_reason="end_turn",
        usage=Usage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
        ),
        raw={"combo": combo, "member_provider": member},
    )
