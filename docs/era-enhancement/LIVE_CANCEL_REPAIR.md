# Live Activity cancellation repair — 2026-10-13

## Evidence

The user's screenshots show duplicate Liked Songs jobs and a tiny per-card ×.
At 18:34 the log records “Cancel every task” followed by scheduled_task cancel
calls removing 12 persisted schedules. The local cancellation grammar did not
recognize that phrase, and the model routed it to the scheduler. This was a real
unintended schedule change, not merely a display problem.

## Implemented behavior

- “Cancel task” / “cancel the current task” cancel the newest Live Activity job.
- “Cancel every task” / “cancel all tasks” cancel all registered live jobs.
- With no live job, the generic task-cancel command is consumed and reports that
  nothing is running. It is not forwarded as a schedule-delete request.
- Explicitly named reminders and scheduled-task cancellation remain a separate
  flow. A final raw-tool guard rejects scheduler mutations during a recognized
  generic live-cancel intent, even if Gemini attempts the wrong tool. The prompt
  also states this scope. Quoted examples and negations are not cancel orders.
- One spoken acknowledgement handles all-task cancellation. Repeated partials
  from the same Gemini utterance do not repeatedly cancel later work or speak
  multiple acknowledgements. The flag resets for a new live utterance; independent
  local/backup voice commands are not latched out.
- Each running task card has a visible, full-width **Cancel task** button. It
  sends the exact card ID through the existing cancellation API. Expand/collapse
  remains a separate button; settled tasks have no cancellation control.
- An atomic registry claim prevents exact tool/argument duplicates while the
  owner task is active. The response identifies the existing live task and says
  no duplicate was started. Different actions remain separate. Fresh live UUIDs
  avoid using provider IDs as reused registry identities.

## Worker cancellation and limits

Spotify now receives its cancel token through offload. Its window/render waits
are cancellable; Liked Songs checks cancellation before playback and after native
UI work; UIA discovery and invoke/fallback steps check before continuing. Existing
PC boost cooperative cancellation is retained. Cancellation does not forcibly
interrupt a native Windows call already executing or undo an action already applied.
Canceled results remain discarded by the existing path. No global media toggle
fallback, scheduled-job cancellation, or running Era restart was performed here.

## Verification

Initial cancellation/regression run passed 132 tests; expanded coverage passed
172, then 183 including Spotify search and cancellation checkpoints. Import-order
and an unnecessary f-string lint finding were corrected. Updated speech gate
totals and final checks are recorded in PHASE6_PROGRESS.md. Pytest still emits the
existing Google SDK deprecation and pywinauto COM-flag warnings; Doctor readiness
is checked separately.

TypeScript and the production HUD build passed. An isolated browser fixture rendered
the actual ActivityPanel at 320 px width with two mocked jobs. Accessibility output
showed one named cancel button per task. Clicking both sent exactly
{"cid":"fixture-spotify"} and {"cid":"fixture-boost"} to /api/tasks/cancel;
no schedule endpoint was called. Backend requests were mocked and no live Era task
was canceled. Initial fixture dependency imports were corrected to Vite's normal
React imports; PowerShell click refs needed quotes. Both browser checks then passed.
Screenshot: TEMP/era-live-cancel-preview-20261013.png. The browser and temporary Vite
server were closed and the temporary HTML fixture was removed after verification.

## Prior schedules removed — restoration remains pending

The prior incorrect command removed these schedules:

1. water_casual
2. surprise_music
3. glasses_charge_warning
4. medication_reminder
5. morning_routine
6. CheckCeramAds
7. evening_movie_search
8. workflow_updates
9. era_feature_research
10. research_innovation
11. LogPerformanceIssues
12. MonthlyMemoryCleanup

The subsequent conversation recreated water_casual, glasses_charge_warning and
medication_reminder in the log. Those calls do not establish that their timing,
conditions or permissions match the originals. This repair does not silently
recreate missing schedules or invent their old settings. The original schedule
definitions need to be recovered/reviewed before restoration. That separate
recovery remains open and is reported to the user.

Live acceptance after restart remains pending: repeat a long job and verify only
one runs; cancel one card and verify the other remains; speak singular/all cancel
commands and verify schedules remain unchanged. Phase 6 remains partial.
