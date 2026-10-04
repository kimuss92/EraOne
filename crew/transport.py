"""crew/transport.py — the transport-agnostic contract for crew bots.

This file is written BEFORE any implementation, on purpose. Whatever carries a bot
turn (today an in-process loop, later Hermes Desktop) implements `Transport` and
nothing else; callers depend on the contract, never on an implementation.

  dispatch(bot_id, turn) -> ToolResult
  register(bot_id, spec)
  capabilities(bot_id)   -> list[str]
  health()               -> HealthStatus

ERROR SHAPE
  Every failure is `fail(error_type, detail)`: a CLOSED `error_type` from ERROR_TYPES
  plus a human `detail`, carried as `ToolResult.fail(error_type=..., error=detail,
  data={"detail": detail})`. `ToolResult.fail` has no `detail` keyword and the core
  dataclass is not changed for this; `data["detail"]` is the one place it lives.
  Never prose-only, never an exception across the boundary.

NO COUPLING TO lane_router
  The crew package must not import `lane_router` / `lane_dispatch` (Era's own router,
  formerly named hermes_*; "Hermes" now only means the separate Hermes Agent app). That router
  decides CM-vs-assistant for typed text; the crew is reached through the `crew` tool.
  Importing its internals would tie a pluggable transport to a router that can change
  underneath it. `tools/test_crew_registry.py` enforces this by scanning the sources.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.tool_result import ToolResult

ERROR_TYPES = frozenset(
    {
        "UnknownBot",
        "BadRequest",
        "InvalidSpec",
        "CapabilityDenied",
        "UnknownCapability",
        "LLMUnavailable",
        "BadModelOutput",
        "StepLimit",
        "HandoffDenied",
        "HandoffDepthExceeded",
        "ReviewPaused",
        "ConfirmationRequired",
        "ConfirmationBusy",
        "TransportDown",
        "MissingCredential",
        "BotError",
    }
)


def fail(error_type: str, detail: str, **data) -> ToolResult:
    """The one constructor for crew failures. Unknown types are a bug, not a string."""
    if error_type not in ERROR_TYPES:
        raise ValueError(f"unknown crew error_type {error_type!r}")
    return ToolResult.fail(
        message=detail,
        error=detail,
        error_type=error_type,
        data={**data, "detail": detail},
    )


@dataclass(frozen=True)
class BotSpec:
    """One bot, as loaded from config/crew/<id>.yaml. Pure data."""

    id: str
    role: str
    description: str
    prompt: str  # the prompt text itself, already read from its file
    capabilities: tuple[str, ...]
    model_tier: str = "fast"  # fast | smart
    brands: tuple[str, ...] = ("*",)  # "*" = every brand
    write_zones: tuple[str, ...] = ("inbox",)
    handoffs_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class Turn:
    """One request to a bot."""

    task: str
    context: dict = field(default_factory=dict)
    depth: int = 0  # handoff depth, so a handoff loop cannot run forever
    origin: str = "user"  # who asked: "user" or a bot id
    deadline: float = 0.0  # time.monotonic() ceiling for the whole call; 0 = set on dispatch


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    transport: str
    detail: str = ""
    bots: int = 0


class Transport(ABC):
    """What carries a bot turn. Implementations must never raise from these methods."""

    name: str = "abstract"

    @abstractmethod
    def register(self, bot_id: str, spec: BotSpec) -> None:
        """Make `bot_id` callable. Re-registering replaces the previous spec."""

    @abstractmethod
    def capabilities(self, bot_id: str) -> list[str]:
        """The capability names `bot_id` may use. [] for an unknown bot."""

    @abstractmethod
    def dispatch(self, bot_id: str, turn: Turn) -> ToolResult:
        """Run one turn. Success -> ToolResult.ok; failure -> `fail(...)` only."""

    @abstractmethod
    def health(self) -> HealthStatus:
        """Cheap liveness. Must not make a model call."""
