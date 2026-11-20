# Phase 11: current-machine release baseline and recovery

2026-10-18. User requests a stable Era project and will provide physical/behavior
feedback later. The final release verification is recorded in
[STABILITY_RELEASE_2026-10-18.md](STABILITY_RELEASE_2026-10-18.md). This page is the
operating/recovery record; current final test results must be read from that
report and the newest handoff.

Verified baseline: full Windows gate 7,971 passed / four skipped, 36 readiness
gates passed, then 334 affected/protected cases after a final default-input
guard. Full-run and final-source hashes both retained; the whole suite was not
rerun after that last guard. Normal restart completed; connected PID 26640,
standby, glasses voice / remembered PC media, optional HUD tones off.
The final 619.4-second observation logged three session rotations with three
microphone reuses and one initial open, without error-level events. Spotify was
then active on PC speakers. Physical listening remains owner acceptance;
evidence: `runtime/stability-final-live-20261018.json`.

## Scope and preservation

- Repaired speech completion/renewal timing, microphone reconnect handle churn,
  Spotify all-output state and crew deadline classification. Existing source
  ownership, approvals, speech normalization, Claude subscription, media choices,
  long-term memory and vault remain authoritative.
- Preserved the existing purple HUD, layout and compiled assets. No frontend
  build; backend/test formatting does not change their runtime AST.
- Snapshotted targeted repairs and formatting drift under
  `.phase7_backups/stability-20261018/`. Do not delete these as part of routine
  cleanup; other uncommitted work and historical backups are retained.
- Aligned Ruff's frozen-phase-backup exclusion with pytest, fixed two test import
  orders and normalized 116 files with zero changed runtime syntax trees.
  Declarations were formatted, so the 59-tool golden was regenerated explicitly.
  No weaker lint rule, test assertion or approval authority was introduced.

## Build identity and checks

`runtime/stability-source-baseline-20261018.json` records SHA-256 for all manifest
entries (source and indexed tests), counts and declaration snapshot identity.
It describes this dirty checkout; it is not a fabricated clean Git revision.
The verified-start helper refuses startup if those files changed during checks.

Verification artifacts:

- `runtime/stability-full-gate-final-20261018.log`: current full Windows gate.
- `runtime/stability-readiness-20261018.log` and
  `stability-readiness-timings-20261018.json`: complete two-worker readiness.
- `runtime/stability-format-ast-20261018.json`: formatting preservation proof.
- `runtime/stability-capture-focused-20261018.log`: 333 focused checks.
- `runtime/stability-native-capture-result-20261018.json`: native reuse/cleanup.
- `runtime/stability-native-spotify-state-20261018.json`: actual all-device read.
- `runtime/stability-vision-result-20261018.json`: known-image and local capture.
- `runtime/stability-python-dependencies-20261018.log`: pip check.
- `runtime/stability-ui-dependencies-20261018.json`: production npm audit.

The installed Python dependency check passed and production npm audit reported
zero advisories. These do not prove a fresh Windows installation or an audit of
every optional external service. No dependency upgrade, account migration,
automatic file deletion or driver installation was performed.

## Normal lifecycle and recovery

The existing supervisor is the process owner. A controlled HUD shutdown was
used for maintenance; the normal relaunch flag/supervisor mechanism loads the
verified source. Keep the latest requested microphone, Era voice, media output
and model preferences; restore the pre-maintenance standby state unless the
user has made a newer request.

For a future regression, first capture its time, check ID, process/build identity
and log evidence. Review the exact source diff and targeted backup; preserve
later edits by Claude or the owner. Restore only the relevant reviewed files,
then regenerate declarations if affected and regenerate/check the manifest.
Run affected/protected checks before loading the replacement. Never bulk-reset
the checkout, memory, vault or user configuration to an older backup.

Use the existing CodeMode/Development review flow for generated changes. The
current stored self-healing mode is auto; its existing scope, verifier and
probation rollback remain in force. This session did not widen that mode or
grant an external judgment provider any new authority.

## Acceptance still requiring later feedback

- Actual complete spoken phrases and Star Glasses beep/reconnect behavior in
  the user's room, through provider renewal and real Bluetooth reconnection.
- Exact user-reviewed action approval/cancellation and a live coding change
  walkthrough; software cases are not substituted for the user's approval.
- Multi-day recall, usual-app/file opening, media contexts and real Netflix
  episode transition where still owed.
- Sustained daily use and fresh-machine setup. A brief post-start observation
  is not the planned 60-minute/next-day acceptance window.
- Optional Jev configuration/integration remains unfinished; the local guarded
  learning and recovery implementation does not depend on it.

Those checks remain in TODO and the establishment/deferred registers. Current
software verification must never be presented as universal zero-fault hardware
acceptance or proof that every command completes within 500ms.
