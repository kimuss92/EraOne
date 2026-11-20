# Transcript-fed Fish/Kokoro ownership — 2026-10-12

Status: implemented and verified with mocked SDK, synthesis, audio devices and
Telegram transports. Phase 4 remains partial; no later phase has started.

## Achieved

- A proven send-time Telegram owner is latched before the first transcript
  sentence enters local synthesis. An unowned or ambiguous turn is not upgraded
  to a newer request halfway through generation.
- Each queued sentence and end marker snapshots its owner. Frames retain that
  owner even after provider completion resets model tracking. Partial buffered
  text keeps its stream owner independently of direct announcements.
- Transcript chunks reuse one open delivery worker. Playback events reveal
  phrase text after its first accepted device write, and the end marker schedules
  one persistent final reply after the queued frames have been accepted.
- The provider-completion legacy Telegram message is suppressed for replies
  assigned to playback delivery, including when synthesis/playback later fails.
- Internal or overlapping model sends that make attribution uncertain cancel
  the existing playback owner. Stale frames cannot add text to another reply.
- Failure notices appear in the UI and Telegram only while the originating
  request is still current. Intentional interruption and uncertain model
  attribution retain their existing distinct reporting paths.
- Direct local announcements remain unowned unless explicitly given an owner;
  placing one between transcript chunks cannot acquire remote provenance.

## Verification and failures

Final focused pytest run: **308 passed**, one existing Google SDK/Python
deprecation warning, **43.81 seconds**. Includes eight new transcript checks,
existing direct/remote input, model ownership, synthesis, caption/playback,
cancellation/panic, stability/recovery, manifest, executor-wrapper and first-patch
gates. Separately, actual custom registry and smoke entry points returned exit 0:
**74 registry checks** and **72 smoke checks** passed.

The integration fixture runs the actual receive, synthesis and playback loops.
It holds synthesis beyond generation completion, verifies there is no premature
Telegram message, releases multiple sentences, and asserts exactly one final
reply. Failure/new-local-input/internal-overlap cases produce cancellation rather
than a stale final. Additional checks reject late/reassigned owners, exercise
current-owner failure notices, and protect unowned interleaved announcements.

An initial existing fixture failed because the helper extraction introduced a
new bound method it did not provide; the fixture now exercises that actual
helper. A later regression reproduced a real interleaving problem: a direct
announcement advanced the turn counter while partial transcript text remained
buffered, breaking owner binding and risking provenance inheritance. Owner
snapshots in queued items plus an independent buffered-text owner corrected it.
These failures were not skipped; the final tests cover the corrected behavior.

Selected Ruff checks and git diff --check passed. Regenerated the manifest after
all production/test edits: 334 modules, 57 tools, zero unparseable files.

No live bot messages, cloud synthesis, microphone recording, preference rewrite,
Era restart, dependency upgrade or destructive action was performed. The entire
repository suite and the slower full validation command were not repeated for
this slice; the prior validation-runner repair's full 30-gate result is recorded
separately in VALIDATION_RUNNER_REPAIR.md.

## Remaining and practical limits

- Native Gemini audio remains final-only in Telegram. Native word/audio alignment
  and migration of that final-only legacy sender remain pending.
- Phrase previews follow device-write acceptance, not measured acoustic timing
  or individual words. Live private-chat draft support and hardware timing still
  need acceptance after Era reloads these code changes.
- Seamless Gemini/Fish/Kokoro recovery remains incomplete, including potential
  replay of earlier phrases by the existing whole-turn Gemini fallback.
- Mixed text/tool completions, multiple distinct response lifecycles within one
  request, and ownership across reconnects remain limited as recorded in the
  model-ownership report. Overlapping unknown server replies are suppressed.
- HTTP requests already submitted cannot be recalled on interruption. Additional
  direct utterances retain the previously documented first-ack behavior.

Lesson: snapshot request identity with the work item before asynchronous queues,
not by looking up a mutable turn counter when synthesis eventually finishes.
