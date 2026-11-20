# Luminous Era One HUD

Implemented the user-supplied canvas-first plan, preserving the existing dashboard, widget layout, tool registry, voice engine and real telemetry sources.

## Behavior

- Dark teal surfaces, restrained violet/gold accents, persistent capability spokes and thin node orbits. Node identities come from `/api/capabilities`; the graph does not invent counts or relationships between tools.
- Cached halo sprites replace the per-frame radial gradient. Canvas DPR cap, adaptive 60/30/20 fps and hidden-tab rendering guard remain. Reduced motion freezes decorative motion but permits real signal updates. Diagnostics now includes measured canvas CPU time (moving average, not GPU/compositor time).
- Dashboard navigation opens telemetry, activity, memory, context, schedule, diagnostics and tools. Overview closes the detail drawer. Detail transitions use opacity/transform; expanded widgets no longer blur the canvas behind them.
- `visual_engine` accepts `{"action":"focus","view":"telemetry"}` and the other allowlisted views. The backend emits `hud.view.focus` over the existing authenticated WebSocket. This action does not persist scene/layout changes or execute a machine action. Existing model tool declarations describe the operation; the deterministic router recognizes phrases such as “Era, show me system telemetry”, “show active tasks”, and “open memory”. Compound or ambiguous requests are left to the existing router.
- Quiet synthesized navigation/completion/error cues require an enabled preference and a real browser gesture. Mute is saved per browser; existing server preference is used only when no local preference exists. Cues are rate-limited and suppressed during listening/transcription/speech, with active cues stopped on voice state changes. Sound controls are in the navigation and Settings.
- Launch screen waits for backend health readiness, WebSocket connection, session snapshot and capabilities. Failed requests retry; the user can open the dashboard while waiting. Readiness triggers a brief fade, without an artificial progress timer or minimum boot duration.

## Verification

- `npm run build` and `npx tsc --noEmit` passed; generated `ui/dist/index.html` updated.
- Backend suite: 97 tests passed across HUD focus, visual engine, HUD events, HUD server and tool-registry validation. An additional authenticated API-to-WebSocket focus test was then added; all 22 focus tests passed, including that integration test.
- `node tools/test_hud_navigation.cjs` passed: view destinations, invalid events, mute persistence, gesture gate, rate limiting, voice suppression and stopping active cues.
- Browser inspection at 1264×631 and 1600×900. Telemetry drawer displayed real backend readings; live `/api/visual` focus request returned HTTP 200 and opened System Telemetry.
- Blocking the health request kept launch status visible with an unavailable/retrying message. Removing the block dismissed it automatically.
- Reduced-motion browser emulation reported a byte-identical canvas over a one-second steady-state sample. Existing hidden-tab guard retained.
- No browser runtime errors observed. `git diff --check` passed.
- Standby sample: approximately 2.4 ms/frame of measured canvas CPU work at 20 fps. A DevTools trace was captured. This is not a controlled idle/busy before-and-after benchmark; shared-machine load and hardware rendering costs remain unmeasured.

Physical microphone recognition and audible cue loudness were not verified by a human listening test. Navigation was verified from parsed command through backend event transport and in the live UI. The running backend accepted the new focus action during final validation.

## Follow-up: animated core and editable layout

- The user explicitly requested motion: animations now start enabled unless the viewer has explicitly selected reduced animations. The top navigation retains the persistent animation toggle. Added counter-rotating orbital lights, clearer idle/standby rotation, panel entrance motion, hover movement, and a softly breathing state indicator. Canvas frame limits and the hidden-tab rendering guard remain.
- Agent state replaces the old central ERA ONE / PERSONAL INTELLIGENCE heading. The conversation is part of the core widget beneath the sphere, with teal agent text and warm neutral user text.
- Default System and System Awareness panels occupy the bottom-left and bottom-right corners. Awareness uses existing process, foreground, audio-device, file and network observations; unavailable or disconnected readings show N/A.
- Layout schema v3 makes saved geometry authoritative. Every panel, including core/weather/system/awareness, has a drag handle and a position/size editor. Grid snapping clamps horizontal bounds; the existing backend collision resolver moves overlapping panels downward. Larger arrangements scroll within the workspace. Layout revisions still prevent stale overwrites.
- v1/v2 layouts migrate to the new default dock geometry while retaining ordering, visibility and revision; migration is only persisted after a successful edit. v2 coordinates were previously ignored by the frontend. Existing v3 custom geometry is preserved.
- Validation: 90 backend tests passed (layout, focus and server), frontend presentation/navigation checks passed, and TypeScript passed. Browser-tested moving/resizing System, reloading to verify persistence, dragging it again, and restoring the requested corner. Default panel bounds fit 1600×900 and 1280×720. Actual canvas pixels changed over a one-second animation sample. No browser errors observed.
- Restarted the idle backend to activate the migration. The live HUD now reports schema v3 and accepts placement edits. A transient Windows file lock prevented one Vite clean-output pass; rebuilding with `npm run build -- --emptyOutDir false` succeeded.
