"""The provider interfaces every adapter implements.

TWO protocols, on purpose.

  LLMProvider          request in, response (or stream) out. Gemini's
                       generateContent path, the Claude CLI, and Ollama all fit.

  RealtimeVoiceProvider an OPEN bidirectional session: PCM in, audio + tool
                       calls + transcripts out, with the provider owning turn
                       detection. Only Gemini Live implements this, and no
                       amount of interface design makes Claude able to.

Keeping them apart means an adapter never carries a method it can only raise
from, and a caller never has to discover the hard way that the model it routed
voice to cannot hear. The capability registry answers that question first.

These are typing.Protocol, not ABCs: adapters are plain classes that happen to
match, so nothing has to import this module at runtime just to be usable, and a
test double is a five-line class rather than a subclass.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from core.llm.types import Capability, LlmRequest, LlmResponse, StreamEvent


@runtime_checkable
class LLMProvider(Protocol):
    """Request/response and streaming. Implementations must not execute tools.

    An adapter's whole job is translation: Era's normalised request in, the
    provider's dialect out, and the provider's reply back into an LlmResponse.
    It returns ToolCall objects; deciding whether a tool may run, and running
    it, belongs to the deterministic execution layer. An adapter that calls
    into actions/ has broken the safety boundary this layer exists to hold.
    """

    name: str

    def capabilities(self, alias: str) -> Capability:
        """What this alias can do, and whether it is usable right now."""
        ...

    def generate(self, request: LlmRequest) -> LlmResponse:
        """One complete turn. Raises LlmError (never a raw SDK exception)."""
        ...

    def stream(self, request: LlmRequest) -> Iterator[StreamEvent]:
        """Incremental turn. Providers without real streaming may emit a single
        text_delta followed by turn_complete — callers must not depend on
        chunk granularity, only on event order."""
        ...

    def cancel(self, request_id: str) -> None:
        """Best-effort abort. Must be safe to call for an unknown or already
        finished request — cancellation races completion by nature."""
        ...


@runtime_checkable
class RealtimeVoiceProvider(Protocol):
    """An open bidirectional audio session. Gemini Live only.

    Deliberately NOT part of LLMProvider — see the module docstring. Gate every
    use on ``Capability.realtime_audio`` rather than on isinstance: an adapter
    may satisfy the protocol structurally while being unconfigured.
    """

    name: str

    def connect(self, request: LlmRequest) -> None:
        """Open the session. The request's system blocks and tools configure it;
        on Gemini these are fixed for the session's lifetime, which is why a
        voice-mode change costs a reconnect."""
        ...

    def send_audio(self, pcm: bytes, *, mime_type: str = "audio/pcm") -> None:
        """Push mic frames. Called at audio-chunk rate — must not block."""
        ...

    def send_text(self, text: str) -> None:
        """Inject text as conversational input to the live session."""
        ...

    def receive(self) -> Iterator[StreamEvent]:
        """Everything coming back: audio, transcripts, tool calls, and
        provider_signal events for things with no neutral equivalent (session
        rotation, GoAway). Callers treat provider_signal as opaque and let the
        adapter's owner interpret it."""
        ...

    def close(self) -> None:
        """Tear down. Must be idempotent — a dropped connection and a
        deliberate close arrive at the same place."""
        ...
