# PF-040 revised candidate — one correction remains

Candidate: `docs/tasks/pf040-scoped.diff` handed off 2026-11-17 01:15Z. Reviewer: dev-review, read-only. Independent Windows Python 3.14 `-m pytest tools/test_busy_bounded.py -q`: **7 passed**, one dependency deprecation warning. `tools/protected_fixes.py check`: **REGISTRY OK**. Builder reports 854 focused tests, 456 protected guards and Ruff clean. No live one-minute/HUD test was run by this reviewer.

F1's busy behavior is fixed: `_processing_age_s` now returns infinity when the real manager's history read fails, and the new guard proves PROCESSING no longer stays busy. F2 is fixed: `listening_mode` logs a change from `window open` to `busy: processing` even though the mode remains active, without logging each countdown tick.

## Remaining protected-rule mismatch: history-read failure is silent

The new PF-040 rule in `docs/PROTECTED_FIXES.md` says an error in the busy check is **logged once** and returns no busy exemption. `_processing_age_s` catches a failing `get_state_history()` and returns `inf` without logging; `_busy_reason` therefore never sees that exception and its one-time error logger does not run. This is a relevant failure mode because unreadable history was the exact case found in the first review. Please log that error once (or have the outer handler do so) and add a guard asserting one log across repeated checks. Preserve the new fail-closed result.

Scope note: a 60-second window can remain `active` for up to 120 seconds after the last PROCESSING transition, by the candidate's deliberate busy grace. The handoff/TODO phrase `within ~1 min` should be reconciled with that behavior before live acceptance. Local turn and local speech exemptions remain unbounded; I did not establish a live hang there.

**Verdict:** behavior findings F1/F2 resolved, but PF-040 is **not accepted yet** because its protected error-logging rule is unmet.
