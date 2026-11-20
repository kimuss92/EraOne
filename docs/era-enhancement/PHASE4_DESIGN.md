# Phase 4 — unified input, speech completion and captions

Status: incremental implementation under the approved Master Plan.

## First slice: Fish completion and word boundaries

Evidence: Fish's generator catches transport failures even after yielding PCM.
SpeechOut consequently commits a truncated phrase as successful. The maximum
sentence length also slices a token at character 320 when no space exists.

Options considered:
- Continue immediate playback and report partial completion: preserves latency,
  but cannot retract speech or replay safely without word/audio alignment.
- Buffer the entire reply: avoids partial network playback but delays long replies.
- Buffer each Fish phrase to clean response EOF (selected): preserves sentence
  pipelining and makes a failed phrase safe to discard before playback.

Fish will raise a typed incomplete-response error if a transport failure follows
PCM. Buffered callers discard that partial response; the router buffers a phrase
before exposing its chunks to SpeechOut. Existing empty-response fallback and
manual engine selection remain intact. Cancellation must propagate immediately.
Success here means clean transport completion, not proof that the provider spoke
every word; acoustic/provider-content validation remains unverified.

The splitter waits for whitespace or final flush when a token exceeds its soft
length limit. It must never fabricate a word boundary. Tests cover long tokens,
partial transport errors, normal completion, cancellation, and worker fallback.

## Remaining slices

Map all existing voice/UI/Telegram ingestion before extending shared dispatch;
preserve the native Gemini Live audio path and existing memory writes. Introduce
phrase identity and playback-driven caption events using the existing HUD event
surface. Caption start/end must reflect playback, not synthesis arrival. Extend
Telegram-origin responses through throttled progressive edits with final delivery
and failure reporting; tests use mocked transport, no live outbound messages.

Implement Fish-to-ready-Kokoro phrase fallback before whole-turn Gemini replay.
Gemini recovery lacks word alignment: never claim seamless recovery or replay
already spoken phrases without evidence. Cold Kokoro loading, interruption,
reconnect queue ownership, and source relevance require explicit verification.

## Gates and limits

No destructive steps, preference rewrite, cloud synthesis or live Telegram send
is needed for this slice. Phase 4 remains open until input/captions/engine recovery
are integrated and verified. Run focused regression checks, then refresh the
manifest after all production and test changes. Record every failure and repair.
