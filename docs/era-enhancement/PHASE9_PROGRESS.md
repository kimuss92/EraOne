# Phase 9: CodeMode implementation and acceptance

October 18 checkpoint: natural coding-intent activation from the October 16
revision supersedes the original explicit-entry-only behavior described below.
Current subscription CLI auth status passed; source loaded after project-wide
verification. Full mode/voice/review acceptance remains owner feedback.
See `STABILITY_RELEASE_2026-10-18.md`; no API billing fallback was enabled.

Follow-up: the user's live test exposed silent Gemini-selected CodeMode speech
and truncated source without automatic retrieval. Both are repaired and tracked
in CODEMODE_SPEECH_SOURCE_REPAIR_2026-10-15.md, including activation limits.

2026-10-15. User approved CODEMODE_DESIGN.md and instructed implementation.
CodeMode software is implemented. The accepted HUD layout is preserved; controls
are inside Development and the existing header button indicates the active mode.
Backend restart and physical voice/approval acceptance remain.

## Implemented behavior

- Explicit `enter CodeMode` / `exit CodeMode`, off at startup. Normal coding
  questions do not enter it. Entry claims ownership synchronously before another
  live-model event. Typed/HUD, owner Telegram and live/offline voice transcripts
  use the same boundary. The public `code_mode` tool reports state; a model
  cannot grant itself mode entry without the user's actual matching instruction.
- One pinned profile from the configured reasoning CLI profile, one exact session
  ID, explicit resume, no gateway/account/billing fallback. Auth status must show
  a Claude subscription login. API/provider credentials and override flags are
  removed from the child environment, even when ordinary adapter opt-outs are set.
