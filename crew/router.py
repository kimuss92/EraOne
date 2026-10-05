"""crew/router.py — tiered, budget-aware, task-classified model routing for crew turns.

A crew turn is CLASSIFIED (five axes, a pure function, no LLM call, no tokenizer), the axes
give a TIER, the tier names an ordered list of providers in config/llm.yaml `routing.tiers`,
and the first provider that is available AND under its daily budget answers. Replaces the
old linear fallback chain.

THE TIER BELONGS TO THE TASK, NOT THE BOT
  Drew searching the vault is `standard`; Drew saving a note is `critical`. Maya drafting prose
  is `standard`; Maya producing a validated JSON artifact for a scheduled post is `critical` for
  THAT turn only. Cody reasoning is `standard`; Cody producing code that must compile is
  `critical`. A bot's yaml may carry a `tier_preference`, but only as a SOFT default for a turn
  whose own shape says nothing; it is never a hard tier.

THE FIVE AXES OF A TASK SHAPE (each is its own pure function so it can be tested alone)
  needs_tool_calling   the bot must emit tool-call JSON (default: it was granted capabilities)
  context_length_tier  "long" at >= LONG_CONTEXT_CHARS of prompt. A LENGTH test (8k tokens ~ 32k
                       characters), not token counting.
  output_verified      the turn must produce output that is mechanically VALIDATED: JSON that
                       parses against a schema, code that compiles, arithmetic. A weak model
                       fails that, so it raises the tier.
  hop_count            "multi" for a turn that IS a handoff
  cost_of_error        "high" when the task is to WRITE something (save, create, append,
                       replace, schedule, publish, send...) AND the bot can write at all

  Where a shape comes from, per axis: `context["task_shape"]` (set by whoever hands the task
  over: Laura's crew.handoff `shape`, a scheduler) wins; otherwise it is derived from the TASK
  TEXT only. Observations (vault text, tool output) are untrusted data and never steer the
  tier. No LLM call, no tokenizer.

SHAPE -> TIER (tier_for)
  offline    the network is down: only `routing.tiers.offline` is considered
  critical   validated output required, or high cost of error
  standard   tool calling, long context, or several hops
  trivial    everything else

NO SILENT CROSS-TIER ROUTING
  A tier whose providers are all unavailable or out of budget FAILS (`LLMUnavailable`, with
  every reason). It moves to another tier only when the TASK carries
  `context["allow_cross_tier"] is True`, and every such move is logged
  (`[crew] cross_tier bot= from= to= reason=`). `on_exhaust: next_tier` in the budget means "skip that provider
  and try the next one in the SAME tier's list" — it never leaves the tier by itself.

OBSERVABILITY (required, no exceptions)
  Every model call a crew turn makes — a provider answer, a cache hit, or a failure to find
  any provider — emits exactly one line:
      [crew] bot=<id> tier=<tier> provider=<provider> tokens_in=<n> tokens_out=<n> cached=<bool>
  Budget tracking and audits are read from it. Token counts are the provider's own usage when
  it reports one, else characters/4 (an estimate, only ever used for this line).

A provider with NO row under `routing.budget` is unusable (fail closed): only
`always_available: true` or a `daily_turns` cap lets a provider answer.

This module must not import lane_router / lane_dispatch (see crew/transport.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logger import setup_logger
from core.utils import BASE_DIR
from crew.cache import CrewCaches, ProviderRequestCache
from crew.providers import (  # noqa: F401  (re-exported: the router's public surface)
    ClaudeCliExecutor,
    Executor,
    OllamaExecutor,
    ProviderReply,
    UnconfiguredExecutor,
    default_executors,
)

router_logger = setup_logger("era_one.crew.route")

TIERS = ("trivial", "standard", "critical", "offline")
LONG_CONTEXT_CHARS = 32_000
COOLDOWN_S = 60.0  # a provider that just failed is skipped this long
OFFLINE_LATCH_S = 60.0  # after a "no network" failure the offline tier is used this long
MAX_CALL_TIMEOUT_S = 90.0

# Where a task that explicitly allows it may go when its own tier has nothing to offer.
# Offline never crosses into the online tiers: there is no network to cross to.
_CROSS_TIER = {
    "trivial": ("standard",),
    "standard": ("critical", "trivial"),
    "critical": ("standard",),
    "offline": (),
}
_EFFECTFUL = frozenset({"vault.create", "vault.append", "vault.replace_staged"})
_VERIFIERS = frozenset({"code.check"})
# The budget table uses the product's name for this provider.
_BUDGET_KEY = {"nous": "nous_portal"}


# ── the five axes: a task shape ─────────────────────────────────────────────
@dataclass(frozen=True)
class Axes:
    needs_tool_calling: bool
    context_length_tier: str  # "short" | "long"
    output_verified: bool  # the turn must produce mechanically VALIDATED output
    hop_count: str  # "single" | "multi"
    cost_of_error: str  # "low" | "high"


TaskShape = Axes

_SHAPE_VALUES = {
    "needs_tool_calling": (bool,),
    "output_verified": (bool,),
    "context_length_tier": ("short", "long"),
    "hop_count": ("single", "multi"),
    "cost_of_error": ("low", "high"),
}
# Whole words in the TASK text. Deliberately short: a false hit only costs a stronger model,
# and a bot that cannot write can never be made "high cost" by its text (see axis_cost_of_error).
_EFFECT_WORDS = re.compile(
    r"\b(save|write|create|append|replace|update|rename|schedule|publish|post|send|commit)\b",
    re.IGNORECASE,
)
_VERIFY_WORDS = re.compile(
    r"\b(json|schema|compile[sd]?|compiling|validated?|unit tests?|tests? (?:must )?pass(?:es)?"
    r"|type-?check(?:s|ed)?|lint(?:ed)?)\b",
    re.IGNORECASE,
)


def coerce_shape(raw: Any) -> dict:
    """The valid part of a caller-supplied shape: known keys, right-typed values, nothing else."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key, allowed in _SHAPE_VALUES.items():
        if key not in raw:
            continue
        value = raw[key]
        if allowed == (bool,):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(value, str) and value.lower() in allowed:
            out[key] = value.lower()
    return out


