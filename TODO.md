# Era One — Phase 0 board

**Phase 0 = STABILIZE** (this week). Phase 1 = EMPOWER (after).
Companion files: `handoff.md` (what was done and how it was verified),
`docs/MANIFEST.md` (where everything lives).

Rule for this board: an item moves to **Done** only when something *ran* and
proved it. "Written" is not "done".

**⚠ ALL AGENTS: read `docs/PROTECTED_FIXES.md` + `AGENTS.md` §0 before any edit.** The gate re-runs every protected fix's guards.

**Latest (2026-11-15, Claude, tool truth — PF-022):** Era could not tell whether she had done a task. Root cause of "type instagram.com in Brave" failing: a never-expiring `last_media_app=netflix` flag made the router turn keypresses/typing into Netflix calls, and the model saw the reply under the wrong tool name. Fixed: the flag expires (15 min, real use only), a `ROUTING NOTE` names the tool that really ran, detached calls log an outcome, tool time is counted, and `protocol_one action=recent` reads back what happened (CONFIRMED / NOT CONFIRMED / FAILED). **Restart owed**; live proof: ask "did you do it?". **Next phase: #19 intelligent fallback protocol** (Fabric F4 and #22 latency, now measurable, are candidates). **Open:** Brave needs a relaunch with the debug port, 1008 idle-close churn (`GEMINI_SESSION_STABILITY.md`), shutdown zombie, Spotify mood-search verification. **Uncommitted.**

**Before that (2026-11-15, Claude, Protocol One):** the Protocol One report is now accurate (duplicate counts, real test causes, rollback/skip notes, full-suite scope, a changes-since-last-cycle section) and **Era can read it**: `protocol_one action=report [section=...]` (PF-020). A real cycle gave 9,025 tests, 1 failed (manifest freshness, regenerated, green). PF-008 (voice mute arms no listener) was found regressed with its guard rewritten to match, and restored. PF-021: a mood request is not a song title. **Restart owed**; live proof: ask "what did Protocol One find". **Open:** Spotify mood-search verification, `state_listener` not wired into `main.py`, 60-tool coverage audit (declaration vs dispatch vs verifier vs safety vs tests). **Uncommitted.**

**Before that (2026-11-12, Claude, protected fixes):** the registry is built (17 entries, enforced by the suite, the gate and a hook). The suite is fully green: **8,981 passed, 0 failed, 1 skipped**. The 1 skip is the deliberate paid-Lyria opt-in (17:15). A real cross-tool bug was fixed: `utils/media_match.py` capped every exact title at 0.86 (PF-017). The 6 stale tests now match the stricter media/attention behaviour, and 3 semantic-recall skips were removed (cold Ollama model). The recall probe's cold-model blind spot is now **fixed (PF-018)**: a cold model is warmed in the background and kept loaded for 30 min, and the gate passes. **Restart owed** for live proof (after more than 5 min idle, a paraphrase recall should log `[SEMANTIC] all-minilm loaded`). **Uncommitted.**

**Before that (2026-11-11, Claude, listening session):** ONE session model is loaded (see handoff). A rolling window opens on "Era" and stays open for **2 minutes by default**, re-anchored by every accepted turn (`core/session.py`, `config/session.json`); the window now applies with or without media. **Standby mode is OFF** (`standby_enabled: false`): "standby" only closes the window, keeping the mic and Live session warm. The UI mic button is now a mute that "activate mic" can undo; a mute asked for **by voice** has no local listener, so ambient noise cannot reopen the mic. `manage_preference` gains `set_session_window` / `get_session_window` ("set the session time to 5 minutes"). `CHUNK_SIZE` 1024→640 (40 ms). **Restart owed.** Live proof owed: say "Era, …" after 2+ min of silence and hear an immediate answer; say "set the session time to 3 minutes" and confirm `config/session.json`; mute with the button, say "activate mic" (works), then mute by voice and confirm a phrase does nothing. **NOT done:** a better ASR model (the ambient-noise mishearing is fixed by authority rules; a model upgrade is the next ears slice).

**Before that (2026-11-11, Claude, Sprint 1 slice 1):** evidence-gated completion is loaded (`core/completion_contract.py`). A success with no evidence-based verifier now travels to the model as **NOT CONFIRMED**, a background report says so out loud, and an approved action is verified under the **real** tool name instead of `confirm_pending_confirmation`. Unknown `file_controller` actions fail closed (MEDIUM + confirmation). 16 new tests; affected suites 238 passed; `era_gate --changed` **GATE PASSED, 2,959 passed**. Owed live proof: approve one disposable delete → `logs/era.log` must show a verifier verdict for `file_controller` (never `No verifier registered for 'confirm_pending_confirmation'`), and a side effect with no proof must be spoken with the qualifier. **New study:** [`docs/era-enhancement/GEMINI_SESSION_STABILITY.md`](docs/era-enhancement/GEMINI_SESSION_STABILITY.md) — the 1008 churn is 120 s client inactivity while muted/standby (246/255 closes), not context fill; five cheap wins ready, no Gemini code changed. **Still uncommitted** on `era/task-lessons`.

**Before that (2026-11-10, Claude, F3):** Era Fabric **F3 built**: declared fallback pathways (`config/fabric/pathways.yaml`, see handoff). A crew review of a staged change falls back to Claude via `claude_cli`, then to the verifier's own summary. Web search skips a Gemini that keeps failing. speak/voice_brain are declared and pinned, not re-routed. §12 decisions 1–3 are recorded as decided. 34 chaos tests (mutation-checked); full `pytest tools/`: 8,723 passed, 1 failed (manifest freshness, regenerated, now green). **Restart owed** (F2+F0+F1+F3). **Still uncommitted** on `era/task-lessons`, per Karim. Next: F4 (outbox/HUD/self_heal/lessons subscribe via the bus).

**Before that (2026-11-10, Claude, later):** Era Fabric **F1 built**: a health registry with circuit breakers (`core/fabric/health.py`). When the Hermes breaker is open, or Hermes refuses before the turn, the crew answers on the local loop with the same BotSpecs. OmniRoute is skipped when its breaker is open. `capability_check` says what is down, and the HUD Diagnostics drawer has a COMPONENT HEALTH panel (see handoff). 37 new tests, 1,146 neighbouring passed; full `pytest tools/`: **8,688 passed, 4 skipped, 0 failed** (16:10); ruff check clean. **Restart owed** (F2 + F0 + F1). §12 thresholds **confirmed** 2026-11-10 (3 / 2 min, 60 s, OmniRoute 120 s). **Waiting on Karim:** the self-review fallback (claude_cli or gemma, needed for F3), disclosure verbosity (F1 always discloses the crew fallback), and committing F2+F0+F1 (still uncommitted on `era/task-lessons`). Next: F3 pathways.

