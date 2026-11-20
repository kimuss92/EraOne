# Phase 6 progress — 2026-10-13

Status: started, partial. The user explicitly deferred native Telegram calls
until a separate Era account is prepared and requested this phase. Phase 5's
32 software gates passed; no deferred acoustic, live-button, or call acceptance
was converted into a pass. No Phase 7 work started.

## Completed first increment

- Read-only GSMTC event observer starts once with the backend and survives voice
  reconnects. One hidden PowerShell helper uses .NET callbacks for native session
  and playback events, plus 15-second reconciliation. It does not play, pause,
  reconnect Bluetooth, change microphone/speaker defaults, or activate browsers.
- StateStore replaces a complete media snapshot atomically, including removed
  sessions and health/freshness. Helper generation scopes session identity;
  simultaneous sessions with the same app label remain separate.
- Fresh observed Spotify playback supersedes intended state without modifying
  the intent cache. Removed, stale or unavailable telemetry cannot resurrect an
  old Spotify “playing” intent after live observation begins. Boolean False is
  absence of affirmative evidence, not verification of paused playback. Browsers
  retain their existing intent fallback until attribution is implemented.
- HUD gets detached media observations through WebSocket events and snapshots.
  The frontend records actual changes on the timeline, excludes unchanged
  reconciliation heartbeats, and ignores older media revisions after a snapshot.
- Session configuration includes fresh OS observations and no longer describes
  Spotify window titles as playback proof. Live reasoning has a queryable cache;
  already connected models still need live status tools. No unsolicited model
  turns or voice resets were added to refresh media context.
- Helper failure clears observed sessions, publishes unavailable health, and
  retries with backoff. A broken HUD publisher does not invalidate valid OS data.
  Duplicate startup is refused, and shutdown reaps the helper and observer thread.

## Verification / transparent failures

- PowerShell Register-ObjectEvent probe failed: Windows PowerShell cannot
  subscribe to WinRT events. A direct C# WinMetadata-reference probe also failed
  to load metadata as an ordinary assembly. The reflection/delegate .NET adapter
  succeeded; no unsupported event implementation was left enabled.
- Native initial snapshot: one OS session observed; parser accepted playback.
  Reconciliation probe: initial then reconcile, same session IDs. Artifact:
  phase6-native-check.txt. No playback/device commands were issued.
- Real service startup/shutdown probe: ready, duplicate start prevented, stopped
  health, and thread stopped. Artifact: phase6-native-lifecycle-check.txt.
- Initial focused regression: 418 passed. Expanded state/media/HUD/tool bridge,
  model reply ownership and standby run: 490 passed; existing Google SDK warning.
  Final rerun results: phase6-observer-checks.txt.
  Final focused run: 490 passed with the existing Google SDK warning.
- First scoped Ruff invocation used the UI directory for repository-relative
  paths and failed path lookup; reran at the repository root. Two import-format
  issues were then corrected. A patch with duplicate file operations was rejected
  without applying changes, then recombined successfully.
- HUD production build and TypeScript check passed. Scoped Python compile, lint,
  whitespace checks passed; refreshed manifest is current (339 modules, 57 tools,
  zero unparseable modules). No new packages installed,
  destructive cleanup, production log overwrite, live Era restart, or account
  login was performed. Earlier uncommitted work/backups were preserved.

## Remaining Phase 6

- User acceptance of manual play/pause and closed-session reflection in running
  Era after restart; native subscription/read proof is not this live acceptance.
- Live acceptance of connected-browser attribution and command read-back; GSMTC
  browser app identity alone cannot distinguish YouTube from Netflix.
- Foreground/app/file/network watchers and reconciliation, audio-device event
  unification, and task/agent state consolidation.
- PC/files/browser/Spotify/Netflix task-family recovery, selector reliability,
  verified outcomes, bounded workarounds, failure lessons, and latency measures.
- Further reasoning integration that preserves input/reply ownership; final
  Phase 6 software and sustained-use acceptance. The latest full global run
  predates this observer; a new global readiness pass is not claimed here.

