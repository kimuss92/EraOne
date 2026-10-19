# Phase 3 progress — 2026-10-12

Status: core foundation verified; advancing to Phase 4. The user authorized advancing after reporting
no idle beeps; remaining audio work stays tracked in PHASE2_PROGRESS.md.

## Implemented and verified locally

- StateStore copies all structured inputs and returns detached snapshots.
  Versioned condition notifications let consumers wait without polling; slow
  consumers receive the latest complete snapshot without an unbounded queue.
  Nested dictionaries, lists and dataclasses cannot mutate the store through
  external references. Older snapshots remain stable after later writes.
- Pending confirmations copy incoming parameters and outgoing previews,
  including the busy exception payload. Consuming a confirmation still clears
  its slot atomically. User-approval rules, expiry and cancellation are preserved.
- Approval lifecycle events reconcile expiry, cancellation and replacement with
  paused jobs. The shared store emits deadline events while idle; callback faults
  cannot change an approval decision. Subscribers receive detached data outside
  the approval lock.
- Internal executor checkpoints preserve completed steps, observations and the
  remaining plan. Real live/executor approval handlers publish the executed
  step result; success queues remaining steps under the same job ID. Failure
  stops continuation. A second held step creates a new approval. Checkpoints
  are private and do not enter serialized tool responses.
- Approval ownership survives the separate user-channel/thread context, and an
  early approval result is retained until the executor finishes pausing.
- Exact active task duplicates are rejected atomically under the existing queue
  lock. Both live and executor channels report the existing job ID. Queueing is
  described as queueing, not proof that execution has started.
- History retention now prunes only terminal records, preserving all pending,
  running and awaiting-approval jobs and their event histories.
- Cancellation keeps the duplicate guard until the executor actually returns.
  Terminal jobs may run again, and restored pending jobs remain protected.
- An idle worker now rechecks stop state after waking before launching work. A
  controlled concurrent-stop reproduction failed before this repair.

## Verification

- Original mutation reproductions: 4 failed, 1 passed, as expected.
- State/confirmation/HUD regression: 172 passed, 1 warning.
- Foundation and queue regression: 185 passed, 1 warning.
- Task outcomes/persistence/API/cancellation regression: 104 passed, 1 warning.
- Expanded duplicate tests, including both user-facing dispatch paths and a
  real worker cancellation race: 12 passed, 1 warning.
- Full suite: 6,552 passed, 2 failed, 4 skipped, 3 warnings in 1,166.99 s;
  `foundation-full-regression.txt`. Failures were crew transport test isolation
  and a log-size assertion during deferred Windows rotation. Both isolated
  cases passed before repair, confirming suite/environment sensitivity.
- Crew reset test now selects its local transport explicitly. Log rotation
  testing advances the documented retry clock and waits a bounded interval
  for filesystem availability, retaining its final size assertion. A separate
  injected sharing-violation test verifies backoff and preservation of writes.
  Both repaired modules passed all 91 tests.
- Retention reproduction failed before the fix. Expanded foundation tests then
  passed 293 tests; state notification/ownership tests passed 33.
- The earlier combined audio/foundation suite passed 622 tests. After the stop
  race repair, 50 lifecycle checks and 8 manifest checks passed.
- Approval lifecycle/continuation regressions passed 206 tests with one warning.
  All eight checkpoint tests passed, including both real confirmation handlers
  and a threaded worker with persisted sequence 1 through 6.
- Initial new continuation-fixture wiring used the imported store accessor
  instead of the executor's bound store; corrected fixture wiring and reran it.
- Ruff passed; manifest regenerated. Final combined regression result is in
  `foundation-final-regression.txt`. The full suite was not repeated after
  the final narrow retention/notification/test-isolation changes.
- Synthetic snapshot benchmark: 500 apps, 100 tabs, 20 sessions and 200 drift
  entries averaged 4.202 ms per snapshot over 200 iterations on this machine.
  This is a local benchmark, not a production latency guarantee.
- Warning observed: google.genai imports deprecated Python `_UnionGenericAlias`,
  scheduled for removal in Python 3.17. No dependency upgrade was attempted.

Two existing tests intentionally documented mutable snapshot aliasing. They
were updated to assert the new ownership contract; no tests were removed.

## Open scope and limits

- Whole-suite run: 6,574 passed, 4 failed, 4 skipped, 3 warnings in 896.43 s.
  Failures were manifest freshness and three doctor/registry/smoke gates. The
  registry extractor had inspected the new wrapper instead of its delegated
  handler; updated it without weakening the unwired-handler check. Source edits
  during the run also made the generated manifest stale; regenerated only after
  the final source/test edits.
- Repaired gates: 108 passed, 1 skipped, 2 warnings in 34.95 s. The skipped domain
  gate ran before its check counters were populated in that explicit file order;
  rerunning it after the domain tests passed all 10 tests. Latest approval,
  checkpoint, identity, cancellation and queue regression: 211 passed, 1 warning.
  The threaded continuation test passed separately. No known failing check
  remains; the complete suite was not repeated on the final tree.
- Two forced-ID-reuse regressions failed before hardening result ownership and
  timer record identity, then passed after the repair. A concurrent run exposed
  a 103 ms versus 100 ms wall-time assertion in an existing planning-timeout
  test. Replaced that fragile benchmark with proof that execution returns a
  timeout while the model remains blocked, with a one-second outer hang guard.
- Tests previously lacked a redirect for the process-wide task database.
  Added ERA_AGENT_TASK_DB and a subprocess bootstrap regression. Test processes
  now use a disposable database. A read-only inspection of the live database
  found no task records. Runtime defaults retain their existing location.
- Checkpoints are process-local. Restart preserves the existing explicit
  interruption/approval-lost outcome and never replays uncertain side effects.
  A consumed approval without a valid executor checkpoint cannot falsely finish
  a whole job; the missing continuation is reported explicitly.
- Deduplication covers exact goals in the shared agent queue, not paraphrases,
  separate processes, or all CM/crew schedulers.
- Threaded approval continuation and persisted sequence ordering are verified.
  Wiring state notifications to all watchers/HUD consumers and expanding task-
  family evidence/lessons remain in the later awareness and HUD phases.
- No destructive cleanup, production database migration, external message, or
  cloud-model call was performed for these changes. Tests use isolated fixtures.
- The pre-existing tool_dispatch.py background-grace change was preserved;
  changes in that file add the agent_task response and per-call approval-result
  binding while preserving the separate background-grace change.
