# Native Gemini playback confirmation — 2026-10-12

Status: verified with simulated transports; Phase 4 remains partial.

## Delivered

Owned native Gemini replies retain send-time request identity in PCM-compatible
queue frames. A per-turn byte ledger survives provider generation completion.
The ordered final marker completes Telegram delivery only if every queued byte
was accepted by the playback device and the transcript retained proven ownership.
One dropped frame suppresses success even if earlier frames played. Generation
completion no longer sends a duplicate final for these replies. Superseded input,
internal overlap and missing transcripts cancel delivery. Legacy unowned audio
and existing local synthesis remain available.

## Verification and failures

- Expanded real receive/playback integration cases cover native success, total
  and partial playback loss, missing transcripts, supersession and overlap.
- First focused run: 70 passed, one failed. The overlap case revealed that an
  invalidated turn omitted its ordered terminal marker. Corrected this to queue
  a failed terminal marker and guard cancellation against a different pending
  owner; corrected focused run: 73 passed.
- Broad voice/input/ownership/manifest regression run: **314 passed**, one existing
  Google SDK deprecation warning, 107.09 seconds.
- Registry validator: **74 passed, zero failed**. Its existing main-only tool
  warning remains; passing tests do not remove this architectural difference.
- Smoke E2E: **72 passed, zero failed**. Expected negative-path errors were
  asserted by the suite, not silent runtime failures.
- Selected Ruff checks passed after correcting one import-format finding.
  `git diff --check` passed. Manifest regenerated: 334 modules, 57 tools,
  zero unparseable files; final freshness check passed.

## Limits and next work

Device acceptance is not acoustic proof. Native Telegram text still appears at
playback completion; progressive native transcript/audio alignment is unfinished.
Gemini streaming failure recovery, seamless engine switching, and live hardware /
cloud / Telegram acceptance remain open. No real Telegram messages were sent,
no microphone recording or cloud synthesis was performed, and Era was not
restarted. The full run_all_checks runner was not rerun for this slice.

Lesson: use per-turn playback evidence and ordered markers, not generation
completion, a mutable global counter, or a queue-empty flag, to confirm delivery.
