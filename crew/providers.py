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

import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Protocol


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


def default_executors() -> dict[str, Executor]:
    return {
        "ollama": OllamaExecutor(),
        "claude_cli": ClaudeCliExecutor(),
        "nous": UnconfiguredExecutor(
            "nous", "no Nous adapter yet (reconnaissance first, then the Hermes transport)"
        ),
        "openai": UnconfiguredExecutor("openai", "no OpenAI adapter or key is configured"),
    }
