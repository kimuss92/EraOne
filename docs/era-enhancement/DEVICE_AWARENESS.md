# Passive audio-device awareness — 2026-10-13

## Design and scope

User authorized continuing Phase 6. Existing audio endpoint helpers and routing
logic already work on demand; this increment adds passive endpoint notifications
and bounded reconciliation rather than changing routing or starting another audio
capture/playback pipeline. Core Audio endpoint identifiers remain opaque stable
identities. Friendly names are labels; device kind comes from the existing hardware
form-factor mapping. No glasses identity is guessed from name ordering.

The observer records active input/output endpoints and console, multimedia and
communications defaults **as reported to this process**. Windows per-application
audio preferences may mask global defaults. These fields are explicitly named
processDefaults; this observer does not claim they are global system routing.
Bluetooth connection and actual stream route remain unknown in this snapshot;
existing routing read-back provides separate evidence for an opened voice stream.

Notifications flag a pending read only. A worker performs enumeration, coalesces
bursts at a 250 ms minimum interval, reconciles every 15 seconds, and sends revisioned
snapshots to the HUD and reasoning layer. Unchanged heartbeats do not add timeline
noise. A registration failure uses explicitly degraded reconciliation-only coverage;
read failures and evidence older than 35 seconds do not claim active hardware.
Inventory/default disagreement is degraded rather than a false disconnect claim.

## Integration and lifecycle

core/device_awareness.py reuses the foreground observer's worker/backoff lifecycle.
It starts once during main startup and stops at shutdown, separately from voice
session reconnects. Native callback registration/unregistration stays on its owner
thread. STA callbacks are pumped there. Failed unregistration retains callback
references, reports unsuccessful cleanup, and refuses a duplicate registration.

The HUD receives system.devices.observed and deviceObservation in reconnect
snapshots, rejects stale event revisions, and adds activity only for meaningful
changes. The model receives at most eight endpoint entries and an explicit omitted
count. Complete HUD snapshots retain the full bounded inventory, up to 256 entries.

Microsoft's [IMMNotificationClient documentation](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nn-mmdeviceapi-immnotificationclient)
requires nonblocking callbacks and forbids registering/unregistering callbacks from
inside a callback. The installed pycaw callback API and these rules guided this
implementation. No new package installation, stream opening, endpoint activation
for playback, volume adjustment, pairing, or default-device change was performed.

The alternative of repeated endpoint polling alone would miss the requested native
event architecture. Generic USB/PnP monitoring would add a separate coverage surface;
that remains separate work rather than being mislabeled as covered by Core Audio.

## Verification and findings

120 focused tests passed, covering validation, stable identity, roles, staleness,
unavailable/degraded states, heartbeat noise, bounded model evidence, HUD state
ownership, passive reads, event pumping, registration failures and cleanup failures,
plus existing audio routing regressions. The updated system-awareness gate passed
237 tests in 9.88 seconds. Ruff, compile, whitespace, TypeScript and production HUD
build passed. Doctor reports zero errors and zero warnings. Manifest: 348 modules,
57 tools, zero unparseable.

The first native probe failed with RPC_E_CHANGED_MODE: importing the COM library
initialized the thread in STA, conflicting with the requested MTA mode. Corrected
to compatible STA initialization and added message pumping; this was an implementation
failure, not a hardware disconnect. An initial import-order lint finding was also
corrected. The final native read-only probe reported four active endpoints, ready
endpoint-events+reconciliation coverage, a live registration, and clean unregistration
with no worker left running. It did not change any device state or restart Era.

The full 33-gate suite was not repeated for this isolated increment. Its preceding
pass, this updated system gate, and the prior 629-test speech gate are distinct
evidence, not a claim of a newly completed global run.

## Remaining acceptance

Manual device/default changes reaching the running HUD still require live acceptance
after an ordinary Era restart. No disconnect or default change was induced during
verification, so native event delivery for that physical action remains pending.
Measured state-to-HUD latency is not yet established. Generic USB/PnP inventory,
Bluetooth transport truth, broader task recovery, live search/cancellation and
glasses acoustics remain open. Native Telegram calls remain deferred until the
separate account is prepared. Phase 6 remains partial.