Telegram native calls remain deferred in TELEGRAM_CALL_DESIGN.md. Earlier voice
and live Telegram approval checks remain in DEFERRED_ACCEPTANCE.md.

## Completed browser increment

- Connected YouTube/Netflix tabs now have a separate read-only observer. Native
  DOM media events and element replacement trigger coalesced read-back; a
  15-second reconciliation timer recovers missed events. Observation never opens
  tabs, activates windows or controls playback. Browsers not connected to Era's
  existing driver are outside this increment's coverage.
- Validate HTTP(S) hostname families, top-level callbacks and sampled document
  URL. Ignore claimed event payloads; store only source, browser, opaque session
  ID, state, freshness and health. No full URLs, titles or page bodies are stored.
- Multiple tabs retain distinct identities. Navigation, close, read failure,
  driver replacement and stale observations remove affirmative playback evidence.
  Previously observed sources cannot fall back to an old requested playing flag.
  Unknown evidence does not mean verified pause.
- Browser observations reach HUD snapshots and events with revision guards;
  unchanged heartbeats do not add timeline entries. The reasoning cache includes
  browser evidence without injecting model turns or resetting voice sessions.
- YouTube pause reads back paused state; resume awaits playback acceptance and
  checks state. Rejected playback/read-back failures are returned without a toggle
  retry. Failed commands no longer overwrite the requested-state cache; a toggle
  that actually pauses records paused rather than always recording playing.
  The legacy keyboard fallback reports its result as unverified. Targeted keyboard
  fallback verification and the legacy single-playback intent sweep remain work
  for the task-family recovery increment; universal verified playback is not claimed.
- Background pause read-back accepts only a positive boolean result, rather than
  any response containing the word true.

Verification: 443 focused state/media/browser/HUD tests passed in 9.36 seconds
(phase6-browser-checks.txt); 56 additional driver-recovery, YouTube selection,
next/direct playback and Netflix hard-lock regressions passed in 24.39 seconds
(phase6-browser-routing-checks.txt). The observer JavaScript was executed in an
isolated Node harness, including manual pause and event coalescing. No real user
media was controlled. Production HUD build, TypeScript check, scoped Ruff, Python
compile and whitespace checks passed. Manifest refreshed and checked: 340 source
modules, 57 tools, zero unparseable modules. Added observer regression coverage
to the existing System awareness readiness gate.

During implementation, navigation-return binding reuse and listener cleanup were
corrected. Ruff reported one test import ordering issue, which was fixed and
rechecked. No unresolved focused check failures. No dependency installs, destructive
cleanup, live Era restart, microphone recordings, Telegram sends or account login.
The full global readiness suite has not been rerun after these Phase 6 changes.

Live acceptance still pending: restart Era, use already-connected YouTube and
Netflix tabs, manually play/pause and close a tab, and check that HUD observations
and Era's live status query follow the actual state. Verify pause/resume reports
and an unavailable player. This check is not recorded as passed. Phase 6 remains
partial; the remaining watcher and task-family work listed above is unchanged.

## Playback recovery increment

Implemented within the approved Phase 6 reliability scope:

- Removed the YouTube global Space-key retry after a missing player, failed DOM
  read or interrupted command. The previous fallback could toggle a different
  foreground tab or reverse an action that completed before its response failed.
  Confirmed DOM controls still work; unknown outcomes now return an explicit
  failure/unverified result. Recovery requiring a selected keyboard target remains
  a future increment rather than sending an unscoped key.
- The exclusivity sweep records paused intent only when its pause path returns
  confirmation. Failed attempts preserve prior intent and emit an unverified
  warning. The post-action sweep reports partial failure instead of claiming
  double playback was resolved regardless of the result.
- Browser pause uses the existing connected, domain-targeted CDP read-back rather
  than window-title selection and a foreground controller. Spotify enforcement
  uses its explicit targeted pause command and native observed paused state. It
  bypasses the old pause helper's global-toggle fallback and premature cache write.
  Observer delivery may arrive after the synchronous check: this is reported as
  unverified, and later observations supply actual state.

