# Visual/development release validation

Validated on Linux, Python 3.12.14, 2026-10-03. This ZIP retains the previous
stability and Autopilot fixes and rebuilds the production HUD.

| Check | Result |
| --- | --- |
| 35 focused runtime, routing, safety, self-edit, scheduler, voice, file and HUD test modules | **1,142 passed, 2 skipped; no thread-exception warnings** |
| New scene/development regression cases (included above) | **53 tests** |
| Authenticated REST/WebSocket and new workspace endpoint checks | **14 passed; 18 unrelated cases deselected** |
| Real development worker positive/negative end-to-end check | Passed, including Python apply/create, frontend typecheck/build, fixed regressions, diagnosis, gated activation and restart guard |
| TypeScript noEmit / Vite production build | Passed; included single-file production HUD rebuilt |
| Chromium 153 browser interaction checks | Passed; no page JavaScript errors |
| Repository Ruff check | Passed |
| Source manifest | 302 source modules, 56 declared tools, zero unparseable; freshness check passed |
| Archive integrity | Checked during packaging |

The focused run uses a 40-second per-test timeout and treats unhandled worker
thread exceptions as errors. Two skips are kept as skips. The test outputs are
in `validation/`. This is not a claim that the entire legacy suite is green.

The real worker check uses authored candidate code in disposable repository
copies, without a Claude request. It verifies an existing Python edit through
self_improve apply, new Python creation, meaningful candidate tests plus fixed
regression gates, syntax/lint, actual TypeScript/build commands, and real
self_diagnosis. It confirms that files remain absent until final approval,
rejects an intentionally failing test candidate, runs an approved feature,
and holds mixed engine/feature changes until a new engine session. The positive
candidate's test count is recorded in `development-end-to-end.txt`.

Run it on an installed development environment:

```powershell
.\.venv\Scripts\python.exe tools\verify_autonomous_development.py --frontend
```

The staged diagnosis honestly reports BROKEN/Doctor NOT READY in the credential-
free Linux copy: missing Gemini key, missing Claude executable, missing media
hosting/vault configuration, speaker enrollment and optional platform packages.
It reports zero syntax problems. Passing code tests do not conceal these
configuration limitations or prove full Windows readiness.

Browser verification uses the actual authenticated localhost backend and built
HUD with a temporary scene/task store. It edits a field live, renders bound
metrics/progress and charts, checks that final activation is disabled until
review acknowledgement, checks the explicit approval callback, creates a real
reminder and pauses it through the real scheduler. The deployment callback is
controlled in this UI check; actual staging/deployment is exercised separately
by the real worker test. Desktop and 390px-wide views were inspected. Screenshots
are included in `validation/`.

A broader integration attempt also recorded eight legacy Windows Explorer/path
assertion failures on Linux; its raw result (459 passed, 8 failed) is preserved
in `visual-integration-platform-failures.txt`. Another wide selection encountered
two unchanged Windows context-card/path assertions; the final focused selection
omits that platform-only module. Intentional tool-surface changes required
updating the pinned count and golden declarations for the three new tools;
round-trip, required-parameter and provider-dialect checks now pass. Existing
full-suite Windows/environment/legacy failures remain in `VALIDATION_RESULTS.md`
and `validation/broad-suite.txt`.

No real Gemini/Claude/Fish/ElevenLabs calls, paid-provider failover, speaker/audio
hardware or 24-hour soak test was performed here. New provider adapters and
always-on streaming STT in `VOICE_24_7_PROPOSAL.md` are proposed work, not features
silently enabled by this ZIP. Windows setup/startup scripts and live desktop
operations still require testing on the target PC. Model files, credentials and
voice enrollment are not included.

Source staging is not an operating-system sandbox. Generated tests/imports run
Python on the host. Approved synchronous feature code may continue after a wait
timeout; the dispatcher does not automatically replay it. Review these boundaries
in `VISUAL_ENGINE_ARCHITECTURE.md` before enabling autonomous development.
