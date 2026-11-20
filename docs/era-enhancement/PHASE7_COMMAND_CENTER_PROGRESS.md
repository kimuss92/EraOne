# Command center first working view

2026-10-14. This records the initial view. The later user-requested visual redesign
supersedes its grid and drag-gesture limitations; see PHASE7_COCKPIT_REDESIGN.md.
Phase 6 software verification was reported before this implementation.
Phase 7 is started and partial, not complete.

Nine predefined widgets reuse existing functioning components: Live Activity,
Telegram calls, schedule, system metrics, system awareness, context, sources,
memory and weather. Conversation/captions/input remain one shared surface.
Approvals stay visible outside the customizable grid. Focus conversation restores
the earlier orb/rails view. New backend unavailability retains the earlier view.

Hide, restore/add, drag reorder and keyboard/button reorder use an authenticated
backend layout service. It atomically persists user-local settings, validates IDs
and visibility, rejects stale compare-and-swap revisions, emits real HUD events,
and supplies reconnect snapshots. Arbitrary code/widgets are not executable.
Independent clients do not overwrite one another blindly. The server-side widget
schema has a version and revision; captions and high-rate mic events are unchanged.

51 focused widget/HUD/input/runner tests passed; the final added layout snapshot
test and expanded catalog passed in the 11-case layout run. Persistence, concurrent
writer rejection, unsafe input and detached snapshots are covered. TypeScript and
production Vite builds passed. Scoped Ruff/Black and Python compilation passed.
Doctor: zero errors, zero warnings, READY. The first TS run caught the required
memory-search ref; passing the existing shared ref corrected it.

Agent-browser QA used a separate local fixture server and temporary layout file,
not the running Era instance. Hide calls → add/restore → reorder awareness →
reload retained changes. Focus conversation restored the original view, and the
Command center button restored the new view. No browser console errors observed.
The final artifact is phase7-command-center-preview.png. Fixture status is not a
claim that live agents or calls are connected; unknown evidence stays unknown.

## Remaining Phase 7 work

- Era-side layout tool and current-schema reasoning integration; layout changes
  currently come from HUD controls, not a spoken customization request.
- Richer agents/device/presence/media controls, widget aliases/titles/settings,
  safely supported custom widget definitions and proactive suggestions.
- Browser verification of drag gestures and multi-client reconnect under edits;
  button reorder and persistence were exercised already.
- Full current project runner after these Phase 7 changes; this increment used
  appropriate scoped gates and Doctor, with earlier Phase 6 full-run evidence.
- Owner live view/audio/HUD acceptance after restarting Era once.

No actual app/media action, phone call, approval execution or Era restart was
performed by this implementation's browser QA. Existing deferred hardware checks
and the original schedule-restoration issue remain open.