Limitations: the existing browser pause helper targets one selected media tab;
all-tab exclusivity is not guaranteed. Active-source intent is still written by
legacy callers before playback finishes. The public Spotify pause/resume paths,
Netflix selection/recovery and broader task-family migration remain separate
work. No universal playback reliability or complete Phase 6 claim.

Verification: 448 focused playback/state/routing tests passed in 24.72 seconds
(phase6-recovery-checks.txt). Tests explicitly reject global keys after missing,
blocked or interrupted YouTube controls, retain failed pause state, report partial
sweep results and require Spotify command/read-back confirmation. Two test import
ordering issues were found by Ruff, corrected and rechecked. Scoped Ruff, compile,
whitespace and refreshed manifest checks passed. No live user playback changed,
packages installed, destructive cleanup or account actions performed.

Full readiness run completed: all 33 gates passed in 433.1 seconds,
VISION_STACK_READY YES, GLOBAL_READY YES, ALL_CHECKS_OK. Evidence:
phase6-global-checks.txt. This supersedes the earlier notes that the global
suite had not been rerun after Phase 6. Software readiness is not live hardware
acceptance or completion of the remaining Phase 6 scope.

Doctor: zero errors, three existing warnings (local credential configuration,
vault filing consistency, missing optional pyaudio/python_vlc). The existing Google
SDK deprecation warning also remains; no package upgrade was attempted. Expected
negative-test errors in the log did not represent failing readiness gates.

## Warning repairs and foreground increment

The user requested repair of all three Doctor warnings before proceeding.
WARNING_REPAIRS.md records the design, migration safety and evidence. Doctor now
reports zero errors and zero warnings. Native credential encryption was verified
after the user closed Era. No unused audio packages were installed, and no
user-authored vault notes were deleted, moved or rewritten. 261 adjacent repair
tests and 70 existing Doctor checks passed before the foreground work began.

Foreground app observation now runs once per backend with a Windows WinEvent
hook, an owning message-pump thread and 15-second reconciliation. Reads HWND,
PID and process name only; no titles, paths, window contents, focus changes or
intent-memory writes. Identity is rechecked after resolution. Detached snapshots
carry revision, health and freshness; failures clear evidence and retry with
backoff. Shutdown unhooks and joins the worker. Voice reconnects do not start it.

Foreground snapshots and events reach the HUD and cached reasoning context.
Only semantic changes add timeline items; older event revisions are ignored.
This is foreground app evidence, not a complete background-process/open-window
inventory. Further task-family and file/network/device watchers remain pending.

Verification: 145 foreground/window/process/media/state/repair tests passed in
2.13 seconds (phase6-foreground-checks.txt). The first run had a test fixture
importing a nonexistent HUD module; corrected to the existing HudBus API and
reran successfully. One main import-order issue was fixed before scoped Ruff.
Native read-only hook probe: ready, identity present, clean stop, worker stopped
(phase6-foreground-native.txt). No real Alt-Tab/input/window activation was
performed. Manual focus-change acceptance after restart remains pending.
HUD build, TypeScript, Python compile, lint, whitespace and manifest checks
passed: 343 source modules, 57 tools, zero unparseable modules. No dependencies
installed or destructive cleanup performed.

Full updated run: all 33 readiness gates passed in 396.8 seconds; Doctor zero
errors, zero warnings; VISION_STACK_READY YES, GLOBAL_READY YES, ALL_CHECKS_OK.
Evidence: phase6-warning-foreground-global.txt. This supersedes the previous
three Doctor warnings and global result predating these repairs. The upstream
Google SDK deprecation warning remains outside Doctor's health warnings.
Phase 6 stays partial: foreground coverage is implemented, but background-process,
file/network/device watchers, remaining task recovery and live acceptance are open.

## Process inventory increment

Read-only process inventory now starts once with the backend, preserves separate
same-name instances, and uses PID plus creation time to distinguish PID reuse.
Window lifecycle events trigger coalesced refreshes; 15-second reconciliation
covers non-window processes. No paths, command lines, environment, contents or
credentials are read. Source identity uncertainty, snapshot omissions and degraded
coverage are explicit; this cache never authorizes process termination.