- CLI tools/MCP are disabled; safe mode disables customizations and hooks. The
  installed CLI's `--bare` bypasses OAuth, so the live probe identified it as
  unsuitable and the implementation uses `--safe-mode` instead. This distinction
  is documented in the [official CLI reference](https://code.claude.com/docs/en/cli-reference).
- Bounded input/stream frames, typed failures, 180-second generation and 45-second
  stream-idle limits, hidden Windows processes and cancellation/reaping of owned
  generation/verification trees. No blind retry. Authentication has a separate
  bounded local status check. Installed flags are verified on CLI 2.1.289.
- Current source context: up to eight files/100KB, path/ranges, actual SHA-256,
  checkout identity/revision, redaction and private-state exclusion. Incomplete
  context is explicitly marked. Existing-file edits must match supplied context
  and the live base; protected files cannot be promoted. Discovery is navigation.
- Replies stream through SpeechOut with existing playback captions on the live
  path; the offline mouth shares sentence splitting and existing local synthesis/
  playback. Interruption mutes remaining speech without claiming generation was
  cancelled. Corrections/cancellation invalidate old revisions and late output.
  Raw candidate JSON/source never enters the spoken reply.
- Parallel Gemini text/audio/tool execution is suppressed while CodeMode owns the
  conversation. Reply tracking resets at transitions. Sentinel mic/standby/wake
  controls retain their established paths, with explicit sleep/wake orders handled
  locally. Completed replies have Claude attribution in actual agent conversation
  history. Verbatim stream fragments preserve spaces inside/between words.
- Explicit specialist requests, such as `Cody, inspect core/example.py`, retain
  the CodeMode work ID, original requirement and server-owned approval authority
  across crew handoffs. A team candidate uses the same verifier/review path;
  other crew observations are untrusted context for the subscription session.
- Complete candidates use existing Development staging, fixed verification,
  protected paths, exact live/candidate hashes, backups and recovery. Submitted
  code does not automatically activate. HUD form proposals while CodeMode is
  active enter its pinned session; generic retries cannot use another provider.
- Approval binds job, session, work/turn revision, input identity, verification
  and a 15-minute expiry. HUD approval is sufficient once. Explicit text approval
  identifies the exact job; a short spoken/typed assent applies only to the current
  presented proposal in its original channel. Owner-bound Telegram input uses the
  existing authenticated transport. Stale/expired/replayed/cancelled/restarted
  authority cannot activate. Exiting preserves an already verified HUD review;
  opening a new session invalidates the old session's authority.
- CodeMode metadata/events contain session/work/revision/sequence/source identity
  and redacted context references. Era's metadata journal excludes full prompts
  and answers; Claude's own session history persists in its selected profile to
  support exact resume. Windows journal writes reuse bounded atomic replacement
  retries for transient file locks.
- The model catalogue/snapshot now register **59 tools**. Source manifest has
  **391 modules**, zero unparseable. New ownership/CLI/context/process/speech
  guard modules are protected from generated edits. The main readiness runner
  includes the CodeMode regression suite.

## Verification

- Real read-only subscription probe: authentication, generation, streamed reply
  and exact session resume all passed. Two small requests verified that the next
  turn recalled a generated nonce from the first turn. No machine tool or source
  mutation was involved. Reproduce with `python -m tools.probe_codemode --live`;
  without `--live`, it checks local authentication status only.
- New CodeMode tests cover incremental JSON reply decoding, Unicode/escapes,
  candidate exclusion from speech, auth-method/provider restrictions, exact CLI
  session flags, malformed/missing/mismatched/oversized result streams, explicit
  ownership, duplicate IDs, correction/cancellation, metadata privacy, current
  source context, shared crew authority, local speech interruption, superseded
  channel delivery, verbatim HUD text and committed conversation history.
- Approval tests cover expiry, revision/session changes, restart, rejection/replay,
  HUD-only approval and the complete temporary candidate → verifier → review →
  voice-decision path. Tests use temporary source; a separate native process test
  verifies cancellation removes both a real verifier parent and its child.
- Final focused schema/CodeMode checks: 59 passed. Initial broad regression:
  1,028 passed and six intentional-count mismatch failures because assertions still
  expected the previous 58 tools/42 required schemas. Updated to 59/43 and verified
  those schema checks. The corrected broad rerun passed: **1,035 tests**, three
  warnings, 130.61 seconds. It covers the then-current 35 CodeMode tests and the listed crew,
  Development, voice, approval, personal-control, schema and manifest regressions.
  A final small follow-up deduplicates an identical mode-control input ID so a
  deterministic entry and its delayed model tool cannot announce entry twice;
  its current-source owner/development/schema/manifest rerun passed **137 tests**.
  Final Windows guards additionally reject protected paths regardless of case,
  resolved aliases and alternate-stream syntax. That final source/approval run
  passed **107 tests**, including all **39 CodeMode tests**, 36 Phase 8 tests and
  32 Development tests. Existing internal-link refusal wording was preserved.
- Regenerated the final manifest after those guard changes. All **8 manifest
  tests passed** on the final source (23.89 seconds). Final scoped lint/format and
  whitespace checks also passed. The temporary Ruff cache-lock warning from an
  earlier check was avoided by running the final lint without its optional cache.
- TypeScript and production HUD build passed. Scoped lint/format and diff checks
  passed. Doctor READY, zero errors/warnings. No complete global-runner exit is
  claimed for this increment.

## Use and remaining acceptance

Restart Era, then say/type **enter CodeMode**, or use Development → Enter CodeMode.
Give a focused coding question or change request. Inspect source context and any
verified diff in Development. Approve that exact candidate there, or answer its
current approval in the original channel. `cancel coding task` invalidates the
pending work; `stop speaking` stops the mouth; `exit CodeMode` returns to ordinary
conversation. No additional verbal approval follows a manual HUD approval.

Physical microphone interruption/caption timing, a full live Era mode round trip,
owner Telegram delivery and a user-reviewed live patch/rollback walkthrough have
not been observed. Existing deferred call/hardware and richer HUD-widget acceptance
remain in their earlier reports. A source copy is not an OS sandbox: verification
tests run with this Windows user's permissions. Arbitrary remote provider calls
already dispatched cannot be recalled; their late output is suppressed. Approval
cannot undo a promotion that already acquired the activation lock and committed.

Phase 9 is software implemented with live CLI evidence; physical application
acceptance is pending. Phase 10 guarded learning/Jev and Phase 11 reviewed cleanup
remain later work. No app restart, live generated patch deployment, external
message/call, dependency upgrade, reset or deletion was performed here.
