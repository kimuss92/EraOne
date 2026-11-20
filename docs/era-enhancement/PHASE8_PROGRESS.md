# Phase 8 progress and acceptance

2026-10-15. Coordinated crew/source work is implemented. The user subsequently
approved the CodeMode design; its implementation and acceptance are recorded in
PHASE9_PROGRESS.md. The user's accepted HUD layout and existing dirty work are preserved.

Implementation details: `PHASE8_DESIGN.md`. Review proposal: `CODEMODE_DESIGN.md`.

## Evidence

- New Phase 8 tests exercise real coordination/capability/development boundaries
  with scripted model replies and temporary source. They cover root owner/task
  propagation, nested handoffs, exact candidate deduplication, forged attribution,
  cancellation including budget wrap-up, independent concurrent CM cancellation,
  fresh source hashes, private/path/link/size limits, redaction, source observations,
  stale-answer cache policy, submission validation, shared development slot, stale
  queued bases, and exact approval/activation/replay behavior.
- Final coordination/development/validation-runner subset: 72 passed.
- Final broader crew/Hermes/development/CM/approval/schema/manifest regression
  rerun: **591 passed**, two warnings, 160.95 seconds. This includes all 36 new
  Phase 8 tests and the final current-source manifest freshness check.
- Spotify, personal memory, native-call/HUD repair and conversation preservation:
  33 passed. These are software regressions, not physical playback/call tests.
- TypeScript check and production HUD build passed. Scoped lint, formatting and
  diff whitespace checks passed. Doctor READY: zero errors and zero warnings.
- Read-only native source probe found all six configured roles with source read
  access, five with staging, and Nabar with read/check only. Protected source read
  returned its current hash and `editable=false` without a model request.
- Regenerated model declaration snapshot: 58 tools. Regenerated source manifest:
  383 modules, 58 tools, zero unparseable. A Phase 8 suite is registered in the
  one-command validation runner; the entire global runner was not rerun here.

The first broad run had 587 passing tests and one runner-order assertion failure.
The new readiness suite had displaced Registry validator from first position.
Restored its established position, retained the Phase 8 gate immediately afterward,
and verified the runner tests. The final clean 591-test run uses the corrected
order and current source. An earlier 265-test compatibility smoke run also passed.

## Remaining acceptance and sequencing

- CodeMode design review: completed by the user's subsequent “proceed” instruction.
  Phase 9 implementation evidence and remaining acceptance: `PHASE9_PROGRESS.md`.
- Restart Era to load the new in-process prompts, grants and coordination code.
  No active app restart, live crew model generation, CM publication or Telegram
  call was performed for this increment. Doctor readiness does not prove that a
  particular Claude subscription session is authenticated.
- After restart, live acceptance: ask Laura/Cody for a small source investigation,
  inspect shared ownership and a staged diff/test report, then deliberately approve
  or reject the exact job. Software tests cover promotion with temporary files;
  they did not deploy generated changes into this live checkout.
- Existing richer widgets/agent-controlled layout and earlier deferred hardware,
  microphone/voice interruption, native Telegram calling and physical approval
  acceptance stay open in their existing reports. User acceptance of the HUD's
  appearance is preserved; it is not a claim that every Phase 7 item is complete.
- Phase 10 guarded learning/Jev and Phase 11 reviewed cleanup remain later phases.
  No deletion, reset, account migration, dependency upgrade or paid-provider
  fallback was introduced.

Cancellation stops later crew capabilities and suppresses late CM continuation;
it cannot undo external CM effects already sent. Development verification that
was already submitted remains an independent bounded process and never activates
automatically. Its forced process cancellation, CodeMode approval expiry/session
binding and persistent subscription voice sessions are proposed Phase 9 work.
