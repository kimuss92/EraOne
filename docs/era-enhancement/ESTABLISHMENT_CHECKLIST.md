# Era One: establishment phases and return checklist

Updated: 2026-10-18 (Africa/Lagos). Current checkpoint: **software stability
baseline loaded; owner feedback / remaining physical acceptance**.

Latest owner-feedback fix: [response delivery recovery](RESPONSE_RECOVERY_2026-10-18.md)
is loaded (PID 24028), with live owner speech/reply observed. 570 affected/protected
tests and eight manifest cases passed; repeated listening acceptance remains #47.
The full baseline below predates this follow-up and was not rerun for it.

Latest delivery: full Windows gate 7,971 passed / four skipped; 36 readiness
gates passed; final default-input guard followed by 334 affected/protected cases.
Era restarted and connected with saved audio choices; optional HUD tones off.
Read [STABILITY_RELEASE_2026-10-18.md](STABILITY_RELEASE_2026-10-18.md) for exact
scope, the post-full-run guard and live observation. The future checks below
remain instructions only where their latest status is still owed.

Use this page to choose the next work session and record what needs checking
again. It indexes the existing Master Plan, TODO board and evidence; it does not
renumber phases or mark earlier software work as unimplemented. Its initial
planning-only checkpoint is superseded by the stability delivery linked above.

## Standing decisions

- Keep the existing purple HUD and layout. Visual polish is paused; TODO #44
  retains the owner's animation/performance acceptance.
- Claude Code uses the signed-in subscription. No API billing fallback.
- Spotify uses the installed desktop app and Windows controls. No Spotify API
  setup. Favorite identity is now saved in existing long-term memory.
- Reuse long-term memory and the vault; no cloud backup or replacement store.
- Preserve the latest user-selected voice/media devices; inspect current state
  before a test rather than restoring an older snapshot's preference.
- Preserve pending-confirmation authority, exact reviewed actions, the killable
  Spotify UI worker, and existing natural-speech ownership through speak_once.

## What is established, and what is still owed

