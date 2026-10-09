# Era One — Phase 0 board

**Phase 0 = STABILIZE** (this week). Phase 1 = EMPOWER (after).
Companion files: `handoff.md` (what was done and how it was verified),
`docs/MANIFEST.md` (where everything lives).

Rule for this board: an item moves to **Done** only when something *ran* and
proved it. "Written" is not "done".

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
---

## 🔴 In progress

| # | Item | Objective | Notes |
|---|------|-----------|-------|
| 39 | **Live** Gemini-independence test | 2, 6 | Mid-conversation, block Gemini (or pull the network) for 30 s: Era announces once, keeps answering in en/fr, runs one tool through the gate, returns to Gemini within ~60 s. `logs/era.log` should show `llm.omniroute.answered` while degraded, and `[TTS] say:` lines must contain no notes or paths |
| 40 | ✅ **Decided 2026-10-10: Fish = English only, current JARVIS voice; `fish_voices` left empty (floor kept for a future en/fr/ar clone)** · was: Pick Era's multilingual voice | 2 | Listen to `runtime/voice_samples/` (5 voices × en/fr/ar/Tunisian). Either set `"fish_voices": {"fr": id, "ar": id}` in `config/voice.json`, or record ~30 s each of en/fr/ar in one voice and clone it (`core/tts_fish.create_voice_model`) for one voice in every language |
| 37b | **Live** Fish-mode voice test | 2 | `config/voice.json` is `fish_audio`. Owed: a few spoken turns — first audio should feel ~1 s faster than before, and a Fish failure must fall back to Gemini audibly (never silent). Check `logs/era.log` `turn.latency.playback_ms` |
| 7b | **Live** proof of the failover | 9 | Unit tests use fakes. Needs the primary account actually driven to its limit — acceptance item 2; no unit test can claim it. |
| 17b | **Acceptance point 1 fired LIVE 2026-09-29 15:45** | 8 | `relaunch.requested source="self_improve"` is in `logs/era_lifecycle.jsonl`. The path is real. It exposed 4 defects, **all four now fixed and gated (24–27)**. Not closed until a live run logs `relaunch.performed` with `forced: false`. |
| 30b | **Live** proof of memory recall | 11 | Acceptance point 4 is *"tell her Monday, ask Friday after restarts"*. Saving, persistence and paraphrase recall each verify (9/12, 0/8 noise), but the acceptance test is a **user** doing it across days. Needs Ollama up for the semantic half; it degrades to lexical silently if not. |
| 31b | **Live** proof of the truthful self-model | 3, 4 | Acceptance point 3 is closed when Karim asks *"what can you actually do?"* and she answers from `capability_check`, **including declining something absent**. The mechanism removes the means to invent; it cannot force the model to call the tool. |
| 35b | **Windows `era_gate.py --all` owed for 34 + 35** | 10, 1 | This session ran on **Linux**, and Era is a Windows program. Targeted verification is green (53 in the changed file; 928 passed / 15 skipped across what this container can collect) and the gate's 8 reds were each reproduced on a **clean stash**, so none is mine — five `test_file_candidates` Windows-path cases, `test_local_fallback` (local models), a `test_protocol_one` frame, and `test_ads_dispatch::test_route_order_is_each_profile_direct_then_the_gateway`. `era_doctor` reads NOT READY here for 3 environmental errors only (no `.claude-freelance`, no OneDrive vault). What is left is ONE full Windows run: the per-test ceiling touches all 5,300 tests, and only that run shows none sits between 20s and 120s where 928 cannot see it. |

---

## ⬜ Next (ordered)

All four defects from the 2026-09-29 live test (24–27) are fixed and gated, and
**#15 (memory + RAG) and #16 (runtime grounding) are built and measured** —
evidence in `handoff.md`. **#17 (the Claude bridge, item 32), #17c and #18 are now
done too** — so the next unstarted item is **#19, the intelligent fallback protocol**.

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
