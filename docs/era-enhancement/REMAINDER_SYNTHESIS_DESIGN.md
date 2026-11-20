# Phase 4 remainder synthesis

Continue the approved voice-recovery work. Keep successful local sentences on
the playback queue. If a later local sentence produces no complete PCM, submit
only that sentence to Google's standalone text-recitation TTS endpoint. Complete
the phrase before queueing it, preserve its request owner and label its actual
engine. Following sentences continue normally. Never replay the Live whole-turn
buffer after any local sentence was queued. If every provider fails, retain the
existing incomplete-speech report and cancellation; availability is conditional.

Use the documented Gemini 3.8 Flash-Lite TTS Interactions REST API, preserving
installed google-genai 2.23.0 and existing Live sessions. Google documents the
new schema for SDK 2.25+, but supports REST. Use the existing Gemini key resolver
and saved voice; fixed official origin, no redirects, no request retries, bounded
response/text/time, store=false. Validate completed status, one audio block,
base64 and full 24 kHz mono int16 WAV before returning bytes. Fail closed on
incomplete or incompatible audio. No provider response body or credentials in
logs. This can consume normal Gemini quota only when runtime recovery is needed.

Fish exceptions should use the already-ready Kokoro fallback just like incomplete
transport. Kokoro must reject text exceeding its token capacity instead of
silently cutting off words. Cold models remain outside the hot path.

Tests block the new cloud transport by default and inject mock responses. Cover
remainder-only requests, cancelled/stale synthesis, malformed/truncated WAV,
HTTP failures, completion status, provider exceptions, token overflow, owner
metadata and unchanged saved preference. Run focused/broad regression and the
configured global readiness suite. Live acceptance is a separate required gate
before starting Phase 5; user hardware feedback is requested in parallel.

Sources verified 2026-10-12:
- https://ai.google.dev/gemini-api/docs/speech-generation
- https://ai.google.dev/api/interactions-api