HUD receives detached revisioned snapshots/events; unchanged heartbeats do not
add timeline entries. Reasoning receives at most 40 executable names with counts,
freshness, coverage and omitted-name counts. Existing live process control/query
tools remain unchanged. Native event registration/pumping is shared with the
foreground service; callbacks signal refresh instead of performing OS reads
inside a reentrant event callback. Each service has a distinct worker, duplicate
start guard and clean stop/unhook. Hook failure falls back to explicitly labeled
reconciliation-only inventory and retries without elevation.

Failed attempts and fixes:

- CIM process trace subscription was denied (CimException status 2). It was not
  bypassed or elevated. Selected WinEvent window lifecycle hints instead. This
  is not instant coverage of every background process: non-window changes can
  take the reconciliation interval, and short-lived instances may be missed.
- First native inventory probe rejected a Windows zero creation time and cleared
  all evidence. Diagnosis found one such system-process value. Normalize it to
  unknown identity, retain readable instances, and mark degraded completeness.
  Added a regression; before/after probe evidence is retained.

Verification: 153 relevant process/foreground/window/media/state/model ownership
tests passed in 15.55 seconds (phase6-process-checks.txt); 127 existing main
tool-bridge checks passed (phase6-process-bridge.txt). Native read-only probe:
282 process instances, 2 omitted entries, 1 unverified birth time; health degraded,
coverage window-events+reconciliation, clean stop and worker stopped
(phase6-process-native.txt). Counts are an observation at probe time, not fixed
system totals. Doctor: zero errors, zero warnings (phase6-process-doctor.txt).
HUD build, TypeScript, scoped Ruff/compile, whitespace and manifest checks passed:
344 source modules, 57 tools, zero unparseable. No packages installed, user apps
launched/closed, focus/input mutations, recordings or destructive cleanup.

The full 33-gate run predates this increment and was not repeated; no fresh global
pass is claimed for this increment. Live app launch/exit and foreground event
acceptance after restart remains pending. Phase 6 is still partial: file/network/
device watchers, broader task recovery and remaining live acceptance are open.

## Local network increment

Native NotifyIpInterfaceChange notifications now trigger read-only, coalesced
interface snapshots, with 15-second reconciliation. Reads adapter name, current
interface index and link-up state; no IP/MAC fields, SSIDs, traffic or credentials
are stored. No active probe or adapter/network setting change was added. Internet
reachability stays explicitly unknown in this observer: an up interface does not
prove internet access. Existing separate HUD reachability probing is preserved.

One backend-scoped service feeds revisioned HUD snapshots/events and a bounded
reasoning context (at most 32 interfaces, with overflow count). Unchanged heartbeats
do not add timeline items. Read failure/staleness removes affirmative link evidence;
registration failure falls back to explicitly labeled reconciliation-only coverage.
Common worker retry handling is now shared with the process observer instead of
duplicated. Native callbacks only signal a worker; cancellation occurs outside
the callback. Callback references are retained if cancellation fails, new starts
are refused, and stop reports failure until the subscription releases.

Diagnostics and repairs: the first native probe registered/read/stopped successfully
but had six unresolved indexes because Windows friendly aliases differ from socket
interface names. Added Windows alias-to-LUID-to-index fallback; final probe resolves
all six interfaces with health ready, zero unknown indexes, coverage interface-events
plus reconciliation, clean stop, subscription released and worker stopped.
Before/after artifacts retained. An initial read command used unsupported PowerShell
brace syntax and was corrected before it executed; no project changes were involved.

Verification: 113 relevant network/process/foreground/media/state/model ownership
tests passed in 4.86 seconds (phase6-network-checks.txt). 127 main tool-bridge checks
passed (phase6-network-bridge.txt). Native registration/read/cancellation proof is
in phase6-network-native.txt. Doctor zero errors, zero warnings
(phase6-network-doctor.txt). Existing Google SDK deprecation warning remains.
HUD build/TypeScript, scoped Ruff/compile, whitespace and current manifest passed:
345 source modules, 57 tools, zero unparseable. No dependency installs, destructive
cleanup, live Era restart, network disconnect/reconfiguration, user app mutations,
recordings or Telegram actions. The full global suite was not repeated; its latest
33-gate pass predates the process/network increments.