**Before that (2026-11-10, Claude):** Era Fabric **F0 built**: one fault taxonomy, the `boundary` bulkhead, a typed event log (see handoff). Log-only, no behaviour change; full gate 8,649 passed / 0 failed. **Restart owed.** Live proof: `logs/fabric/events-<today>.jsonl` gains a `tool.result` line per tool call. Next: F1 health + breakers.

**Before that (2026-11-09, Claude):** Era Fabric **F2 built**: crew asks and Development jobs carry a by-reference `TaskContext`, so a crew review gets the staged change instead of answering "missing" (see handoff). Full gate green after the manifest regen. **Restart owed.** Live proof: stage a change, say "have Cody review the change before I approve", then check `python scripts/task_log.py --ctx <id>`. Next per the design: F0 contracts, then F1 health.

**Before that (2026-11-05, Claude, later):** Era now learns from failed tasks (`core/task_lessons.py`, see handoff), and the floating icon returns on workspace close and snaps to the screen edge. **Restart owed.** Live proof: a failure followed by a working retry should add a lesson to `memory/task_lessons.json`, and the same failure next time should come back with "LEARNED FIX".

**Earlier (2026-11-05, Claude):** api-anything installed, Era reads websites through `era_workbench` capability `web_api` (see handoff). **Restart owed to load it;** then ask Era "what does Goodreads rate The Wild Robot?" as the first live proof.

**Before that (2026-10-18, Claude):** Phase 9–11 audit against the live log. Open-by-name
repair (bare `desktop` no longer glued to the filename; `%TEMP%` debris no longer
outranks real files) and `python tools/era_observe.py --since last-start` for the
Phase 11 observation window. Default-tier gate 8,073 passed / 4 skipped; Phase 10
suites 708 passed. **Restart owed to load these edits.** Re-ask Era to open a
report by bare filename as FILE-1 evidence.

**Checkpoint before that (2026-10-18):** follow-up to owner reporting complete silence:
response-delivery recovery and native interruption capture repair loaded; Era
is connected and answering again (PID 24028). 570 affected/protected tests plus
eight manifest cases passed. #47 repeated conversation/listening acceptance is
next; #46 speech/glasses and #43 separate approval/cancellation remain open.
The [establishment checklist](docs/era-enhancement/ESTABLISHMENT_CHECKLIST.md)
indexes pass criteria, failure follow-ups and rechecks across all phases.
Phase 10's existing local learning/recovery was audited; optional Jev remains
unconfigured. Phase 11 current-machine verification/formatting cleanup passed;
fresh setup, sustained use and physical acceptance remain open. #19–23 are
improvement themes to revisit against evidence; #44 visual polish stays paused.

---

## ✅ Done

