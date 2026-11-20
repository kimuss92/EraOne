# Phase 7 command center design

Preparation for the already-authorized roadmap; implementation waits for Phase 6
software verification. Existing deferred live acceptance remains separately tracked.

## Design choice

Extend Era's React HUD and current authenticated event/snapshot protocol. Keep the
existing conversation, microphone meter, approval controls, media automation and
native call stack. Adapt the reference's unified command-center and workspace
patterns; do not adopt its subscription proxies or marketing promises.

Alternative: build a separate dashboard/server, which would duplicate ownership
and state. Alternative: add decorative tiles/local-only layout preferences, which
would not let Era know the layout or synchronize multiple views. Recommended:
one backend-owned widget schema delivered through the existing stream.

## Surface

Keep the dark/cyan Era identity, clear connection/model/voice controls and one
conversation transcript. Add a command-center view with compact overview tiles
and an editable widget grid. The conversation stays prominent, with the orb
reduced when the command center needs space; retain a focused conversation view.

Initial widgets reuse working controls: live jobs (individual cancellation),
schedule, Telegram calls, devices, process/network/file health, memory/sources,
agent/team status from real available data, notifications and tool capabilities.
Unknown/unavailable sources show their actual status. No invented agent activity.
Each widget has a real action or explicit read-only detail view, context help and
source freshness. Widgets can be added, hidden and reordered with keyboard as
well as drag controls. File-drop gestures remain reserved for attachments;
widget dragging must not trigger upload behavior.

## Ownership and safety

Persist schemaVersion, revision, widget IDs/kinds, visibility, order and safe
display settings in user-local configuration. The server validates kinds and
allowed properties, then uses compare-and-swap revisions; stale edits fail with
the current schema. Updates emit one event. Reconnect snapshots carry the newest
layout and widgets. The reasoning layer receives a bounded latest schema summary
so 'the devices widget' resolves to actual IDs and available operations.

Era's layout tool uses the same validated service as HUD controls; no arbitrary
JavaScript, module import, OS command or unrestricted iframe can be inserted as a
widget. Proactive widget suggestions do not modify the layout unless requested.
Rich workspace previews retain the existing isolated preview boundary.

## Verification

Schema validation, persistence, concurrent revision conflict, reconnect snapshot,
hide/reorder/create actions and tool/HUD shared ownership tests. Browser QA of the
real built UI with fixture events; every presented action checked against an
existing endpoint/tool. TypeScript/build and affected Python checks pass. Native
audio/app/call actions are not triggered by browser fixtures. Produce a visible
preview for review before reporting Phase 7 complete.

Reference studied: https://agentos.guide/system.html
