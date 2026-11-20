# Native Gemini progressive delivery — 2026-10-12

Status: implemented with mocked verification; Phase 4 remains partial.

## Delivered

Native PCM retains its existing playback path. Sentence-ending transcript
checkpoints follow the audio already queued for their owned turn. The playback
worker reveals Telegram previews only after that checkpoint's audio bytes were
accepted by the device. Checkpoints never reach the output stream as PCM.
The final marker verifies all bytes and adds only the unrevealed suffix before
finalizing once. Conflicting transcript revisions fail rather than duplicate
or replace previously revealed text. Interrupted, superseded or ambiguous
ownership still cancels the reply. The single HUD transcript is preserved.

## Verification

- Focused ownership/delivery suite: 78 passed in 14.54 seconds.
- Broad speech, threading, routing, ownership, cancellation, stability,
  manifest and integration suite: 316 passed in 78.94 seconds.
- Registry validator: 74 passed, zero failed. Existing main-only tool warning
  and nine default-safety informational entries remain.
- Smoke E2E: 72 passed, zero failed. Its expected invalid-confirmation and
  empty-query negative cases emit error logs and are asserted successfully.
- Selected Ruff checks passed. No new test failures or lint findings.
- Regenerated the manifest: 334 modules, 57 tools, zero unparseable files.

The new staged integration case keeps the provider mid-turn and blocks output:
no preview is sent before playback; releasing output produces the first
sentence draft while generation remains incomplete. The final contains both
sentences exactly once. Existing missing-transcript, dropped-output, partial
failure, internal-overlap and local-supersession cases remain passing.

## Limits and next work

These are best-effort sentence previews, without provider word timestamps.
Device acceptance does not prove acoustic audibility. Draft support and real
hardware/cloud latency still need live acceptance. Already delivered previews
cannot be recalled if a later phrase fails. No live Telegram messages, cloud
synthesis, microphone capture or Era restart occurred in these checks.
The full run_all_checks.py gate was not rerun for this slice; the targeted
316-test suite plus registry/smoke gates are the evidence reported here.

Next: prevent whole-turn Gemini fallback from replaying successful local
phrases, evaluate Gemini-to-local provider recovery, and run live Phase 4
acceptance. Phase 5 and native Telegram calls have not started.

Lesson: carry cumulative transcript checkpoints with a per-turn audio watermark
and reveal only new suffixes. Global queue-empty state and transcript generation
alone are insufficient evidence for owned progressive speech delivery.