| # | Item | Objective | Evidence |
|---|---|---|---|
| 1 | Baseline audit recorded | 1 | `era_doctor` → READY, 0 errors; full suite **5,105 passed, 1 skipped, 13m07s** (only failure was our own freshness guard) |
| 2 | Codebase index generator `tools/era_manifest.py` | 13 | 284 modules, 52 tools, 0 unparseable |
| 3 | Navigation map `docs/MANIFEST.md` + `runtime/manifest.json` | 13, 4 | generated, not hand-written |
| 4 | Anti-drift guard `tools/test_era_manifest.py` | 13, 3 | 7 passed in 13.5s |
| 5 | `handoff.md` restructured into a technical log | workflow | prior CM content kept verbatim |
| 6 | `TODO.md` created (this board) | workflow | — |
| 7 | Dual-account Claude failover for **all** paths | 9 | `core/llm/claude_accounts.py` + adapter loop; 24 new tests, 207 passed in regression, doctor still READY |
| 8 | Regression gate `tools/era_gate.py` | 10, 1 | 3 tiers: targeted (import-graph), fast, `--all`. Depth 2 selects 2 files where unbounded selects 99. 17 tests pass in 0.08s |
| 9 | **Bug fixed:** prompt claimed a tool Era cannot call | 3, 4 | Protocol Zero read 53 tools, model offered 52; `spotify_bridge` documented as real |
| 10 | Prompt synced — false capability removed | 3, 4 | `grep -c spotify_bridge core/prompt.txt` → **0**; drift check clean at 52 tools |
| 11 | **Bug fixed:** manifest lost `from pkg import submodule` edges | 13 | Two-pass resolve; this was silently under-reporting most of the repo's coupling |
| 12 | Full-suite baseline with Era stopped | 1 | 5,105 passed, 1 skipped, 13m07s |
| 13 | **Bug fixed:** self-edits could verify against zero tests | 8, 10 | `_find_test_file` was name-only, so `claude_accounts.py` ran no tests and still reported verified. Now import-graph, ordered by specificity |
| 14 | Gate proven end to end | 10 | **GATE PASSED in 101.7s** (23s for a narrow change) vs 13-minute suite |
| 15 | Full `--all` verification | 1, 10 | **5,122 passed, 1 skipped, 12m50s.** Every Era test passes; the only red is the pre-existing lint debt (item 14b) |
| 16 | Gate self-heals the manifest | 10 | Was `--check`, which failed on the one thing it could fix — cost 4 wasted cycles in one session. Now regenerates; the pytest assertion stays strict |
| 17 | **Voice → code → test → hot-reload** | 8 | **Acceptance point 1 built.** `reload` opt-in signal + `_arm_relaunch` / `_reload_after_self_edit`; 16 tests; GATE PASSED 195.8s. Live demo still owed |
| 18 | Lint debt cleared individually | 1 | `ruff check .` clean, `ruff format --check .` clean (683 files). One of my own warnings was wrong and is recorded as such |
| 19 | **Bug fixed:** one bad import could abort all 2,427 tests | 10, 1 | `importorskip` only catches ImportError; comtypes raises OSError. Pre-existing import-ORDER bug the gate exposed |
| 20 | Ruff scope corrected | 1 | It was reformatting Python inside vendored `.md` docs; excluded, and confirmed zero real `.py` hidden |
| 21 | COM apartment pinned in `conftest.py` | 10 | Root cause of #19. `sys.coinit_flags = 0` before any import, so collection order stops mattering and the 9 skipped tests RUN again |
| 22 | **Final full verification** | all | `era_gate.py --all` → **GATE PASSED 808.5s**; **5,139 passed, 1 skipped**; ruff clean; doctor READY |
| 24 | **Bug fixed:** the reload never exited, so it was always a force-kill | 8, 10 | `_shutdown_requested` is read only *between* Live sessions (`main.py:1556`), so the flag sat unread past the supervisor's 25s grace. The exit sequence existed as a **local** function inside `_shutdown_era`; extracted to `_exit_after_speaking`, now called by both. 8 new tests, the key one proved load-bearing against the pre-fix behaviour. **GATE PASSED 97.1s** (904 passed). Live signal to confirm: `relaunch.performed` with `forced: false` |
| 25 | **Bug fixed:** she claimed to be working when she had already been stopped | 3, 4 | The guard and the action layer were both honest; the **dispatcher** promised before it checked, because apply is backgrounded. Now resolves first (`_self_improve_refusal`), returns "NOT started… nothing is running", fails open. 14 tests, incl. one pinning that guard + dispatcher use the same resolver. **GATE PASSED 83.0s** |
| 26 | **Bug fixed:** "modify the HUD" resolved to `actions/open_app.py` | 8, 13 | **The board's diagnosis was wrong** — two causes: `app/` (43 modules, all of the HUD) was not a candidate at all, so "HUD" matched *nothing*; and a score of **1** counted as confident. Candidates now from the manifest + sub-package names; floor of 2 **measured** (every correct resolution scores 2+, the wrong one scored 1) and kept executable. 193→246 candidates. 24 tests. **GATE PASSED 83.7s** |
| 27 | **Bug fixed:** a self-edit could silently delete the module header | 1, 8 | 9 lines of 434 = 2% shrink, under `_SHRINK_LIMIT`'s 30%, no safety marker → `pass`. New verifier step 7 `header_loss()` → **rollback**. Narrow: removal only, `ast`-based, silent on a SyntaxError. 13 tests, one replaying the real 2026-09-29 edit. **GATE PASSED 32.0s** |
| 28 | `actions/open_app.py` restored after the live self-edit | 1, 8 | Approved by Karim. Header back (9 lines), dead `open_file_explorer_to_path()` gone from source AND manifest (0 hits). It duplicated `/api/reveal` while having none of its validators. **GATE PASSED 31.4s** |
| 29 | **Full verification of 24–27** | all | `era_gate.py --all` → **GATE PASSED 785.2s**; **5,198 passed, 1 skipped** (5,139 + exactly the 59 tests added, so none was skipped or renamed away); ruff clean; doctor READY. Safety-critical gate re-checked by hand after the item-26 floor: still HIGH/confirm for a description that resolves to one |
| 30 | **Semantic recall — acceptance point 4's missing half** | 11 | Lexical recall measured at **top-1 6/12** on paraphrases of real entries ("where do I live" → `live_ui_feed_plan`). New `memory/semantic_recall.py` (local `all-minilm` via Ollama, 46 MB, ~46 ms/query, incremental cache) combined as **max(lexical, semantic)** so no existing hit can be lost. **Now top-1 9/12, top-3 10/12, noise 0/8** — a 50% recall gain for identical noise. Cosine provably CANNOT separate answerable from unanswerable queries (0.290–0.743 vs 0.148–0.404, overlapping; z-score and gap-to-2nd too), so 0.36 is the lowest cut with zero noise — a budget, not a preference. 21 tests. **GATE PASSED 44.3s**, then **`--all` GATE PASSED 775.1s** with 5,220 collected (5,199 + exactly the 21 added) |
| 31 | **Runtime grounding — acceptance point 3** | 3, 4 | Protocol Zero fixed the *prompt*; nothing made her **refuse**. New `core/capability_index.py` + `capability_check` tool (53rd) + one prompt rule: the answer is **computed** from `load_declarations()` — the gated list the model is actually handed, which is why it isn't the raw literal (that once reported 53 where 52 were offered). Three verdicts; the middle one says "RELATED TOOLS FOUND — NOT confirmation", because `file_processor` really declares `document`/`translate` actions and she can neither print nor translate speech. Measured **13/13 can-do reached, 0/10 cannot-do confirmed**. Two bugs found by measuring: a name weighted like prose denied `flight_finder`, and pluralisation let one word count twice. 42 tests. **GATE PASSED 101.6s**, then **`--all` GATE PASSED 828.8s** — 5,261 passed, 1 skipped (5,220 + exactly the 42 added) |

