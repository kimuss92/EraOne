# Gemini final-reply ownership — 2026-10-12

Status: send-time text request ownership and final-message routing implemented
and verified. Phase 4 remains partial. No later phase has started.

## Achieved

- Existing text SDK sends use a common wrapper that captures the immutable
  originating input at the actual send, after lane classification. Intent,
  payloads, tool dispatch and existing error propagation remain unchanged.
- A session-local tracker preserves the owner across empty tool-only turns.
  Same-request text continuations may retain their owner; distinct or unowned
  overlapping sends make ownership uncertain until a response boundary drains.
- Final Telegram mirroring requires a captured Telegram owner matching the
  latest accepted input. A misleading shared reply flag is insufficient in
  production. Embedded receivers without input-envelope support retain an
  explicit compatibility fallback.
- New input and output flush invalidate the owner. Voice transcription already
  enters the same source-acceptance path, so local speech cannot inherit a pending
  Telegram request. Internal cancellation/recovery/upload notes are unowned.
- Send failures clear only an isolated registration; concurrent failures retain
  uncertainty instead of accidentally authorizing an older response tail.
- Session teardown clears ownership and reports a pending remote reply as
  interrupted. Ambiguous final replies are suppressed, logged and reported
  without revealing their text to Telegram. Notification-hook failures cannot
  tear down the audio connection.
- SDK submission and acceptance have separate log events. A queued coroutine
  is not logged as an accepted send before its SDK await succeeds.

## Verification

Final focused suite: **300 passed**, one existing Google SDK/Python deprecation
warning, **28.21 seconds**. Includes 25 new ownership checks, actual receive-loop
routing, thread/input/direct-speech delivery regressions, stability, tool
recovery, cancellation, panic-stop, text parity, CM report delivery, and manifest/
registry/executor/first-patch/smoke gates.

Receive-loop tests prove a confirmed remote answer is mirrored, a local or
unattributed answer is suppressed despite an armed flag, an empty tool-only
turn retains the owner for its later answer, and a disconnected stream reports
its pending remote request and releases ownership. Other checks cover delayed
lane results, two remote requests, same-owner continuation, internal notes,
overlapping send failures and failed reporting hooks.

All tests passed. Initial Ruff checks found two import-order issues, corrected
before final verification. Selected Ruff checks and git diff --check passed.
Regenerated the manifest after all production/test edits: 334 modules, 57 tools,
zero unparseable files. Graph lookup pointed to historical backup code; current
targeted inspection established the actual integration before editing it.

No live Telegram traffic, cloud synthesis, microphone recording, OS changes,
destructive cleanup or Era restart was performed. The full repository suite was
not rerun. Running Era needs to reload these code changes before live acceptance.

## Remaining and limits

- Native Gemini replies remain final-only. This does not establish progressive
  spoken-text timing, acoustic completion or seamless engine failover.
- Transcript-fed Fish/Kokoro speech still needs the captured model owner carried
  into its sentence/frame lifecycle. Direct owned utterances already support
  playback-driven delivery; that path is preserved.
- Overlapping distinct model requests cannot be reliably distinguished by this
  receive integration. Suppression protects attribution but can require the
  user to repeat the latest remote request; local HUD/audio responses continue.
- The final-only path retains the legacy Telegram sender's truncation and retry
  behavior. Migrating it to the owned delivery transport is still pending.
- Empty tool-only boundaries are covered. A mixed text-and-tool completion or
  additional autonomous follow-up after the first completed textual reply is
  not claimed as a fully correlated multi-turn response lifecycle.
- Ownership is not carried across reconnects. A resumed server answer without
  a new attributable request is kept local rather than guessed into Telegram.

Lesson: accepted input, actual model submission and completed provider output
are distinct lifecycle points. Preserve identity across those points and prefer
an explicit uncertain outcome over attributing an old answer to a newer request.
