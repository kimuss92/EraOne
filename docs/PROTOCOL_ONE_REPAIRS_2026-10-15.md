# Protocol One repairs — 2026-10-15

Scope: the maintenance report dated 2026-10-14 20:32, plus failures exposed by full-suite verification.

## Repairs

| Report finding | Resolution |
| --- | --- |
| Stale manifest artifacts | Regenerated `docs/MANIFEST.md` and `runtime/manifest.json` from the current source. |
| Windowless spawn test | Media telemetry uses the shared `NO_WINDOW` constant. |
| Self-healing target refusal test | Resolve path scope before file-size validation; protected state and tests remain refused. |
| Engine renewal test timeout | Isolated the lifecycle test from real Telegram credentials and native call listeners. Offline call-coordinator tests continue to cover call cleanup. |
| One-step scheduled chains | Normalize an unconditional single step, or a chain containing only a tool call, into a single scheduled call. Existing validation and Protocol authority checks still apply. |
| Brave attachment failures | Ignore renderer processes and isolated headless automation when checking whether the user's browser profile is already running. Added a graceful maintenance restart script. Verified a real CDP connection. |
| Invalid development ID | Return a structured validation result with instructions to list valid jobs; retain strict ID validation. |
| Forgetting an absent memory | Report that the key is already absent without writing memory or treating a repeated deletion as an execution failure. |
| Search transport failure | Retry a transient fallback transport error once, respecting cancellation; permanent failures and repeated outages still report failure. Verified the live fallback returned a result. |
| Import-order findings | Applied Ruff's import-order fixes. |
| Four indexer SQL warnings | Replaced interpolated query fragments with fixed SQL and bound parameters. |
| Feature-loader `exec` warning | Reviewed intentional trusted plugin execution and documented it in Protocol One's accepted-risk table. Hardened activation and restoration to check the approved hash against the exact bytes executed. This remains trusted Python code running with Era's privileges, not a sandbox. |

## Netflix verification

Netflix's live French interface explicitly reported no results for `coffre forte` on the existing configured Marwa profile. The original no-match response therefore reflects the catalog response, rather than proof of a broken player.

Live verification also exposed an English-only native-search fallback. It now recognizes the French launcher and input, replaces the query using real keyboard events, and reports a keyboard failure accurately. The repaired fallback was verified against the live French interface without starting playback.

## Additional failures found during verification

- HUD layout persistence now retries transient Windows replacement locks with a bounded delay. Persistent failures preserve the saved data and revision, publish no successful update, and clean up temporary files.
- Updated the Anthropic tool snapshot to include the existing HUD navigation description. The reviewed snapshot diff changes only that description.
- Updated the memory diagnostic to assert the new idempotent deletion response.
- The diagnostic-script gate now inspects top-level AST definitions, so a nested async `check` helper is not mistaken for an ungated script.

## Validation

The first full run completed with 7,574 passed, 4 skipped and 4 failures; those four additional failures are addressed above. Focused follow-up checks passed (105 passed, 1 skipped). Earlier repair checks passed (270 tests), followed by 20 manifest and maintenance checks.

Expanded Netflix and maintenance checks passed: 220 tests. Era Doctor reports READY, zero errors and zero warnings. Manifest freshness was verified after the final source change.

The final full-suite run completed in 18:11: 7,581 passed, four skipped, five failed, and three warnings. All five failures were Netflix source-inspection checks whose process had loaded the module before the live-verification repair was written. Their tracebacks show truncated source fragments caused by the old line locations.

The exact five failures, the four original report failures, and the new native-search keyboard regression then passed together in a fresh process: 10 passed. The affected source-inspection groups also passed independently: 17 passed. The expanded Netflix run covers the one new test added after the full run's collection. Taken together, 7,587 of the current 7,591 collected cases have passing results, and four are skipped; no failing case remains unresolved. This is combined full-run and clean-rerun evidence, not a claim that the mixed-snapshot full run exited successfully.

Evidence:

- `runtime/protocol-one-repair-full-final.log`: full run.
- `runtime/protocol-one-repair-exact-rerun.log`: exact failed cases plus the original failures and new keyboard regression, all passing.
- `runtime/protocol-one-netflix-tests.log`: 220 passing expanded Netflix and maintenance checks.
- `runtime/protocol-one-source-checks.log`: 17 passing source-inspection checks.
- `runtime/protocol-one-repair-doctor.log`: READY, zero errors, zero warnings.

The full run's three warnings come from the Google SDK, pywinauto's COM initialization notice, and an existing pytest fixture deprecation. They did not fail checks.

The latest Protocol One lint/security checks report no actionable lint findings and no unresolved security findings. Seven security findings remain explicitly reviewed and documented, including intentional approved feature execution.

Runtime source changes take effect when Era next starts. Historical error log records are retained as evidence; they are not erased by these repairs.