| 32 | **Headless Claude transport for read-only asks** | 8 | Item 17, scoped by Karim after measurement: `claude -p` costs **~$0.64/message** (63.5K tokens of context reloaded each time; `--resume` cost MORE) and is invisible, so it is NOT a full replacement. New `core/claude_headless.py`: plain `ask` goes headless **read-only** (`--permission-mode plan` + a tool denylist, neither reachable from a tool argument); `reply`/`new_chat` stay in Karim's visible window. Every failure falls back to the window — never a dead end. **The gate caught that routing `ask` here made the TEST SUITE invoke the paid CLI**; `available()` now refuses under pytest (`ERA_CLAUDE_HEADLESS=0` to disable). Confirmation gates asserted unchanged. 32 tests. **GATE PASSED 24.4s**, then **`--all` GATE PASSED 1438.5s** — 5,293 passed, 1 skipped (5,262 + exactly the 32 added). Two odd things in that run were both chased and **neither was real**: the 23:48 suite time was contention (clean re-run **12:10**, faster than the 13:36 baseline) and the new thread warning did not recur when escalated to an error. See `handoff.md` |
| 33 | **Bug fixed:** one malformed event discarded the WHOLE report | 10, 8 | `write_details` read `ev["ts"]` directly, and `_note` funnels that KeyError into `note_failure`'s catch-all ("ignored") — so one event without a timestamp silently threw away the entire sustained-failure report, the one thing the module exists to deliver. Found by RUNNING the suite: `test_the_written_details_file_survives_giving_up` was **already red in this repo** (pure stdlib, not a platform artifact), so item 17c's recorded evidence was not green. New `_event_ts()`; `build_report` had the same two reads |
| 34 | **Item 17c complete — the report is no longer thrown away** | 10, 8 | The budget half was already done (`_unmark`); the drop was not. At `DEFER_MAX_S` the report text was **discarded** and the user told nothing but a stdout `print` — and that fires precisely when they have been at the keyboard for 30 minutes, so the reports lost were the ones raised while they worked. Waiting longer is not the fix (the wait holds `_inflight` and would block every other report), so it now stops WAITING without being lost: `_queue_pending` → `runtime/claude_bridge/pending/`, deduped by fingerprint, bounded (8 / 3 days / 5 attempts), the user is TOLD in a fixed phrase, and `flush_pending()` delivers one when idle — spending the budget only then. Tick wired into `_monitor_idle_state`, incl. **inside the standby wait** (the case that matters: standby means the user is provably away). 18 tests, **13 proven load-bearing** — reverting the give-up failed 11, deleting the call site failed the wire test, and the `write_details` test was already red; the other 5 bound NEW code (throttle, off switch, no-raise) and would only fail pre-fix with `AttributeError`. 53 passed in that file; ruff clean |
| 35 | **Item 18 complete — `pytest-timeout` installed and wired** | 10 | `timeout = 120` in `pytest.ini`, headroom over a MEASURED 19.0s slowest real test. The naive wiring would have broken the suite: `tools/test_zz_check_gate.py` shells out with its own `timeout=600` and alone can exceed the rest combined, so it carries `@pytest.mark.timeout(660)` rather than lifting the ceiling for all 5,300 tests to suit one. **A wrong decision of mine, corrected before it landed:** I first pinned `timeout_method = thread` believing `signal` does nothing on Windows — false, `pytest_timeout` already picks `signal`/`thread` per platform, and pinning `thread` would end the whole run on Linux instead of failing one test. Both semantics verified, not assumed (ini really 120.0; a marker really beats `--timeout=1`). Proven: `Failed: Timeout (>3.0s) from pytest-timeout` names the hung test |
| 36 | **Hermes session 1 — Phase 0 audit** (`docs/AUDIT_2026-10-09.md`) | 1, 2 | No code changes. Baseline **3 failed / 6,076 passed / 1 skipped, 15:48** (all 3 pre-existing; root causes in audit §5a). OmniRoute `era-gateway` → `claude-sonnet-5` verified live. Fish first audio ~0.34 s p50; the ~1.5 s extra in Fish mode is Era's sentence wait, not Fish. P0 order in audit §5b |
| 37 | **Audit P0s 1–4 fixed** | 1, 2, 6, 10 | Gate green (`.backups` excluded, live-repair leak stubbed + conftest guard, 44-file format drift fixed). Crew: step-budget warning + capability-free wrap-up, one Hermes chat retry. Fish: held fallback audio outlives Gemini's turn (silent-turn fix), one DNS retry, mojibake fixed. First audio: replayed median wait **1,455 → 586 ms**, prewarm idle 835 → 707 ms. Details in `handoff.md` |
| 38 | **Karim's 2026-10-09 decisions applied** | 2, 9 | Gemini is the first-run voice default (switch persists); `config/voice.json` = `fish_audio` for testing. `claude_cli` aliases ask OmniRoute `era-gateway` first, fall back to the subscription accounts; live-verified `era.reason` → `claude-sonnet-5` via member `cc` |
| 41 | **Session 3 / Phase 2a: voice cascade bounded + chaos-tested** | 2, 6 | Fish hang **45.0 → 3.0 s** (`stall_timeout_s`; 2 s false-tripped 1/32 live). Reported failures fall through <1 s (18 chaos tests on a loopback fake Fish). Piper-also-down → text only, one HUD line. `voice.fallback/unavailable/restored` events. Piper is the offline floor (Kokoro removed 2026-09-16). 517 voice tests pass; full suite owed with Era stopped |
| 42 | **Kokoro George on the GPU: fast local voice + `kokoro_audio` mode** | 2, 6 | CPU Kokoro ~1x realtime (dead end), CUDA ~10x: first audio **329-634 ms** through `SpeechOut`. Runtime + model in `D:\Kokoro` (outside Era's Python and Voicebox). Local chain Fish → Kokoro → Piper; "switch to kokoro" waits for the load, never lands on a cold voice. Live test 2026-10-10 found it on CPU (Era's Python has a CPU onnxruntime) + glued words (transcript chunks stripped): both fixed. Full suite **6,225 passed, 0 failed**; a test leak into the real voice.json fixed with a conftest guard. Owed: Era restart + second live listen |
| 35b | Current full Windows gate verified (October 18) | 10, 1 | `era_gate.py --all` passed: 7,971 passed / 4 skipped, root lint/format/manifest/Doctor pass, gate 976.9s. After a final default-input guard, 334 affected/protected cases and invariants passed again; full suite not repeated for that last guard. Evidence: `docs/era-enhancement/STABILITY_RELEASE_2026-10-18.md`. Earlier Linux-only verification debt superseded by this Windows run. |
---

## 🔴 In progress

**2026-11-16 · PF-034 + PF-008 built (Claude, in dev-review):** the window now
really expires: music pauses and scheduled reports no longer re-open it
(root cause of Era answering unnamed speech at 18:04 UTC). Any mute, incl. a
voice mute, can be undone by voice ("activate the microphone" added), typing or
the button. 398 guards pass. Owed live: idle 2+ min then speak without "Era"
(with music too) -> ignored; voice-mute -> "activate the microphone".

**2026-11-16 · PF-033 HUD inactivity indicator built (Codex):** amber
INACTIVITY MODE plus wake hint reads the engine window on connect/every 2 s.
7 mode tests, 392 protected Windows guards, TypeScript and Vite build pass;
isolated visual preview checked. Owed after normal Era reload: live idle timeout
shows the badge, Era/Era One clears it, standby/mic-off stays distinct.

**2026-11-16 · PF-032 built (Codex desktop, owner reassignment):** inactive
attention no longer passes continuous background audio indefinitely or fails
open without wake recognition. Existing session timer/config and manual
standby/mic logic preserved. 92 focused Windows tests and all 385 protected
guards pass; registry OK. Claude exact-candidate review queued as
`qitem-20261116174920-3a2e3c5caaa99776` after usage reset. Owed after normal
Era reload: quiet for configured 2 minutes; ignored room speech/noise; "Era"
and "Era One" wake; bare follow-ups; another expiry; standby and mic controls.
No live restart was performed. See newest handoff for recognizer-failure limits.

**2026-11-16 · PF-031 built (Claude):** "move X to screen one" no longer becomes
`primary` (= display 2); a minimized window already on the target is restored.
373 guards pass. Owed after Era restart: live "move Spotify to screen one" and
"move Brave to screen two" while minimized.

**2026-11-16 · PF-030 built and REVIEW-ACCEPTED (Claude; dev-review 15:15):** reads, Era's own switches, Spotify
playlists and window move/focus/minimize/maximize no longer end in "couldn't
confirm it took effect"; tests + 351 guards pass. Owed after Era restart: live
"new releases", "move Brave to screen one", "what's new about Protocol One".
Follow-up: Spotify volume, audio devices and Netflix fullscreen now read back
too (362 guards pass). Netflix open verified by tab address. Remaining: Spotify like (and it is a toggle), F11 fallback.

