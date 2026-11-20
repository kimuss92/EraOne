# Native call audio foundation — 2026-10-13

Status: offline transport foundation verified; Gemini connection, runtime call
coordinator, authenticated IPC, HUD controls and real duplex calls remain pending.
Calls remain disabled. This follows the approved separate-account/helper design
and the user's instruction to proceed after successful local owner binding.

## Verified account prerequisite

Protected local status read-back confirms authenticated/account_authenticated,
owner_bound, saved session and owner ID. calls_enabled remains false. Only status
booleans were surfaced; account secrets were not printed or written to this repo.

## Design and interfaces

Continue with an isolated native helper, rather than injecting phone audio into
the local microphone queue or sharing the mutable local conversation context.
Direct process embedding risks bringing native failures into Era; virtual audio
device routing risks the previously repaired glasses connection. Existing design
alternatives and account safety remain in TELEGRAM_CALL_DESIGN.md.

core/telegram_call_audio.py implements an offline CallAudio boundary. It knows one
configured owner ID, one call generation, an explicit transport phase, and one
reply token at a time. Unknown peers and overlapping calls are rejected. Ended
generations cannot feed input, queue output, acknowledge playback or terminate a
successor. Shared state updates are protected by a lock.

Incoming input is ordered 16 kHz mono int16 PCM; duplicates are discarded, gaps
or malformed packets fail explicitly. Input and pending output are bounded by
count and byte budgets. Default capacity is 25 frames: at most 500 ms of input or
output frame data, with less than one output frame retained for packetization.
Backpressure fails and clears the buffers rather than accumulating stale speech.

Outgoing Gemini-compatible 24 kHz mono PCM is packetized in 20 ms frames. Arbitrary
whole-sample fragment boundaries preserve data. Final short tails are padded with
silence instead of discarded. A finalized reply cannot append more audio. Barge-in
invalidates its token, queued frames and pending receipts; already submitted native
audio cannot be undone. New replies invalidate old output. No device stream is
opened by this module.

Queue insertion does not count as delivery. Native acceptance requires an exact
pending packet object and is recorded once. The diagnostic active state requires
both observed incoming bytes and native-submitted output. It does not prove remote
audibility: remote_playback_verified stays false. Snapshots contain no PCM or text;
packet repr omits audio. Input counters describe received bytes, not STT acceptance.

## Native proof and shutdown limitation

tools/probe_telegram_call_native.py creates only a synthetic local native P2P
connection. It does not load the account session, request a Telegram call, connect
to a remote peer, or select a physical microphone/speaker source. It configured
external microphone data at 24 kHz and external speaker capture at 16 kHz, submitted
a synthetic 20 ms frame and stopped the native connection successfully. These are
source configuration/submission proofs, not remote media reception or playback.
The native library is responsible for its requested external PCM conversion; no
stateless per-chunk Python resampler was added.

The first native probe emitted its successful teardown receipt but hung at process
shutdown. It was stopped by matching only the newly created optional-environment
stdin probe process, excluding existing setup windows and Era. A subsequent
self-exit attempt also hung. The exact native shutdown cause is not established.
Neither natural finalization nor direct os._exit is treated as a reliable path.

The supervised probe holds the disposable child after explicit endpoint teardown
and flushes a structured receipt. The parent owns that exact process and reaps it
at an eight-second deadline using subprocess.run's timeout cleanup. Final proof:
sources_configured, frame_submitted, connection_stopped, supervisor_reaped_child
and shutdown_complete are true; clean_process_exit and native_finalization_verified
are false. This is bounded supervised shutdown, not a repair of the library's
natural destructor. The future helper must stop call signaling/audio, flush state,
invalidate its generation and disconnect its user session before supervised exit.
If those steps fail, it must report cleanup/delivery uncertainty rather than pretend
the remote call ended. No native process can be embedded into Era on this evidence.

## Verification

Initial focused queue/setup/Telegram tests: 113 passed. Expanded ownership and
speech regressions passed 148 cases before the final boolean-sequence case was
added. Audio-specific coverage: 26 cases, 98.86% statement coverage before that
final added case; two known isolated pytest plugin-configuration warnings remain.
Final expanded regression: 149 passed in 9.20 seconds, with the existing SDK
deprecation warning; see TELEGRAM_CALL_SETUP_PROGRESS.md.

Scoped Ruff, Black, strict mypy for both new source modules, compilation and
whitespace checks passed. Initial import-order finding was corrected. Doctor:
zero errors/zero warnings, READY. The new offline audio tests join the existing
confirmation gate. Native probe is an explicit optional diagnostic, not a global
readiness requirement for users who have calling disabled.

No Era restart, Gemini provider connection, account login replay, call, message,
device-default change or local recording was performed. No full global-suite rerun
occurred. Old hardware acceptance, Phase 6 work and schedule restoration remain
separate open items.

## Next integration gate

Build and test the authenticated helper/coordinator boundary using these packets
and teardown receipts. Bind remote input/output to a dedicated owned conversation
adapter and existing intent/tool/memory policies. Preserve local mic-off and glasses
routing, account/owner verification, biometric policy if enabled, and user approvals.
Implement incoming/outgoing signaling, bounded timeouts, Hang up and actual HUD
state. Only then coordinate one user-attended real call in each direction. Provider
fallback, barge-in, captions, remote echo, native end confirmation and usable latency
must be measured; this checkpoint cannot certify any of them.
