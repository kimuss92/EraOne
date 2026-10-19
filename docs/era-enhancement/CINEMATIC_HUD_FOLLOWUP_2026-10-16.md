# Cinematic HUD follow-up: motion disabled and avoidable wakeups

User report: no visible animation and slower HUD. Diagnosis reproduced against
the live localhost server in installed Edge: OS reduced motion is true; the HUD
shows Animations off and disables its own toggle. Windows MinAnimate is 0.

Plan before implementation:

1. Follow OS reduced motion by default, but honor an explicit HUD on/off choice.
   Keep the toggle available and explain that it overrides the system default.
   Make CSS and CodeMode use that same effective preference.
2. Pace the existing canvas loops at their actual drawing intervals instead of
   waking every display frame and immediately returning. Retain visibility,
   boot, observer and unmount cleanup. Preserve live voice/state rendering.
3. Back up dist before building. Test both OS/HUD combinations, actual computed
   CSS, preference persistence, canvas scheduling, existing presentation checks,
   protected suites and build. Compare browser wakeups and responsiveness.

No backend, device, media, approval, layout or palette changes. Do not change
Windows accessibility settings or restart the user's Era session.

## Result and evidence

- `lib/motion.ts`, `ViewNavigation.tsx`, `lib/codeTransition.ts`, `index.css`
  and `codemode.css` now use one effective motion preference. Windows is the
  default; an explicit HUD choice wins. The toggle is never disabled by Windows.
  Existing saved `full` choices work again. CSS no longer reduces animations to
  0.01 ms despite that explicit choice. Explicit off still disables decoration.
- `lib/canvasLoop.ts` schedules a timeout near the next required frame and then
  aligns drawing to rAF. `NeuralCore`, `CodeModeOrb` and the command waveform use
  this clock; timers and frames cancel on hiding, boot pause and unmount.
- The actual running localhost server served the rebuilt bundle; tested in a
  separate installed Edge session, without restarting the owner's app window or
  changing any Windows setting, provider, microphone or media routing.
- Same four-second live-server sample, OS reduced motion on: **480 → 73 total
  canvas callbacks**, while the main canvas drew eight times in each run. This
  cuts callbacks by about **85%**, not a claim of 85% whole-app CPU savings.
  Explicit full motion: **120 main draws / 4 seconds**, 198 total callbacks,
  0.5 ms median / 0.8 ms p95 canvas callback cost, no recorded long tasks.
  These are short browser observations, not sustained GPU measurements.
- OS remains reduced, HUD full: rotating loader computed duration **2.8 s**,
  status pulse **3 s**, Eclipse bitmap changes across 1.2 seconds. Both on and
  off persist across reloads. The original 33 interaction checks pass with the
  native OS reduced-motion setting still enabled; no runtime errors captured.
- **261 Python tests pass**: the required six protected suites (186), plus
  CodeMode workbench, HUD focus and HUD layout suites (75). Three Node checks,
  TypeScript and production build pass. New Node coverage checks default versus
  explicit motion choices and sleeping timer/frame cleanup, including late
  callbacks and boot pause. All 16 protected file hashes remain unchanged.
- Repository-wide Ruff still reports two preexisting I001 blocks and 124 files
  needing formatting; no Python source was edited. Manifest is current and
  scoped `git diff --check` is clean. Backup before build is recorded in
  `runtime/cinematic-followup-backup-path.txt`.

Evidence: `runtime/cinematic-followup-before.json`, `*-after-reduced.json`,
`*-after-full.json`, `*-browser-results.json`, `*-motion-proof.json` and
`*-protected-hashes.json`. Video capture was attempted but the recording encoder
fell behind; no completed video is claimed. The purple screenshot uses synthetic
data; the separate live-server sample uses actual backend state.

Activation: **Ctrl+R in the existing HUD**, then choose **Animations on** if it
still says off. A saved full preference restores automatically. Sustained
responsiveness in the owner's existing window remains for user feedback (#44);
the earlier #43 voice/media/approval acceptance is untouched.