**2026-11-16 · OpenRig restart shortcut fixed (PF-029):** original conversations
and queued work restored; both agents running / startup ready. Existing
`C:\Users\nesri\.openrig-install\open-era-one-team.cmd` now starts the daemon
when needed and restores the saved team before opening panes. Never replaces
running/partial/unknown teams or falls back to fresh conversations. **17 launcher
tests, 324 protected guards passed**, registry OK; independent recheck accepted.
Real installed shortcut opened both panes; repeated invocation preserved agent
PIDs. Owed: normal-use proof after the next physical reboot; do not stop the
owner's live agents just to simulate it. See newest handoff.

**2026-11-15 · Five reliability repairs BUILT, awaiting review and live proof** (dev-build, PF-023..026, newest handoff):
- Brave CDP is healthy; no restart was done.
- Live checks owed after the owner restarts Era:
  1. "reboot Era One": `logs/era_lifecycle.jsonl` shows `forced: false`.
  2. "play some chill music": verified, not PlaybackNotVerified.
  3. Netflix failure, then Enter in Notepad: the key goes through, and it is still held on the Netflix tab.
  4. 24 h muted/standby soak: count `close_kind=1008-inactivity` against `keepalive_frames`.
  5. 50 forced drops: one replay, no duplicate action.
- Independent review is pending: `dev-review` is out of credits until 01:59.

**2026-11-15 · OpenRig five reliability repairs ASSIGNED:** builder
`dev-build@era-one` claimed `qitem-20261115204241-28ac231e8e8465b3`; reviewer
`dev-review@era-one` claimed `qitem-20261115204249-59aafe71ca62d973` and requested
exact candidate evidence. Scope: Gemini idle churn G0, Spotify mood playback,
bounded Windows relaunch shutdown, and Netflix failure Enter suppression in
other apps. **Brave debugging is already working** (127.0.0.1:9222, Brave PID
14352, version endpoint verified); recheck before restarting. Windows protected
baseline: **221 passed**. No repair claimed by desktop dispatch; implementation,
independent review, final Windows guards and live acceptance still owed. See
`docs/era-enhancement/OPENRIG_RELIABILITY_2026-11-15.md` and newest handoff.

**2026-11-15 · OpenRig mixed team LIVE:** Claude Pro login completed.
`dev-build@era-one` (Claude Code) and `dev-review@era-one` (Codex gpt-6-astra)
both responded and are running / startup ready / idle. Owner approved the team
roster once; both conversations are open in Windows Terminal. Refreshed the
expired local ChatGPT credential copy and restored missing WSL `python` for
existing safety hooks. **221 protected guards pass**, registry OK; existing
guidance and permission rules preserved. Reopen existing terminals with
`C:\Users\nesri\.openrig-install\open-era-one-team.cmd`; do not relaunch the
spec while running. Still unproven: a substantive assigned change plus exact
review, Windows app acceptance and reboot/long-running recovery. See handoff.


**2026-11-12 · HUD typed confirmation replies:** typed "yes"/"approve"/"confirm" (or "no"/"cancel") in the HUD text input now immediately consumes/cancels a pending high-risk action confirmation instead of being sent to the model. Code in `main.py:_handle_text_command` (lines 1244–1310). **Restart owed.** Live proof: trigger a high-risk action (e.g., delete a file), then type "yes" in HUD → executes immediately without model turn; type "no" → speaks "Action cancelled." **Also verify HUD Approve/Cancel buttons still work** (same confirmation store, same execution path) — Cancel now **speaks "Action cancelled."** so user hears it worked.

**2026-11-12 · Codex · Spotify non-API root causes repaired; desktop + HUD live proof passed:**
Saved references now override model-supplied `play_uri` IDs and recover omitted
title/artist from the matching saved URI. URI navigation now activates the
named track-page Play control once; unrelated old playback is never success.
Next/previous compare native metadata before/after the command. The registered
verifier now follows the synchronous dispatcher contract. **130 targeted tests
passed.** Live with Spotify Web API calls forbidden: saved track played, then a
single next changed tracks with `VERIFIED` / completion `proven`.
**Restart completed** via supervisor (13:56:45 Africa/Lagos, PID 31100);
real HUD "play my track" played سواد الغيم by Mikasa Music, then "Next track."
played Moulat Lkhala by Eljoee and replied "Track skipped." with `VERIFIED`.
**Still owed:** owner's microphone/ASR + audible acceptance; cold-start/slow-page
activation; previous-track live test; named search and fresh status voice tests.
Identical consecutive track metadata stays unconfirmed. Earlier media-search
debt remains open (see Nov 11 item below). Broad affected gate: **3,810 passed,
6 failed, 3 skipped**; five existing media/API contract failures plus an older
`test_open_gemini_input` fixture that forbids the intentional attention gate
(reproduces alone). Reconcile that fixture with listening-session requirements;
keep the wake/attention behavior. Details and validation snapshot in handoff.

**2026-11-11 · Claude · Listening session — live acceptance owed:** the 23:12
live test failed at step (f): "era standby" closed the session by entering the
standby STATE, so audio was dropped as "standby without wake word" and typed
input was blocked (`STANDBY_NO_WAKE`) — fixed, close is now `ONLINE_IDLE` and the
HUD leaves STANDBY (see handoff). **Restart again**, then prove in one sitting:
(a) stay silent 2+ min, say "Era, what time is it" → an immediate answer, no
"System online." greeting; (b) "set the session time to 3 minutes" →
`config/session.json` says 180 and the next session lasts that long; (c) press
the mic button off, say "activate mic" → it reopens; (d) say "mute your mic",
then say "activate mic" → nothing happens (the mic button or a typed order lifts
it); (e) with music playing, bare "next track" works inside the window and needs
"Era" after it; (f) "Era, standby" → HUD shows READY (not STANDBY) and the next
"Era, …" answers directly with no reconnect wait.
Gate: **4,808 passed, 5 failed**; all 5 are pre-existing reds in the prior Codex
media work (`youtube_video._handle_play` returns a dict where its test reads
`.success`; `spotify_api.execute` returns `PlaybackNotVerified` where its test
expects an accepted URI). Not this slice, but they block a fully green gate.
Not yet done: a better ASR model — the reported mishearing was ambient noise
triggering voice-unmute/standby, and that path is now closed; a model upgrade is
the next ears slice, to be chosen from measurement (Whisper second opinion vs a
larger local model vs the transport/brain split).

