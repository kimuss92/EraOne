# CodeMode workbench — 16 October 2026

## Audit before implementation

The current implementation already owns coding input independently of Gemini,
uses the signed-in Claude subscription, persists model selection, supports voice
entry/exit, and stages verified candidates with exact approval tokens. These
contracts are retained. The 15 October revision records their verification.

The HUD currently adds orange labels and opens a Development settings drawer.
It does not yet provide a separate warm workbench, mode transition, compact
process eclipse, source explorer, or dedicated console.

## Design and implementation plan

Implement the attached, authorized design as a separate presentation surface:

1. Scope warm charcoal/amber tokens, monospace labels, subtle grid, cream text,
   and orange controls to the workbench. Preserve the main HUD's theme/layout.
2. Share one CodeMode snapshot among the header and workbench. Drive presentation
   from actual backend enabled state, including voice commands. Keep main HUD
   mounted and inert while coding, remembering focus without changing its drawer.
3. Use an 800 ms native CSS transition (300 ms departure, 200 ms wipe, 300 ms
   staggered arrival). No animation library is installed. Respect both OS reduced
   motion and the HUD motion setting. Cosmetic animation never gates commands.
4. Provide a 60/40 source/chat layout, persistent model/mic/exit toolbar, real
   context/source discovery, read-only highlighted source, candidate diff review,
   and collapsible verification console. Reuse Development's approval controls
   and the existing transcript/command transport. Source editing stays in the
   existing Claude candidate/verification/approval workflow.
5. Adapt the existing eclipse renderer to a 140 px process indicator with orange
   rings and structured circuit traces. Drive it with actual coding phase,
   running tools, voice levels and CPU telemetry; show unknown telemetry honestly.
6. Expose only bounded, redacted project source through an authenticated read-only
   endpoint, reusing the existing source scope and credential exclusions.

## Verification

Run TypeScript/build, source endpoint boundary tests, existing affected CodeMode
tests, and browser checks with an isolated fixture for entry/exit, reduced motion,
model switching, source selection, console/review tabs, narrow layouts, focus and
main-state restoration. Inspect screenshots. Record actual results below after
implementation. Do not repeat unrelated repair work or change provider billing.

## Implemented

- Separate warm workbench with a 60/40 source/assistant layout, responsive stacked
  layout, Editor/Terminal/Preview/Git tabs, collapsible console and prominent exit.
- Bounded, redacted source pages with refresh, hashes, line numbers and highlighting.
  Source is read-only; coding changes use the existing candidate workflow.
- Development's existing staging, verification and exact approval controls are
  embedded in Preview. Git displays candidate diffs; Terminal displays actual
  tool/check output. These surfaces do not invent a shell or repository status.
- One shared, serialized CodeMode snapshot poll, refreshed by existing CodeMode
  WebSocket events. A four-second timeout recovers hung status reads while keeping
  the last mode/context. Source reads abort on selection changes and time out.
- Native 800 ms departure/wipe/arrival animations. Both OS and HUD reduced-motion
  preferences bypass animation. Input dispatch is independent of animation.
- A compact eclipse reuses the existing renderer, with structured orange/green
  traces, real phase/tool/voice signals and a CPU-load arc. Unknown CPU stays unknown.
- Main HUD remains mounted and inert; entry does not change its drawer, layout,
  theme or draft. Exit restores its focus. Desktop window controls and the
  borderless drag region remain available in the workbench.

## Results

| Check | Result |
|---|---|
| Affected Python suites, authenticated HTTP source tests and manifest tests | **110 passed in 28.35 s**; `runtime/workbench-python-final.log` |
| TypeScript and production build | Passed; rebuilt `ui/dist/index.html`, 704.99 kB / 295.06 kB gzip |
| Scoped Ruff | Passed |
| Existing HUD navigation and presentation Node checks | Passed |
| Scripted browser interaction/fault checks | **28 passed**, no captured runtime errors; `runtime/workbench-browser-results.json` |
| Desktop host window-control presence | Minimize, maximize/restore and close present in Qt-host preview |
| Responsive screenshot inspection | 1440 × 1000 and 390 × 844; no page overflow at 390 px |
| Manifest generation | Current; 402 source modules, 59 tools, zero unparseable modules |

Browser checks cover model/draft preservation, input during entry, source display,
keyboard tabs, actual candidate/check output, exact approval tokens, mic transport,
one polling stream, identical main drawer/draft/focus after exit, warm input styling,
voice-origin mode events, reduced motion, stalled status recovery and failed source
read recovery. The final scripted fixture observed entry after 62 ms and accepted
input during the entry transition after 97 ms. These are isolated fixture timings,
not production model latency or live voice measurements.

Preview artifacts: `runtime/workbench-final-desktop.png` and
`runtime/workbench-mobile.png`. Browser fixtures use synthetic source/candidates;
they do not activate changes, call Claude, or operate the user's microphone/window.
Live spoken acceptance remains for the user. The preceding 36-gate stabilization
run is recorded separately; unrelated repairs were not repeated.

## Runtime activation

The production HUD bundle is rebuilt. An already-running Era engine needs one
restart to register the new `/api/codemode/source` route; reload its HUD afterward.
This revision did not restart the user's running engine or interrupt its voice
session. Subscription credentials, API billing policy and local memory/vault
behavior retain the user's choices.
