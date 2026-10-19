# Stability pass: speech, glasses and project verification

**Later owner-feedback repair:** Era stopped answering despite remaining
connected. [Response delivery recovery](RESPONSE_RECOVERY_2026-10-18.md) is now
loaded (PID 24028), with live voice/reply evidence and 570 affected/protected
tests plus eight manifest cases passing. The full baseline below predates it.

User authorized continuing the remaining phases on 2026-10-18, with later
physical/behavior feedback. Preserve existing theme, source work, subscription
Claude, desktop Spotify, memory/vault, approvals and natural speech ownership.

## Diagnosis and plan before implementation

1. Reproduce current known failures, run Doctor and inspect real logs/native
   device state before changes. Keep private conversation/credentials out of
   reports. Doctor currently READY, zero errors and one vault filing warning.
2. Trace generation versus output delivery. Full conversation text is not audio
   completion. Live logs show audio_flush interruptions and device stream
   close/reopen around provider rotations. These observations do not prove a
   particular physical Bluetooth disconnect or beep source.
3. Repair the queue/device-buffer timing boundary: after successful PCM writes,
   keep speaking until the stream's reported output latency has elapsed. Do not
   treat an empty application queue or bytes copied to the device as physical
   listening proof. Respect real interruption and forced provider deadlines.
   Test healthy/faulted output and renewal before integrating.
4. Repair crew whole-call deadline classification after a model wait, retaining
   cancellation, capability grants, retry bounds and suppression of late work.
5. The reproduced audio-reclaim failure passes 15 ASCII bytes into strict mono
   int16 PCM validation. Repair that fixture with valid even-length PCM; retain
   every fallback/backoff assertion and rejection of malformed PCM. This does
   not establish or alter actual hardware playback.
6. Run focused regressions, required protected suites, scoped Ruff/format,
   manifest checks, current full Windows tests and readiness checks. Fix
   reproduced defects without relaxing protected contracts. Record failures and
   coverage limits accurately. Use the normal lifecycle for loading changes.
7. Audit existing guarded learning/fallback/supervision and cleanup requirements
   against current evidence before adding Phase 10 mechanisms. Never deploy an
   unverified external integration or erase existing dirty work to claim closure.

Backups before editing: `.phase7_backups/stability-20261018/`.

## Hardware investigation and troubleshooting basis

Present Windows Bluetooth inventory reports CY03-CB36 and Intel Wireless
Bluetooth OK; Bluetooth Support Service is running. A bounded last-three-hours
System log sample contains BTHUSB event 18 and Kernel-PnP 219 near system startup,
not a continuous trace proving the reported reconnect sequence.

Likely classes to distinguish: provider/local interruptions, audio output loss,
device buffered-tail timing, routine session stream reopening, Bluetooth driver
or power-management faults, and physical glasses battery/radio conditions.
Battery/radio/beep interpretation is not established for this glasses model.

