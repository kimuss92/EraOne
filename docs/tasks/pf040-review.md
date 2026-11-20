# PF-040 independent review — changes requested

Candidate: `docs/tasks/pf040-scoped.diff` handed off 2026-11-16 22:38Z. Reviewer: dev-review, read-only. Independent Windows Python 3.14 `-m pytest tools/test_busy_bounded.py -q`: **6 passed**, one dependency deprecation warning. Builder reports 853 focused tests, 455 protected guards, registry OK and Ruff clean. No live one-minute/HUD exercise by this reviewer. The log evidence shows audio passed past an expired window, but does not prove which exemption caused it; the builder records this limit accurately.

## F1: processing-history errors still fail open

`app/era_live/audio.py::_processing_age_s()` catches an exception from `get_state_history()` or a malformed history item and returns `0.0`. `_busy_reason()` then treats PROCESSING as fresh for 120 seconds on **every** check, so the busy exemption can remain open indefinitely. This contradicts the new PF-040 rule that an error returns `None` and the window decides. The new error test makes `session_manager` itself raise; it does not exercise a history-read failure. Please make history failure mean no processing exemption, and add a test with a real PROCESSING snapshot plus a raising `get_state_history()`.

## F2: the new log can miss the reason for the owner's exact symptom

`app/hud/status.py::listening_mode()` logs only when `mode != _last_mode["mode"]`. With a 60-second window, the first status can log `active, window open`; after expiry, a busy exemption can keep the mode `active` while the reason becomes `busy: processing` (or `busy: confirmation`). No new event is logged because the mode did not change. If it remains active as in the owner's report, `listening.mode` never names the reason. The test asserts only an eventual active-to-inactive change, so it misses this case. Please log reason changes as well as mode changes, with a bounded rate if seconds-remaining would otherwise produce noise; guard the open-to-busy transition while the mode stays active.

## Scope notes

The 120-second PROCESSING bound is a reasonable policy for a detached tool; a long owner call after that point may require saying Era's name again. `PendingConfirmationStore.peek()` expires its own pending item. Local turn and local speaking remain separate exemptions; this review did not prove that either is bounded under a hung local task or stale speaking flag. The one-minute live exercise and HUD transition remain owed.

**Verdict:** PF-040 is **not accepted yet**. The happy-path timeout is guarded, but the error fallback and diagnostic promise do not hold.