def axis_needs_tool_calling(spec: Any) -> bool:
    return bool(spec.capabilities)


def axis_context_length_tier(*texts: str) -> str:
    return "long" if sum(len(t or "") for t in texts) >= LONG_CONTEXT_CHARS else "short"


def axis_output_verified(turn: Any) -> bool:
    """Validated output is required: the caller says so, or the TASK asks for JSON, a schema,
    code that compiles, passing tests."""
    ctx = turn.context or {}
    return bool(ctx.get("output_verified")) or bool(_VERIFY_WORDS.search(turn.task or ""))


def axis_hop_count(turn: Any) -> str:
    return "multi" if turn.depth > 0 else "single"


def axis_cost_of_error(spec: Any, turn: Any) -> str:
    """High only when the task is to WRITE something and this bot is able to. A read-only bot
    cannot do harm by writing, whatever its task text says."""
    can_write = any(c in _EFFECTFUL for c in spec.capabilities)
    return "high" if can_write and _EFFECT_WORDS.search(turn.task or "") else "low"


def _shape(spec: Any, turn: Any, system: str, user: str) -> tuple[Axes, bool]:
    """(shape, signalled). `signalled`: the turn said something about its own shape (an explicit
    task_shape, a write/validate request in the task, a long prompt, a handoff)."""
    explicit = coerce_shape((turn.context or {}).get("task_shape"))
    derived = Axes(
        needs_tool_calling=axis_needs_tool_calling(spec),
        context_length_tier=axis_context_length_tier(system, user),
        output_verified=axis_output_verified(turn),
        hop_count=axis_hop_count(turn),
        cost_of_error=axis_cost_of_error(spec, turn),
    )
    shape = Axes(**{**derived.__dict__, **explicit})
    signalled = bool(
        explicit
        or shape.output_verified
        or shape.cost_of_error == "high"
        or shape.context_length_tier == "long"
        or shape.hop_count == "multi"
    )
    return shape, signalled


def classify(spec: Any, turn: Any, system: str = "", user: str = "") -> Axes:
    return _shape(spec, turn, system, user)[0]


def tier_for(shape: Axes, *, online: bool = True) -> str:
    if not online:
        return "offline"
    if shape.output_verified or shape.cost_of_error == "high":
        return "critical"
    if (
        shape.needs_tool_calling
        or shape.context_length_tier == "long"
        or shape.hop_count == "multi"
    ):
        return "standard"
    return "trivial"


