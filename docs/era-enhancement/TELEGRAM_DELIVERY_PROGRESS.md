# Owned Telegram delivery component — 2026-10-12

Status: component implemented and verified; live playback integration is pending.
Phase 4 remains partial. No later phase has started.

## Completed

- `core/telegram_reply.py` supplies one worker per immutable Telegram input,
  with a captured destination and unique draft ID. Local inputs cannot own it.
- Revealed phrases produce coalesced, throttled draft previews. A final sequence
  persists the full reply, with lossless conservative UTF-16 chunk limits.
- Reply text is bounded at 65,536 characters. Exceeding capacity raises an
  explicit error and emits a diagnostic instead of silently truncating.
- Network calls run outside the audio event loop. Cancellation stops queued
  work and backoff; a submitted HTTP request cannot be recalled.
- Explicit flood-control refusals honor retry_after, up to three attempts.
  Unknown final-send outcomes are never automatically retried. Failed final
  chunks stop later chunks, preserving ordering and reporting partial delivery.
- Draft failures remain observable and disable previews while permitting final
  delivery. Final completion is idempotent within the worker's lifetime.
- `era_telegram.send_reply_update` checks the configured owner destination,
  isolates its HTTP connection from existing send/poll sessions, validates Bot
  API acknowledgement data, and returns sanitized outcomes. Exception text
  containing an authenticated URL is not printed or retained.
- Bounded diagnostic events carry request ID, stage, outcome and message ID.
  A failing reporter cannot cause a duplicate send.

## Verification

Final focused suite: **76 passed**, one existing Google SDK/Python deprecation
warning, **33.29 seconds**. Included:

- 24 new delivery tests: two remote owners, preview coalescing, throttling,
  Unicode/long messages, empty replies, overflow, unsupported/failed drafts,
  cancellation before dispatch and during backoff, rate-limit retry budget,
  ambiguous final outcomes, acknowledgement validation, owner authorization,
  secret-safe errors, reporting-hook failure and a blocked HTTP worker while
  the event loop remains responsive.
- Existing Telegram voice and input-ownership regressions.
- Manifest, registry validator, executor wrapper, first-patch and smoke gates.

Selected Ruff checks and git diff --check passed. Regenerated the manifest after
all production/test edits: 332 modules, 57 tools, zero unparseable files.

One initial test failed because it counted draft previews as final messages.
The test was corrected to distinguish the two API methods; separate final-only
assertions verify there is one final sequence per owner. Initial reconnaissance
also attempted a missing pyproject.toml and a PowerShell glob passed to rg; both
returned errors and neither affected implementation or final verification.

All Telegram transports were mocked. No real messages, cloud synthesis, driver
changes, deletions, task-database changes or Era restart were performed. The full
repository suite was not rerun for this isolated component.

## Pending and limitations

The component is deliberately **not attached to the shared _tg_reply_pending
flag**. Existing production replies and approval notifications retain their
current behavior, including legacy truncation/retry behavior. This work does
not yet make Telegram reveal Era's spoken text progressively.

Next establish model-response ownership, retain it through local phrase queues
and tool continuations, cancel it on local/voice interruption, then connect
verified playback events to this worker and retire the legacy final-only path
for those replies. Test native voice, reconnects, stale playback and delayed
TTS before enabling it. Native Gemini text/audio alignment and Gemini/Fish/
Kokoro recovery remain separate unverified requirements.

Do not equate draft submission with persistent delivery, provider-generated
text with spoken audio, or an ambiguous final timeout with a definite failure.
User acceptance of the glasses microphone correction is recorded separately in
GLASSES_MIC_DIAGNOSIS.md; earlier physical Bluetooth disconnect causes remain
unproven.