Agent Reach search and official Microsoft Support confirm that Bluetooth drops
can interrupt playback and recommend the Windows Bluetooth troubleshooter,
checking energy-saving settings and updating the Bluetooth driver. Source:
[Microsoft: Bluetooth keeps disconnecting in Windows](https://support.microsoft.com/en-us/windows/hardware/bluetooth/bluetooth-keeps-disconnecting-in-windows).
No driver installation, pairing removal, Bluetooth service reset or power-policy
change has been performed. These steps need a controlled hardware test window;
they must not silently replace the user's selected audio route.

## Evidence before changes

- `runtime/stabilization-known-failures-20261018.log`: 2 reproduced failures,
  46 passed. Audio payload validation and crew deadline classification above.
- `runtime/stabilization-doctor-before-20261018.log`: READY, 0 errors, 1 warning.
- The existing main process was started after the earlier Oct 18 Spotify source
  change; a live favorite attempt then failed metadata/playback verification.
  Earlier fresh-process success is retained as limited evidence, not a universal
  live acceptance pass. Investigate actual tool arguments/state before retrying.

Final results below distinguish the full baseline run, subsequent focused
review guard and actual application startup from later physical acceptance.

## Verification completed during this pass

- Initial targeted speech/routing/deadline/protected group: **294 passed**.
  Expanded capture/ownership/Spotify/continuity/protected group: **333 passed**.
  These overlap; do not sum them as unique project coverage.
- Complete readiness: **36/36 passed in 321.4s**, two workers;
  `VISION_STACK_READY: YES`, `GLOBAL_READY: YES`, `ALL_CHECKS_OK`, exit 0.
  Profile: `runtime/stability-readiness-timings-20261018.json`. Netflix accuracy
  remains the longest suite at 195.0s; this is suite time, not command latency.
- Native capture: six leases / one physical input stream; initial open 131.5ms,
  subsequent lease acquisition 0–0.2ms; input discarded, stream closed at end.
  `runtime/stability-native-capture-result-20261018.json`.
- Native all-device Spotify state: **active**, **91.5ms**, without changing
  playback or using an API. The earlier default-only reader returned null on
  this real PC-speaker route. `runtime/stability-native-spotify-state-20261018.json`.
- Real known-image vision: read code 67 and three red circles in **3,152.8ms**;
  native two-monitor capture **349.7ms**, 3840×1080. Actual desktop pixels remained
  local/in-memory and were not submitted to the provider or saved. One known
  image is not representative accuracy or a sub-500ms analysis claim.
- All three existing HUD Node behavior checks and TypeScript pass. No HUD build.
- Root Ruff lint/format pass (1,069 files checked by format); normalized 116 real
  files with **zero runtime AST differences**. Scoped production changes reviewed
  separately. Python compilation and manifest regeneration pass; 403 source
  modules, 267 indexed test modules, zero unparseable.
- `pip check`: no broken requirements. Production npm audit: zero advisories.
- Prompt BOM and ONE REPLY, LIKE A PERSON rule verified intact. Declaration
  golden explicitly regenerated after formatting, still 59 tools.
- Persisted HUD sounds were enabled. Existing ready/error/notification tones
  are a separate possible beep source; switched only `hud_sounds` off through
  the existing settings updater for this complaint. Read-back confirms false
  and all other settings unchanged. No source/build or speech-route change.
  `runtime/stability-hud-sound-choice-20261018.json`.

## Final gate, review and loading

`python tools/era_gate.py --all` passed: **7,971 passed, four skipped, three
warnings**, tests 961.81s; complete gate 976.9s including root lint/format,
manifest and Doctor. `runtime/stability-full-gate-final-20261018.log`.
This followed the earlier one-failure baseline, not a claim that the first run
was green. The known crew deadline failure is fixed in production; the audio
reclaim fixture now uses valid PCM without weakening validation/backoff.

A final review then reproduced one new capture-cache edge: unresolved default
input must reopen to follow a changed Windows microphone. Added a failing
regression and restricted handle reuse to explicit device identities. **334
affected/protected tests passed** after that guard, with root lint/format,
manifest regeneration/freshness passing again. The full 7,971-test run and the
36 readiness gates preceded this last guard; they were not repeated afterwards.
Only `audio_capture.py` and its test changed after the full gate, proven by
the source fingerprint ledger; no other indexed source changed. Evidence:
`runtime/stability-postreview-tests-20261018.log`,
`stability-default-guard-red-20261018.log`, and
`stability-source-baseline-20261018.json` (full-run and final hashes retained).

Current pinned Claude profile's subscription auth status passed through the
existing isolated CLI boundary; no generation or API fallback was performed:
`runtime/stability-claude-auth-20261018.json`.

Era was restarted through the normal relaunch flag/supervisor after validation.
New PID **26640** connected; original standby restored, Era voice verified on
**Star glass (CY03-CB36)**, media preference **PC speakers**, HUD sounds false.
No current active media was observed, so this startup snapshot does not claim
Spotify was playing. Saved choices were not reset. Evidence:
`runtime/stability-post-start-20261018.json`.

A bounded post-start observation found no error-level events; source hashes
remained unchanged. At the final **619.4-second** checkpoint, the current process
had logged **three real session rotations and three microphone reuses, with one
initial microphone open**. Era remained connected in standby, voice routing was
verified on the glasses, and active Spotify was detected on the PC speakers.
Evidence: `runtime/stability-final-live-20261018.json`. This supersedes the earlier
180-second checkpoint, when no rotation had occurred. It proves live handle
reuse, not audible phrase completion or elimination of physical Bluetooth beeps.
One startup model.reply.unattributed warning was suppressed by the existing
ownership guard; that guard was not weakened to make speech appear successful.

Phase 10's existing local learning, self-healing and recovery were audited and
software-checked; Jev remains optional/unconfigured, so its integration is not
complete. Phase 11 now has a current-machine baseline and reviewed formatting
cleanup; no deletion proposed for the previously flagged background/vision
modules merely because they lack runtime importers. Fresh-machine setup,
sustained daily use and actual audible/glasses/approval behavior remain the
owner's later feedback, as requested. See the Phase 10/11 reports and speech
troubleshooting guide. This is not a universal zero-fault or sub-500ms claim.

## Additional confirmed findings during the project run

- Read-only native MTA probe: the default-device Spotify session reader returned
  null while the existing all-endpoint media reader found active Spotify on PC
  speakers. All-endpoint observation took 135ms (one sample). Reuse that reader
  for playback state; do not substitute a window title for audio-session proof.
- Bluetooth adapter: Intel Wireless Bluetooth 22.180.0.2, driver date September
  23, 2022. Age is a troubleshooting lead, not proof of cause; no automatic
  driver/service/pairing changes are justified by this snapshot alone.
- Initial root lint checks included frozen `.phase7_backups` copies because
  Ruff excludes phases 1/5/6 explicitly, while pytest already excludes all
  phase backup folders. Align the lint exclusion with that existing backup rule,
  preserve snapshots untouched, repair two real HUD-test import orders, then
  normalize real-source formatting only with backup and AST-equality evidence.
  The current format check identifies 116 real files and eight backup copies.
- Full Windows test run proceeds against unchanged source while running. Apply
  further repairs only between runs so manifest/source evidence stays coherent.

## Reconnect capture repair design

The first full Windows run completed: 7,956 passed, four skipped, one failure
(a standby allowlist fixture lacked the now-required actual user order).
Preserve the production grounding rule and repair the fixture, adding the
negative case rather than allowing an unsolicited standby call.

Keep one healthy input stream for at most 15 seconds between Live connections,
reusing it only for the identical device/format. Detach the old callback while
idle; discard frames rather than queueing/replaying old speech. A new lease gets
only its own callback; stale session callbacks also check queue ownership.
Close immediately on a device fault, changed device/format or shutdown. One
daemon expiry timer closes an unreused idle stream; obsolete timer callbacks
cannot close a new lease. Close retained capture before any between-session
PortAudio re-enumeration; never terminate while the capture lease is active.
No change to the user's microphone choice, mute, standby or barge-in policy.
This targets observed stream churn, not a claimed repair of the Bluetooth radio.
Test retention, idle frame disposal, route change, faults, stale expiry, shutdown
and refresh ordering; perform a bounded native handle-reuse probe without
recording/storing microphone audio. Physical beep acceptance remains user feedback.
