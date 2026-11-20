# PC automation cancellation — 2026-10-13

## Scope and design

This increment continues the approved Phase 6 task-reliability work and the user's
Live Activity cancellation request. The graph query located the vendor control
module and dispatcher; the graph contains old backups, so current cancellation
contracts were checked directly before editing.

Three approaches were considered: hide/discard the result only, extend cooperative
worker checkpoints, or terminate vendor processes. Cooperative checkpoints fit
the existing registry and preserve vendor functionality. Result suppression alone
does not stop later actions; terminating MSI/PC Manager processes could disrupt
unrelated work. No process termination, service restart, registry-write enablement,
or new retry policy is introduced.

The dispatcher already offloads system_performance with its live cancellation
token. MSI switching previously used plain sleeps during UI discovery, focus,
verification and retries. Cancellation could therefore arrive but a later click
still execute. PC Manager had checks, but discovery needed to propagate the
cancellation exception rather than treating it as an ordinary accessibility error.

## Behavior

- Cancelled public system requests stop before their vendor action starts.
- MSI discovery, focus waits, state verification and retry waits check cancellation.
- Cancellation exceptions propagate through click/reacquisition error handling.
  Cursor restoration remains in the physical-click cleanup path.
- PC Manager discovery checks cancellation before scanning the next button or
  poking another WebView child. A cancelled failed Invoke cannot fall through to
  a physical fallback click.
- Existing MSI read-back verification remains authoritative. PC Boost still reports
  that cleanup was triggered, rather than claiming the vendor cleanup completed.

No token means normal callers retain their usual behavior. Cancellation does not
undo a completed click or stop vendor cleanup that has already been triggered.
An active native UIA call must return before Python can observe its cancellation.
The disabled MSI direct-write route remains disabled and outside this increment.

## Verification and limits

Initial focused cancellation/vendor regressions: 133 passed, with two existing
dependency warnings (Google SDK deprecation and pywinauto COM initialization).
Two additional full MSI-path cancellation cases were then added. The expanded
system-awareness gate passed all 299 tests in 18.16 seconds, including the new
vendor cancellation cases and existing MSI verification regressions.

Scoped Ruff, Python compilation and whitespace checks passed. An initial Ruff
finding for zip without strict=True was corrected. Doctor: zero errors, zero
warnings, READY. Fixtures simulate vendor windows, controls and scenario state;
no physical boost, power-mode change, native click or Era restart was performed.
The full global readiness suite was not rerun.

Live hardware acceptance remains pending. Phase 6 is partial: general device
coverage and additional task-specific reliability remain. Phase 7 has not started.
The 12 schedules removed before the live-cancel repair have not been restored.
