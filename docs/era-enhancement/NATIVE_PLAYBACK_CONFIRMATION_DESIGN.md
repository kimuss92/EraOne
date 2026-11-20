# Native Gemini playback confirmation

Continue the approved Phase 4 ownership and playback-verification work. Preserve
native Gemini PCM and local Fish/Kokoro synthesis. Bind a Telegram request once,
before its first native audio frame, using the existing send-time ownership
tracker. Queue PCM-compatible frames with a per-turn byte ledger and a zero-PCM
final marker. The playback worker may finalize the transcript only after every
queued byte for that turn was accepted by the device. Failure, interruption,
supersession or uncertain ownership suppresses successful final delivery.

Generating the final transcript currently sends too early. Waiting for a global
queue-empty flag would mix adjacent turns; use a turn-specific ledger and ordered
marker instead. Keep legacy routing for requests without owned native audio.
Suppress the duplicate generation-time message for owned native playback.

Verification uses the real receive and playback loops with mocked Gemini,
device and Telegram transports, including queued playback, dropped frames,
superseded input and internal overlap. No live messages or microphone capture.
Device acceptance is not proof of acoustic audibility. This change does not
claim progressive native transcript/audio alignment or seamless Gemini failover.
