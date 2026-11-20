# Phase 6 verification and remaining acceptance

2026-10-14. User requested completing Phase 6 before the Phase 7 command center.
Software acceptance is based on bounded, truthful coverage; universal app control
and literal instantaneous knowledge of every OS object are not claimed.

## Final implementation increment

- General PnP inventory uses native present-instance IDs, driver started/problem
  evidence and an explicit unknown status if a device disappears during reading.
  A hidden native notification window receives interface changes, with 15-second
  reconciliation. It opens no media streams and changes no drivers/devices.
  Combined device events/snapshots feed existing HUD and bounded reasoning hints.
  It does not equate a present Bluetooth devnode with a live glasses connection.
- Newly created/moved-in directories gain metadata-only asynchronous discovery.
  Bounded roots/queue, three-second slices, 5,000-entry and depth limits, link/
  junction exclusion and reported incomplete/dropped work prevent hidden scans.
  Watcher callbacks retain existing indexed-subtree repair. Changes invalidating
  the active scan cancel/roll back its transaction and schedule reconciliation;
  deletion cannot be undone by a stale discovery write. No user files are changed.
- Read recovery additionally covers file find/largest, browser inventory and PC
  running-app/active-window/system snapshots. Existing cancellation, copied
  arguments, five-second eligibility and one-retry constraints still apply.
  Media toggles, navigation and file mutations are not replayed generically.
- Supported file create/write/folder/rename/delete postconditions gain independent
  filesystem read-back. Unobserved/large/append cases retain unverified status,
  rather than a false verified flag. Proven postcondition mismatch fails the
  result without repeating the operation. Reads are bounded; no content is logged.

## Coverage matrix

| Area | Available evidence/control | Practical limits |
|---|---|---|
| Apps/windows | Stable process/window identity, safe executable/shortcut discovery, cancellation, launch read-back | Some custom/tray-only/inaccessible targets remain unconfirmed; never substitute uninstall/repair |
| Files | Existing broad roots + event metadata, FTS/index updates, subtree discovery, safe read retries, mutation read-back | Access-denied roots, queue/budget/depth exclusions are explicit; large copied trees may remain partially indexed |
| Browser | Existing CDP attach/reconnect, tab/page reads, stable selectors and vision fallback, cancellation | DRM, login, unsupported/non-Chromium/custom UIs may require user intervention |
| Spotify/Netflix/YouTube | Existing UI/DOM/GSMTC state, app-specific verification/recovery and cancellation | Official APIs remain unused; protected/vendor UI and actual playback need hardware acceptance |
| Processes/network | Existing bounded process inventory/lifecycle and interface notifications | CIM instant process coverage denied on this machine; internet reachability remains unknown from link alone |
| Devices | Audio roles plus native present PnP inventory/hotplug events | Devnode presence does not verify Bluetooth transport health or TTS routing |
| Task outcomes | Ownership/deduplication, cancellation, structured attempts and honest verification | In-flight vendor/native effects cannot be undone; no speculative mutation replay |

## Evidence and corrected failures

Native PnP probe: ready, 274 present instances, owned observer stopped cleanly.
Disposable moved-in folder fixture and search/FTS read-back passed. Initial scoped
file/device checks: 102 passed. Expanded task-family checks: 184 passed; new read
family/awareness/recovery set: 80 passed. Verification layer: 31 checks; main tool
bridge: 127 checks. No real user file/app/media mutation or device disconnection
was induced by the coding agent.

The first project-wide run found three stale source-inspection assertions in the
speech gate: they looked only inside EraLive.run, while actual conversation logic
was delegated to _run_local_session by the earlier Telegram lifecycle wrapper.
The checks now verify that run actually awaits the delegated method and inspect
that method for Claude ordering, health watchdog and session-task ownership.
Corrected Claude checks: 102 passed. The failed original run is preserved, not
rewritten as passed. Final readiness evidence will be appended after completion.

## Live acceptance carried forward

Earlier explicitly deferred acoustic, device unplug/replug, usual-folder HUD,
browser/media and safe application-launch tests remain in DEFERRED_ACCEPTANCE.md.
The user confirmed native Telegram two-way conversation, but new auto-calling,
manual presence/physical return-home and natural hangup still require live tests.
These are not silently marked passed by offline tests.

The 12 previously deleted schedules remain unrestored: their original parameters
are not proven. Reconstruction must be reviewed independently; no invented jobs
are created as part of Phase 6 closure. Generic app limitations and indexing
bounds above remain documented product limitations, not universal-success claims.

Final evidence: original full runner completed in 480.7 seconds, 32 gates passed
and Speech pipeline failed its three stale source assertions. Corrected Speech
pipeline: 682 passed in 26.33 seconds. Updated System awareness: 342 passed in
15.16 seconds. These reruns repair the one failed gate and cover final changes;
all 33 gate families have passed. The original report remains GLOBAL_READY NO;
it was not edited or hidden. Current one-command runner includes the corrected
checks. Doctor in the full run passed. Scoped Ruff and compilation passed.

Native moved-in-folder proof also found SQLite connections left open because the
SQLite transaction context manager does not close connections. Explicit closing
fixed it: native descendant indexed and temporary fixture removed successfully.
One initial fixture move and two later fixture preparations encountered transient
Windows WinError 5 locks before the indexed operation ran. Disposable fixture
preparation now retries only failed, unchanged renames for at most two seconds;
assertions and application operations are not skipped. OS contention provenance
was not proven. One automated recursive TEMP cleanup was rejected; cleanup was
completed safely using the exact remaining fixture file and empty directory.