| Existing phase | Recorded position | Remaining work |
|---|---|---|
| 1: audit and baseline | Historical audit/baseline evidence exists | Establish a current release baseline after outstanding repairs; old full-suite counts are not today's results |
| 2: audio and routing | Software repairs and native probes recorded | Load Oct 18 COM repair; first-attempt listening, hotplug, route persistence and acoustic checks |
| 3: task and approval foundation | Core software verified | Successful exact-action execution, isolated cancellation, truthful outcomes and restart behavior |
| 4–5: speech, listening, standby and Telegram | Software verification recorded; physical checks deferred | Actual-room speech/interruption, provider recovery and owner-channel approval/call checks where still owed |
| 6: awareness and desktop tools | Software verification and bounded recovery recorded | Current live device/file/network observations, Spotify contexts, Netflix and task outcomes |
| 7: HUD | Existing design retained; partial acceptance | File open/reveal, real status updates and deferred owner-window animation/performance checks (#44) |
| 8: coordinated crew | Software verified | Live ownership, bounded Laura failure/fallback and staged source investigation |
| 9: subscription CodeMode | Implemented; real CLI auth/generation/stream/resume evidence | Full Era round trip, model selection, session continuity, exact approval and rejection |
| 10: guarded learning/self-improvement, Jev | Existing local mechanisms audited/software-checked; deadline repair verified | Optional Jev configuration/design/integration and real-use learning accuracy remain open; extend only proven gaps |
| 11: cleanup and release readiness | Current-machine regression baseline and formatting cleanup verified; snapshots retained | Fresh-machine setup and sustained/physical acceptance remain open; no destructive cleanup justified merely by static reachability |

Sources: [Master Plan](MASTER_PLAN.md), [latest operational audit](OPERATIONAL_RELIABILITY_2026-10-18.md),
[revision evidence](REVISION_2026-10-15.md), [CodeMode progress](PHASE9_PROGRESS.md),
[deferred acceptance](DEFERRED_ACCEPTANCE.md), and repository `TODO.md`.
Newer handoff entries and explicit user decisions supersede older dated reports.

## Next work session: finish Phase 9 acceptance and outstanding foundation checks

Do these blocks in order. A failed check gets a reproducer, evidence and a
targeted repair before its dependent block is accepted. Independent work can
continue; carry explicitly deferred checks forward with their limits visible.

| Check ID | Action / check | Pass evidence | If it fails, investigate | Current status |
|---|---|---|---|---|
| LOAD-1 | Capture source revision/diff, current process, voice/media choices and active work; restart Era through its normal lifecycle | New process/startup timestamp; intended source loaded; user choices retained; startup/health logs | Lifecycle/relaunch and initialization; distinguish restart failure from provider outage | Completed Oct 18 normal restart, PID 26640 connected, original standby and audio choices retained; final source fingerprint recorded. Repeat after a new source change, not merely because an older report says restart owed |
| LOAD-2 | Observe device awareness after restart, then a deliberate real device reconnect | Ready observer; no recurring apartment errors; changed endpoint reflected; clean reconnect | COM ownership, endpoint notification registration and reconciliation | Native isolated registration/read/cleanup passed; live/hotplug owed |
| AUTH-1 | Ask what's pending with no pending action, then arm one disposable rename and ask again | Both replies use scheduled_task(pending); armed reply names exact target; HUD/store agree | PendingConfirmationStore lookup and tool arguments | Empty/armed lookup observed; repeat only as part of the current case |
| AUTH-2 | Review and approve one disposable rename with correct source and new_name | One execution; actual destination exists, source absent; row clears; natural outcome reply | Path-intent routing, model arguments, exact approval binding and file result | Owed: previous attempt used name instead of new_name and failed |
| AUTH-3 | Arm a fresh disposable action and cancel before any approval; try its resolved control again | Original unchanged, destination absent; pending row clears; resolved control cannot execute | Consent ordering, cancellation and stale controls | Owed: earlier mixed approve/cancel transcript is not an isolated cancellation pass |
| AUDIO-1 | Normal glasses request, first request after >2 minutes quiet, quiet interruption, standby/wake and a request with music playing | User hears one natural answer; first request captured; interruption/wake works; no unintended self/lyric response in tested setup | Input/output ownership, capture and fallback; record actual room/device conditions | Hardware acceptance owed; do not add removed attention gates to obtain a pass |
| MEDIA-1 | Say play my favorite song after restart, then ask where it is playing; pause/resume/skip | Saved URI used; matching title/artist and active Spotify audio; live media session on remembered output; measured durations and audible confirmation | Personal reference resolution, bounded URI worker, metadata/audio observation and route enforcement | Fresh-process desktop favorite passed in 3,194ms on PC speakers; live Era voice round trip owed |
| MEDIA-2 | Separately request Liked Songs and New Releases | Named context Play control used; New Releases maps to Release Radar; requested context actually plays; no stale/offscreen result chosen | Existing primary/search UI worker and context readiness | Live acceptance owed; no API setup |
| CODE-1 | Use a coding-intent request; select an available Claude model by HUD and voice; return from CodeMode | Mode enters once, selected model observed, subscription session retained, normal voice/session survives exit; selection survives a new session | Intent detection, model persistence and mode/session ownership | Software/CLI evidence exists; complete Era round trip owed; use actual installed models |
| CODE-2 | Investigate one small source issue, inspect staged diff/test evidence, reject a candidate; separately review an approved disposable change | Correct source context; rejection changes no source; approval applies only reviewed content once; cancellation/expired approval cannot apply | Context freshness, verifier, approval hashes and promotion/recovery | Real CLI generation/resume passed; live review walkthrough owed |
| TASK-1 | Complete one small background task and separately force a controlled task failure; ask what's running | Accurate live queue; terminal success/failure shown and spoken once via existing outcome path; detail log available | Task state/continuation, HUD delivery and spoken_outcome | Implemented/software tested; current end-to-end delivery owed |
| MEMORY-1 | State a harmless preference, correct it, restart and ask a paraphrase; inspect recall; request forgetting and confirm through existing flow | Correct latest value across restart; no unrelated recall; confirmed item removed from relevant stores | Existing memory_context, long-term manager, journal and vault retrieval/deletion | Local favorite persistence/read-back passed; broader and multi-day recall owed (#30b) |
| VISION-1 | Use a small labeled image set and real screenshots with known answers, plus corrupt/empty input | Record expected/observed answers and latency per case; bad input contained; errors reported; actual screen capture works | Frame acquisition, validation/cache, provider connection and worker | Software checks and one known-image result exist; representative capture/accuracy/latency owed |
| RECOVER-1 | In a controlled test, interrupt the provider/network during a conversation and restore it | One clear degradation/recovery notification; permitted fallback answers; captured unsent input/context retained; no duplicate mutation or consent replay | Input buffer, reconnect and fallback ownership | Software fault cases exist; live recovery owed (#39); uncaptured offline speech cannot be recovered |
| CREW-1 | Ask Laura/Cody for a bounded source investigation; exercise a controlled timeout separately | Shared task ownership; bounded retry/fallback and honest failure; late result cannot execute tools or apply a patch | Crew shared deadline, cancellation, heartbeat and fallback chain | Software coverage exists; live acceptance and known budget failure remain |
| FILE-1 | Generate a disposable code file/document; Open File and Show in Folder; create/rename/delete a disposable usual-folder fixture | Correct editor/default app and exact folder; one meaningful watcher/HUD update; inaccessible roots remain explicit | Structured path handling, default opener and index/watch reconciliation | Software/native TEMP evidence exists; usual-folder/open/reveal acceptance owed |
| NETFLIX-1 | Request next episode in an existing user playback session | Correct next episode loads and remains playing; verified state agrees with actual playback | Episode navigation, explicit resume and player-state verification | Software tests exist; real transition owed |

Dispose only of the unchanged/known test fixtures created for these checks.
Telegram approve/cancel/calling tests stay in the deferred register and require
the intended owner-channel test context; this checklist sends no external message.

## Phase 10: guarded learning and reliable autonomy

Start the design from observed Phase 9 failures and existing mechanisms. This is
the next new delivery phase; do not rebuild memory, retries or coordination that
already exist. Review [Master Plan](MASTER_PLAN.md) and TODO #19–21 first.

1. Inventory fallback, feedback capture, memory retrieval and supervisor behavior
   against actual callers and evidence. Identify the smallest missing behavior.
2. Specify one bounded learning loop: observed outcome → proposed lesson →
   validated preference/context or reviewed change. Corrections retain provenance;
   observations do not grant new execution authority.
3. Define safe retry eligibility and timeout budgets. Mutation outcomes that are
   unknown require inspection, not automatic replay. Keep approvals and late-result
   suppression intact.
4. Evaluate Jev integration in a separate reviewed design before enabling it;
   availability, account access and current compatibility are not established by
   this roadmap. No new account, provider or dependency is assumed.
5. Verify representative correction, stale/conflicting lesson, timeout, denied
   approval, restart and rollback cases; then observe one bounded live workflow.

Completion evidence: supported autonomous behavior uses validated context;
unsafe/stale/denied changes cannot activate; failures produce a truthful outcome;
tests and live observations identify their exact scope. Previously deferred
hardware checks remain visible until separately accepted.

## Phase 11: release baseline and reviewed cleanup

1. Capture a named source/diff baseline and backups. Inventory live dependencies,
   references and dirty work before proposing file moves/removal. Obtain exact
   approval for destructive cleanup; no blanket reset/dependency upgrade.
2. Repair reproduced current failures without weakening protected contracts.
   Run the full current Windows regression/readiness checks in an appropriate
   maintenance window; report exact command, duration, failures and environment.
3. Verify documented setup/recovery with no exposed secrets; preserve subscription
   authentication, existing memory/vault and user device/model preferences.
4. Run a daily-use observation window. Proposed initial window: one 60-minute
   representative session plus next-day restart/recall. Record tasks attempted,
   errors, duplicate actions, reconnects, memory growth and latency samples.
   This is a planned sample, not a universal reliability guarantee.
5. Publish the release checklist, known limitations and recovery steps. Keep
   optional deferred checks separate from release-critical defects.

Ready for daily use means the agreed critical checks passed on the recorded
build, current regressions are reported accurately, and remaining limitations
are explicit. It does not mean literal zero faults or every command under 500ms.

## Known failures and paused work to return to

| Item | Last known position | Next action |
|---|---|---|
| Incorrect rename arguments | Live request supplied name instead of required new_name; action failed | Reproduce through exact reviewed disposable case AUTH-2; inspect routing/model arguments before proposing a fix |
| Audio reclaim test | Resolved Oct 18: fixture passed 15-byte ASCII to valid-int16-only output; valid PCM fixture retains all fallback/backoff assertions, full gate passed | Preserve malformed-PCM rejection; if it regresses, inspect new evidence rather than repeating the old fixture diagnosis |
| Crew wall-clock budget test | Resolved Oct 18: production rechecks shared deadline after model wait and reports StepLimit; full gate passed | Preserve deadline, cancellation and late-work suppression; retest only after relevant changes or a new failure |
| HUD animation / speed complaint | Separate Edge motion checks passed; owner's sustained experience still owed | Keep paused under #44; later compare owner-window motion setting and measured load with effects on/off |
| Full Windows suite | Oct 18 current baseline 7,971 passed / four skipped; follow-up default-input guard passed 334 affected/protected cases; exact source delta recorded | Use the recorded scope; repeat affected checks after changes, full gate for a new release baseline; never add overlapping counts as unique coverage |

## How to resume and record a result

At each session: read AGENTS.md, newest handoff and TODO first. Choose the first
unfinished applicable check above; inspect prior evidence before repeating it.
Check whether source changed or a restart is owed. Do not rerun a proven check
without a changed dependency, new failure or an explicitly owed live observation.

Record each result in the linked phase report/handoff with:

| Field | Required content |
|---|---|
| Identity | Date/time, check ID, source revision/diff or backup reference, running-process/startup identity |
| Conditions | Input, actual model/device/provider, foreground app/network and relevant preconditions |
| Outcome | Expected vs observed behavior, elapsed time, user audible/visual observation if needed |
| Evidence | Exact log/test command/result and artifact path; no credentials or whole private conversation dumps |
| Status | Implemented / software verified / live passed / failed / blocked / deferred; never treat these as interchangeable |
| Repair and retest | Reproducer, suspected vs confirmed cause, changed files, targeted/protected checks, next action and any restart owed |

TODO remains the work queue; handoff remains the chronological change log. This
page is their phase/check index. **Immediate next feedback: AUDIO-1 / TODO #46,
then AUTH-2, AUTH-3 and MEDIA-1. LOAD-1 is completed for this source.** Phase 10's
local mechanisms were audited; optional Jev remains unconfigured. Phase 11 has
current-machine verification and reviewed formatting cleanup; sustained/physical
and fresh-setup acceptance remain open. Newer user choices supersede snapshots.
