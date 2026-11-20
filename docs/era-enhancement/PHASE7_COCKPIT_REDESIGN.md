# EraOne cockpit redesign — 2026-10-14

The user rejected the equal-card dashboard and authorized a futuristic redesign
with movable widgets, inspired by the linked YouTube HUD at 5:28. The reference
was viewed in a separate browser: a central circular voice interface, dark panels,
cyan highlights and amber focus accents. This design adapts those visual ideas;
it does not copy branding or invent telemetry.

Use a twelve-column snapping canvas with a central neural core, asymmetric
panels, cyan and violet glass surfaces, warm amber details, and an uncluttered
conversation input. Arrange mode exposes pointer dragging, corner resizing,
keyboard-accessible order controls and hide/restore. Moving a panel makes room
by shifting collisions downward; panels cannot cover controls on other panels.
Below 900px use a stacked view with order controls rather than tiny draggable
windows. Respect reduced motion and the existing light theme.

Schema 2 adds bounded integer geometry and the neural-core widget. Migrate the
original nine-widget layout without changing saved visibility, order, revision
or conversation view; add the new core. Save with the existing revision conflict
check, atomic persistence and authenticated API. Keep one live transcript and
the existing input-only meter. Preserve actual calls, approvals, tasks and other
panel operations. No native calls or production task actions are needed for QA.

Validate schema migration, bad geometry, collisions, stale edits and restart
persistence; compile TypeScript, build the production HUD, then test actual
pointer dragging/resizing and responsive layouts in an isolated preview.

## Delivered and verified

Ten panels now include the existing neural core. Arrange exposes pointer drag,
corner resize and arrow-key resize; keyboard-accessible order controls remain.
Collision overflow rejects the entire edit without a partial write. An edit in
another client cancels an in-progress drag instead of committing stale geometry.
The original nine-widget order maps to cockpit slots on migration. Atomic saves,
owner-independent HUD authentication and concurrent-edit protection are preserved.

59 focused layout, HUD launcher, input ownership and transcript ownership tests
passed. Ruff, Python compilation, TypeScript and production Vite build passed.
Doctor: 0 errors, 0 warnings, READY. One upstream google.genai Python 3.14
deprecation warning remains in the test output; it is not a test failure.
Black was unavailable in this interpreter, so Ruff formatting was used and checked.
A compiler command issued from the repository root resolved the wrong tsc CLI;
running the installed TypeScript compiler from ui_src corrected the tooling error.

Isolated browser QA verified actual pointer drag (activity x=0 to x=3, y=2),
reload persistence, actual corner resize (w=3 to w=4, h=11 to h=12), hide/restore,
Focus mode, one transcript, and a 390px viewport without horizontal overflow.
Light and dark views were inspected; mobile input spacing and light-theme text
contrast were corrected. No production task, Telegram call, mic setting or Era
restart was triggered. Unknown core/call state in the preview is intentional.
Two independent browser clients also converged on hide/restore through real HUD
events. The first fixture omitted main.py's hud.attach broadcast wiring; after
adding that wiring to the isolated fixture, the peer showed nine then ten panels
without a reload. No browser console errors were observed.
Preview artifact: phase7-cockpit-preview.png.

Full Phase 7 is still partial: voice-driven layout customization, richer controls,
custom widget definitions and live owner acceptance remain. No broad global-run
claim is made for this visual increment. The previous full Phase 6 evidence remains.