Phase 6 remains partial. Native subscription/read proof does not verify real
adapter-change event delivery after restart; that live check remains pending.
File/device watchers, broader PC/files/browser/media task recovery, and remaining
hardware/live acceptance are still open. Native Telegram calls remain deferred.

## Conversation regression takes priority — 13 October

Phase expansion paused after the user supplied a recording showing loss of
voice/text responsiveness. PC Boost and media coordinator were missing from the
slow-tool grace policy and could suspend receive processing. Their grace,
worker cancellation, optional media explicit-address fallback and typed-request
health monitoring were repaired. See CONVERSATION_REGRESSION_20261013.md for
the log evidence, regression checks and pending live acceptance. The user's
glasses and conversation tests must not be silently counted as passed.
Full readiness after these repairs passed all 33 gates in 414.2 seconds with
GLOBAL_READY YES. This updates the earlier software readiness baseline; live
conversation/audio acceptance and unfinished Phase 6 work remain open.

## User-requested direct Gemini listening

The user rejected the media voice filter as impractical. Removed optional media
and attention filtering from the live send path and the post-reply capture hold.
The security voice lock remains explicitly configurable and currently OFF.
Found and installed the missing native speech-VAD dependency; live interruption
now requires sustained, speech-shaped input instead of amplitude-only noise.
The bottom waveform is input-only. Gemini-first watchdog recovery and a default
grace for newly added live tools prevent automatic Claude handover and uncovered
slow-tool stalls. See GEMINI_OPEN_LISTENING_20261013.md for verification, initial
failures/repairs and remaining real-room acceptance. Phase expansion stays paused.
Final software verification: all 33 readiness gates passed in 385.0 seconds,
GLOBAL_READY YES; Doctor zero errors and zero warnings. This does not complete
the pending live glasses/music/interruption acceptance test.

## File awareness increment — resumed by user authorization

The user requested the next step after the Gemini repair and chose broad file
coverage starting with commonly used locations. Phase 6 resumes; the pending live
audio checks remain deferred, not passed. Reused the existing search-index watcher
for metadata events, preserving its configured personal-folder and fixed-drive
roots. Personal-folder events take priority under load. Added bounded queues,
recent-path reconciliation, explicit gaps/staleness and revisioned HUD/reasoning
snapshots. No second observer or new recursive scan was added.

Fixed own-database/WAL notification feedback, missing-root recovery and partial
observer shutdown handling. Installed the already-declared watchdog dependency
after a native probe found it missing; Doctor now checks it. The optional watchmedo
CLI PATH warning requires no change for library use. An initial HUD test used a
nonexistent snapshot key; corrected to the actual state contract. Both initial
failures and the coverage limits are detailed in FILE_DESIGN.md.

Native Windows TEMP-only create/rename/delete and existing search deletion passed,
with one emitter, ready health, zero drops and clean service/observer shutdown.
No live Era restart or user-file mutation occurred. Live broad-root coverage/HUD
acceptance is pending. Directory-subtree search-index repair remains open.

Focused verification: 196 tests passed (file/index/system/foreground/process/network,
HUD, input ownership and warning repairs); one existing Google SDK deprecation
warning. Scoped Ruff, compile, whitespace, HUD production build and TypeScript
passed. Doctor reports zero errors and zero warnings. Manifest: 346 modules,
57 tools, zero unparseable. Full global readiness passed all 33 gates in 401.5
seconds (GLOBAL_READY YES; TEMP artifact era-file-awareness-full-checks-20261013.txt).
This does not complete Phase 6 or live acceptance.

### Search voice interruption repair — 2026-10-13

