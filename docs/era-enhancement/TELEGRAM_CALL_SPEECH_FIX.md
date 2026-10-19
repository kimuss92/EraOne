# Telegram speech repair — 2026-10-14

## Evidence and causes

The owner answered but heard no reply, then reported unclear voice after retrying.
New generation diagnostics recorded native connection, a Gemini greeting request,
provider PCM arrival and native submission, followed by a ValueError in the speech
worker. A separate later attempt failed during provider startup with an APIError;
its original numeric status was not retained. The temporary test window also expired.

Two verified implementation defects explain broken speech:

1. Era submitted 20 ms (960 bytes) of mono 24 kHz PCM to each native send_frame.
   NTgCalls 3.0.3 consumes 10 ms per external audio frame and its audio streamer
   derives the consumed sample count from frame_size rather than the submitted
   byte length. Submitting 20 ms can lose the packet's second half. The SDK
   boundary now delivers both 480-byte halves, spaced 10 ms apart. Parent IPC
   remains unchanged at 20 ms, with acceptance reported after both halves.
2. Fast Gemini audio accumulated beyond the two-second provider buffer, causing
   a valid speech burst to fail. Valid chunks now wait for available bounded
   capacity. Oversized/malformed chunks and stalled consumers still fail visibly.

Sources for the pinned native version:
[AudioSink frame geometry](https://github.com/pytgcalls/ntgcalls/blob/v3.0.3/ntgcalls/src/media/audio_sink.cpp)
and [AudioStreamer sample count](https://github.com/pytgcalls/ntgcalls/blob/v3.0.3/ntgcalls/src/media/audio_streamer.cpp).

## Additional repairs

- Request a brief greeting only after the native audio connection finishes.
  This synthetic turn grants no user transcript or tool approval authority.
- Advance native packet delivery using an audio clock rather than accumulating
  native send/IPC latency into each subsequent frame.
- Isolate native descriptor-1 output from the machine-readable protocol pipe.
- Preserve original call errors if teardown also fails. Cleanup proof remains
  separate from confirmation of remote hangup.
- Record bounded stage, exception type, traceback locations and numeric provider
  status metadata. Never retain audio, transcripts, credentials or error messages.
  ERA_LOG_DIR isolates tests/helper logs; the main process otherwise uses the
  protected private account directory. Tests were initially written to the private
  metadata log before the isolation fix; these contain no speech or credentials.
- Before signaling only, use configured backup Gemini keys for 429/temporary
  server failures. Active conversations and tool calls are never replayed.
- Explain provider quota/availability and protocol errors in the HUD.
- Renewed the attended test window for 30 minutes; no call was placed by the agent.

## Verification and remaining acceptance

192 targeted transport/coordinator/Gemini/tool/diagnostics tests passed, including
lossless reconstruction of both native 10 ms halves, a six-second burst exceeding
the old buffer cap, initial greeting, safe diagnostics, native stdout isolation and
startup key fallback. Strict mypy, scoped Ruff/Black and TypeScript/build checks
passed. A real Gemini probe generated 197,762 bytes of mono 24 kHz greeting PCM;
no Telegram call or microphone audio was involved. The updated native helper also
authenticated, started and reaped cleanly in a no-dial probe.

The first test run caught an incorrectly named callback diagnostic variable in
disconnect cleanup; it was corrected before the passing runs. Formatting findings
and an atomic patch-context mismatch were corrected. Existing google-genai emits
one deprecation warning in pytest. The complete global suite was not rerun.

Clear phone playback, owner speech recognition on the phone microphone,
interruption and hangup still need an attended retry after restarting Era and
refreshing the HUD. These hardware outcomes are not marked verified. The exact
earlier APIError status remains unknown; subsequent diagnostics retain it safely.
