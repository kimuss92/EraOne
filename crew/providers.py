"""crew/providers.py — the model providers crew/router.py can route a turn to.

An `Executor` turns (system, user, tier) into a `ProviderReply`. Today two are real, through
Era's existing adapters: `ollama` (the local floor) and `claude_cli` (the subscription
`claude -p` oracle: no tools, a scratch cwd, under the shared USD ledger). `nous` and
`openai` are named in `routing.tiers` but Era has no adapter or credentials for them yet, so
they are `UnconfiguredExecutor`s that say why and are skipped until one exists.

The real executors refuse to run under pytest: a model call from the suite spends money and
needs a login (this repo has been burned by tests reaching live services before). Tests
inject their own executors.

This module must not import lane_router / lane_dispatch (see crew/transport.py).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from core.utils import CONFIG_PATH


@dataclass(frozen=True)
class ProviderReply:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


class Executor(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def alias_for(self, tier: str) -> str: ...

    def call(self, system: str, user: str, tier: str, timeout_s: float) -> ProviderReply: ...


def _under_pytest() -> bool:
    # A real model call from the suite spends money and needs a login (this repo has been
    # burned by tests reaching live services before). Tests inject their own executors.
    return "PYTEST_CURRENT_TEST" in os.environ


def _request(alias: str, system: str, user: str, timeout_s: float):
    from core.llm.types import LlmMessage, LlmRequest, PromptBlock

    return LlmRequest(
        alias=alias,
        messages=(LlmMessage.text("user", user),),
        system=(PromptBlock(text=system),) if system else (),
        timeout_s=timeout_s,
        request_id=f"crew-{int(time.time() * 1000)}",
    )


def _reply_from(response: Any) -> ProviderReply:
    usage = response.usage
    return ProviderReply(
        text=response.text or "",
        tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
        tokens_out=int(getattr(usage, "output_tokens", 0) or 0),
        model=str(response.model_id or ""),
    )


class OllamaExecutor:
    name = "ollama"

    def available(self) -> tuple[bool, str]:
        if _under_pytest():
            return False, "not under pytest"
        return True, ""

    def alias_for(self, tier: str) -> str:
        return "era.local"

    def call(self, system: str, user: str, tier: str, timeout_s: float) -> ProviderReply:
        from core.llm.adapters.ollama_chat import OllamaChatProvider

        alias = self.alias_for(tier)
        return _reply_from(OllamaChatProvider().generate(_request(alias, system, user, timeout_s)))


class ClaudeCliExecutor:
    """The subscription `claude -p` oracle (no tools, scratch cwd), under the shared USD ledger."""

    name = "claude_cli"

    def available(self) -> tuple[bool, str]:
        if _under_pytest():
            return False, "not under pytest"
        from core.llm.adapters.claude_cli import CLAUDE_BIN

        return (True, "") if shutil.which(CLAUDE_BIN) else (False, "the claude CLI is not on PATH")

    def alias_for(self, tier: str) -> str:
        return "era.reason" if tier == "critical" else "era.cheap"

    def call(self, system: str, user: str, tier: str, timeout_s: float) -> ProviderReply:
        from core.llm import registry
        from core.llm.adapters.claude_cli import ClaudeCliProvider
        from core.llm.spend import from_registry

        alias = self.alias_for(tier)
        ledger = from_registry()
        ok, why = ledger.can_afford(registry.resolve(alias).call_overhead_usd)
        if not ok:
            raise RuntimeError(why)
        response = ClaudeCliProvider().generate(_request(alias, system, user, timeout_s))
        try:
            ledger.record(response.usage.est_cost_usd, alias=alias)
        except Exception:  # noqa: BLE001 — bookkeeping must not fail the answer
            pass
        return _reply_from(response)


DEFAULT_NOUS_BASE = "https://inference-api.nousresearch.com/v1"
# The free model the user's own Hermes profile already names for Nous. UNVERIFIED against a live
# call until tools/probe_nous.py has run; override with routing.nous.model / routing.nous.models.
DEFAULT_NOUS_MODEL = "poolside/laguna-xs-2.1:free"
NOUS_KEY_ENTRY = "nous_api_key"
_NOUS_HOST = "nousresearch.com"

# http(method, url, headers, body_or_None, timeout) -> (status, parsed_json_or_None, lower-cased headers)
HttpFn = Callable[[str, str, dict, "bytes | None", float], "tuple[int, Any, dict]"]


class RateLimited(RuntimeError):
    """A provider said slow down. `retry_after_s` is how long; the router honours it."""

    def __init__(self, message: str, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_s = float(retry_after_s or 0.0)


def _https_json(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https, nous host only
            status, raw, hdrs = resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        status, raw, hdrs = exc.code, exc.read(), dict(exc.headers or {})
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        parsed = None
    return status, parsed, {str(k).lower(): str(v) for k, v in hdrs.items()}


def read_nous_key(config_path: Path | None = None) -> str:
    """The Nous key from config/api_keys.json, read NOW (never cached). "" when absent."""
    try:
        data = json.loads(Path(config_path or CONFIG_PATH).read_text(encoding="utf-8-sig"))
        value = data.get(NOUS_KEY_ENTRY) if isinstance(data, dict) else None
    except (OSError, ValueError):
        value = None
    key = value.strip() if isinstance(value, str) else ""
    return "" if not key or key.lower() == "placeholder" else key


def _check_nous_base(base_url: str) -> str:
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == _NOUS_HOST or host.endswith("." + _NOUS_HOST)):
        # So a mis-edit of the config can never send the key anywhere but Nous, in the clear.
        raise ValueError(f"the Nous base URL must be https on {_NOUS_HOST}, got {base_url!r}")
    return base_url.rstrip("/")


def _rate_limit_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.startswith("x-ratelimit") or k == "retry-after"}


def _retry_after(headers: dict) -> float:
    try:
        return max(0.0, float(headers.get("retry-after", "") or 0))
    except ValueError:
        return 0.0


class NousExecutor:
    """Nous Portal's OpenAI-compatible inference API: text in, text out, no tools.

    The key is `nous_api_key` in config/api_keys.json, read at call time, sent only as a Bearer
    header to an https *.nousresearch.com URL, and never logged or put in an error. A 429 raises
    `RateLimited` with the Retry-After the router then honours; the rate-limit headers of the
    last answer are kept in `last_rate_limit` so what Nous actually allows can be OBSERVED.
    """

    name = "nous"

    def __init__(
        self,
        base_url: str = DEFAULT_NOUS_BASE,
        model: str = DEFAULT_NOUS_MODEL,
        models: dict[str, str] | None = None,
        *,
        config_path: Path | None = None,
        http: HttpFn | None = None,
    ) -> None:
        self._base = _check_nous_base(base_url)
        self._model = str(model or DEFAULT_NOUS_MODEL)
        self._models = {str(k): str(v) for k, v in (models or {}).items() if v}
        self._config_path = config_path
        self._injected = http is not None
        self._http: HttpFn = http or _https_json
        self.last_rate_limit: dict = {}

    @classmethod
    def from_config(cls, routing: dict | None, **kw: Any) -> NousExecutor:
        block = dict((routing or {}).get("nous") or {})
        return cls(
            base_url=str(block.get("base_url") or DEFAULT_NOUS_BASE),
            model=str(block.get("model") or DEFAULT_NOUS_MODEL),
            models=block.get("models") if isinstance(block.get("models"), dict) else None,
            **kw,
        )

    def available(self) -> tuple[bool, str]:
        if not self._injected and _under_pytest():
            return False, "not under pytest"
        if not read_nous_key(self._config_path):
            return False, f"no {NOUS_KEY_ENTRY} in config/api_keys.json"
        return True, ""

    def alias_for(self, tier: str) -> str:
        return self._models.get(tier) or self._model

    def call(self, system: str, user: str, tier: str, timeout_s: float) -> ProviderReply:
        key = read_nous_key(self._config_path)
        if not key:
            raise RuntimeError(f"no {NOUS_KEY_ENTRY} in config/api_keys.json")
        model = self.alias_for(tier)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        status, body, headers = self._http(
            "POST",
            f"{self._base}/chat/completions",
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
            timeout_s,
        )
        self.last_rate_limit = _rate_limit_headers(headers)
        if status == 429:
            raise RateLimited("Nous rate limit (HTTP 429)", _retry_after(headers))
        if status in (401, 403):
            raise RuntimeError(f"Nous rejected the key (HTTP {status})")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"Nous answered HTTP {status}")
        try:
            text = str(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            text = ""
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return ProviderReply(
            text=text,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            model=str(body.get("model") or model),
        )


class UnconfiguredExecutor:
    """A provider named in `routing.tiers` that Era has no adapter or credentials for yet."""

    def __init__(self, name: str, why: str) -> None:
        self.name, self._why = name, why

    def available(self) -> tuple[bool, str]:
        return False, self._why

    def alias_for(self, tier: str) -> str:
        return ""

    def call(self, system: str, user: str, tier: str, timeout_s: float) -> ProviderReply:
        raise RuntimeError(self._why)


def default_executors(routing: dict | None = None) -> dict[str, Executor]:
    try:
        nous: Executor = NousExecutor.from_config(routing)
    except ValueError as exc:  # a bad routing.nous.base_url must disable Nous, not Era
        nous = UnconfiguredExecutor("nous", str(exc))
    return {
        "ollama": OllamaExecutor(),
        "claude_cli": ClaudeCliExecutor(),
        "nous": nous,
        "openai": UnconfiguredExecutor("openai", "no OpenAI adapter or key is configured"),
    }
