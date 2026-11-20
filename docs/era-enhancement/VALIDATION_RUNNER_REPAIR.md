# Validation runner repair — 2026-10-12

Status: fixed and verified. Enhancement sequencing remains in Phase 4.

## Cause and correction

The user's full run failed eleven gates, mostly in about 0.2 seconds, while the
Doctor independently reported READY. Direct reproduction of registry, logging
and YouTube volume checks raised ModuleNotFoundError for core/actions before
their assertions ran. The runner invoked these checks as script paths; that
puts tools/, rather than the repository root, on Python's import path.

Changed eleven commands to `python -m tools.test_<name>`, preserving their
custom __main__ test behavior and making repository imports available. Existing
pytest commands and Doctor invocation remain intact. No packages were installed
and no runtime audio, task, Telegram or application code was changed.

Also corrected four custom check scripts (registry, main bridge, execution and
verification) to exit nonzero when their assertion counters report failure.
Previously those scripts could print failures and still appear green to a
subprocess runner. No assertions were removed or weakened.

README now distinguishes a component Doctor READY result from the runner's
final GLOBAL_READY/ALL_CHECKS_OK and from manual hardware/live-channel acceptance.

## Verification

- Full user command `python tools/run_all_checks.py`: exit 0, **30/30 configured
  gates passed**, **357.8 seconds**, VISION_STACK_READY YES, GLOBAL_READY YES,
  ALL_CHECKS_OK and READY. All eleven previously failed gates passed.
- Full output retained in validation-runner-checks.txt. Existing Doctor warnings
  remain non-blocking; their component status does not hide failed suites.
- Four runner regressions passed (0.90 seconds): importable suite commands,
  passing Doctor cannot hide a failed gate, complete success reports readiness,
  and intentionally wrong ToolResult conversions produce failed execution checks
  and process exit 1 without an import exception.
- Eight manifest checks passed (16.43 seconds). Selected Ruff checks and
  git diff --check passed. Regenerated the manifest after all runner/test edits:
  334 modules, 57 tools, zero unparseable files; final --check passed.
- Initial direct reproductions failed as reported. A subsequent manifest check
  found stale hashes after a runner comment edit; regeneration corrected them.

This was the configured validation command, not the entire repository pytest
collection. It does not complete pending Phase 4 native captions/failover or
replace live Telegram and physical Bluetooth acceptance. No destructive action,
Era restart, preference rewrite or unsolicited message was performed.

## Lesson

Check process launch and imports before interpreting fast test failures as
component regressions. Preserve custom test entry points and propagate failed
assertions through exit codes. Read the final aggregate readiness status, since
an individual component can pass while the full gate remains blocked.
