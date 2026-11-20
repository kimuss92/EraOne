# PF-040 independent acceptance — code candidate

Candidate: `docs/tasks/pf040-scoped.diff` handed off 2026-11-17 01:18Z. Reviewer: dev-review, read-only. **Accepted at code and Windows focused-test scope**; no live one-minute/HUD behavior claimed.

The three review findings are resolved in the inspected candidate. A PROCESSING state is busy for at most 120 seconds after its last transition, and an unreadable history returns no exemption. That history failure now prints one diagnostic per process across repeated checks. `listening_mode` logs when either mode or reason kind changes, so `window open` becoming `busy: processing` is visible even while the mode remains active; countdown ticks are suppressed. The handoff, TODO and PF-040 entry now state the busy grace can keep a one-minute window active for up to two minutes after the last tool call.

Independent Windows Python 3.14 checks: `-m pytest tools/test_busy_bounded.py -q` **8 passed**, one dependency deprecation warning; `tools/protected_fixes.py check` **REGISTRY OK**. Builder reports 74 attention/HUD focused tests, 457 protected guards and Ruff clean; I did not rerun those wider checks. I did not exercise Era live, a one-minute idle interval, a long owner tool, or the HUD badge. Local turn and local speaking remain separate exemptions and were not proven bounded by this review.

Please integrate the scoped patch with its guard and perform the owed live one-minute inactivity/HUD check. If Era remains active, inspect `listening.mode` for the recorded busy reason.
