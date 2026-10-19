# Cinematic HUD — 16 October 2026

## Authorized design and audit

Keep the supplied purple/navy main HUD and blue/purple boot screen, every saved
widget placement, the central eclipse, chat dock, and existing controls. The
user's Phase 3 brief supersedes the earlier orange CodeMode visual treatment.

Read AGENTS.md's shared log, Claude's 16 October handoff, and TODO #43 before
edits. Protected backend speech, approval, Spotify and audio-routing work stays
intact. Its live test remains owed; this presentation pass does not claim it.
Protected-file SHA-256 baselines and the original HUD bundle are backed up under
the path recorded in `runtime/cinematic-backup-path.txt`.

The current UI already has a rotating boot orbit, some staggered entrance CSS,
cached eclipse sprites, state-dependent traffic capped at 28 packets, model
menus, and a motion setting. Refine these instead of duplicating them.

## Implementation sequence

1. Keep readiness factual. Add staggered boot copy/check drawing, a rotating
   gradient segment, one slow CSS background, and a purple bloom/crossfade into
   the unchanged main layout. Start panel entrances at handover, not behind boot.
2. Add breathing and a traveling arc in the existing eclipse renderer, without
   moving constellation nodes. Reuse its packet layer. Pause canvas scheduling
   when hidden/offscreen and cap work; no additional perpetual JS loop.
3. Delegate pointer interaction to one listener group, with at most one queued
   animation frame per pointer update. Tilt only the hovered panel, at most two
   degrees; disable during arrangement, touch and reduced motion. Keep geometry
   and persisted widget positions unchanged.
4. Polish model menus, real voice/connection indicators, controls, task hover and
   pending-approval display. Add send ripple and truthful processing feedback,
   without introducing audio or speech paths. Retain all approval authority.
5. Keep mode transitions dark and purple. Retheme the existing CodeMode surface
   and compact orb to the existing space palette. Use at most one transient blur
   overlay during transitions; add no permanent panel blur layers.
6. Honor OS and HUD reduced motion, clean up frames/listeners, back up dist before
   every build, run affected UI checks and all six protected regression suites,
   inspect screenshots, measure canvas work, and add a new handoff/TODO entry.

No dependencies, WebGL, fake progress, fake waveform data, hidden capability
changes, media playback or device routing are introduced.

## Verification results

Implemented the sequence above. The main palette and saved geometry remain
unchanged; only the superseded orange CodeMode theme is rethemed to purple.
The existing canvas gains a 1.00–1.02 breathing radius and one traveling arc;
constellation placement uses its original radius so nodes do not drift. Packet
traffic remains capped at 28. Main canvas drawing pauses behind boot and all
canvas scheduling stops when hidden/offscreen. Idle/active drawing keeps the
existing 30/60 fps ceiling, adapts downward if CPU draw cost rises, and uses a
2 fps idle ceiling in reduced motion. No dependencies were added.

### Executed checks

| Check | Result / evidence |
|---|---|
| Six requested protected Python suites | **186 passed, 1 warning in 12.11 s**; `runtime/cinematic-protected-tests.log` |
| Node cinematic resource/motion, HUD navigation and presentation checks | All passed; includes OS/HUD preference, tilt bounds, frame budgets, offscreen/hidden stop and listener/frame cleanup |
| TypeScript | `npx tsc --noEmit` passed |
| Production build | Passed; 722,823 bytes / 300.37 kB gzip; original bundle 704,993 bytes |
| Installed Microsoft Edge, synthetic API/socket fixture | **40 browser checks passed**, no captured runtime errors; `runtime/cinematic-browser-results.json` |
| Protected source SHA-256 / prompt BOM | All **16 files unchanged**, UTF-8 BOM retained; `runtime/cinematic-protected-hashes.json` |
| Original vs final widget rectangles / main background | All **10 rectangles identical** within 0.1 px; background strings identical; `runtime/cinematic-performance-comparison.json` |
| Scoped whitespace | Passed |

Browser checks exercise truthful 3/4 readiness, paused entrances, boot handover,
drawn checks, delayed eclipse arrival, animated keyboard model selection, exact
command transport, bounded hover tilt, arrangement exclusion, near-core glow,
send ripple, processing state, Escape behavior, inert drawer exit, purple
CodeMode, zero hidden-main callbacks, draft/drawer restoration, and both OS and
persisted HUD reduced motion. The media-awareness labels remain distinct.

### Performance observation

Same installed Edge executable, 1920 × 1080, same synthetic layout/preset, four
seconds at idle after startup. Original and final each drew **120 frames** (30/s).
Original canvas callback CPU cost: **0.4 ms median / 0.6 ms p95**. Final:
**0.7 ms median / 1.1 ms p95**. Added motion costs about 0.3 ms per draw in this
sample; it remains well below a frame budget. Hidden main rendering scheduled
zero callbacks during CodeMode. This measures JS canvas drawing, not GPU usage
or whole-app CPU, and is not proof of sustained performance in the owner's
existing Edge app window.

Screenshots inspected: `runtime/cinematic-boot.png`, `runtime/cinematic-main.png`,
and `runtime/cinematic-codemode.png`. They show synthetic data, not live device
or media acceptance. No microphone, playback, device route, Claude generation,
approval action or engine restart was performed by browser validation.

### Repository-wide lint/format results

No Python source was edited in this pass. Read-only `ruff check .` found two
existing I001 import blocks in `tools/test_hud_focus.py` and
`tools/test_hud_layout.py`. Read-only `ruff format --check .` reports **124 files
would be reformatted**, 987 already formatted. Logs:
`runtime/cinematic-ruff-check.log`, `runtime/cinematic-ruff-format.log`.
These checks are not claimed green; concurrent/unrelated Python work was not
reformatted and no rule/test was weakened. The known audio reclaim and crew
wall-clock failures were not changed or bypassed; no full-suite pass is claimed.

### Activation and remaining acceptance

The production bundle is rebuilt. Its server reloads the bundle when its mtime
changes, so reload the existing HUD; this frontend pass needs no engine restart.
Every build was preceded by its own dist backup under the directory recorded in
`runtime/cinematic-backup-path.txt`. Claude's TODO #43 remains owed and unchanged.
TODO #44 records live startup, animation, hands-free controls and sustained
CPU/GPU acceptance in the real Edge app window.