def choose_tier(
    spec: Any, turn: Any, system: str = "", user: str = "", *, online: bool = True
) -> tuple[str, Axes]:
    """The tier for THIS turn, and the shape it came from. The bot's `tier_preference` is a soft
    default: it applies only when the turn said nothing about its own shape, and never offline."""
    shape, signalled = _shape(spec, turn, system, user)
    tier = tier_for(shape, online=online)
    preference = str(getattr(spec, "tier_preference", "") or "")
    if online and preference in TIERS and preference != "offline" and not signalled:
        tier = preference
    return tier, shape


# ── config ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Policy:
    tiers: dict[str, tuple[str, ...]]
    budget: dict[str, dict]
    cache: dict

    @classmethod
    def from_mapping(cls, routing: dict | None) -> Policy | None:
        """None when `routing.tiers` is absent: crew turns then keep their old model path."""
        routing = dict(routing or {})
        raw = routing.get("tiers")
        if not isinstance(raw, dict) or not raw:
            return None
        tiers = {
            str(t): tuple(str(p).strip() for p in (providers or []) if str(p).strip())
            for t, providers in raw.items()
            if str(t) in TIERS
        }
        return cls(
            tiers=tiers,
            budget={str(k): dict(v or {}) for k, v in dict(routing.get("budget") or {}).items()},
            cache=dict(routing.get("cache") or {}),
        )


# ── budget ──────────────────────────────────────────────────────────────────
def _budget_path() -> Path:
    override = os.environ.get("ERA_CREW_BUDGET_PATH", "").strip()
    return Path(override) if override else BASE_DIR / "runtime" / "crew_budget.json"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class Budget:
    """Daily turns per provider. The in-memory count is the authority; the file only keeps a
    restart from handing back a fresh day (same philosophy as core/llm/spend.py)."""

    def __init__(
        self,
        rows: dict[str, dict],
        path: Path | None = None,
        today: Callable[[], str] = _today,
    ) -> None:
        self._rows = rows
        self._path = Path(path) if path else _budget_path()
        self._today = today
        self._lock = threading.Lock()
        self._day = today()
        self._turns: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("day") == self._day:
                self._turns = {str(k): int(v) for k, v in dict(data.get("turns") or {}).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            self._turns = {}  # absent or corrupt: start the day at zero, never refuse

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"day": self._day, "turns": self._turns}), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._turns = today, {}

    @staticmethod
    def key(provider: str) -> str:
        return _BUDGET_KEY.get(provider, provider)

    def used(self, provider: str) -> int:
        with self._lock:
            self._roll()
            return self._turns.get(self.key(provider), 0)

    def allowed(self, provider: str) -> tuple[bool, str]:
        row = self._rows.get(self.key(provider))
        if row is None:
            return False, "no budget configured (routing.budget)"
        if row.get("always_available"):
            return True, ""
        cap = row.get("daily_turns")
        if cap is None:
            return False, "no daily_turns configured"
        used = self.used(provider)
        if used >= int(cap):
            return False, f"daily budget spent ({used}/{int(cap)} turns)"
        return True, ""

    def take(self, provider: str) -> None:
        with self._lock:
            self._roll()
            k = self.key(provider)
            self._turns[k] = self._turns.get(k, 0) + 1
            self._save()


