"""Provider-neutral request/response types for Era's LLM layer.

WHAT BELONGS HERE
  The shapes every provider must speak. Nothing in this module imports a
  provider SDK, reads config, or makes a call — it is pure data, so it stays
  importable from anywhere (including a test that has no keys and no network).

WHAT DOES NOT
  Anything a single provider can do and the others cannot. Those go in
  ``LlmRequest.provider_options``, keyed by provider name, and are read ONLY
  inside ``core/llm/adapters/``. That rule is what keeps provider quirks from
  leaking back into actions/ — the exact leak this layer exists to prevent.

WHY A SEPARATE RealtimeVoiceProvider (see provider.py)
  Era's voice loop sends raw 16 kHz PCM into an open bidirectional session and
  gets audio back. Gemini Live does that; Claude has no realtime audio API at
  all. Modelling it as an optional method on one big interface would mean every
  other adapter carries a method it can only raise from, and callers would have
  to guess. It is a separate protocol, gated by ``Capability.realtime_audio``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── Vocabulary ──────────────────────────────────────────────────────────────

Role = Literal["system", "user", "assistant", "tool"]

StopReason = Literal[
    "end_turn",  # finished naturally
    "tool_use",  # wants one or more tools run
    "max_tokens",  # hit the output ceiling
    "refusal",  # declined on policy grounds
    "cancelled",  # caller cancelled
    "error",  # provider or transport failure
]

# Normalised failure taxonomy. Adapters map their SDK's exceptions onto these so
# the reliability policy (retry? route elsewhere? tell the user?) is written
# once instead of per provider.
ErrorKind = Literal[
    "auth",  # bad or missing credentials — never retry
    "rate_limit",  # backoff and retry, or route elsewhere
    "quota",  # exhausted for a while — cool down (Gemini 1011)
    "timeout",
    "context_overflow",  # input too large for the model
    "unsupported",  # asked for a capability this alias lacks
    "refusal",  # model declined; information, not a fault to retry
    "invalid_response",  # unparseable / schema-violating output
    "transient",  # network blip, 5xx — retry
    "cancelled",
]

StreamEventKind = Literal[
    "text_delta",
    "tool_call",
    "transcript_in",  # what the user said (voice)
    "transcript_out",  # what Era is saying (voice)
    "audio",  # PCM frames out
    "turn_complete",
    "usage",
    "error",
    "provider_signal",  # e.g. Gemini GoAway / session rotation — opaque upstream
]


# ── Capability ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capability:
    """What one alias can actually do, plus whether it is usable right now.

    ``available`` is deliberately separate from the feature flags: an alias can
    be perfectly capable in principle and still be unreachable in this
    environment. Era's own ``era.cheap`` is exactly that case — see
    config/llm.yaml. Reporting it as "unavailable, because <reason>" lets the
    router explain itself out loud instead of surfacing a 404 mid-sentence.
    """

    realtime_audio: bool = False
    tools: bool = False
    json_schema: bool = False
    vision: bool = False
    streaming: bool = False
    long_context: bool = False
    cost_reporting: bool = False
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    available: bool = True
    unavailable_reason: str = ""

    def satisfies(self, **needs: bool) -> tuple[bool, str]:
        """Check requested capabilities. Returns (ok, human-readable reason).

        The reason is for saying out loud, so it names the missing capability
        rather than dumping a flag set.
        """
        if not self.available:
            return False, self.unavailable_reason or "this model is not available"
        for name, needed in needs.items():
            if not needed:
                continue
            if not getattr(self, name, False):
                return False, f"it does not support {name.replace('_', ' ')}"
        return True, ""


# ── Messages ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContentPart:
    """One piece of message content. ``kind`` decides which field is set."""

    kind: Literal["text", "image", "audio", "document"] = "text"
    text: str = ""
    data: bytes | None = None
    mime_type: str = ""
    # For provider file-reference styles (Gemini file API, Anthropic file_id).
    uri: str = ""


@dataclass(frozen=True)
class LlmMessage:
    role: Role
    parts: tuple[ContentPart, ...] = ()

    @classmethod
    def text(cls, role: Role, text: str) -> LlmMessage:
        return cls(role=role, parts=(ContentPart(kind="text", text=text),))

    @property
    def as_text(self) -> str:
        return "".join(p.text for p in self.parts if p.kind == "text")


@dataclass(frozen=True)
class PromptBlock:
    """A chunk of system prompt, with a caching hint.

    ``cache_hint`` marks a block as stable enough to cache. It exists because
    Era's assembled system prompt puts its most volatile content — a timestamp
    and a live window list — at the very FRONT, which on a prefix-matching
    cache means nothing after it can ever be reused. Ordering frozen blocks
    first and volatile ones last is the whole optimisation; this flag is how a
    caller expresses which is which without knowing whose cache it is.
    """

    text: str
    cache_hint: bool = False
    label: str = ""  # for logs/diagnostics, e.g. "prompt.txt", "os_vision"


# ── Tools ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCall:
    """A tool the model wants run. NEVER executed by an adapter — adapters
    return these, and only the deterministic execution layer acts on them."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResultMsg:
    """The outcome handed back to the model."""

    tool_call_id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