User supplied a recording of repeated cancellation and Era's admission that she
recited raw search listings. Paused further phase work to repair this regression.
Logs confirmed grounded-search quota exhaustion, fallback listings forced into a
say-exactly wrapper, overlapping report requests, and a cancellation grammar that
waited too long. See SEARCH_VOICE_REPAIR.md for evidence, changes and limitations.
142 focused checks and the updated 629-test speech gate passed (23.41 seconds;
one existing SDK deprecation warning). Doctor remains zero errors/zero warnings. Live
summary/cancel/follow-up acceptance is pending. Search quotas are external; the
repair does not claim to remove their limits or restore forbidden microphone gates.

### Folder-search subtree repair — 2026-10-13

User authorized proceeding. Known directory descendants and open-history entries
now relocate or disappear together when a folder moves or is deleted. Transactions
use an independent SQLite connection; no recursive filesystem scan or production
file mutation is introduced. Added directory creation indexing and FTS-safe upserts.

The overlapping-root regression reproduced FTS corruption before the repair. The
first native Windows deletion probe also failed because watchdog reports deleted
directories as files; recovering their known cached type repaired history cleanup.
These initial failures and remaining discovery limits are documented in
FILE_SUBTREE_REPAIR.md. No automatic rebuild of the user's cached database occurred.

Verification: 150 focused tests and all 218 updated system-awareness gate tests
passed; native TEMP-only folder rename/delete and FTS integrity passed. Scoped
Ruff, compilation and whitespace passed. Doctor: zero errors/zero warnings.
The preceding full 33-gate pass and search repair's 629-test speech pass remain
separate evidence; the entire global suite was not rerun for this isolated increment.

Phase 6 remains partial: device watchers and broader task recovery remain. Unknown
moved-in trees still need native child events or the normal rebuild mechanism.
Live broad-folder/HUD, search/cancel/follow-up and glasses checks remain pending;
native Telegram calling remains deferred until the separate account is ready.

### Passive audio-device awareness — 2026-10-13

Added native Core Audio endpoint notifications, passive active-endpoint inventory,
process-default roles, reconciliation and bounded freshness. Stable IDs, device
kind and roles are distinct from Bluetooth transport and opened stream route.
Wired revisioned snapshots to HUD/reasoning and startup/shutdown without audio
stream changes. Generic USB/PnP coverage is not claimed. See DEVICE_AWARENESS.md.

Initial native probe failed due to COM apartment mismatch; compatible STA handling
and message pumping repaired it. Final native registration/readback/cleanup passed
with four active endpoints and no stream or device mutation. Initial import-order
lint finding corrected. Final verification: 120 focused tests, 237 system-awareness
gate tests, Ruff, compile, whitespace, TypeScript and HUD build passed. Doctor:
zero errors/zero warnings. Manifest: 348 modules, 57 tools, zero unparseable.

Live manual default/connection changes and state-to-HUD latency remain unverified.
Phase 6 remains partial: general device coverage and broader task recovery remain;
previously deferred live audio/search/folder tests and Telegram account setup remain
pending. No full global rerun or running Era restart was performed this increment.

### Bounded read recovery — 2026-10-13

User authorized proceeding. Added one transient retry for explicit read-only file
and browser actions, preserving original parameters, cancellation context, existing
raw gates and final verification. Mutations, approval-bearing calls, searches with
their own retries, and media toggles do not gain replay. No unused global-toggle
fallback was activated. Five seconds is an eligibility window, not an execution
timeout; existing tool timeouts still apply. See READ_RECOVERY.md for the scope.

Recovery attempts and outcomes feed task trace/logs and HUD tool rows. Permission,
held approval, cancellation and long-read failures remain unretried. A verifier can
still reject returned content after recovery; that remains a failed result.

Verification: expanded 205 focused tests passed; final edge cases passed in the
48-test targeted run. Updated speech gate: 657 passed in 42.95 seconds, with the
existing Google SDK deprecation warning. Execution layer: 22 passed; verification:
31 passed; main tool bridge: 127 passed. Ruff, compile and whitespace passed.
Doctor: zero errors/zero warnings. Manifest: 349 modules, 57 tools, zero unparseable.
An initial import-order lint finding was corrected. No live Era restart or injected
user-file/browser/media failure occurred. No full global suite rerun was performed.