# ── the router ──────────────────────────────────────────────────────────────
class Router:
    def __init__(
        self,
        policy: Policy,
        budget: Budget,
        executors: dict[str, Executor],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy, self.budget, self.executors, self._clock = policy, budget, executors, clock
        self._lock = threading.Lock()
        self._cooldown: dict[str, float] = {}
        self._offline_until = 0.0

    def online(self) -> bool:
        return self._clock() >= self._offline_until

    def note_failure(self, provider: str, exc: BaseException) -> None:
        """Skip a provider that just failed; a "no network" failure latches the offline tier."""
        with self._lock:
            wait = max(COOLDOWN_S, min(float(getattr(exc, "retry_after_s", 0) or 0), 3600.0))
            self._cooldown[provider] = self._clock() + wait
            try:
                from core.llm.quota_failover import is_offline_error

                if is_offline_error(exc):
                    self._offline_until = self._clock() + OFFLINE_LATCH_S
            except Exception:  # noqa: BLE001
                pass

    def usable(self, provider: str) -> tuple[bool, str]:
        executor = self.executors.get(provider)
        if executor is None:
            return False, "no executor registered"
        with self._lock:
            until = self._cooldown.get(provider, 0.0)
        if self._clock() < until:
            return False, f"failed a moment ago (retry in {until - self._clock():.0f}s)"
        ok, why = executor.available()
        if not ok:
            return False, why
        return self.budget.allowed(provider)

    def plan(self, tier: str, allow_cross_tier: bool) -> Iterator[tuple[str, str, bool]]:
        """(tier, provider, crossed) in the order they may be tried."""
        for provider in self.policy.tiers.get(tier, ()):
            yield tier, provider, False
        if allow_cross_tier:
            for other in _CROSS_TIER.get(tier, ()):
                for provider in self.policy.tiers.get(other, ()):
                    yield other, provider, True


# ── the required line ───────────────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


def log_turn(
    bot: str, tier: str, provider: str, tokens_in: int, tokens_out: int, cached: bool
) -> str:
    line = (
        f"[crew] bot={bot} tier={tier} provider={provider} "
        f"tokens_in={int(tokens_in)} tokens_out={int(tokens_out)} cached={str(bool(cached)).lower()}"
    )
    router_logger.info(line)
    return line


def log_cross_tier(bot: str, origin: str, to: str, reason: str) -> None:
    router_logger.warning(
        f"[crew] cross_tier bot={bot} from={origin} to={to} reason={reason[:200]!r}"
    )


# ── the LLM callable the transport uses ─────────────────────────────────────
class RoutedLLM:
    def __init__(self, router: Router, caches: CrewCaches | None = None) -> None:
        self.router = router
        self.caches = caches or CrewCaches()

    @property
    def _provider_cache(self) -> ProviderRequestCache:
        return self.caches.provider

    @staticmethod
    def _timeout(turn: Any) -> float:
        left = (turn.deadline - time.monotonic()) if getattr(turn, "deadline", 0) else 1e9
        return max(5.0, min(MAX_CALL_TIMEOUT_S, left))

    def ask(self, spec: Any, turn: Any, system: str, user: str) -> dict:
        """One model call for a crew turn. Returns the reply dict, or
        {"__unavailable__": reason} when no provider may answer, or
        {"__bad_output__": text} when one answered with no JSON object."""
        from crew.nabar import parse_json_object

        origin, _shape_used = choose_tier(spec, turn, system, user, online=self.router.online())
        allow = (turn.context or {}).get("allow_cross_tier") is True
        reasons: list[str] = []
        crossed_to: set[str] = set()
        for tier, provider, crossed in self.router.plan(origin, allow):
            if crossed and tier not in crossed_to:
                crossed_to.add(tier)
                log_cross_tier(spec.id, origin, tier, "; ".join(reasons) or "no provider")
            ok, why = self.router.usable(provider)
            if not ok:
                reasons.append(f"{provider}: {why}")
                continue
            executor = self.router.executors[provider]
            key = ProviderRequestCache.key(provider, executor.alias_for(tier), system, user)
            hit = self._provider_cache.get(key)
            if hit is not None:  # costs no budget
                log_turn(spec.id, tier, provider, hit.tokens_in, hit.tokens_out, True)
                return self._parse(hit.text, parse_json_object)
            self.router.budget.take(provider)
            try:
                reply = executor.call(system, user, tier, self._timeout(turn))
            except Exception as exc:  # noqa: BLE001 — a provider failure is a reason, not a crash
                self.router.note_failure(provider, exc)
                reasons.append(f"{provider}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            tin = reply.tokens_in or estimate_tokens(system + user)
            tout = reply.tokens_out or estimate_tokens(reply.text)
            reply = ProviderReply(reply.text, tin, tout, reply.model)
            self._provider_cache.put(key, reply)
            log_turn(spec.id, tier, provider, tin, tout, False)
            return self._parse(reply.text, parse_json_object)
        log_turn(spec.id, origin, "none", 0, 0, False)
        detail = "; ".join(reasons) or f"no provider is configured for the {origin} tier"
        return {"__unavailable__": f"the {origin} tier has no provider to answer: {detail}"}

    @staticmethod
    def _parse(text: str, parse: Callable[[str], dict | None]) -> dict:
        obj = parse(text)
        return obj if obj else {"__bad_output__": (text or "")[:200]}


def build_default() -> RoutedLLM | None:
    """The router from config/llm.yaml, or None when `routing.tiers` is not configured."""
    try:
        from core.llm.router import routing_policy

        routing = routing_policy()
        policy = Policy.from_mapping(routing)
    except Exception:  # noqa: BLE001 — an unreadable registry keeps the old model path
        return None
    if policy is None:
        return None
    return RoutedLLM(
        Router(policy, Budget(policy.budget), default_executors(routing)),
        CrewCaches.from_config(policy.cache),
    )