**2026-11-11 · Claude · Sprint 1 slice 1 — live acceptance owed:** evidence-gated
completion, verified approvals and fail-closed unknown actions are loaded (see
handoff). Restart Era, then: (a) approve one disposable delete and confirm
`logs/era.log` carries a `file_controller` verification verdict, not
`No verifier registered for 'confirm_pending_confirmation'`; (b) trigger one
side effect with no verifier (e.g. archive a mail) and confirm the spoken report
includes "couldn't confirm it took effect"; (c) ask for an action the model
invents on `file_controller` twice and confirm the second asks for approval.
Also owed: full `pytest tools/ --all` with Era stopped. Next slice: remove the
generic verifier for the eight tools that currently claim verified without
evidence, and fail closed on unknown actions for the remaining tool families.

**2026-11-11 · Claude · Gemini Live stability — study done, fix not started:**
[`docs/era-enhancement/GEMINI_SESSION_STABILITY.md`](docs/era-enhancement/GEMINI_SESSION_STABILITY.md).
Measured: 246 of 255 closes are `1008 client inactivity` at `conn_age_s`
119.8–120.0, every one preceded by muted/standby (Era sends no frames while
muted), not context fill. Ready to implement G0 (silence keepalive, 40 ms
chunks, 2 h resumption handle, fault naming, 3 s buffer-replay). Do not start
without the owner's go-ahead; no Gemini code changed yet.

**2026-11-11 · Codex · Media/HUD reliability remains open:** Contract/routing and failure honesty repairs implemented; affected tests passed (handoff has exact runs). Owner uses **NO-API Spotify desktop control**. Nonexistent-song live test passed: no replacement track. Positive Sabr El Rijal still fails native visible-tree matching and YouTube title selection; investigate these fresh failures, do not repeat blindly. Actual Drew audited engine; Cody/Nabar provider fallback was inconclusive. Owed: native exact-title selection + Windows metadata/playback proof, YouTube matching + progressing player proof, independent review, Era restart, voice/HUD rendering acceptance. No perfect-function claim.