Phase 6 remains partial: generic device coverage and additional task-specific
recovery remain, with all deferred live acceptance still pending. Phase 7 has not
started; native Telegram calls remain deferred pending the separate account.

### Live Activity cancellation repair — 2026-10-13

Generic "cancel task" now cancels the newest live task; "cancel every task"
cancels all live tasks. These commands are consumed even when no live job exists,
and scheduler mutations are blocked for generic live-cancellation utterances.
Every running HUD card has its own visible Cancel task button tied to that job ID.
Atomic claims prevent identical concurrent jobs from creating duplicate HUD rows.
Spotify workers inherit cancellation context and check it during discovery, waits
and before playback actions. Already executing native calls cannot be forcibly
interrupted or undone. See LIVE_CANCEL_REPAIR.md for details and evidence.

Verification: 183 focused tests passed; the final updated speech gate passed all
682 tests in 29.41 seconds. Two existing dependency warnings remain in that test
run (Google SDK Python deprecation and pywinauto COM initialization). Scoped Ruff,
compilation, whitespace, TypeScript and the HUD production build passed. Doctor
reported zero errors/zero warnings before the final Spotify cancellation checks.
An isolated browser preview verified both per-task buttons send only their own
task ID to the live cancellation endpoint; it contacted no running Era service.
Temporary preview files and browser/server helpers were removed or stopped.

Logs show the earlier generic cancellation deleted 12 persisted schedules; three
were subsequently recreated by the model. Original definitions have not been
restored or invented. This remains an explicit unresolved incident. No live Era
restart, physical cancellation acceptance, or full global-suite rerun occurred.
Phase 6 remains partial and previous hardware acceptance checks remain pending.

### PC automation cancellation follow-up — 2026-10-13

Continued the approved Phase 6 task-reliability work. MSI scenario discovery,
focus, verification and retry waits now honor the existing live cancellation token.
Cancellation exceptions propagate instead of becoming ordinary UI errors. PC Boost
checks discovery steps and prevents a fallback click after cancellation. Existing
MSI read-back verification and disabled direct-write policy remain in place.
See SYSTEM_TASK_CANCELLATION.md for design, evidence and native-call limitations.

Verification: initial 133 focused tests passed (two existing dependency warnings);
after adding two full MSI-path cases, all 299 expanded system-awareness tests
passed in 18.16 seconds. Scoped Ruff, compile and whitespace passed. Doctor:
zero errors/zero warnings, READY. An initial zip strictness lint issue was fixed.
No vendor app action, physical click, power-mode change or live Era restart was
performed; no full global rerun. Previously deferred hardware acceptance and
restoration of the 12 deleted schedules remain open. Phase 6 remains partial.

### Safe application opening — 2026-10-13

User reported app opening selecting uninstallers and Brave launch failures. Found
first-substring shortcut selection, absent target inspection, default-browser
launch in the Brave registry path, blind Windows Search Enter fallback, and an
unconditional open_app verifier. Added ranked/inspected safe launch targets,
process-owned existing-window focus/read-back, requested executable routing,
registered Store-ID fallback, early close handling and cancellable offload/waits.
Slow checks use existing background reporting to keep voice reception available.
See APP_LAUNCH_REPAIR.md for design, native observations and limitations.

Final system-awareness gate: 324 passed in 15.78 seconds. Speech gate: 682 passed
in 32.43 seconds (two existing dependency warnings). Verification layer: 31 passed;
main bridge: 127 passed. Doctor zero errors/zero warnings. Ruff, compile and
whitespace passed. Initial lint and COM-proxy release findings were corrected.

Native read-only probes identified both running app windows and Brave's safe
executable. Telegram's D: shortcut targets are unavailable to this session despite
the running process reporting that location; underlying path visibility remains
unresolved. No app, drive, shortcut or installation was changed by these probes.
No live launch/focus/uninstall interaction, restart or full global-suite rerun.
Universal app manipulation is not claimed. Phase 6 remains partial; live acceptance,
broader app-specific control and the earlier removed schedules remain open.
