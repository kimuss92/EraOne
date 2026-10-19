# Doctor warning repair design — 2026-10-13

User requested investigation and repair of all three existing warnings before
continuing Phase 6. Preserve credentials, settings and vault notes; do not silence
warnings or install unused audio integrations merely to make checks green.

## Credential configuration

Windows DPAPI, current-user scope, protects actual credential strings in place.
Non-secret settings remain editable JSON. Central readers transparently decode
protected values; legacy readers and the configuration writer migrate to this
contract. Existing plaintext input is supported for careful migration. Write only
after in-memory and file round-trip verification, atomically replace, reject
concurrent file edits, and never create a plaintext backup or print secret values.
Malformed/wrong-account protected values must fail rather than overwrite settings.
No cloud credentials are rotated or changed. Microsoft documents user-scoped
protection and integrity checking:
https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata

Era was running; user confirmed closing it to allow migration without disturbing
the live session. Re-check the backend before mutating its credential file.
Protection is for local storage; previously synced OneDrive versions or external
backups are not rewritten. Moving machines/accounts requires re-entering secrets.

## Vault pressure

Delta Cuisine context was 51,507 estimated characters against a 55,000 cap. Keep
all standing rules inline. Long working plans get bounded previews and explicit
source paths with a requirement to read the file before using omitted details.
No notes deleted, moved, rewritten or excluded. Budget audit uses the same rendered
block as the actual brief, so it measures the real prompt rather than an estimate.
Retain warning/error thresholds, and test overflow and discoverability.

## Optional audio packages

No active runtime imports PyAudio or VLC: microphone/output use sounddevice.
PyAudio and python-vlc are explicitly optional in requirements.windows.txt.
The old Doctor used python_vlc as a module name; python-vlc actually imports as
vlc. Remove these unused integrations from required health probes; report their
nonrequirement explicitly. Active integration dependencies still warn when absent.

## Verification

Real DPAPI round-trip with fake credentials; corrupt/tampered blob rejection;
atomic failure and concurrent-edit preservation; key fallback/config readers;
writer preserving unrelated settings; migration idempotency. Vault rule retention,
plan source references, measured headroom, existing overflow and warning tests.
Doctor must report actual local state. Phase 6 proceeds only after these checks.

## Results

- User confirmed closing Era; process check and backend mutex prevented migration
  during a running backend. Local migration applied and read-back verified. All
  settings and credential values compare equal before/after decoding; current
  configuration has no plaintext credential values. No secret values printed.
  Setup/config writers and legacy readers now use the protected-config contract.
  Future migration command: `python -m tools.protect_api_config --apply` with Era
  closed; check-only invocation has no writes.
- Delta Cuisine brief is 43,779 characters across all 39 previously selected
  notes, below the unchanged 44,000-character warning threshold and 55,000 hard
  cap. Source notes unchanged. Long plans require file reads for omitted details.
- Doctor: zero errors, zero warnings, READY (warning-repair-doctor.txt). Active
  dependency detection remains tested; no unused PyAudio/VLC package installed.
- 261 repair/adjacent tests passed in 23.20 seconds, including vault writes,
  voice/vault routing, key failover, Hermes transport and provider contracts
  (warning-repair-checks.txt). Doctor's existing 70 regression checks also passed
  (warning-repair-doctor-regression.txt). Two lint issues corrected before final
  scoped Ruff and compile checks. Existing Google SDK deprecation warning remains
  upstream; it is separate from these three Doctor warnings.
- The initial vault maintenance diagnostic also refreshed its generated health
  report; no user-authored source note was changed, moved or deleted.

Era must restart to load the updated readers. Current encrypted credentials are
usable by this Windows account; old processes holding plaintext-only reader code
must not be resumed. The broader Phase 6 foreground increment followed these
verified repairs. Full updated readiness outcome: all 33 gates passed in 396.8
seconds; Doctor zero errors, zero warnings, GLOBAL_READY YES. Evidence is in
phase6-warning-foreground-global.txt; foreground scope and deferred live checks
are tracked in PHASE6_PROGRESS.md.
