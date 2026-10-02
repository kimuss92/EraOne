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
| 2 | Codebase index generator `tools/era_manifest.py` | 13 | 284 modules, 52 tools, 0 unparseable **when built (2026-09-29)**; regenerated 2026-09-30 → **290 modules, 53 tools, 0 unparseable**. The generator is the source of truth, so this row records what it said then and what it says now — the count is expected to move as the repo grows, and a row frozen at the first number is how a "generated, not hand-written" map starts being read as stale |
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
---

## 🔴 In progress

| # | Item | Objective | Notes |
|---|------|-----------|-------|
| 7b | **Live** proof of the failover | 9 | Unit tests use fakes. Needs the primary account actually driven to its limit — acceptance item 2; no unit test can claim it. |
| 17b | **Acceptance point 1 fired LIVE 2026-09-29 15:45** | 8 | `relaunch.requested source="self_improve"` is in `logs/era_lifecycle.jsonl`. The path is real. It exposed 4 defects, **all four now fixed and gated (24–27)**. Not closed until a live run logs `relaunch.performed` with `forced: false`. |
| 30b | **Live** proof of memory recall | 11 | Acceptance point 4 is *"tell her Monday, ask Friday after restarts"*. Saving, persistence and paraphrase recall each verify (9/12, 0/8 noise), but the acceptance test is a **user** doing it across days. Needs Ollama up for the semantic half; it degrades to lexical silently if not. |
| 31b | **Live** proof of the truthful self-model | 3, 4 | Acceptance point 3 is closed when Karim asks *"what can you actually do?"* and she answers from `capability_check`, **including declining something absent**. The mechanism removes the means to invent; it cannot force the model to call the tool. |
| 35b | **Windows `era_gate.py --all` owed for 34 + 35** | 10, 1 | This session ran on **Linux**, and Era is a Windows program. Targeted verification is green (53 in the changed file; 928 passed / 15 skipped across what this container can collect) and the gate's 8 reds were each reproduced on a **clean stash**, so none is mine — five `test_file_candidates` Windows-path cases, `test_local_fallback` (local models), a `test_protocol_one` frame, and `test_ads_dispatch::test_route_order_is_each_profile_direct_then_the_gateway`. `era_doctor` reads NOT READY here for 3 environmental errors only (no `.claude-freelance`, no OneDrive vault). What is left is ONE full Windows run: the per-test ceiling touches all 5,300 tests, and only that run shows none sits between 20s and 120s where 928 cannot see it. |
| 36 | ~~Recall measurement drifts against the live store~~ — **CLOSED 2026-10-01** | 11, 1 | Was RED because the floors were asserted against the LIVE gitignored store, which grows every time Era learns something. Fixed by item 40's frozen 50-entry synthetic corpus, and closed by a measured run on Windows with Ollama/all-minilm at cut 0.36: **top-1 8/12, top-3 11/12, noise 0/8**, now baked as `FIXTURE_TOP1`/`FIXTURE_TOP3`. Run 1 (7/12, 10/12, **noise 1/8**) was deliberately NOT banked — a floor from a run that also reported noise is measured under a condition the noise test forbids. Words-only on the same corpus is 5/12, so semantic recall is worth **+3 on top-1 — the same gain the real store showed (6 → 9)**, which is the evidence the synthetic corpus is a fair stand-in rather than an easier one. |
| 37 | **Fixed: a provider 5xx was logged as an Era crash, forcing DEGRADED** | 10, 1, 4 | Karim's `diag_20260930_150251.md` read **DEGRADED** off 18 `engine.crash` (Gemini internal errors during audio reception). `verdict_for`'s `crashes >= 5` makes that verdict arithmetic, not a measurement. Recovery was always correct (`_classify_disconnect` → `"transient"`); only SEVERITY was wrong, because `_is_network_blip` — which decides crash-vs-quiet — omitted `internal error`/`500` that **four other modules already treat as transient** (`ai_provider`, `gemini_generate`, `claude_cli`, `openai_compat`). New `_is_upstream_service_error` + a quiet `engine.upstream.error`, kept SEPARATE from `engine.network.blip` so an upstream outage stays countable apart from bad Wi-Fi. Markers are now **exported from `ai_provider`, not copied** — a second copy is how `NETWORK_MARKERS` drifted. **And the dismissal is not a blind spot:** `verdict_for` now reads the suppressed list and reports the 24h upstream rate (≥12 → HEALTHY WITH ISSUES naming the count). Caught a bug in my own first version: `_crash_echo` ran only for UNclassified crashes, so a suppressed crash left no marker and its echo double-counted — 4 drops read as 8. 10 tests, 5 load-bearing; 1,592 passed and **all 17 gate reds byte-identical before/after** (stash-proven) |
| 38 | ~~1 unhandled `email` exception during OAuth~~ — **DROPPED by Karim 2026-09-30** | 1, 4 | Closed without a fix, deliberately and on his call. The finding stands if it ever recurs: `core/gmail_service.py:12` promises *"Nothing raises. Every entry point returns a result dict"*, and every OAuth path readable from here is already guarded — token load (`gmail_auth.py:175`), refresh (`:184`), `InstalledAppFlow.run_local_server` (`:270`), watcher loop (`gmail_watcher.py:126`). It escaped somewhere the code does not show, and closing it needs the `location` line from a diagnosis report. Not deleted from the board, because a dropped item that recurs should be recognised rather than rediscovered. |
| 39 | **Item 22's instrument: one `turn.latency` line per turn** | 2 | Karim: *"it responds too late"*. Two answers. **The dial**, which needed no measurement: `core/vad.py` ships `silence_ms: 800` + `end_sensitivity: "low"` and its own docstring calls that *"the core of 'let me finish my sentence'"* — the lateness is bought deliberately to stop her interrupting, and is tunable via `ERA_ENDPOINTING_SILENCE_MS` / `ERA_ENDPOINTING_END` with no code change. **The instrument**, new `core/turn_latency.py`: `response_ms` (last word → first sound) broken into `endpoint_wait_ms` / `model_ms` / `playback_ms` / `tool_ms`. Honest limit, and it shaped the design: the local VAD runs only while SHE speaks, so there is no local speech-end timestamp — `endpoint_wait_ms` is a PROXY off the last transcript delta, logged beside `configured_silence_ms` so the two can be compared. 17 clock-driven tests; 1,074 passed with all 8 gate reds byte-identical before/after. **Wiring verified by reading, not execution** — no mic or Live session here; the live proof is the first `turn.latency` line on Karim's machine |
| 40 | **Item 36 fixture: the recall measurement no longer drifts** | 11, 1 | `tools/golden/semantic_recall_store.json` — **50 synthetic entries** (11 targets, 12 traps, 27 filler) across the five real categories, `updated`/`access_count` identical everywhere so the tiebreakers in `score_memory_entry` cannot sway it, and nothing answering the 8 UNANSWERABLE queries (enforced by a test that scans the file). **Validated, not just written:** words-only **top-1 5/12, top-3 10/12, noise 0/8**, with every trap firing — `"where do I live"` returns ONLY `live_ui_feed_plan` (item 30's bug, reproduced), and `"what do I do for a living"` returns nothing at all. Better split than planned: the lexical half needs **no Ollama**, so `FIXTURE_LEXICAL_TOP1=5` / `TOP3=10` are baked now with proof and pinned by two model-free tests. 22 passed, 3 skipped; gate ruff+manifest+tests PASS. Also: the brief's `test_prompt_injection_never_embeds` already existed and is a PERFORMANCE invariant (memory→prompt), so the security one was added beside it as `test_injection_shaped_memory_is_data_not_instruction`; atomic cache replace and `ERA_SEMANTIC_RECALL=0` already existed |
| 41 | ~~The embedding probe times out on a COLD model~~ — **FIXED 2026-10-02** | 11 | Ollama unloads an idle model after 5 minutes; Era's measured cold load is ~4s (`core/vision_config.resolve_ollama_timeout`); the probe allowed **1.5s**. So the first recall after ANY conversational pause timed out, dropped to word-matching, cached that for a minute and repeated — forever, and **silently**, which is why board item 30b's "degrades to lexical silently" had no identified cause. **Not fixed by a bigger timeout**, which the board ruled out in advance: the probe sits mid-sentence, so paying 4s there buys memory at the exact price item 22 exists to remove. Three changes, none on the conversational path. **(1)** `keep_alive` on every embed (verified against the live Ollama API docs for Karim's **0.35.0**, and against `/api/embed` — the non-deprecated endpoint Era already used; default `1h`, `ERA_EMBED_KEEP_ALIVE` overrides, accepts `-1` for indefinite), so a loaded model stops being unloaded at all. **(2)** A cold probe still fails fast but now starts a **background warm-up** with a real 20s cold-load budget, one at a time, daemon, which clears the cached "unavailable" the moment the model answers — so the turn that found it cold is lexical and the NEXT one is not. **(3)** The drop is **logged on change**, because invisibility is what let this run undiagnosed. 10 tests; **8/8 targeted mutations caught** (no keep_alive, no warm-up, warm-up on the probe budget, warm-up not clearing the cache, no one-at-a-time guard, non-daemon thread, logging every probe, silent degradation). 175 memory tests pass. **Live proof owed:** ask her something from memory, wait >5 min, ask again — see item 30b. |
| 42 | **OPEN: a fresh clone is RED until `era_manifest.py` is run** | 1, 10 | Found 2026-10-01 while attributing a full-suite run. `runtime/` is gitignored (`.gitignore:44`), so a clean checkout has **no `runtime/manifest.json`** — and **26 tests fail for its absence**, across `test_era_manifest`, `test_era_gate`, `test_self_improve_targets`, `test_self_heal`, `test_lint_feedback`, `test_claude_self_improve`, `test_self_improve_verifier`. Proven: generating the manifest in a baseline worktree turned 27 'failures' into 1. `era_gate.py` self-heals the manifest (item 16) so the GATE hides this, but a bare `python -m pytest` does not, and nor would CI — anyone cloning fresh sees 26 red with no hint that one command fixes them. **Fix is a decision:** document it as a required first step, or have `conftest.py` generate the manifest when missing. The second is a behaviour change (a test fixture writing a build artifact) and should not be made as a side effect. **Also parked here:** `test_zz_check_gate::test_script_checks_all_passed[test_execution_layer]` fails at baseline AND HEAD and is not platform-shaped — that file exists because script checks once "sat green for months while asserting the OPPOSITE of correct behaviour", so it deserves a look on Windows. |
| 43 | **Item 36 closed: floors measured and baked** | 11, 1 | `FIXTURE_TOP1 = 8`, `FIXTURE_TOP3 = 11`, measured 2026-10-01 (Windows, Ollama all-minilm, COSINE_CUT 0.36, 50-entry frozen corpus, noise 0/8). Asserted as `>=` so a better model pushes them up without failing; falling below them on a corpus that cannot drift is now a real regression rather than the store having moved. **One case semantic recall still does not rescue on this corpus, recorded rather than tuned away:** `"where do I live"` returns ONLY `live_ui_feed_plan` — `current_city: Bristol` scores below the 0.36 cut even as a real city, where the live store's `Tunis` cleared it. Raising top-1 to 9 by rewording that entry would be fitting the corpus to the result, which is the same sin as lowering a floor. 22 passed, 3 skipped (model-gated); ruff clean. |
| 44 | **Item 19: the failure announcement no longer pre-empts the workaround** | 6, 4, 1 | Two defects, both read off the code rather than guessed. **(1) The prompt promised a mechanism that cannot exist.** §4 deferred recovery to "self-healing routing"; `core/self_healing_router.py` is a **pre-dispatch** rewriter — two production call sites, neither after a result, and no parameter for one. Rewritten as **FALLBACK BEFORE ASKING**: ONE attempt down a genuinely different route before reporting anything, naming the fact that nothing else will do it, with a hard carve-out — never auto-retry a HIGH-risk, destructive, irreversible or ConfirmationRequired action. **(2) The announcement came first, from code.** `_execute_tool` called `speak_error` the instant a tool failed; on the Fish voice that reaches the speakers LITERALLY, before the model had even been handed the failure. So "try workarounds before asking" was not unimplemented, it was **impossible by construction** — any recovery could only contradict a sentence the user had already heard. New `core/tool_recovery.py` arms the notice instead; it is flushed at **both** turn boundaries (cloud in `audio.py`, local brain in `local_fallback.py` — the local path never produces a `turn_complete`). A later success of the SAME tool+action cancels it. **Narrow on purpose:** a different tool succeeding does NOT cancel, because "anything succeeding clears everything" would swallow a real failure inside a compound command, and silence about a failure is worse than a late report. The (tool, action) key has a **known false negative** — one tool, two targets, first forgiven — pinned by its own test rather than left to be rediscovered; closing it would break the corrected-parameter retry the whole module exists for. Residual recorded too: a mid-turn disconnect reports late, bounded by `MAX_AGE_S`. 24 tests; **load-bearing proven by reverting, not asserted** — restoring the `speak_error` line fails `test_a_failed_tool_says_nothing_during_the_turn`, deleting either flush site fails the wiring guard, and 6/6 targeted mutations of the store were caught. 24 passed, ruff clean on 697 files, `era_doctor` unchanged at its 3 environmental errors. **Full-suite attribution: 5,139 passed / 56 failed against the baseline worktree's 5,116 / 55 — ZERO introduced and zero accidentally 'fixed'.** Took two runs to get an honest reading, and both corrections were mine: the first run reported `test_build_is_deterministic` red because I was still editing source files WHILE it ran, and that test builds the manifest twice asserting *"any diff means the code moved"* — it caught me moving the tree, so the whole run was discarded rather than explained away. The clean re-run then found a REAL fault in the first commit: `test_committed_artifacts_are_fresh` red, because the manifest had been regenerated BEFORE the last edits and the committed copy was stale by 28 shifted line numbers. Regenerated and fixed in the follow-up commit — the freshness constraint is a gate for exactly this. |

---

## ⬜ Next (ordered)

All four defects from the 2026-09-29 live test (24–27) are fixed and gated, and
**#15 (memory + RAG) and #16 (runtime grounding) are built and measured** —
evidence in `handoff.md`. **#17 (the Claude bridge, item 32), #17c and #18 are now
done too**. **#19's mechanism is now built as well (item 44)** — what it owes is a
live run, not more code — so the next unstarted item is **#20, the continuous
learning pipeline**. (#20b stays deferred on its own evidence; #22's tuning still
waits on real `turn.latency` lines; #23 is last by design.)

Two of those four were misdiagnosed on this board until the logged tool
arguments were replayed against the running code — do that first, every time.

**All three of acceptance points 1, 3 and 4 now need the same thing: Karim,
out loud.** The mechanisms are built, gated and measured; what is left is a live
check no unit test can make (see "In progress").

| # | Item | Objective | Why this order |
|---|------|-----------|----------------|
| 19 | Intelligent fallback protocol — **mechanism built (44), live proof owed** | 6 | Started 2026-10-02. The audit found the protocol was not merely missing but **contradicted**: `core/prompt.txt` §4 said *"exactly one target tool, no retries"* and *"report the failure … and allow self-healing routing to select a safer existing route"* — and `route_tool_call` has **two production call sites, both PRE-dispatch**, with a signature (`tool, parameters, user_text`) that cannot receive a result. She was told to stand down and wait for a recovery layer that structurally cannot exist. Fixed in 44. **What is left is Karim, out loud:** a tool has to fail on his machine and she has to take the other route. No unit test can claim that — the prompt permits the attempt, it cannot force the model to make it. |
| 20 | Continuous learning pipeline (web + RLHF + error logs) | 5, 7 | Feeds on 15 |
| 20b | **Quota ledger — proposed, approved, then NOT built on the evidence** | 10 | The plan (shared persisted ledger, wall-clock, key fingerprints never plaintext, read by both `quota_failover` and `main.py:_rotate_api_key`, surfaced in era_doctor) was approved *subject to measuring first*. The 24h counts came back: **`engine.quota.exhausted` = 1**, against 37 `engine.network.blip`, 25 `engine.crash` (historical, pre-item-37) and 4 `engine.upstream.error`. Building persistence for a once-ever event is work the data does not justify, so it is deferred, not dropped. **The three defects behind it stand and are real:** (1) `_rotate_api_key` and `quota_failover._LATCH` are two independent quota memories that never consult each other; (2) `_LATCH` is in-process on `time.monotonic()`, so every relaunch re-tries keys it knew were dead; (3) `quota_failover.status()` exists and **nothing calls it** — quota health is invisible to era_doctor and self_diagnosis. Revisit when the quota count stops being 1. |
| 21 | Watchdog / self-maintenance routines | 10 | `era_supervisor.py` exists; audit before extending. Note it behaved correctly on 09-29 — the hang check is what caught 24 |
| 22 | Latency pass — **instrument built (39), tuning now waits on real data** | 2 | Measure first — do not optimize on a guess. As of 2026-09-30 the measurement EXISTS: every turn logs `turn.latency`. Nothing should be optimized until a few days of those lines say which of `endpoint_wait_ms` / `model_ms` / `tool_ms` actually dominates — the first guess ("the model is slow") and the real first suspect (Era's own deliberate 800ms patience) point at opposite fixes. **First measurement already banked (2026-09-29):** the 15 slowest TESTS are all deliberate-timeout paths (19.0s, 15.9s, 4×13.6s…) — roughly 3 minutes of every suite run is sleeping through timeouts that could be injected instead. Clean full suite is **12:10**. |
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

**Wake-word decoys guard the wrong word (found 2026-09-30, item 39):**
`config/wake_word.json` sets `wake_word: "shazam"`, but its `decoys` are
`["error", "her", "area", "here", "hey", ...]` — words that sound like **"era"**, the
previous wake word. So the near-miss protection is inert for the word actually in
use, and nothing rejects things that sound like "shazam". Harmless today (a decoy
only ever causes a rejection) which is why it was left alone: changing wake-word
behaviour on a live voice assistant mid-session is Karim's call, not a side effect
of a latency investigation. Either refresh the decoys for "shazam" or, if "era" is
meant to be the wake word again, change it back and the list is already right.

**A third decision, opened 2026-09-30 by board item 36:**
`tools/test_semantic_recall.py`'s measured numbers (`MEASURED_TOP1 = 9`,
`MEASURED_TOP3 = 10`) are asserted against a copy of Karim's **live, gitignored**
`memory/long_term.json`. That makes the one test carrying acceptance point 4's
numbers unreproducible: it drifts as he uses Era, it cannot be re-measured on any
other machine, and it has now gone red without a line of relevant code changing.
Two ways out, and the choice is his because one of them concerns his own data:

  * **Freeze a fixture.** Give this test a committed store under `tools/golden/`
    and calibrate against that; live drift then cannot break it, and the numbers
    mean something a year from now. It needs ONE re-measurement on a machine with
    Ollama to bank the new floors. **Do not simply commit the real store** — it
    holds his birthday, city and family details, so the fixture must be synthetic
    entries in the same shape, not a copy. That is the recommendation.
  * **Keep it live and accept the drift**, treating the floors as a rolling
    measurement to be re-banked whenever the store grows. Honest, but it means a
    red gate is sometimes just news about the store, which is exactly the signal
    a gate is supposed not to send.

What must NOT happen is lowering `MEASURED_TOP3` to 9 to make the red go away:
that is fitting the assertion to the result, and it would mask the real
regression this test exists to catch.

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
