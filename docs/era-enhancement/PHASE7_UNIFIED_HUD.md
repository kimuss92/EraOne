# Unified single-screen HUD — 2026-10-14

User feedback supersedes the scrollable cockpit: one interface, a larger central
voice sphere, task-only activity, a separate system-event bar, time with weather,
hover expansion and temporary intelligence/result surfaces.

Keep the existing protected layout order and visibility; retain stored free-grid
geometry for compatibility. Render those widgets in viewport-relative docks.
Drag rearranges the surrounding docks; the core stays centered and visible.
Order-only edits do not keep pushing unused free-grid coordinates downward;
120 repeated reorder operations are tested without altering saved geometry.
No page or workspace scroll is needed in the overview. Panels expose longer
details on hover, keyboard focus or their Expand button. Detailed content can
scroll inside an expanded panel; it cannot make the page grow. Older conversation
view preferences no longer create a separate dashboard. The missing-layout
fallback remains available while an older backend starts.

Live Activity contains only real tool/task runs and existing crew progress,
including per-task cancellation. File/process/foreground changes are summarized
in the separate system-events strip. System values remain measured or unknown.
Time and weather share one panel. CoreStage's larger hero mode still uses the
existing output amplitude and spectrum; the command meter remains input-only.

Real context.added results drive a temporary result surface. Existing card
renderers preserve guarded file actions and escaped text; no raw HTML is executed.
Email uses the existing gmail_control result adapter, subject to the actual email
integration and permissions. No mailbox is queried by this change or its QA.
Email result detail is capped at 6,000 characters and rendered as escaped plain
text, without clickable sender-supplied local paths or executable markup.
Result surfaces pause expiry while hovered/focused, can be pinned or dismissed,
and fade after 30 seconds. Archived results stay in Context and can be restored.
Explicit user utterances such as "hide that" or "show the result" control only
presentation. Negations/questions/quoted commands and app/file operations are
excluded. These commands do not cancel tasks or delete email/data. New tool
results may show again; dismissal does not suppress future work.

Existing notification TTL, deduplication, approval handling and error persistence
remain. Toast appearance and ordinary dismissal have entry/exit animations;
reduced-motion viewers receive static transitions. Approvals remain independently
actionable and are never executed merely because a card appears or closes.

## Validation and limits

The installed TypeScript compiler and production build pass. Pure frontend helper
checks exercise hide/show intent, rejected negations/questions/file actions,
stale-result suppression and non-overlapping viewport docks. The initial email
failure test passed an unnormalized dict to an adapter whose contract takes
ToolResult; correcting the fixture to the actual contract fixed that test.
No execution-layer behavior was changed to make a test pass.
The final focused Python run passed 55 tests; Ruff and Python compilation passed.
Doctor reported 0 errors, 0 warnings, READY. Pytest still shows the upstream
google.genai Python 3.14 deprecation warning. The broader global runner was not
repeated for this HUD increment.

Isolated browser events verified task-only activity, a separate file/foreground
strip, actual result arrival, explicit hide/show user utterances, dismiss/restore,
hover expansion, saved dock reordering and one transcript. At 1366×768 the page
height equals the viewport height, workspace scroll height equals its client
height, and every panel is within the viewport. Pin/read-focus expiry guards
were tightened after a boundary-case pin observation; pinning is guarded before
queued timer callbacks can remove a card. Real local speech, live Gmail retrieval
and hardware audio synchronization still require owner acceptance after restart.
The 1600×1000 overview also has ten panels, exactly one transcript and matching
workspace scroll/client heights. Real HTML drag-and-drop was exercised between
memory and call docks, in addition to keyboard/button order controls. Routine
play/volume/app-open result cards stay in Context rather than covering the sphere.
Auto-expiry, notification removal and pin retention were checked in the preview;
new results do not replace a pinned card. Preview assets are
phase7-unified-hud-preview.png and phase7-unified-result-preview.png. Demo email
content is explicitly marked as a fixture and is not fetched from the owner's
mailbox. Native Era was neither restarted nor stopped by the preview.
The pinned fixture remained visible for 186 seconds and retained its original
result ID after another result arrived. Ordinary unpinned expiry was observed;
explicit user hide/show and notification expiry were verified separately.

The phone view prioritizes the sphere and expandable docks; full simultaneous
detail on every tiny panel is inherently limited by the screen size. Expand or
tap a panel to read its detail. Only the desktop overview promises every panel
and its summary at once. Errors remain visible until dismissed, and approvals
retain their separate actionable prompt.