# ── Usage and cost ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Usage:
    """Tokens, money and time for one request.

    ``est_cost_usd`` is an ESTIMATE unless ``reported_cost`` is True. The
    Claude CLI reports a real ``total_cost_usd``; Gemini reports nothing today,
    so its figure is derived from the registry's prices. Keeping the flag means
    a cost dashboard can say which numbers it actually measured.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    est_cost_usd: float = 0.0
    reported_cost: bool = False
    latency_ms: int = 0
    first_token_ms: int | None = None

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


# ── Request / response ──────────────────────────────────────────────────────


@dataclass
class LlmRequest:
    """One call, described without reference to any provider.

    ``alias`` is an INTERNAL name (era.reason, era.voice…), never a provider
    model id. Resolving it is the registry's job, so a model rename is a config
    edit rather than a code change.
    """

    alias: str
    messages: tuple[LlmMessage, ...] = ()
    system: tuple[PromptBlock, ...] = ()
    tools: tuple[dict[str, Any], ...] = ()  # provider dialect, from toolspec
    json_schema: dict[str, Any] | None = None
    max_output_tokens: int = 4096
    effort_hint: Literal["low", "medium", "high"] | None = None
    timeout_s: float = 60.0
    request_id: str = ""
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def system_text(self) -> str:
        """Blocks joined for providers that take a single system string.
        Order is preserved — it is load-bearing for prefix caching."""
        return "\n".join(b.text for b in self.system if b.text)


@dataclass
class LlmResponse:
    request_id: str
    provider: str
    model_id: str
    alias: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    # Escape hatch for adapter-internal debugging. Reading this outside
    # core/llm/adapters/ defeats the whole abstraction.
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use" or bool(self.tool_calls)


@dataclass(frozen=True)
class StreamEvent:
    kind: StreamEventKind
    text: str = ""
    tool_call: ToolCall | None = None
    audio: bytes | None = None
    usage: Usage | None = None
    error: LlmError | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ── Errors ──────────────────────────────────────────────────────────────────


class LlmError(Exception):
    """A provider failure, normalised.

    ``retryable`` and ``kind`` are what the reliability policy branches on, so
    a new provider only has to classify its own exceptions once.
    """

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        provider: str = "",
        alias: str = "",
        retryable: bool | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider = provider
        self.alias = alias
        self.retry_after_s = retry_after_s
        self.retryable = _DEFAULT_RETRYABLE[kind] if retryable is None else retryable

    def __str__(self) -> str:
        where = f" [{self.provider}/{self.alias}]" if self.provider else ""
        return f"{self.kind}{where}: {self.message}"


# Whether a failure is worth trying again AT ALL — distinct from whether to try
# the same provider. auth/unsupported/refusal are terminal by nature: retrying
# a bad key or a policy decline just burns time and quota.
_DEFAULT_RETRYABLE: dict[str, bool] = {
    "auth": False,
    "rate_limit": True,
    "quota": False,
    "timeout": True,
    "context_overflow": False,
    "unsupported": False,
    "refusal": False,
    "invalid_response": True,
    "transient": True,
    "cancelled": False,
}
