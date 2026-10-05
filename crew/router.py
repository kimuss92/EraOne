"""crew/router.py — the classifier: a crew turn's task shape -> a quality hint for Hermes.

Era is the GATE; Hermes is the brain and the router. Era does NOT pick a provider. It classifies
the TASK (five axes, a pure function, no LLM call, no tokenizer) into one of three quality hints,
`trivial`, `standard` or `critical`, and the Hermes transport (crew/hermes_transport.py) sends
that hint along with the text. Which provider answers, in what order, and what happens when one is
down or spent is Hermes's own configuration (its profile's `model_routes` and
`fallback_providers`). Era holds no provider list, no budget and no provider adapter.

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
  critical   validated output required, or high cost of error
  standard   tool calling, long context, or several hops
  trivial    everything else

OBSERVABILITY (required, no exceptions)
  Every model call a crew turn makes emits exactly one line:
      [crew] bot=<id> tier=<tier> provider=<provider> tokens_in=<n> tokens_out=<n> cached=<bool>
  Token counts are the provider's own usage when it reports one, else characters/4 (an
  estimate, only ever used for this line).

This module must not import lane_router / lane_dispatch (see crew/transport.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.logger import setup_logger

router_logger = setup_logger("era_one.crew.route")

TIERS = ("trivial", "standard", "critical")
LONG_CONTEXT_CHARS = 32_000

_EFFECTFUL = frozenset({"vault.create", "vault.append", "vault.replace_staged"})


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


def tier_for(shape: Axes) -> str:
    if shape.output_verified or shape.cost_of_error == "high":
        return "critical"
    if (
        shape.needs_tool_calling
        or shape.context_length_tier == "long"
        or shape.hop_count == "multi"
    ):
        return "standard"
    return "trivial"


def choose_tier(spec: Any, turn: Any, system: str = "", user: str = "") -> tuple[str, Axes]:
    """The hint for THIS turn, and the shape it came from. The bot's `tier_preference` is a soft
    default: it applies only when the turn said nothing about its own shape."""
    shape, signalled = _shape(spec, turn, system, user)
    tier = tier_for(shape)
    preference = str(getattr(spec, "tier_preference", "") or "")
    if preference in TIERS and not signalled:
        tier = preference
    return tier, shape


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
