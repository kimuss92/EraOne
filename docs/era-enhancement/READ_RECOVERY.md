# Bounded read recovery — 2026-10-13

## Approved design

User authorized continuing Phase 6. The old self-healing router contains an unused
alternate-route helper suggesting global play/pause and browser Space toggles.
Activating it generically could reverse an action that partially completed, or
target the wrong app. This increment adds a narrow same-operation read retry;
it does not activate those toggle fallbacks or claim complete task-family recovery.

Allowlisted operations: file read/list/info/disk_usage/index_status/list_favorites,
and browser get_text/get_url/list_tabs/page_snapshot. Calls with a confirmation ID
or an approval flag stay on the existing single-execution path. All other operations,
including file mutations, media controls, messages, navigation, and searches with
their own retry policies, retain existing behavior.

One retry is eligible for a recognized transient failure within five seconds of
starting. The 250 ms asynchronous delay is inside that eligibility window. Five
seconds is **not** a new hard limit for executing a read; the second attempt retains
the tool's existing execution timeout. A long first read is not restarted.
Permission/authorization/confirmation/cancellation failures are not transient.
Typed exceptions and structured failure status are used rather than keywords
inside successful document content. Arguments are copied so a failing tool cannot
accidentally change the replayed request.

Each retry passes through the existing raw tool gate and carries cancellation
context. Cancellation during the delay prevents a second call. The existing verifier
runs on the final returned result and can still reject it; recovery never grants
verified status. Final exceptions propagate to the existing error and stack logger.

## Reporting and evidence

Recovery start/end events and task trace rows preserve attempt counts, strategy,
initial error type, task correlation ID where available, and observed outcome.
This records the symptom classification, not a proven underlying root cause.
No file contents or complete parameters are logged by this new layer.
Dictionary result data carries recovery evidence and final verification status;
non-dictionary payloads preserve their original shape and retain recovery details
in logs. HUD tool rows display that a read retried, preserve failure status, and
include detached recovery metadata. This is diagnostic reporting, not persistent
unverified claims that a workaround always fixes a future failure.

Initial focused run passed 182 tests; an import-order lint finding was corrected.
Expanded run passed 205 tests, including confirmation, cancellation, search,
responsiveness and HUD regressions. Final permission-timeout and HUD copy checks
passed in the focused 48-test run. Execution diagnostics passed 22 cases; verification
and the main tool bridge also passed (127 bridge cases). Doctor reports zero errors
and zero warnings. Manifest: 349 modules, 57 tools, zero unparseable. Updated speech
gate passed 657 tests in 42.95 seconds. The existing Google SDK pytest
deprecation warning remains separate from Doctor readiness.

All fault injection used stubs and disposable test state. No live retry was induced,
no user file was modified, no browser/media action was triggered, and Era was not
restarted. Ruff, compilation and whitespace checks passed.

## Remaining work

More task-specific recovery requires target identity and read-back evidence before
any alternate mutation route can run. Native USB/PnP coverage, moved-in unindexed
tree discovery, broader PC/file/browser/media task upgrades and live acceptance
remain open. This increment does not complete Phase 6 or start Phase 7.
Previously deferred glasses, search/cancel/follow-up, device-change and folder/HUD
checks remain pending; native Telegram calls still await the separate account.
