# Autopilot release validation

Validated on Linux / Python 3.12.14, 2026-10-03. The source is an update to the
previous EraOne-stable ZIP, with all earlier reliability fixes retained.

## Final results

| Check | Result |
| --- | --- |
| Combined routing, runtime, safety, self-edit, audio, file, HUD, and manifest checks | **1,054 passed, 2 skipped; no warnings** |
| New Autopilot regression cases, included above | **70 tests** |
| Ruff repository check | Passed |
| Manifest freshness | Passed |
| Source manifest | 295 source modules, 53 declared tools, zero unparseable modules |
| Routing/engine Python syntax | Passed |
| ZIP integrity and required contents | Checked during packaging |

The combined command ran 35 test modules, with a 40-second per-test timeout and
`PytestUnhandledThreadExceptionWarning` treated as an error. The output is
included. Two skipped tests are reported as skips, not successes.

Covered failure contracts include: ambiguous commands abstain; glasses volume
keeps its scope; local commands work without a Gemini session; protected files
remain blocked by the real execution gate; stored confirmations retain their
arguments; competing approvals cannot replace the current question; local
failures are not replayed; coding failures fall back only to analysis; Gemini
fallback tool execution is blocked; CLI timeout/cancellation kills the process;
and strict coding generation does not silently switch to Gemini.

The existing confirmation concurrency test previously let unexpected worker
exceptions escape and never added them to its error list. It now checks the
single-slot contract: one winner, nine busy refusals, and the winner still stored.
The manifest now excludes the known temporary `tools/_verifier_selftest.py`
fixture used by self-edit tests; that fixture cannot create source-map drift.

## Scope and remaining verification

Tests used fake desktop/CLI/provider transports, Qt offscreen mode, and staged
native libraries outside the delivered source. No real Claude process or Gemini
request was made by the new Autopilot tests. Claude availability is deliberately
disabled under pytest unless a test explicitly supplies its own fake transport.
No credentials, models, account quotas, or physical devices are supplied.

The full legacy suite was not rerun for this Autopilot release. The earlier broad
run and its Windows/environment/legacy assertion failures remain documented in
`VALIDATION_RESULTS.md` and `validation/broad-suite.txt`. The 1,054 passing checks
are a focused integration run, not a claim that the full project suite is green.

Frontend source did not change in this update. The production HUD built and
typechecked during the preceding stability release remains included, along with
its validation logs. Windows setup scripts still require execution on Windows.

Before live use on the target PC, verify:

1. Normal local system volume and explicitly scoped glasses volume.
2. Window selection and safe file listing/copy/move with exact paths.
3. Approval/cancellation and protected-path rejection.
4. Claude login, registry/profile configuration, available model, and CLI flags.
5. A broad repository investigation and a verified single-file change/rollback.
6. Coding timeout/abort while a separate volume command remains responsive.
7. Local voice models/enrollment if using the local/cascade input path.
8. Ordinary Gemini voice and network recovery.

Cloud voice audio still reaches Gemini before its transcript exists. See
`AUTOPILOT_ARCHITECTURE.md` for why that path is not intercepted a second time.

Start with `README_STABILITY.md`; use `tools/era_autopilot.py` to inspect decisions
without operating the desktop or contacting a provider.