**2026-11-04 · Hotkeys send the keys asked for, or say they could not:** `core/hotkeys.py` now handles every hotkey and key press; unknown names are refused instead of silently dropped. Prompt teaches when to use shortcuts (screen recording is `win+shift+r`; Game Bar is not installed). Off-list chords wait for a yes. Tests and gate pass. **Owed:** restart Era, then a live "record my screen". No real chord has been confirmed delivered. Listed shortcuts now skip the 5 s vision check (Karim's choice); lone keys, off-list chords and `press` keep it.

**2026-11-04 · Lock screen said "done" and never locked:** DONE and live-proven by Karim at 19:35 and 19:36: `Screen locked. Windows confirms the session is locked.`, 150 ms, `VERIFIED`.

**2026-11-04 · Guided Telegram setup + restart handling:** Implemented approved HUD wizard with encrypted draft/recovery, identity verification, enable/restart guidance. Existing Telegram calling confirmed working by owner. Direct restart supports common explicit phrases at user-speech completion, blocks late old-session greetings, handles scheduling failure honestly. 205 affected tests passed; final audio/setup/relaunch pass 102 (overlap); browser simulated full wizard, TypeScript/build/lint pass. Owed: restart to load changes, first-request live voice reboot, future real wizard login. Supersedes earlier pending Telegram recovery and reboot wording implementation entries; live acceptance remains.


**2026-11-04 · Telegram recovered:** Protected login readable, account authenticated, owner bound. Daily calling and incoming reception enabled with validated read-back. Supersedes earlier pending sign-in/binding entries. Owed: restart Era and verify real inbound/outbound audio; fix stale wizard completion text and deferred reboot acknowledgement.

**2026-11-04 · Telegram owner binding:** Era login recovered. Restored expected owner ID from existing TELEGRAM_CHAT_ID to protected account settings; reopened wizard after OWNER_UNVERIFIED. Await successful owner binding, then activation and live verification.

**2026-11-04 · Telegram recovery underway:** Unreadable encrypted account archived in its private folder; local interactive setup window opened. Await owner-entered phone numbers, application credentials and Telegram login code, then verify binding and restore calling.

**2026-11-04 · Reboot wording (user requested later):** Screenshot shows “Back online, sir. Ready for the next objective.” in response to “Era reboot era one” before reboot. Trace reboot acknowledgement versus reconnect greeting; say “Restarting Era One now” before restart and announce readiness only after reconnect. Fix and live verification still owed. Telegram recovery first. Existing setup wizard currently aborts on unreadable saved credentials; recovery must preserve the old payload before local sign-in.

**2026-11-04 · Telegram:** Saved calling account exists but Windows credential decryption fails. Corrected misleading setup message; 78 coordinator tests and UI build pass. Owed: recover protected login or local reauthentication, restart, then verify incoming/outgoing calls.

**2026-10-27 · Floating menu repair:** 48px icon; explicit launcher exit and
backend-offline cleanup no longer blocked by the orb's close-event veto. Hide
persists without an open workspace. All menu actions exercised in real Qt with
isolated backend responses; native Windows icon/menu and actual Chromium
open/focus/close passed. Targeted HUD tests 167 passed; final launcher/lifecycle
suites 86 passed (overlap). Lint/format/manifest passed. Overall gate remains
blocked by unreadable API credentials (`ProtectedConfigError`); credential files
untouched. Owed: full production Quit/Restart with connected engine once that
environment issue is resolved; physical tray click and multi-monitor/DPI check.
Removed the stale launcher; backend was already stopped and remains stopped.

**2026-10-22 · Launcher follow-up:** real Era logo now animates; tray/floating
clicks focus before launching. 25 tests passed and real double-click restored
the same minimized HUD window with no duplicate. Observe prolonged animation
resource use and multi-monitor/DPI placement during normal use; separate physical
tray-click acceptance remains owed. Backend stayed connected in standby.

**2026-10-22 · Workbench acceptance:** Brahma-inspired capabilities are implemented;
full gate 8,215 passed / 4 skipped, UI build and TypeScript passed. Settings,
phone-browser pairing/chat/revocation and Qt show/hide passed isolated live checks.
Live Era served the added routes and displayed the floating icon; standby was restored.
Final recheck found an intentional shutdown; preserved it. Icon/tray preferences
remain enabled for the next normal launch.
See `docs/era-enhancement/ERA_WORKBENCH_GUIDE.md`. Still owed: physical iPhone Safari
pairing over trusted LAN, actual Office layout/recalculation, Home Assistant devices,
voice-command end-to-end invocation and an owner-approved generated skill activation.
Circuit coverage is DHT11/Uno/ESP32 templates; native iOS phone/SMS/GPS, universal
circuit design, 3D globe, Discord/fitness/game adapters are not implemented parity.
Spotify MCP was excluded. Do not claim these acceptance items are done from unit tests.

| # | Item | Objective | Notes |
|---|------|-----------|-------|
| 48 | **Design proposed; implementation pending:** Chief of Staff reliability | Prefix / truth / recovery / lifecycle | October 18 source audit finds live audio bypassing optional attention, silent automatic reconnect continuation, and minimize gated on success text rather than sustained playback. Proposed boundary enforcement, fresh evidence, coordinated bounded recovery and verified candidates for safe relaunch: `docs/era-enhancement/CHIEF_OF_STAFF_DESIGN_2026-10-18.md`. Existing five suites: 191 passed / 2 warnings; no new behavior or live acceptance claimed. Resolve strict silence versus immediate reconnect notice and approve design before implementation. Owed: cross-channel prefix rejection, real interrupted conversation without replay, stable Spotify-before-minimize, Crew/Hermes/audio fault injection and staged relaunch/rollback with zero paid fallback. |
| 47 | **Repair loaded; live reply observed:** Era ignoring user | Speech / recovery | Separate delivery deadline survives wire activity/partial transcripts/empty turns; native server interruption releases half-duplex capture immediately. Unresponsive recovery starts fresh without action replay, preserving mute/standby and tools/CodeMode budgets. 570 affected/protected tests and 8 manifest cases pass. PID 24028 accepted owner speech and completed a reply with playback evidence; latest owner command "Stand by now" honored (final state standby/connected). Still owed: audible correctness and repeated normal/interrupted conversation, isolated typed check and real failure recovery while speaking. See `docs/era-enhancement/RESPONSE_RECOVERY_2026-10-18.md`. |
| 46 | **Software verified; owner feedback owed:** cut-off speech / Star Glasses | Audio / stability | Output-tail completion/renewal, bounded healthy capture reuse and default-input guard tested. Optional HUD tones switched off. Full Windows baseline 7,971 passed / 4 skipped; 36 readiness gates passed; final guard followed by 334 protected/affected cases. Native six leases used one stream; 619.4s live observation showed three real session rotations/reuses, one initial input open and no error-level events. Still owed: actual complete phrases, beep/reconnect behavior, real provider-renewal listening and hotplug. See `docs/era-enhancement/STABILITY_RELEASE_2026-10-18.md` and `docs/era-enhancement/SPEECH_GLASSES_TROUBLESHOOTING.md`. |
| 45 | **Loaded; live acceptance partial:** October 18 repairs | Audio / routing | Normal supervisor restart loaded COM observer, typed path guard, desktop favorite/all-output state and speech/capture repairs. PID 26640 connected; original standby, glasses voice and PC media preference verified; optional HUD sounds off. Short post-start watch had no error-level/output/awareness faults. Real hotplug, successful exact-action rename and audible favorite remain #43/#46. Latest owner device choices always win. See `docs/era-enhancement/STABILITY_RELEASE_2026-10-18.md`. |
| 44 | **Live** cinematic HUD acceptance in the existing Edge app window | HUD | **Follow-up to owner's no-animation/slow report:** Ctrl+R; choose Animations on if off. Windows reduced motion no longer locks the HUD toggle or overrides an explicit choice. Live-server Edge sample: idle callbacks 480→73/4s with motion off, full Eclipse 30fps; 33 interaction checks pass with native OS reduced still enabled. **October 18 recheck:** separate live Edge session confirms animated core pixels, full-motion reload persistence, all ten position/resize controls and requested corner panels; three Node checks pass. Still owed: visible boot/Eclipse/panel effects and sustained responsiveness in owner's actual window, physical voice navigation; quiet-cue listening only if the owner re-enables optional HUD sounds (off Oct 18 for beep investigation). Short samples do not prove whole-app CPU/GPU. See `docs/era-enhancement/CINEMATIC_HUD_FOLLOWUP_2026-10-16.md`. Claude's #43 remains separate and owed. |
| 43 | **Partially verified live** Claude's 2026-10-16 fixes | 3, 4 | Oct 16: owner heard combined pending/media reply; both tools observed. Oct 18: armed action named; row cleared after recorded voice approval, but rename failed on model arguments. Later cancellation/badge feedback does not prove a clean cancelled-action test. New voice-to-glasses request matches opened stream; media preference is PC speakers. Verified favorite Spotify URI now saved; fresh-process desktop-only playback changed to matching song with audio on remembered PC speakers in 3,194ms (earlier search 30 s). **Restart now completed; still owed as owner feedback:** favorite via live voice and audible correctness; successful exact-action execution and isolated cancellation; Liked Songs/Release Radar; hardware speech/echo acceptance. Criteria/evidence: `docs/era-enhancement/OPERATIONAL_RELIABILITY_2026-10-18.md` and `docs/era-enhancement/SPOTIFY_SPEED_2026-10-18.md`. Empty pending state must come from `scheduled_task(pending)` / PendingConfirmationStore. |
| 39 | **Live** Gemini-independence test | 2, 6 | Mid-conversation, block Gemini (or pull the network) for 30 s: Era announces once, keeps answering in en/fr, runs one tool through the gate, returns to Gemini within ~60 s. `logs/era.log` should show `llm.omniroute.answered` while degraded, and `[TTS] say:` lines must contain no notes or paths |
| 40 | ✅ **Decided 2026-10-10: Fish = English only, current JARVIS voice; `fish_voices` left empty (floor kept for a future en/fr/ar clone)** · was: Pick Era's multilingual voice | 2 | Listen to `runtime/voice_samples/` (5 voices × en/fr/ar/Tunisian). Either set `"fish_voices": {"fr": id, "ar": id}` in `config/voice.json`, or record ~30 s each of en/fr/ar in one voice and clone it (`core/tts_fish.create_voice_model`) for one voice in every language |
| 37b | **Deferred** Fish-mode listening acceptance | 2 | Current owner-selected mode is Gemini native audio; do not switch it merely to close this historical test. If the owner selects Fish later, listen to several turns and its audible fallback, recording `turn.latency.playback_ms`. October 10's Fish/English voice decision remains historical, not the current default. |
| 7b | **Live** proof of the failover | 9 | Unit tests use fakes. Needs the primary account actually driven to its limit — acceptance item 2; no unit test can claim it. |
| 17b | **Acceptance point 1 fired LIVE 2026-09-29 15:45** | 8 | `relaunch.requested source="self_improve"` is in `logs/era_lifecycle.jsonl`. The path is real. It exposed 4 defects, **all four now fixed and gated (24–27)**. Not closed until a live run logs `relaunch.performed` with `forced: false`. |
| 30b | **Live** proof of memory recall | 11 | Acceptance point 4 is *"tell her Monday, ask Friday after restarts"*. Saving, persistence and paraphrase recall each verify (9/12, 0/8 noise), but the acceptance test is a **user** doing it across days. Needs Ollama up for the semantic half; it degrades to lexical silently if not. |
| 31b | **Live** proof of the truthful self-model | 3, 4 | Acceptance point 3 is closed when Karim asks *"what can you actually do?"* and she answers from `capability_check`, **including declining something absent**. The mechanism removes the means to invent; it cannot force the model to call the tool. |


---

## ⬜ Next (ordered)

Historical foundation items 15–18 are built; the October 18 audit also found
existing local learning, bounded recovery and supervision. The themes below
remain improvement candidates for observed gaps, not instructions to rebuild
those systems. Read the current stability report and user feedback first.

Two of those four were misdiagnosed on this board until the logged tool
arguments were replayed against the running code — do that first, every time.

**All three of acceptance points 1, 3 and 4 now need the same thing: Karim,
out loud.** The mechanisms are built, gated and measured; what is left is a live
check no unit test can make (see "In progress").

| # | Item | Objective | Why this order |
|---|------|-----------|----------------|
| 19 | Intelligent fallback protocol (try workarounds before asking) | 6 | — |
| 20 | Continuous learning pipeline (web + RLHF + error logs) | 5, 7 | Feeds on 15 |
| 21 | Watchdog / self-maintenance routines | 10 | `era_supervisor.py` exists; audit before extending. Note it behaved correctly on 09-29 — the hang check is what caught 24 |
| 22 | Latency pass | 2 | Measure first — do not optimize on a guess. **First measurement already banked (2026-09-29):** the 15 slowest tests are all deliberate-timeout paths (19.0s, 15.9s, 4×13.6s…) — roughly **3 minutes of every run is sleeping through timeouts** that could be injected instead. Clean full suite is **12:10**. |
| 23 | Response-quality pass | 12 | Last: judged against a system that must already be correct |

*(A duplicated block of items 12–19 that repeated 15–23 was removed here on
2026-09-29 — same work listed twice under two numbers.)*

---

## 🟡 Open decision for Karim

`src/fast_local_vision.py` has **zero importers** — a working local-vision path
nothing can reach. Left in place deliberately rather than deleted as a side
effect of a lint pass. Rewire or retire is a product call.

**A second instance of the same pattern, found 2026-09-30:**
`core/background_manager.py` also has **zero importers** outside its own tests.
It reads like the obvious home for any long-lived loop — `start_all()` /
`stop_all()`, two Windows sync loops, a non-Windows fallback — and nothing ever
calls it, so `BackgroundManager` starts nothing. Item 34's flush tick was very
nearly registered there, which would have been dead code that passed its own
unit tests forever; it went into `_monitor_idle_state` instead, because that loop
actually runs. Note the trap for whoever picks this up: `tools/test_background_loops.py`
passes either way, so the tests are no evidence that the manager runs. Same call
as above — rewire (boot it from `main.py` and move the timer loops into it) or
retire. Until then, treat it as a decoy when looking for somewhere to put a loop.

---

## ✅ Acceptance test — CONFIRMED by Karim, 2026-09-29

These 5, and only these 5, decide whether Phase 0 is complete.

1. **Voice → code.** Say "Era, change X on the HUD." She edits her own source,
   tests it, reloads, and the change is visible. *(Obj 8)*
2. **Account failover.** Exhaust or sign out of the primary Claude account
   mid-task. Work continues on the second account with no interruption. *(Obj 9)*
3. **Truthful self-model.** Ask "what can you actually do?" — the answer comes
   from the generated manifest, and she declines to claim anything absent from
   it. *(Obj 3, 4)*
4. **Memory.** Tell her something on Monday, ask on Friday after restarts. She
   recalls it. *(Obj 11)*
5. **Health after change.** Any modification runs the regression gate; a
   regression is caught and reported, not shipped. *(Obj 10, 1)*

---

## Design note — acceptance point 1 (voice → code → hot-reload)

Settled 2026-09-29 by reading the code, so the next session does not re-derive it.

**What already exists.** `actions/self_improve.py` is the only sanctioned
self-edit path and already does: backup → Claude-generated text (tool-free CLI)
→ `py_compile` + ruff → covering tests (fixed in item 13) → deterministic ruff
repair → one model repair → auto-rollback → independent second-pass verifier.
The one missing step is the **reload**.

**Where the reload lives — and where it must NOT.**
`actions/self_improve.py` rule 5 says nothing there ever executes a repo file,
and rule 6 says it reports "restart needed" rather than restarting. Those rules
are load-bearing: a second Era instance means a Telegram 409 collision.

So the action layer must NOT call the relaunch. The live-session layer already
owns that sequence — `app/era_live/tool_dispatch.py:1199-1215`, the
`shutdown_era(relaunch=True)` path:

    era_relaunch.request_relaunch(reason=..., source=...)   # writes the flag
    if not supervised and not era_relaunch.spawn_supervisor(os.getpid()): warn
    self._shutdown_requested = True                          # loop then exits

`request_relaunch()` only writes `~/.era/era.relaunch`; the caller exits and
`era_supervisor.py` brings Era back in about a second, skipping its crash delay
and not counting the exit as a crash.

**Plan.**
1. `self_improve(..., reload=False)` — opt-in, default OFF. On a *verified*
   success it returns a structured "reload requested" signal; it never exits.
2. `tool_dispatch.py` reads that signal and runs the existing relaunch
   sequence — extract lines 1199-1215 into one helper so there is a single
   copy of it, not two.
3. Add the `reload` parameter to `app/tool_declarations.py` (and remember
   `RUNTIME_GATED_TOOLS` is the place for any gating).

**Why opt-in and default OFF.** Protocol One calls `_apply()` from inside a
~12-minute automated maintenance cycle, and the scheduler can too. An
auto-relaunch on *every* self-edit would restart Era mid-cycle. The voice path
passes `reload=true`; automation does not.

**The safety check that matters most.** `is_supervised()` must be consulted,
and `spawn_supervisor()` used when it is false. Exiting with nothing watching
is not a hot-reload, it is an outage — that single branch is the difference.
