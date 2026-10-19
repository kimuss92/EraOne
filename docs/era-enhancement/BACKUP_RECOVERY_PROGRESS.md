# Backup conversation recovery — 2026-10-13

Status: implementation complete; hardware acceptance pending. Phase 4 remains
partial and Phase 5 has not started.

## Findings and changes

- Latest log: Gemini stopped transcribing, triggering Claude backup at
  2026-10-12T23:29Z. Claude produced subsequent answers, including one delayed
  about 19 seconds. The available tail ends in a requested relaunch, not proof
  of a fatal crash. The underlying Gemini transcription failure is unresolved.
- Confirmed code defect: backup capture used the system default microphone,
  without the saved input resolver or the confirmed wearable amplification.
  It now shares those existing components and resets the barge-in baseline
  before identifying the actual endpoint. PC microphones remain unamplified.
- Confirmed code defect: a Gemini voice preference could be treated as an active
  Gemini voice during backup operation. Spoken requests now bypass the backup
  model. Typed and tool switches request the same thread-safe handover rather
  than returning an "already using Gemini" result. Claude windows are closed
  and cascade is paused before retrying; actual success remains tied to the
  existing successful-connect hook. Telegram typed acknowledgements also
  distinguish retrying from success.
- Added sanitized per-phrase synthesis diagnostics (engine, byte count,
  character count, backup mode) to distinguish missing audio from model failure.
- Updated one stale standby test to execute the coroutine and assert the actual
  submitted SDK text, preserving all its content assertions.

## Verification history

- First focused run: 224 passed, 2 failed. One patch import was missing; fixed.
  The other failure exposed the outdated standby test described above; fixed.
  That run also emitted an unawaited-coroutine warning from the stale mock.
- Focused rerun: 226 passed, Google SDK deprecation warning only. Artifact:
  `backup-recovery-checks.txt`.
- Two additional cross-thread/live-session regression tests: 2 passed.
- Initial lint caught the missing import, then two import-format findings during
  correction. All corrected; final scoped Ruff check passed.
- Main tool bridge: 127 passed, 0 failed. Artifact:
  `backup-recovery-bridge-checks.txt`.
- Registered speech pipeline: 420 passed, SDK deprecation warning only.
  Artifact: `backup-recovery-speech-checks.txt`.
- Manifest currency initially failed because source hashes changed; regenerated
  using the project's normal manifest generator.
  Generated inventory: 335 source modules, 57 tools, 0 unparseable modules.

No full global suite or physical Bluetooth test is claimed for this repair.
The existing unrelated workspace modifications were preserved.

## Live acceptance

Restart Era. Request "use Claude" to enter the backup conversation loop, ask
a normal-volume question through the glasses, then say "switch to Gemini voice".
Expected: the backup input-open log identifies CY03-CB36 with gain 8; the switch
reports retrying, closes backup mode, and reports restoration only after an
actual successful Gemini connection. If Gemini is still unavailable, preserve
the new log rather than assuming the voice switch succeeded.
