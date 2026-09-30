# Era One — Handoff (technical log)

Newest first. One entry per fix or improvement: what was wrong, what changed,
how it was verified. Paired with `TODO.md` (the board) and `docs/MANIFEST.md`
(the map). Phase 0 = stabilize; Phase 1 = empower.

---

## Phase 0 — Stabilization (week of 2026-09-29)

### 2026-09-30 · Item 36 — the measurement gets a corpus that cannot drift
**Objectives:** 11, 1 — board item 36, Phase 0 closing

**What was wrong.** `test_measured_recall_beats_the_lexical_baseline` asserted
`MEASURED_TOP1=9 / TOP3=10` against `load_memory()` — which conftest.py seeds from
the REAL `memory/*.json`. That store is gitignored, personal, and grows every time
Era learns something, so the one test carrying acceptance point 4's numbers could
not be reproduced on another machine, could not be re-measured later, and went red
on 2026-09-30 with no relevant code change: entries added since the calibration had
pushed one target out of the top 3.

**What was built.** `tools/golden/semantic_recall_store.json` — **50 synthetic
entries** across the five real categories, and `tools/test_semantic_recall.py`
rewired onto it. The store is synthetic on purpose: committing the real one would
buy stability with Karim's birthday, city and family details in the repository,
which is a worse trade than the flakiness it fixes. Live recall against his actual
memory is still covered by board item 30b — the acceptance test no unit test can
make.

The corpus is 11 targets, 12 near-miss **traps**, and 27 filler entries, with
`updated` and `access_count` held identical across all 50 so recency and
popularity — tiebreakers in `score_memory_entry` — cannot influence the result.
Nothing in it answers any of the 8 `UNANSWERABLE` queries, and a test enforces
that by scanning the file for their topics, because an entry that quietly answers
one turns the noise guard into a test that cannot fail.

**The fixture was validated here, not just written.** Measured with
`ERA_SEMANTIC_RECALL=0`:

    words only:  top-1 5/12   top-3 10/12   noise 0/8

and every trap fires exactly as designed — `"where do I live"` returns **only**
`live_ui_feed_plan` (the original item-30 bug, reproduced synthetically),
`"when is my birthday"` ranks `birthday_card_supplier` above `birthday`,
`"what song does my kid like"` ranks `song_licensing_rules` above
`son_favorite_song`, and `"what do I do for a living"` returns **nothing at all**
— the case only meaning can rescue. The real store's words-only baseline was
6/12, so this corpus is comparably hard rather than easier.

**That produced a better split than planned: most of the measurement now needs no
Ollama at all.** The lexical half is model-free and therefore reproducible in
every gate on every machine, so `FIXTURE_LEXICAL_TOP1 = 5` /
`FIXTURE_LEXICAL_TOP3 = 10` are **baked now, with proof**, and pinned by
`test_the_fixture_is_hard_enough_without_a_model` plus `test_the_traps_actually_trap`.
Only the two semantic floors need a model.

**Those two are deliberately left as `None`.** The old 9/10 were measured against a
different corpus; carrying them over would be fiction in the opposite direction
from lowering them, which the brief rightly forbids. While they are `None` the
measurement test **skips with the numbers it just observed in the skip reason**,
so the gate stays green and the unmeasured state stays loud. `python
tools/test_semantic_recall.py` prints the full table via the same `measure()` the
test asserts with — so the numbers pasted back cannot disagree with the numbers
baked in. That file is now both a pytest module and that script, which is why it
carries a `sys.path` bootstrap: run directly it would otherwise fail on `from
memory import ...` before reaching `__main__`, and asking for `PYTHONPATH=.` first
is a step people forget on Windows.

**Three things in the brief that the code already did**, reported rather than
re-done:

  * **`test_prompt_injection_never_embeds` already exists and means something
    else.** Despite the name it is a PERFORMANCE invariant: "prompt injection" is
    injecting MEMORY INTO THE PROMPT (`format_memory_for_prompt_ranked` runs in
    every `_build_config()`, roughly every 2.5 minutes), and it guards that path
    against triggering embeddings and stalling a prompt build behind seven Ollama
    timeouts. It is untouched. The security-flavoured test the brief meant was
    added beside it under a name that cannot be confused with it:
    `test_injection_shaped_memory_is_data_not_instruction`, with an
    injection-shaped entry in the fixture, asserting the text reaches the embedder
    verbatim as a string (embedded, never interpreted) and never hijacks an
    unrelated query.
  * **Atomic cache replace already exists** — `semantic_recall._save_cache` writes
    a `.tmp` and `os.replace`s it, and its docstring explains why it deliberately
    avoids `safe_store` (one backup per write, forever, of recomputable data).
  * **`ERA_SEMANTIC_RECALL=0` is already set suite-wide** in `conftest.py`.

**Verification.**

    tools/test_semantic_recall.py            22 passed, 3 skipped (model-gated)
    era_gate.py --changed <the test file>     ruff format PASS, ruff lint PASS,
                                              manifest PASS, tests PASS
    python tools/test_semantic_recall.py      exits with a clear "start Ollama" message

`era_doctor` is the gate's only red and reads NOT READY here for three
environmental reasons only — no `config/api_keys.json`, no Cloudinary config, no
OneDrive vault. Unchanged from the previous three sessions.

**What is owed, and item 36 is NOT closed until it lands:** one run of
`python tools/test_semantic_recall.py` on Windows with Ollama, then
`FIXTURE_TOP1` / `FIXTURE_TOP3` baked from its output. The noise line matters as
much as the recall lines: noise is 0/8 lexically here, but semantic scoring can
only ADD candidates, so 0/8 with the model running is a claim this container
cannot make.

**A separate finding, from the log counts Karim pasted** — and it changes the next
item rather than this one:

    37  engine.network.blip        25  engine.crash          7  tool.claude_bridge.unauthorized
     4  engine.upstream.error       1  engine.quota.exhausted

**Quota fired once.** The approved quota ledger (shared, persisted, fingerprinted)
is real work aimed at an event that has happened a single time, so it is NOT being
built on this evidence — the board's own rule is measure first, and the
measurement says no. The three defects behind it stand and are recorded for the
day the counts change. Two other readings: `4 engine.upstream.error` against `25
engine.crash` is item 37 working, since only post-pull drops classify as upstream;
and `engine.network.blip` at 37 is now the most common event by a wide margin,
which points at connection quality rather than anything in Era.

---

### 2026-09-30 · Board item 22 — the turn-latency instrument, and the dial that was already there
**Objectives:** 2 — "she listens and doesn't respond, it responds too late"

Karim reported Era answering late. The board's rule for item 22 is *"Measure first
— do not optimize on a guess"*, so this is the measurement, plus one finding that
needed no measurement at all.

**The finding that costs nothing: the lateness is partly a setting.**
`core/vad.py:523-529` — `silence_ms: 800`, `end_sensitivity: "low"`,
`start_sensitivity: "high"`. Its own docstring says why: *"Longer silence_ms +
end_sensitivity='low' make Era wait through the pauses in a trailing thought
instead of cutting in — the core of 'let me finish my sentence'."* The lateness is
bought deliberately, to stop her interrupting. It is tunable from the environment
(`ERA_ENDPOINTING_SILENCE_MS`, `ERA_ENDPOINTING_END`) without touching
`config/api_keys.json`, which the guard hook blocks anyway. That is a dial, not a
defect, and there is no setting that is both maximally patient and maximally
quick.

Also noted while there: `config/wake_word.json` has `wake_word: "shazam"` but its
`decoys` are `["error", "her", "area", "here", ...]` — words that sound like
**"era"**, not "shazam". The decoy list was built for the previous wake word and
never updated, so it is not guarding the current one. Harmless (decoys only cause
rejections) but it means that protection is inert. Not fixed here: changing wake
word behaviour on a live voice assistant is Karim's call, and it is on the board.

**What was built.** `core/turn_latency.py` — a pure tracker with an injected
clock, logging one `turn.latency` line per turn:

    response_ms        the user's last word -> the first sound back  (THE number)
    endpoint_wait_ms   a PROXY for the silence_ms wait (see below)
    model_ms           the model thinking
    playback_ms        audio received -> audio heard
    tool_ms/tool_count time spent in tools during the turn
    total_ms, interrupted, configured_silence_ms

**The honest limit, which shaped the whole design.** Era's local VAD runs only
WHILE SHE IS SPEAKING — `audio.py`'s barge-in detector sits behind
`if era_speaking:`. While the user talks, mic frames go straight to Gemini
unexamined, so there is **no local "the user stopped making sound" timestamp**.
`endpoint_wait_ms` is therefore the gap between the last input-transcript delta
and Gemini's `finished` mark: a proxy, inflated by however long the server sat on
that delta. It is logged next to `configured_silence_ms` precisely so the two can
be compared — if they track, the wait is the config and the dial is the fix; if
the proxy runs far above it, the server is slow and the dial will not help.
Measuring it properly would mean running VAD on every mic frame during normal
capture, which is a CPU and behaviour change, not instrumentation, and that
decision is deliberately not made here.

**Marks wired at six points**, all idempotent and none load-bearing: first/last
input-transcript delta, `input_transcription.finished`, first `response.data` AND
first `output_transcription` (whichever comes first — in the transcript-synthesis
voice modes Gemini's audio is discarded, so text is the only sign of an answer),
`interrupted`, first byte actually played, and `turn_complete`. Tool time attaches
at the single funnel every tool result passes through, beside `hud.tool_finished`.

**Two things guarded on purpose.** The tracker is reset in a `finally`, so a turn
that logs nothing cannot leak its marks into the next one; and the first-played
mark sits inside the existing try/except in `_play_audio`, whose comment already
says it: *"Diagnostics are never load-bearing."*

**A weak test of my own, caught by checking it was load-bearing.** The bleed guard
was first asserted on `endpoint_wait_ms` — which is immune, because it measures
from the LAST delta and a new turn refreshes that. Reverting the `finally` left
the test green. `total_ms` measures from the FIRST mark, which is exactly what a
missed reset leaves stale (30,400ms instead of 400ms — two turns reported as one).
The assertion moved there and now fails against the pre-fix code. The lesson is
the one this repo keeps relearning: a test is not load-bearing until it has been
watched to fail.

**Verification.** 17 tests, all clock-driven — the subject is arithmetic over
timestamps, and a test needing a real microphone and a real 800ms wait to check
subtraction is a test that gets deleted the first time it flakes.

    tools/test_turn_latency.py                17 passed
    era_gate.py --changed <the four modules>   ruff PASS, manifest PASS,
                                               1,074 passed, 15 skipped, 8 failed
    ruff check . / format --check .            clean, 694 files

All 8 gate failures are **byte-identical before and after** (stash-proven, `comm`
empty both ways) — the container's usual Windows-path and local-model artifacts.

**What is NOT verified here, and cannot be.** The six wiring points inside
`_receive_audio` are verified by reading, not by execution: there is no Live
session, no microphone and no speaker in this container. The unit tests prove the
arithmetic and the logging contract; `_log_turn_latency` is driven directly via
the real method borrowed onto a bare object. **The live proof is Karim's next
spoken turn producing a `turn.latency` line** — and if it does not appear, the
wiring is wrong and the numbers above say nothing about that.

**Reading the result** (tested against a synthetic log, not written from memory):

    python -c "import json,statistics as st;r=[json.loads(l) for l in open('logs/era.log',encoding='utf-8',errors='replace') if '\"turn.latency\"' in l];r=[x for x in r if not x.get('interrupted')];f=lambda k:[x[k] for x in r if x.get(k) is not None];p=lambda k:(k,len(f(k)),int(st.median(f(k))),max(f(k)));print('turns:',len(r));[print('%-18s n=%-3d median=%-6d max=%d'%p(k)) for k in ('response_ms','endpoint_wait_ms','model_ms','playback_ms','tool_ms')]"

Interrupted turns are excluded: their numbers describe the interruption, not the
latency, and averaging them in drags every statistic toward whatever was cut
short.

---

### 2026-09-30 · "DEGRADED" was the threshold talking — Gemini's 5xx was being logged as an Era crash
**Objectives:** 10, 1, 4 — board item 37, from Karim's `diag_20260930_150251.md`

Self-diagnosis reported **DEGRADED / "system integrity appears degraded"** on six
signatures: 18 `engine.crash` (Google GenAI internal errors during audio
reception), 4 `file_controller` failures on non-existent paths, several
`scheduled_task` not-found failures, and one unhandled `email` exception in OAuth.

**The verdict was arithmetic, not a measurement.** `verdict_for`:

    crashes = sum(... startswith(("engine.crash", "engine.reconnect_error")))
    if crashes >= 5 or total >= 50:  return "DEGRADED"

18 `engine.crash` forces DEGRADED with certainty, whether or not Era recovered
from every one. It did recover from every one.

**Root cause: recovery and severity are decided by different code, and only one
of them knew about provider 5xx.** `_classify_disconnect` falls through to
`"transient"` for anything unrecognised, so the reconnect loop handled these
correctly all along. Severity is decided separately by `_is_network_blip`, whose
marker list covers `1006`, `connection reset`, `winerror 64/1231/1232`,
`getaddrinfo failed` — and **not** `internal error` or `500`. So a Google-side 500
fell through to `log_crash(...)` and minted a CRITICAL `engine.crash` per
occurrence.

That this was an oversight rather than a decision is settled by the codebase
disagreeing with itself: `core/ai_provider.py` already listed `"500"` AND
`"internal error"` in `_TRANSIENT_MARKERS`, and `gemini_generate.py`,
`claude_cli.py` (`500: "transient"`) and `openai_compat.py` (`_RETRY_CODES`) all
agree. `_is_network_blip` was the only one that missed it — and its own docstring
describes this exact failure mode for a flaky connection: *"a flaky connection
produced a stream of fake crashes — which then drove Era's own self-diagnosis to a
'Degraded' verdict with no real defect behind it."* The precedent at
`self_diagnosis.py:275` is the same story for 1011s.

**What changed.**

  * `core/ai_provider.py` — the marker list and predicate are now **exported**
    (`TRANSIENT_SERVER_MARKERS`, `QUOTA_MARKERS`, `is_transient_server_error`)
    rather than copied. A second copy is exactly how `NETWORK_MARKERS` in
    self_diagnosis drifted from its source and left `WinError 1231` mis-reported
    for weeks. One list, three consumers.
  * `ReconnectMixin._is_upstream_service_error` — severity only, same contract as
    `_is_network_blip`. **Not** folded into it: a Wi-Fi drop is the user's network,
    a 500 is Google's servers, and logging both as `engine.network.blip` would make
    an upstream outage indistinguishable from a bad cable. EVERY leaf must match,
    mirroring the existing rule that an ExceptionGroup also holding a
    `PortAudioError` is a real audio fault and must stay loud.
  * `audio.py` (`_receive_audio` — where Karim's 18 came from) and `main.py`
    (`run_connect_loop`) — a new quiet branch logging `engine.upstream.error` at
    WARNING instead of a CRITICAL crash.
  * `self_diagnosis.py` — classifies both the new event and the `engine.crash`
    lines **already in era.log** (tagging is not retroactive, the same reason
    `NETWORK_MARKERS` exists), reusing the imported markers and inheriting their
    quota veto, so a 429 can never be dismissed as a transient blip.

**The dismissal is not a blind spot, which was the condition for making it.**
18 recovered drops is still 18 interrupted conversations, so `verdict_for` now
takes the suppressed list and reports the 24h upstream RATE: at
`_UPSTREAM_RATE_NOTE` or above it returns HEALTHY WITH ISSUES naming the count and
that every one recovered. Not DEGRADED — Era did its job each time. The threshold
is **12, and it is a first cut, not a measurement**: the only data point is
Karim's 18. It is a named constant for exactly that reason.

**A bug I introduced and caught while implementing it, worth recording because the
first version of this fix was wrong.** `phase_logs` runs `_classify` first and
`_crash_echo` only when `_classify` returned None. That was safe while no
`engine.crash` could be classified — but the moment one could, the crash set no
`last_crash` marker, so its `engine.reconnect_error` echo 0.1s later was counted a
SECOND time under the same benign class. Four drops would read as eight and the
rate note would fire at half its intended threshold — the very double-count
`_crash_echo` was written to stop, reappearing one level up. `_crash_echo` now runs
unconditionally and its verdict wins. Proven load-bearing: reverting just that
ordering fails `test_crash_and_its_reconnect_echo_count_once`.

**The other three signatures, and why only one of them is a defect.**

  * `file_controller`, non-existent paths (4) — **not a defect.** The board's own
    `_ALREADY_IMPLEMENTED` note: *"`tool.execution.failed` covers ANY tool
    returning a failure, including a user cancelling a confirmation or a file that
    does not exist — not only bugs."* Era reporting honestly, counted as a fault.
  * `scheduled_task` not found — **not a defect.** `ts.TaskError` is the designed
    channel and `scheduled_task()` catches it into `_fail()`. Its own comment notes
    the task name "is often one the model made up", which is the likely cause.
  * **1 unhandled `email` exception in OAuth — the one real finding, and it is NOT
    fixed here.** `core/gmail_service.py:12` states the module invariant:
    *"Nothing raises. Every entry point returns a result dict"*, so an unhandled
    exception violates its own contract. But every OAuth path reachable by reading
    is already guarded — token load (`gmail_auth.py:175`), refresh (`:184`),
    `InstalledAppFlow.run_local_server` (`:270`), and the watcher loop
    (`gmail_watcher.py:126`, *"the loop must never die"*). It is escaping somewhere
    the code does not show, and the report records the deepest repo frame as
    `location`. Guessing at a fix without that line would be inventing one. On the
    board as item 38.

**Verification.**

    tools/test_protocol_one.py                   50 passed, 1 failed (pre-existing)
    era_gate.py --changed <the five modules>     ruff format PASS, ruff lint PASS,
                                                 manifest PASS,
                                                 1,592 passed, 15 skipped, 17 failed
    ruff check .                                 All checks passed

**All 17 gate failures are byte-identical before and after**, established by
stashing the change and re-running the same files: `comm` on the two sorted lists
is empty in both directions. Zero introduced, zero accidentally fixed. They are the
container's Windows-path and local-model artifacts (`test_file_candidates`,
`test_youtube_direct_play`, `test_file_processor_edit`, `test_browser_media_routing`,
`test_local_fallback`, `test_ads_dispatch`, and the `test_protocol_one` traceback
frame). `era_doctor` reads NOT READY here for the same 3 environmental reasons as
before.

**Being exact about load-bearing, since "all of them" was wrong last time:** of the
10 tests added or changed, **5 fail against pre-fix code** — the two classification
tests, the rate-signal-reads-what-phase_logs-writes test, and the echo-count test
(which fails for two independent reasons, the classification and the ordering). The
other 5 are negative controls that pass either way by design: quota is not
dismissed, a group holding a real fault stays visible, a real engine fault is still
a crash, a handful of upstream errors stays quiet, and the Live gate agrees with the
classifier (that last one would fail pre-fix only with `AttributeError`, since the
method did not exist).

**What this cannot be verified against here.** No Windows, no live era.log with real
Gemini 5xx text. The classification is pinned against the exact string from Karim's
own logs (`"1011 None. Internal error encountered."`, already in the suite's
fixture) and against `"APIError: 500 INTERNAL. Internal error encountered."`, but
the proof that the verdict flips on his machine is his next `self_diagnosis` run.

---

### 2026-09-30 · The first Windows `--all` after 34+35 — one red, and it is the test, not the code
**Objectives:** 11, 1 — board item 36

Karim ran the owed full gate (item 35b). Result:

    5,315 passed, 1 skipped, 1 failed in 747.08s (12:27)
    ruff format PASS · ruff lint PASS · manifest PASS · era_doctor PASS
    FAILED tools/test_semantic_recall.py::test_measured_recall_beats_the_lexical_baseline
           AssertionError: top-3 recall fell to 9/12 from 10

**First question, answered before touching anything: is this mine?** No, and it was
settled with the repo's own instrument rather than an opinion. `era_gate.py --list
--changed core/claude_error_router.py app/era_live/reconnect_mixin.py` selects **30
test files** through the import graph, and `tools/test_semantic_recall.py` is not
among them — it cannot reach a line either commit touched. The other candidate was
item 35's new per-test ceiling, and that is ruled out by the failure's own shape: a
timeout kill reports `Failed: Timeout (>120.0s) from pytest-timeout`, not an
`AssertionError` about a recall count.

**What it actually is.** `conftest.py:55-67`:

    # The temp directory is SEEDED with a copy of the real store rather than left
    # empty, because several tests expect realistic content to be present.

So the one test holding acceptance point 4's numbers calls `load_memory()` on a copy
of Karim's **live `memory/long_term.json`** — a gitignored file that is not in the
repo at all (it does not exist in the cloud container; `era_doctor` reports it
missing there). `MEASURED_TOP3 = 10` was calibrated against that store as it stood
on 2026-09-29. He has used Era since; every entry added is a new competitor in a
top-5 ranking, and one paraphrase's target has slipped to position 4 or 5. No code
had to change for this to go red, and none did.

The irony is on the record already: this file's own docstring explains that a test
depending on whether a *service* is running "is a test that fails for reasons
unrelated to what it asserts (the suite already learned this from live browsers and
the real vault)", and gates the Ollama dependency accordingly. The live *data*
underneath it was never gated by the same reasoning.

**What was changed here, and what deliberately was not.**

Changed: the assertions now report the store size and, for each miss, **the top 5
actually returned** rather than a bare count. The top-3 assertion previously carried
no miss list at all, so its failure message could not distinguish drift (target at
position 4) from a real regression (target gone entirely) — and finding that out
cost a 12-minute run that taught nothing. Verified the entry count agrees with
`rank_memory_entries`'s own notion of an entry (`isinstance(entry, dict) and
"value" in entry`), including a malformed entry and a non-dict category.

NOT changed: `MEASURED_TOP3` stays at 10. Lowering it to 9 would make the red go
away by fitting the assertion to the result, and would mask the regression the test
exists to catch. The real fix is a **decision**, now on the board under Open
decisions, and one branch of it concerns Karim's own data: the right answer is a
synthetic frozen fixture under `tools/golden/`, because committing the real store to
make a test stable would put his birthday, city and family details in the repo. That
is not a call to make on his behalf, and it needs one re-measurement on a machine
with Ollama to bank new floors.

**One number in that run is unexplained, and is written down rather than smoothed
over.** 5,315 + 1 failed + 1 skipped = **5,317 collected**, where 5,294 (item 32)
plus exactly the 18 added should give 5,312. That is **5 tests more than accounted
for**. Two readings: the GitHub baseline commit was taken on 09-30 from a tree that
had already moved past the 09-29 full run, or Karim's local tree carries uncommitted
work. It cannot be settled from the cloud container, because the 09-29 tree is not
in git — `git status` on his machine distinguishes them. Until it is settled, "no
code changed but mine" is an inference from the single-commit history, not a
measurement, and the diagnosis above does not rest on it: the import-graph
reachability result holds either way.

**Verification.** `tools/test_semantic_recall.py` → **18 passed, 3 skipped** (the
three `@requires_model` tests skip without Ollama, as designed, which is also why
the measurement itself cannot be reproduced in this container). `ruff check .` and
`ruff format` clean. The changed assertions cannot be exercised here without the
store and the model, so the counting expression was verified directly against the
ranker's own entry predicate instead of being taken on trust.

---

### 2026-09-30 · Items 17c + 18, and a bug that silently ate whole reports
**Objectives:** 10, 8 — the two items the board had queued next

Three things, all in the auto-reporting path the board flagged as the next fix.

**1. A bug found by running the suite: one bad event discarded the whole report.**

`write_details()` read `ev["ts"]` directly. `_note()` wraps that call in a `try`
whose exception `note_failure()` then swallows as `"ignored"` — so a single event
without a timestamp threw away the **entire** sustained-failure report, silently,
which is the one thing this module exists to deliver. A cosmetic defect in one
line of a details file was costing the whole delivery.

It was not hypothetical: `test_the_written_details_file_survives_giving_up` was
**already red in the repo** (`KeyError: 'ts'`, pure stdlib, nothing to do with the
platform) and had been passing nowhere. New `_event_ts()` falls back to now, and
`build_report()` — which had the same two reads — uses it too.

Worth saying plainly: the board recorded item 17c's evidence as green. It was not.
The test existed and failed. Running it was the whole cost of finding that out.

**2. Item 17c — the report is no longer thrown away.**

The budget half of 17c was already done (`_unmark`, so giving up stops spending a
6h cooldown and one of four daily slots). The headline defect stood: at
`DEFER_MAX_S` the report text itself was **discarded**, and the user was told
nothing but a `print` to stdout. That fires precisely when someone has been at the
keyboard for 30 minutes — so the reports thrown away were the ones raised *while
they were working*, and the one person who could act on it was the one person not
told.

Waiting longer is not the fix: the wait holds `_inflight`, so one busy user would
block every other report. So the report stops **waiting** without being lost:

  * `_queue_pending()` writes it to `runtime/claude_bridge/pending/`, deduplicated
    by fingerprint (five copies of one failure is not five findings, and the newest
    copy carries the freshest evidence);
  * bounded on purpose — `PENDING_MAX` 8, `PENDING_MAX_AGE_S` 3 days,
    `PENDING_MAX_ATTEMPTS` 5. A buffer, not a log: a failure nobody could be told
    about for three days has been overtaken by events, and a report that will not
    send after five tries is not going to start working;
  * the user is now **told**, in a fixed phrase (no error text may enter the live
    session): the report is being held and will go out when they are away;
  * `flush_pending()` delivers **one** report when the moment allows — a single
    idle check, never a wait, never a focus steal. One per call because
    `ask_async` leaves the bridge busy, so draining in a loop would collide with
    itself; at four reports a day, one per idle moment drains it many times over;
  * delivery then spends the cooldown and the daily slot, which is the same rule
    `_unmark` protects from the other side: **the budget is spent by reports that
    arrive.** The file is removed on success only, so a crash mid-send retries.

**The gate's invariants were re-checked across the new store, not assumed.** The
queue is a new place a report is written to disk and later re-sent, so the module's
central property — what reaches Claude's chat input holds only values Era's code
controls — is asserted again after a round trip through it. The off switch
(`ERA_CLAUDE_AUTOREPORT=0`, and never under pytest) gates the flush too, or the
queue would have become a way around the off switch; it holds the report rather
than discarding it.

**Where the tick lives, and the dead end I nearly built.** A queue needs draining.
The obvious host was `core/background_manager.py` — and it has **zero importers**
outside its own tests, exactly like the `src/fast_local_vision.py` the board has
open for Karim. A flush registered there would never have run, while passing its
own unit tests forever. It is now on the board as a second instance of that
pattern.

The tick went instead into `_monitor_idle_state` (`app/era_live/reconnect_mixin.py`),
the one loop Era genuinely runs on a timer — off-thread because the send blocks,
throttled to 60s because that loop ticks every 5s, and `try`-wrapped because this
task owns standby and a fault in error *reporting* must never be the thing that
stops Era idling. It is called **twice**: at the top of the body, and inside the
standby wait. The second is not redundant and is the case that matters most —
standby means the user is provably away, and without it the queue would wait for
them to come *back*, at which point they are at the keyboard again and the router
rightly refuses to send.

`notify` is deliberately `None` on that path: the user was already told the report
was held, this fires when they are away, and speaking into an empty room is the
exact thing the standby comment in that loop warns about.

**3. Item 18 — `pytest-timeout`, with the one exception measured rather than guessed.**

Installed (both requirements files) and wired in `pytest.ini` at `timeout = 120`.
120s is headroom over a measured figure, not a preference: the slowest real test is
19.0s and the 15 slowest are all deliberate-timeout paths under 20s.

The naive wiring would have broken the suite. `tools/test_zz_check_gate.py`
shells out with its own `timeout=600` and "alone can exceed the rest combined", so
a flat 120s would have killed the suite's slowest *legitimate* test and reported it
as a hang. It carries `@pytest.mark.timeout(660)` instead — above its own ceiling,
so that ceiling stays the thing that bounds it — rather than lifting the limit for
all 5,300 tests to suit one of them.

**A wrong decision of my own, corrected before it landed.** I first set
`timeout_method = thread`, on the reasoning that SIGALRM does not exist on Windows
so `signal` would silently do nothing there. That reasoning is **false**, and
reading `pytest_timeout` rather than trusting it is what showed so:
`HAVE_SIGALRM = hasattr(signal, "SIGALRM")` already selects `signal` on Unix and
`thread` on Windows by itself. Pinning `thread` for Windows' benefit would have
thrown away the rest of the run everywhere else — `thread` ends the process,
`signal` fails the one test and lets the other 5,300 finish. The setting is now
deliberately absent, and the reason is written in `pytest.ini` so it is not
"fixed" again.

Both semantics the wiring depends on were verified rather than assumed: the ini
ceiling is really in effect (`config.getini("timeout")` → 120.0), and a marker
really does beat a tighter ceiling (a `@pytest.mark.timeout(30)` test survives
`--timeout=1`). And the mechanism does what item 18 wanted: a hung test now dies
and **names itself** — `Failed: Timeout (>3.0s) from pytest-timeout`.

**Verification, and what this container could NOT prove.**

    tools/test_claude_bridge.py                 53 passed  (35 + exactly the 18 added)
    era_gate.py --changed <the three files>      ruff format PASS, ruff lint PASS,
                                                 manifest PASS,
                                                 928 passed, 15 skipped, 8 failed
    ruff format --check .                        692 files already formatted
    ruff check .                                 All checks passed

**13 of the 18 new tests were proven load-bearing** against the pre-fix code, which
is the only thing that makes them worth having: the give-up path was reverted to
dropping-and-silence and **11 failed**; the call site in the idle loop was deleted
and the wire test failed; and the `write_details` test was red in the repo before
the fix, so it proved itself.

Being exact about the other five, because "18 of 18" is the kind of round number
that hides a gap: they cover the tick's throttle, its empty-queue case, its
try-wrapping, and the flush's empty-queue and off-switch cases. Those would all
fail against pre-fix code too, but only with `AttributeError` — the methods did not
exist — so they demonstrate nothing about behaviour. They are bounds on NEW code
(don't hit the disk every 5s, don't raise into the loop that owns standby, don't
become a way around the off switch), which is worth testing and is not the same
claim. The first draft of this entry, and PR #1's description, both said "all 18";
that was wrong in a way the arithmetic underneath it (11 + 1 + 1) already showed.

**This session ran on Linux, and Era is a Windows program — so the gate's last two
reds are the container, not the code, and that claim was tested rather than
asserted.** All 8 failures were reproduced on a **clean stash** of this tree: five
`test_file_candidates` cases asserting Windows paths (`'...\\app\\era_live\\audio.py'
is not in the subpath`), `test_local_fallback` wanting local models, a
`test_protocol_one` traceback frame, and `test_ads_dispatch`
`test_route_order_is_each_profile_direct_then_the_gateway`. None is mine. Likewise
`era_doctor` reads NOT READY here for 3 environmental errors only — no
`C:\Users\nesri\.claude-freelance`, no OneDrive vault at
`/root/OneDrive/.../era_brain` — where it reads READY on Karim's machine.

So what is **owed on Windows**: one `era_gate.py --all` run. The per-test ceiling is
the change that touches all 5,300 tests, and only a full Windows run can show that
none of them sits between 20s and 120s where this container's 928 cannot see it.
That is the single check left on both items.

**Two things noticed in passing, written down rather than rediscovered.**

  * `core/claude_error_router.py` is now **497 lines**, against this project's
    500-line rule. It is under, but only just: the next addition to it has to split
    it rather than extend it. The natural seam is the pending queue — `_queue_pending`
    / `_list_pending` / `flush_pending` / `_flush_once` are a store with one job and
    no dependency on the streak logic above them.
  * `test_ads_dispatch::test_route_order_is_each_profile_direct_then_the_gateway`
    fails on a plain `AssertionError`, not an import or a path — unlike the other
    seven it is not obviously a platform artifact. It is pre-existing and outside
    both items, so it was left alone, but it is the one of the eight worth a look
    on Windows.

---

### 2026-09-29 · Item 17 — measured first; the premise did not survive, so the scope changed
**Objectives:** 8 — measured, then built to Karim's decision (read-only asks only)

The board said: *replace the GUI-paste bridge with the headless CLI path;
`core/claude_bridge.py` pastes into a window, fragile by construction.* The
fragility is real — foreground checks, clipboard, an unverified `ctrl+n`, and
transcript polling because the GUI has no delivery confirmation. `claude -p`
would remove all of it: stdout instead of polling, no focus, no clipboard.

**Two things measured before writing any of it.**

1. **It does not work out of the box.** With the default model alias the run
   fails outright — `[claude-code:unrecognized_model] {"model":"auto/best-coding"}`
   and `terminal_reason: "api_error"`. An explicit `--model` is required. Also,
   the CLI writes diagnostics to **stderr** that corrupt a merged `2>&1` JSON
   parse, so any implementation must keep the streams apart.

2. **Cost, which is the finding that matters.** One trivial "reply OK":

        headless, fresh      cache_creation 63,516   total_cost_usd 0.637
        headless, --resume   cache_creation 27,505
                             cache_read     63,516   total_cost_usd 0.944

   Every invocation re-loads the whole project context — CLAUDE.md, hooks,
   plugin skills, MCP servers — so a one-word message costs **~$0.64**. I
   expected `--resume` to amortise that into cache reads; it did the opposite
   and cost **more**. There is no cheap follow-up.

   `--bare` is the flag that would strip that context, and it is unusable here:
   it states that auth becomes "strictly ANTHROPIC_API_KEY or apiKeyHelper",
   and this project runs on the subscription CLI with no API key.

**So the trade is not "fragile vs robust".**

    GUI paste   fragile transport, but pastes into Karim's already-warm session:
                near-zero marginal context cost, and he SEES it
    headless    robust transport, ~$0.6-0.9 per message, and invisible

**The second cost is the one the board did not account for.** `core/safety_guard.py`
already says a message to Claude Code "is an instruction to an agent that can
act on this machine OUTSIDE Era's gates", and `core/claude_error_router.py`
sends on that path **without the user's confirmation** (streak-triggered auto
reports). Today the blast radius is bounded by the message landing in a chat
Karim can watch. Headless, an auto-report would silently start an agent — so the
transport swap is also a threat-model change, on the one path that already
bypasses confirmation. It can be contained (`--permission-mode plan`,
`--disallowedTools`), and containment is a decision, not a detail.

One genuine improvement headless WOULD bring, and it is not about fragility: the
router currently waits for `IDLE_S` of hands-off time because a GUI send steals
focus for ~3 s, and **drops the report entirely** after `DEFER_MAX_S`. A headless
send steals nothing, so that gate — and the dropped reports — would go away.

**Karim's call: headless for read-only asks only.** Plus a standing
requirement — *"I don't want Era to stop or hit a dead end."*

**What was built.** `core/claude_headless.py` + a preference in
`actions/claude_bridge.py`. A plain `ask` goes to the CLI; `reply` and an explicit
`new_chat` stay in his window, because a reply continues a conversation headless
cannot join and a new chat is a request for something VISIBLE.

**Containment, since an invisible agent is the risk.** `--permission-mode plan`
AND `--disallowedTools Bash Edit Write NotebookEdit WebFetch WebSearch Task
KillShell` — the policy and the belt, neither reachable from a tool argument,
because a model that picks its own containment is not contained. The network
tools are denied too: a question that pulls in web text would let that text steer
the same agent. `Read` stays allowed — that is how it answers anything.

**Never a dead end.** Every failure — no CLI, timeout, API error, unreadable
reply — returns `ok=False` and the caller falls back to the window. A slower
answer, never a refusal. Live end-to-end: *"what file holds Era's system
prompt?"* → "`core/prompt.txt` holds Era's system prompt." No window, no
clipboard, no transcript polling.

**The gate earned its keep here.** It went RED on a pre-existing test,
`test_tool_ask_then_read_reply`, and the reason was worse than a broken
assertion: routing `ask` to this transport made the **test suite invoke the real
`claude`**. At ~$0.64 a call that is money per test run, and a hard failure
wherever nobody is signed in. `available()` now refuses under pytest — the same
rule `core/claude_error_router.py` already follows, and the fourth time this repo
has been bitten by tests reaching a live thing (browser, vault, Era lock).
`ERA_CLAUDE_HEADLESS=0` is the off switch for Karim.

**Gates untouched**, asserted rather than assumed: `ask`/`reply` still require
the user's confirmation at MEDIUM, and reads are still free. A faster transport
must not become a cheaper way past the gate.

**What was NOT done, deliberately.** The auto-report path
(`core/claude_error_router.py`) still uses the GUI. It sends *without*
confirmation, so moving it headless is a threat-model change, and it is the one
place where landing in a chat Karim can watch is the safety property. Its real
defect stands and is now on the board: it **drops** reports entirely after
`DEFER_MAX_S` when he is at the keyboard, because a GUI send steals focus.

**On OmniRoute** (Karim suggested using it): it is a *gateway* for Claude API
calls via `ANTHROPIC_BASE_URL`, not a second brain, so it cannot answer when
Claude is unreachable and does not belong in this fallback ladder. `core/omniroute.py`
already manages it. I checked it while looking: it is **healthy** — HTTP 401/405
in 0.1-0.6 s. My first probe timed out and I wrongly called it hung; that was a
cold first request, and there is no defect in the port-probe readiness check.
`ANTHROPIC_BASE_URL` here is `https://api.anthropic.com`, so this session goes
direct and the cost figures above are subscription-direct.

**Verification.** 32 new tests.

    python tools/era_gate.py --changed core/claude_headless.py         actions/claude_bridge.py tools/test_claude_headless.py
    → GATE PASSED (24.4s)

    python tools/era_gate.py --all
    → GATE PASSED (1438.5s); 5,293 passed, 1 skipped

5,262 -> 5,294 is exactly the 32 tests added.

**Two things in that run looked wrong. Both were chased down and neither was
real** — recorded because the chase is the useful part.

    clean re-run: 5,293 passed, 1 skipped in 730.04s (12:10)

  * **Suite time: RESOLVED, it was contention.** 12:10 clean, which is faster
    than the 13:36 baseline and nothing like the 23:48 that prompted this. The
    slow run overlapped my own concurrent work. Worth saying plainly: I guessed
    contention, and the guess was only worth anything once it was measured.
  * **The thread warning: RESOLVED, transient.** The re-run escalated
    `PytestUnhandledThreadExceptionWarning` to an ERROR, so it would have failed
    the run. It did not recur — only the two long-known warnings appeared (genai
    `_UnionGenericAlias`, pywinauto `coinit_flags`).

**What the durations did expose**, and it is a real finding for later: the 15
slowest tests are all deliberate-timeout paths — 19.0s
`test_watch_page_no_video_no_player_timeout`, 15.9s `test_click_ref_refuses_when_label_changed`,
four at 13.6s, and so on. Roughly three minutes of every run is spent sleeping
through timeouts that could be injected instead. That is a latency item (22), not
a correctness one, and it is now written down rather than rediscovered.

**Original observations, kept for the record:**

  * **The suite took 23:48, against 13:36 for the previous full run** — a 75%
    jump in one session. My changed area is provably not the cause: the two
    bridge test files together run in **5.35s** (measured, `--durations`). The
    likeliest reason is machine contention (this run overlapped other work), but
    that is a guess, so a clean re-run with `--durations=15` was started to
    settle it. If it comes back near 14 minutes it was contention; if it stays
    near 24, it is real drift and belongs on the board.
  * **A new `PytestUnhandledThreadExceptionWarning`** (with a ResourceWarning
    asking for tracemalloc) appeared, meaning a thread raised somewhere in the
    full run. It does NOT reproduce in the bridge tests. The test name was lost
    because the run was piped through `tail`; the re-run captures the whole
    output and escalates that warning to an error so it names itself.

Worth noting what would have made both of these cheap to diagnose: board item 18,
`pytest-timeout`, is not installed, so a hung test is still indistinguishable
from a slow one.

---

### 2026-09-29 · Item 16 — she now answers "what can you do?" from her real tools
**Objectives:** 3, 4 — acceptance point 3, a truthful self-model

**The gap.** Protocol Zero already keeps section 16 of `core/prompt.txt` in step
with the real declarations. That fixed the *prompt*; it never made Era *refuse*,
because a prompt is still text a model reads and paraphrases. This session
watched her assert three untrue things about herself — a tool the model is never
offered (`spotify_bridge`), work the safety layer had already barred (item 25),
and a HUD feature that was never built (item 26). The board's own wording is the
requirement: *the answer comes from the generated manifest, and she declines to
claim anything absent from it.*

**What changed.** New `core/capability_index.py` + a `capability_check` tool
(53rd), dispatched inline, plus one hand-written prompt rule sending capability
questions to it. The answer is **computed at the moment it is given** from
`actions.protocol_zero.load_declarations()` — the same list the live model is
handed, with `RUNTIME_GATED_TOOLS` applied. That last part is why it does not
just read the literal or import the module: reading the literal alone once
reported 53 tools where 52 were offered, and the 53rd went into Era's prompt as a
capability she had. One source of truth, and it is the one that knows about gates.

**Three verdicts, and the middle one is the honest part.**

    NOT SUPPORTED       nothing relates at all -> "tell the user plainly you cannot"
    NOT CONFIRMED       one shared word, which is not evidence
    RELATED TOOLS FOUND a tool in the same AREA, with its real summary attached

The first version of that third verdict said "SUPPORTED, probably" — and it was
wrong. `file_processor` genuinely declares `document` and `translate` actions, so
"print a document" and "translate speech in real time" both land there, and Era
can do **neither**. Announcing those as supported would be the exact
over-claiming the item exists to stop. It now says "this is NOT confirmation",
attaches the declarations, and says a tool in the same area is not the same
capability. The module deliberately does not decide; it hands over evidence.

**Measured both directions, because both are real failures.**

    things she CAN do      13/13 reach their tool
    things she CANNOT do    0/10 come back as a confirmation

**Two bugs found by measuring rather than by reading.**

  * **Under-claiming.** `book me a flight` answered NOT CONFIRMED while
    `flight_finder` exists. Cause: a tool's NAME was weighted the same as its
    prose. A noun shared with a tool's name is near-decisive; the same word
    buried in a paragraph means almost nothing. Name hits are now worth two.
  * **One word counted twice.** The tokeniser expanded "shares" into
    `{shares, share}` so either would match — which let a SINGLE word in the
    request supply two pieces of evidence, and "buy shares in Apple" came back
    as supported. Both sides are now canonicalised to one stem each (guarding
    `ss`, so "access" survives). This is the item-26 lesson arriving early: a
    score is only evidence if it counts distinct things.

**Consistency with Protocol Zero, read out of the code rather than assumed.**
`capability_check` sits at prompt line 54 (hand-written) and NOT inside the
auto-synced block at 906-930, and that is correct: `entries_for()` skips any tool
whose name `_mentioned()` finds in the hand-written text, and since this tool has
no `action` parameter, `action_tokens()` returns `[]` so there is no "further
actions" line to add either. No drift. Worth recording because no test asserts
the LIVE prompt is drift-free — by design, `test_the_real_prompt_is_never_written
_under_pytest` keeps the suite off it — so a passing gate says nothing here. A test pins that
the tool is never listed in `RUNTIME_GATED_TOOLS`: a withheld honesty tool is
worse than none, because the prompt still points at it.

**Verification.** 42 new tests. `core/prompt.txt` backed up to
`prompt.txt.bak-20260929-item16`, and its utf-8-**sig** BOM preserved (editing it
away has broken this file before). Golden file regenerated and its diff reviewed;
`EXPECTED_TOOL_COUNT` 52 -> 53 with its change-log line added.

    python tools/era_gate.py --changed core/capability_index.py \
        app/tool_declarations.py app/era_live/tool_dispatch.py core/prompt.txt \
        tools/test_capability_index.py tools/test_toolspec.py
    → GATE PASSED (101.6s)

    python tools/era_gate.py --all
    -> GATE PASSED (828.8s); 5,261 passed, 1 skipped

5,220 -> 5,262 is exactly the 42 tests added.

**What this does and does not achieve.** It removes the *mechanism* for
confident invention: a capability answer is now derived from data, and a refusal
is written for her. It cannot force the model to call the tool — that is a prompt
rule like any other. Acceptance point 3 is closed only when Karim asks "what can
you actually do?" out loud and she answers from the tool, including declining
something absent. That is a live check, and no unit test can claim it.

---

### 2026-09-29 · Item 15 — recall by MEANING (acceptance point 4)
**Objectives:** 11 — the weakest point, by Karim's own reckoning and by measurement

**The baseline, measured before writing anything.** Recall was token overlap
between the query and the stored value/key. Against 12 paraphrases of *real*
entries in the live store, top-1 recall was **6/12**:

    "what do I do for a living"  -> nothing          (the entry is `job`)
    "what should you call me"    -> nothing          (`preferred_name`)
    "how should you greet me"    -> nothing          (`startup_greeting_preference`)
    "where do I live"            -> live_ui_feed_plan, smart_glass_experiment

That last one is the shape of it: a lexical scorer matching the token "live" in
`live_ui_feed_plan`. No tuning fixes "living" -> "job" or "kid" -> "son".

**Backend: local, and it had to be.** No torch, no sentence-transformers. Ollama
was already installed and already used by this repo, but `gemma4` and
`qwen2.5vl` both return **501 Not Implemented** for `/api/embed` — a generative
model has no embedding head exposed, which is the answer to "why can't you use
the models I already have". `all-minilm` (46 MB, 384-dim) was pulled with
Karim's approval; models already go to `D:\ollama models`, so C: was untouched.
Local rather than an API because recall must work when the network is down or a
quota 1011 has already degraded Era — losing her memory at that exact moment is
the worst possible time. Measured: **~46 ms** per query warm, with occasional
multi-second keep-alive reloads, which is what the timeout is for.

**THE MEASUREMENT THAT DECIDED THE DESIGN, and it was a negative result.**
An earlier fix in `memory_manager.py` stopped unanswerable queries ("showroom")
from returning the five least-irrelevant entries scored on recency alone,
because the model then answered confidently from unrelated notes. That fix had
to survive. So the first thing measured was whether cosine can tell an
answerable query from an unanswerable one:

    answerable   queries: cosine 0.290 .. 0.743
    unanswerable queries: cosine 0.148 .. 0.404   <- OVERLAP

They overlap. So do z-score-against-the-store and gap-to-second-best; all three
were measured and all three fail. **Similarity cannot decide that an answer
exists** — it can only order candidates. A naive absolute threshold would have
either readmitted the noise or dropped three real matches, and I would not have
known which without this.

So the operating point was swept on both things that matter at once:

    cut    top-1    top-3    unanswerable queries returning something
    0.25   10/12    11/12    3/8   <- readmits the noise the earlier fix removed
    0.30    9/12    10/12    1/8
    0.36    9/12    10/12    0/8   <- chosen: lowest cut with ZERO noise
    0.45    8/12     9/12    0/8

**Result, verified end-to-end through the real ranker: top-1 9/12, top-3 10/12,
noise 0/8.** Lexical-only is 6/12 with the same 0/8. A 50% recall gain for
identical noise. `0.36` is a noise budget, not a taste setting, and a test says
so against the number.

**What changed.** New `memory/semantic_recall.py` (Ollama `/api/embed`,
incremental disk cache keyed by text+model hash, availability probed with a
60 s TTL rather than assumed once). Wired into `rank_memory_entries` through
`_apply_semantic_scores` as **`max(lexical, semantic)` — never a sum**, so an
entry the words already found can never be pushed down or dropped: the worst
case of the whole feature is the behaviour before it. Entries rescued by meaning
are tagged `matched_by: "meaning"`.

**Three things found while building, each of which would have bitten later.**

  * `available()` did not catch an unexpected exception, so the module's own
    stated "never raises into recall" contract was false at its own layer. Found
    by the test written to assert that contract, not by inspection.
  * The cache was going through `safe_store.safe_write_json`, which keeps a
    **versioned backup of every write** — correct for `long_term.json`, wrong
    for a cache: one backup per new memory entry, forever, of data that can
    always be recomputed. Now an atomic replace, with a test.
  * **The real hazard.** `format_memory_for_prompt_ranked` runs inside every
    `_build_config()` — every reconnect, ~2.5 min — and ranks once PER ephemeral
    category. Had it passed a query, one prompt build would embed the same
    string ~7 times, and a hanging Ollama would stall prompt construction behind
    seven timeouts. It passes no query, so it takes the pure-recency branch and
    never embeds — verified, and pinned by
    `test_prompt_injection_never_embeds` so it cannot change silently.

**Off by default in the test suite.** `conftest.py` sets
`ERA_SEMANTIC_RECALL=0`; without it, every one of the thousands of tests that
ranks a memory would reach a real local service, write caches, and depend on
whether Ollama happens to be running. The three tests that measure real quality
turn it back on and skip when the model is absent — and are the only place the
numbers live. The cache honours `ERA_MEMORY_DIR`, so tests cannot touch the
user's live store; that is asserted, given this suite's history with the vault.

**Verification.** 21 new tests.

    python tools/era_gate.py --changed memory/semantic_recall.py         memory/memory_manager.py conftest.py tools/test_semantic_recall.py
    → GATE PASSED (44.3s)

    python tools/era_gate.py --all
    -> GATE PASSED (775.1s); 5,220 tests collected

5,199 -> 5,220 is exactly the 21 tests added, so none was skipped or renamed
away. That run was piped through `tail`, which cut the pass/fail summary line;
the collected count was measured separately rather than quoted from memory.

The first gate run warned "NO TEST REACHES memory/semantic_recall.py" — test
selection reads the manifest *before* the run regenerates it, so a brand-new
module is invisible for exactly one run. Re-run: 23 test files reach it, warning
gone. Worth knowing: that warning on a new file is a stale-index artifact, but
on an existing file it is real.

**Honest residue — three cases still not top-1, with the reason for each.**

  * `job` ("what do I do for a living") — cosine **0.354**, just under the 0.36
    cut. It is excluded *by the noise constraint*, not by accident; buying it
    costs 1/8 noise. This is the one I most wanted and did not get.
  * `current_city` ("where do I live") — 0.290, under the cut, so lexical noise
    wins the top slot. `city: Tunisia` also legitimately competes.
  * `era_voice_upgrade_vibe_voice` — rank 3 behind two genuinely related voice
    entries. A top-3 hit, and arguably correct behaviour.

The 12-case set is a floor, not a proof, and it is small enough to overfit — so
the assertions are floors (`>= 9`), not equalities, and the lexical baseline is
asserted separately so a silent fallback to 6/12 fails loudly.

**Still owed for acceptance point 4:** the live Monday-to-Friday run. Saving,
persistence across restarts and recall are each verified, but the acceptance
test is a *user* telling her something and asking days later, and no unit test
can claim that.

---

### 2026-09-29 · Full verification of items 24–27 — one run, at the end

    python tools/era_gate.py --all

    PASS  ruff format             0.2s
    PASS  ruff lint               0.1s
    PASS  manifest                4.0s
    PASS  era_doctor              6.4s
    PASS  tests                 774.4s      5,198 passed, 1 skipped
    GATE PASSED  (785.2s)

**5,139 → 5,198 = +59**, which is exactly the 8 + 14 + 13 + 24 tests the four
fixes added, so nothing was silently skipped or renamed away. Every one of the
four defects from the 2026-09-29 live test is fixed, and the repo is still
lint-clean, format-clean and READY.

Checked by hand, because the floor in item 26 could have weakened a gate rather
than a target: a description that resolves to a safety-critical file still comes
back **HIGH / requires_confirmation=True**, ordinary files stay MEDIUM
unconfirmed, and the live HUD request is LOW *and writes nothing* — which now
reaches the user as item 25's refusal instead of a false claim.

**Not done:** no commit. Git is disabled in this repo by the user's own standing
decision (`.git/HEAD` renamed to `HEAD.disabled`), so there is no repository to
commit to, and re-enabling it was not asked for.

---

### 2026-09-29 · Items 25–27 + the open_app.py restore — the other three live defects
**Objectives:** 3, 4, 8, 13, 1 — defects B, C and D of the live test, and the cleanup

**0. `actions/open_app.py` restored** (approved by Karim). The on-disk diff
against `runtime/self_improve/backups/20260929-154441/` was exactly two things:
the 9-line module header **deleted**, and a dead `open_file_explorer_to_path()`
added. Restored wholesale, which reverts both in one move. The dead function
duplicated `/api/reveal` (`app/hud/api.py`) — which already opens and reveals
files from HUD path buttons behind `_checked_path`, `_published_path` and an
open-allowlist — while having none of those checks itself, so wiring it up would
have put a hole beside a gate that was built deliberately. A repo-wide search
including `.js`/`.ts`/`.json` found two hits: the definition, and the manifest
recording that the definition exists. Nothing could break by removing it.
Manifest regenerated (286 modules); `grep` for the name now returns 0.
**GATE PASSED (31.4s)** — 581 passed.

---

**Item 25 — she said she was working when the safety layer had already stopped
her.** The lie was in exactly one place, and it was not the safety layer or the
action layer. `core/safety_guard.py` had classified correctly (`LOW: target
ambiguous — will only report, not write`) and `actions/self_improve.py` returned
the refusal and wrote nothing. But apply/rollback is **backgrounded**, so
`tool_dispatch.py` answered the model with a fixed optimistic string *before the
work was attempted, let alone resolved*:

    "Working on my own code in the background — I'll back it up, verify it,
     and report the outcome."

A promise made before the check is a promise that can be false. The dispatcher
now resolves FIRST (`_self_improve_refusal`, calling the same `resolve_request`
the guard consults) and only promises what it can keep. On a non-ok status it
returns "NOT started — I have not touched any of my code, and nothing is running
in the background", passes through `resolve_request`'s own message (which names
the candidates), and instructs the model not to dress it up as progress. It
fails **open**: a broken resolver must not silence the tool, because the real
protection is the pipeline's backup/verify/rollback, and this method only governs
what Era *says*. 14 tests, including one that asserts the guard and the
dispatcher consult the *same* resolver with the *same* arguments — the guard's
"it will not write" is a fiction otherwise. **GATE PASSED (83.0s).**

---

**Item 27 — a self-edit must not delete what it was not asked to touch.** The
generation prompt already said "Preserve all existing behavior that the change
does not touch: comments, public function signatures, imports used elsewhere."
An unverified instruction to a model is a preference, not a guarantee. Nothing
caught the header deletion: the file compiled, imported, linted and passed its
tests; the removed lines matched no `_SAFETY_MARKERS`; and 9 lines out of 434 is
a **2% shrink against `_SHRINK_LIMIT`'s 30%**. Verdict: `pass`.

New step 7 in `actions/self_improve_verifier.py` — `header_loss()` → **rollback**,
not a warning. A model that silently discarded the header has already broken the
non-destructive contract, and rejecting is cheap here: `_keep_rejected` preserves
the rejected version, so a wanted change is never lost, it just stops landing
unattended. Deliberately narrow: outright REMOVAL of the module docstring or the
leading comment block only — rewording, extending, shortening, reformatting,
changing quote style, and adding one where there was none all still pass, and
function/class docstrings are out of scope. It uses `ast`, not a regex, so
`"""Doc."""` → `'''Doc.'''` reads as the same docstring; and it stays silent when
the rewrite does not parse, so a SyntaxError never gets a second misleading
cause. 13 tests, one of which reconstructs the **real** 2026-09-29 edit from the
real backup and asserts it stays below the truncation limit — because that is why
it got through. **GATE PASSED (32.0s).**

---

**Item 26 — and the board's diagnosis of it was wrong.** The board said
`resolve_request()` "picked `actions/open_app.py`". Replaying the three live
descriptions verbatim from `logs/task_trace-20260929.jsonl` found something more
specific, and two causes rather than one. Worth noting first: **no `file_path`
was ever passed** — all three calls were description-only, so this really was
the resolver.

  1. **The HUD was invisible.** `_candidate_files()` globbed `*.py` one level
     deep in a hand-kept tuple that did not include `app/`, and the glob did not
     recurse. All 43 modules under `app/` — the whole HUD, the live-session
     layer, the tool dispatcher — were not candidates. The word "HUD" matched
     **nothing in this repo**; "modify the HUD" returned `none`, zero matches.
     Candidates now come from `runtime/manifest.json` (generated from the code,
     regenerated by the gate, `is_test` used to keep the test suite out), and a
     module's **sub-package** counts as one of its names, so "hud" finds
     `app/hud/*`. The top-level directory deliberately does not count — "app",
     "core" and "actions" would each hand a free point to a hundred files.
     193 candidates → 246, with private `actions/_netflix/*` internals excluded
     so they stop crowding out `netflix_control.py`.

  2. **There was no confidence floor.** Score is "how many words of the
     description appear in the module's name". `actions/open_app.py` scored
     exactly **1** — the single word "open" — and a score of 1 with one candidate
     came back as a confident `ok`, indistinguishable from a strong match.

**The floor is 2, and it was measured, not chosen.** Across a spread of
realistic requests, every *correct* resolution scores 2 or more
(`core/vision_policy.py`, `core/voice_output.py`, `core/safety_guard.py`,
`app/hud/api.py`) and the one *wrong* resolution scored 1. That measurement is
kept executable as `test_the_confidence_floor_is_where_the_measurement_put_it`:
if a future change lands a correct resolution on a score of 1 it fails and says
to re-derive the floor rather than lower it.

Result on the live request: `ambiguous`, with HUD files named among the
candidates — which reaches the user as item 25's honest refusal. "improve the
HUD api" resolves cleanly to `app/hud/api.py`, so the HUD is now reachable by
voice at all, which it never was. 24 tests. **GATE PASSED (83.7s)** — 1,160 passed.

**What these three have in common.** None was a crash and none was unsafe: the
safety layer was right every time. What was wrong was what Era *believed and
said* about herself — the Phase 0 shape exactly. And two of the three were
misdiagnosed on the board, both times because the evidence had not been replayed
against the running code. Replaying the actual logged arguments cost minutes and
changed the fix in both cases.

---

### 2026-09-29 · Board item 24 — the "hot-reload" was a force-kill, because it never exited
**Objectives:** 8, 10 — defect A of the live test above, my own wiring from the day before

**What was wrong.** `_reload_after_self_edit()` wrote the relaunch flag, said
"Restarting now", and then *stayed up*. `_arm_relaunch()` sets
`_shutdown_requested = True`, but that is a **request, not an exit**: the only
place it is read is the top of the connect loop (`main.py:1556`), *between*
Gemini Live session attempts. With a live session up — normally minutes — nothing
acts on it. 26 s later `era_supervisor.py` hit its 25 s `RELAUNCH_GRACE_S` and
killed the process tree: `exit_code 1, forced: true` in
`logs/era_lifecycle.jsonl`, with anything mid-flight truncated.

**Why it was missed by 16 passing tests.** Every one of them stopped at the
flag. `era.relaunch` written, `era.stop` absent, `_shutdown_requested is True` —
all four assertions were true of a process that never left. The whole file
tested the *decision* and nothing tested the *departure*. Same shape as the rest
of this session: not a test gap, a gap in what was being **checked**.

**Root cause.** The exit sequence existed and worked — it was a local function
`_shutdown()` **inside** `_shutdown_era`, so the reload path had no way to reach
it. A private copy is a copy the second caller cannot use.

**What changed** (`app/era_live/tool_dispatch.py`, backup
`tool_dispatch.py.bak-20260929-item24`):

  * new `_exit_after_speaking(*, close_hud: bool)` — the one door out of the
    process, next to `_arm_relaunch` for the same reason;
  * `_shutdown_era` now calls it (its 26-line local copy is gone — a net
    deletion, not an addition);
  * `_reload_after_self_edit` calls it after it speaks. That single missing line
    is the whole defect;
  * `close_hud` is the only difference between the two callers, and it is now
    a parameter rather than a closure over `relaunch`.

**One real improvement, not just a move.** The old wait was "speech started,
then speech stopped". The reload path queues **two** utterances (the self-edit
summary, then "restarting now"), and the pause between them would read as
silence — exiting mid-sentence, so the user watches Era vanish without hearing
why. It now waits for 1.5 s of *continuous* quiet. Every wait stays bounded:
worst case ~20 s against the supervisor's 25 s, and a test pins that against
`era_supervisor.RELAUNCH_GRACE_S` itself rather than a number copied into the
test.

**Verification.**

    python tools/era_gate.py --changed app/era_live/tool_dispatch.py \
                                       tools/test_self_improve_reload.py
    → GATE PASSED (97.1s) — 904 passed, 29 test files, ruff/manifest/doctor clean

Eight new tests. The one that matters — `test_a_verified_edit_actually_leaves_
the_process` — was **proved load-bearing** rather than assumed: with
`_exit_after_speaking` stubbed to a no-op (exactly the pre-fix behaviour) the
relaunch flag is still written and the assertion fires. The others hold the
three refusals to *no exit at all*, the two-utterance gap, the grace-period
bound, and `os._exit(` appearing exactly **once** in the module.

**What to look for in the logs next live run.** `relaunch.performed` with
`exit_code: 0, forced: false`. The supervisor already records `forced`, so the
fix is observable without adding anything — and the user cannot tell the
difference either way, which is precisely why it had to be read from the audit
trail.

**Still open from the same live test:** items 25, 26, 27. And `actions/open_app.py`
is still modified on disk from that test (dead function added, module docstring
deleted) — awaiting Karim's call on restore vs rewire; the recommendation is
restore, because `/api/reveal` in `app/hud/api.py` already does the job behind a
real validator that the new function has none of.

---

### 2026-09-29 15:44 · FIRST LIVE FIRING of acceptance point 1 — read from the logs, not from a test

Karim ran Era live and exercised the voice -> code -> hot-reload path for real.
This entry is **observation, not a fix**: nothing was changed in response yet.
Evidence is in `logs/era_lifecycle.jsonl`, `logs/era_crash.log`,
`logs/task_trace-20260929.jsonl` and `runtime/self_improve/backups/20260929-154441/`.

**The machinery fired.** `logs/era_lifecycle.jsonl`:

    {"ts":"2026-09-29T15:45:11","event":"relaunch.requested","pid":28660,
     "reason":"self_improve applied actions/open_app.py",
     "source":"self_improve","flag_written":true,"supervised":true}
    {"ts":"2026-09-29T15:45:37","event":"relaunch.performed","pid":8204,
     "exit_code":1,"forced":true,"ran_for_s":410}

`source:"self_improve"` is the `_arm_relaunch()` call wired yesterday, and
`supervised:true` / `recoverable:true` is the `is_supervised()` branch working.
Era did come back (pid 8204) and was still alive at 15:55. **The path is real.**

Four defects came with it. None would fail a test, because none is a test gap —
they are gaps in what is *checked*, which is this session's recurring shape.

**A. The reload is a FORCED KILL, not a clean exit.** `logs/era_crash.log`:

    15:45:37,444  WARNING  Era asked for a relaunch 26s ago but is still
                           running - killing it.
    15:45:37,690  INFO     ... exit code 1 after 410s.

`_arm_relaunch()` sets `_shutdown_requested = True` and the event loop is
supposed to notice and exit. It did not within 26s, so the supervisor's hang
check killed it. The user cannot see the difference — Era returned either way —
but `exit_code 1, forced: true` means anything mid-flight was truncated. **This
is a defect in yesterday's wiring, and it is the one to fix first.**

**B. Wrong target: she was asked for the HUD and edited `actions/open_app.py`.**
The request was "Modify the HUD to dynamically add a button whenever a new file
or folder is created." `resolve_request()` picked `actions/open_app.py` and
appended a new `open_file_explorer_to_path()` at line 436. The function itself
is *correct* code. It is also **dead: nothing calls it** (`grep` finds exactly
one hit, the definition). No HUD file was touched, so the feature does not
exist. That is why the next thing Karim said was "create a test file on the
desktop to see if it's work or not".

**C. She reported working while the safety layer had already decided not to
write.** The 15:46 and 15:50 calls were classified

    LOW: Self-improve target ambiguous — action will only report, not write.

(`core/safety_guard.py:759`), yet both returned to the user:

    "Working on my own code in the background — I'll back it up, verify it,
     and report the outcome."

They wrote nothing. Proof: `runtime/self_improve/backups/` gained exactly **one**
directory today (`20260929-154441`, the 15:44 call). Two of three self-edits
silently no-opped while announcing success. **This is acceptance point 3 —
truthful self-model — failing in production.**

**D. The regenerated file lost its module docstring.** Diff of the backup
against the live file shows the 9-line header ("High-performance Windows
Application Launcher…") deleted. The LLM rewrote the file and dropped it. Small,
but it violates the non-destructive rule: nothing in the pipeline checks that a
self-edit only *adds*.

**Not defects, noted so they are not re-hunted.** `save_memory` correctly
refused an unprompted write and asked first (the gate working as designed).
`retrieve_memory_context` answered "No relevant memories found" for "HUD display
preferences" — accurate, and an argument for acceptance point 4.

**Verification of this entry:** `python -m py_compile actions/open_app.py` → OK
(the file is valid, just wrong). No code changed in this session.

---

### 2026-09-29 · Session close — full verification
**Everything below this entry was verified together, at the end, in one run.**

    python tools/era_gate.py --all

    PASS  ruff format             0.2s
    PASS  ruff lint               0.2s
    PASS  manifest                4.6s
    PASS  era_doctor              7.2s
    PASS  tests                 796.3s      5,139 passed, 1 skipped
    GATE PASSED  (808.5s)

Test count across the session: **5,105 -> 5,122 -> 5,139**, all green, with the
repo lint- and format-clean for the first time.

**Five acceptance points, honestly scored.**

| # | Point | State |
|---|-------|-------|
| 1 | Voice -> code -> test -> hot-reload | BUILT, 16 tests. **Live demo still owed** |
| 2 | Dual-account failover | BUILT, 24 tests. **Awaiting a real quota exhaustion** |
| 3 | Truthful self-model | PARTIAL — a false capability was found and removed; she does not yet REFUSE what is absent from the manifest |
| 4 | Memory (vector + RAG) | NOT STARTED — next |
| 5 | Regression gate | BUILT, PROVEN, and it has now caught three real defects |

**What the session actually found, and it was not what the brief predicted.**
The premise was "operational but buggy". The measurement said otherwise: the
suite was already green and `era_doctor` already READY. Every real defect
found was the same shape — **something Era believed about herself had drifted
from what was true**:

  * her prompt claimed a tool the model is never offered (`spotify_bridge`);
  * her self-edit pipeline reported changes "verified" while running zero
    tests, because coverage was found by filename;
  * the codebase index silently lost most of the repo's import edges;
  * one optional import could abort all 2,427 tests, hidden by collection
    order.

None of those would fail a test, because each was a gap in what was being
CHECKED. That is the argument for generated, self-verifying artifacts over
bug-hunting, and it is why the manifest went first.

**Open decision for Karim.** `src/fast_local_vision.py` now has zero importers
— a working local-vision path that nothing can reach. Left in place
deliberately. Rewire or retire.

---



### 2026-09-29 · One bad import could abort the whole test run
**Objectives:** 10 (reliable suite), 1 — found by the gate, not by looking

**How it surfaced.** The gate on the lint changes came back FAILED in 22s —
too fast for a test failure. It was a COLLECTION error:

    tools/test_spotify_search.py
    OSError: [WinError -2147417850] Cannot change thread mode after it is set

**Root cause.** That file guarded itself with
`pytest.importorskip("pywinauto")`. `importorskip` converts **ImportError**
into a skip — nothing else. pywinauto imports comtypes, which calls
`CoInitializeEx()` at import time, and if something earlier in the same
process already chose the other COM apartment model, that raises **OSError**.
Not an ImportError, so it sailed past the guard and became a collection error,
which aborts the ENTIRE run before one test executes. 2,427 tests, stopped by
one optional dependency.

**Proved pre-existing, not mine.** Bisected to a two-file pair, neither of
which this session had touched:

    test_memory_guard.py  then  tools/test_spotify_search.py   -> ERROR
    tools/test_spotify_search.py  then  test_memory_guard.py   -> 22 collected

Pure import ORDER. A normal `pytest` run survives it only by luck: `testpaths
= tools .` collects `tools/` first, so pywinauto is imported before the
root-level test that sets the other mode. `tools/era_gate.py` passes an
explicit, differently-ordered file list, which is why a latent fragility that
had been sitting there became visible now. The gate did not cause it; it
stopped hiding it.

**Fix.** Guard on the real failure mode, not the convenient one: catch any
exception from the import and `pytest.skip(..., allow_module_level=True)`. A
machine-environment condition must cost this one file, never the run.

**First fix — the guard.** Catch any exception, skip at module level:

    bad order   -> 13 passed, 1 skipped   (was: run aborted)
    good order  -> 22 passed              (still runs)
    file alone  ->  9 passed              (not silently disabled)

That last line is the one that matters: "make it skip" is an easy fix that
quietly deletes coverage, and it would have looked identical in CI.

**But a skip is still nine tests not running, so the root cause was fixed
too.** `comtypes` calls `CoInitializeEx()` at import, reading
`sys.coinit_flags` and defaulting to APARTMENTTHREADED when unset; `pywinauto`
sets that flag to 0 (MULTITHREADED) when IT is imported. Whichever lands first
decides the apartment, and the loser raises. So `conftest.py` now pins
`sys.coinit_flags = 0` as its FIRST statement, before anything can import
either — the two agree, and collection order stops mattering.

    previously-fatal order -> 22 passed   (all nine now RUN, none skipped)
    audio/COM group        -> 186 passed

pywinauto emits an advisory "Revert to STA COM threading mode" warning under
MTA and works. This is `conftest.py`, so it is the TEST process only — Era's
own runtime never loads it and its apartment behaviour is unchanged.

The module-level guard is KEPT as well. The conftest pin prevents the clash;
the guard is the backstop for a machine where pywinauto genuinely is not
importable. Belt and braces, because the failure mode it protects against
costs the entire run.

---

### 2026-09-29 · Lint debt cleared, one file at a time
**Objective:** 1 — item 14b

Karim approved fixing these INDIVIDUALLY with manual review, and explicitly
not with a blanket `ruff --fix`. Each was checked before being touched, and
checking changed the answer twice.

**I was wrong about one of my own warnings.**
  * `src/fast_local_vision.py:13` — right. `import mss.tools` sits inside the
    `try` that sets `_MSS = True`; it is imported for its side effect, to
    prove the screenshot-to-PNG path exists. Deleting it would silently widen
    the probe to "mss exists at all". KEPT, with `# noqa: F401` and the reason
    written down so the next linter pass does not relitigate it.
  * `actions/screen_processor.py:37` — WRONG. Not a probe. The backup at
    `runtime/protocol_one/backups/manual_20260916` still has
    `return fast_local_vision()`; the live file no longer calls it. Dead
    import from an unwired "option (b)" path. REMOVED.

**Raised rather than decided:** removing that import leaves
`src/fast_local_vision.py` with **zero importers** — a working local-vision
path that is now unreachable. The module is left in place. Rewiring or
retiring it is a product decision, not a side effect of a lint pass, and
objective 4 says Era should not believe she has a capability she cannot reach.

**The rest, each verified before removal.**
  * `actions/computer_control.py` — `sys`, `remember_action`, `is_safe_path`,
    `load_config`, `os` each appeared exactly ONCE in the file (the import
    line). Genuinely dead; removed.
  * `actions/browser_control.py` — `extract` likewise dead. `second_score` was
    computed and never read; the ambiguity signal the comment described is
    actually `_last_tab_candidates`, which IS consumed at line 1892. Removed
    the variable and corrected the comment to describe what really happens.
  * `actions/web_search.py:201` — E402. Checked for the obvious reason to
    defer an import and there is none: `feature_inspire` does not import
    `web_search`, so there is no cycle. Moved to the top.
  * `actions/_netflix/js_snippets.py` — trailing whitespace inside generated
    JavaScript. Semantically inert, and `tools/test_netflix_scan_js.py`
    (69 tests, `node --check`) proves the JS still parses. See
    [[era-generated-js-needs-parsing]] for why that test exists.
  * `actions/feature_inspire.py`, `scripts/cleanup_delete.py` — trivially dead.

**The 12 "unformatted files" were mostly not code.** `ruff format` also
formats Python code blocks INSIDE Markdown, and was rewriting quotes and line
breaks in vendored agent definitions (`.claude/agents/swarm/*.md`), dated
design specs (`docs/superpowers/specs/*.md`) and a skill document. Those are a
record of what was decided at the time; restyling their examples edits history
to satisfy a linter. `ruff.toml` already excluded `.claude/skills` as "not
ours to restyle", so the exclusion was extended on the same reasoning.
Confirmed safe: **zero `.py` files** exist under any of the three added paths,
so no real Python is now hidden from the linter.

Only **three** of the twelve were genuine Era source —
`actions/media_coordinator.py`, `actions/protocol_one.py`, `era_scheduler.py`,
all predating the auto-format hook. Diff reviewed first (pure line-joining),
then formatted; all three still import.

**Verified.** `ruff check .` -> **All checks passed**. `ruff format --check .`
-> **683 files already formatted**, none outstanding.

---

### 2026-09-29 · Voice → code → test → hot-reload (acceptance point 1)
**Objective:** 8 — the headline gate

**What was missing.** `actions/self_improve.py` already did the hard part:
backup -> Claude-generated text over the tool-free subscription CLI -> compile
+ ruff + covering tests -> deterministic ruff repair -> one model repair ->
auto-rollback -> independent second-pass verifier. What it would not do is the
last step. Its rule 6: "we report 'restart needed', we do not restart
ourselves." So the loop stopped one step short of the change being VISIBLE,
and the user had to issue a second command.

**Change — the restart is signalled by one layer and performed by another.**

  `actions/self_improve.py`
      New `reload` parameter, DEFAULT FALSE. On a verified apply it sets
      `out["reload_safe"]` and `out["reload_requested"]`. It still never
      restarts, never execs a repo file, never imports the relaunch
      machinery — rule 5 intact, and now asserted by a test rather than
      trusted.

  `app/era_live/tool_dispatch.py`
      New `_arm_relaunch(reason, source)` — the single copy of the restart
      sequence, extracted from the "reboot Era One" path, which now calls it
      too. New `_reload_after_self_edit(outcome)` decides whether to use it.
      The background self-improve task passes the `out` dict through and
      calls it when the signal is set.

  `app/tool_declarations.py`, `core/prompt.txt`
      The parameter declared to the model, plus one hand-written line telling
      Era when to pass it ("change the HUD", "make it live"). The managed
      Protocol Zero block was NOT hand-edited; drift check still clean at 52.

**Why default false.** `_apply()` is also driven unattended: Protocol One
calls it inside a ~12-minute maintenance cycle and `core/self_heal.py` calls
it from the healing loop. A default-on reload would restart Era mid-cycle.
The voice path opts in; automation does not.

**The four refusals, which are most of the work.**
  * verdict is not a clean `pass` — `review`, `environment` and `skipped` all
    mean the independent check could NOT vouch for the change. Restarting
    into it is the exact outcome the pipeline exists to prevent.
  * a shutdown or reboot already in flight — two exit paths racing is worse
    than a late reload.
  * a community-management job is running — the same protection "reboot Era
    One" has. A CM job is minutes of real work against live brand accounts.
  * nothing would bring her back — `is_supervised()` false and
    `spawn_supervisor()` also failing. Exiting there is an outage wearing a
    reload's clothes; she stays up and asks to be restarted instead.

Each refusal is SPOKEN with its reason. A silent non-restart would leave the
user believing the change was live.

**Security property checked explicitly.** A new parameter must not become a
new bypass: `reload=true` on a SAFETY_CRITICAL target still returns HIGH /
`requires_confirmation: True` from `core/safety_guard.classify_action`. The
guard classifies on the target, not on the caller's eagerness.

**Verified.** `pytest tools/test_self_improve_reload.py` -> **16 passed**
(9 on the signal, 6 on the live-session decision, 1 on the safety gate). One
test asserts the two paths share a single relaunch sequence, because two
copies of a restart sequence drift until one of them forgets the supervisor
check.

**A test-isolation bug in my own first draft, worth recording.** Six of these
tests initially failed with a Gemini **429** — I had stubbed the verifier but
not the generator, so a unit test of a *decision* was making a real API call
and depending on a network quota. `_generate_updated_code` is now stubbed too.

**The gate earned its keep immediately.** The first run after this change came
back GATE FAILED on tests — `tools/test_toolspec.py::test_anthropic_output_
matches_golden`. Adding a tool PARAMETER changes the emitted Anthropic tool
surface, which is pinned to a golden file at
`tools/golden/tool_declarations_anthropic.json`. Regenerated with the command
the test itself prints:

    python -m core.llm.toolspec --dialect anthropic --out tools/golden/tool_declarations_anthropic.json

Diff reviewed rather than accepted blind: **11 lines, all of them the new
`reload` property**, still 52 tools, nothing else moved. Worth remembering —
ANY change to `app/tool_declarations.py`, including a new parameter on an
existing tool, requires that regeneration.

**A lesson about my own tooling.** The failure detail was missing from the
first report because I piped the gate through `tail -18`, discarding the
pytest output above the summary. The gate was fine; the invocation threw the
evidence away. Capture the whole log and tail the FILE.

**Not proven yet.** No live run: this is unit-tested only. The real proof is
the acceptance test — say "change something on the HUD" to a running Era and
watch the change appear after an automatic restart.

---

### 2026-09-29 · Clean full-gate run, and a gate that stops wasting a cycle
**Objective:** 10, 1

**Full `--all` run:** **5,122 passed, 1 skipped, 1 failed in 12m50s**
(test count up from 5,105 by this session's 17 new gate tests). `era_doctor`
PASS. The failures reported were:

  * `ruff format` / `ruff lint` — the 18 pre-existing errors and 12 unformatted
    files described in the previous entry. The untargeted tier lints the whole
    repo on purpose, so this is the debt staying visible.
  * manifest freshness — caused by regenerating the manifest *while* the run
    was in flight, not by a defect.

So the honest state of Era's own suite is: **every test passes.**

**A usability defect in my own gate, fixed rather than excused.** The freshness
step used `era_manifest.py --check`, so any source edit made the gate fail on
the one problem it was capable of fixing itself. Over this session that cost
four wasted cycles, each ending in the same mechanical command. The manifest is
a DERIVED artifact; regenerating it is idempotent and takes ~3s.

The gate now REGENERATES it. The strict check stays where it earns its keep —
as the pytest assertion in `tools/test_era_manifest.py` — so anyone running the
suite directly is still told the map has drifted rather than quietly reading a
stale one. The gate fixes it; the suite reports it.

**Verified.** `era_gate.py --changed tools/era_gate.py` ->
**GATE PASSED in 23.4s**, manifest step PASS, no wasted cycle.

---

### 2026-09-29 · Era could self-edit a file and verify it against no tests
**Objectives:** 8 (voice-to-code), 10, 1 — acceptance points 1 and 5

**Found while scoping the voice-to-code work.** `actions/self_improve.py` is
the only sanctioned path for Era to modify her own source, and its whole
safety argument is "the change must pass compile + ruff + its tests, or it is
rolled back". But `_find_test_file()` located tests by NAME ONLY —
`test_<stem>.py` in the repo root, `tests/`, or `tools/`.

For `core/llm/claude_accounts.py` that looks for `test_claude_accounts.py`,
which does not exist. The real coverage is
`tools/test_claude_account_failover.py`, which no naming rule can find. So the
lookup returned `None`, the pytest step was skipped entirely, and the change
was reported verified having run **zero tests**. Silent zero-coverage is the
worst possible failure for this pipeline: it does not weaken the safety
argument, it removes it while leaving the wording intact.

**Change.** `_find_test_files()` now asks the import graph in
`runtime/manifest.json` — the same selector `tools/era_gate.py` uses — for
every test that can reach the target. `_find_test_file()` remains as a
back-compat shim. When nothing covers a file, that is now LOGGED rather than
passed over in silence. Name matching is kept as a fallback so that a missing
or stale manifest degrades coverage instead of removing it.

**A flaw in my own first version, caught before it shipped.** Results were
capped at four files (a self-edit needs a fast verdict, not a suite run) but
ordered arbitrarily. For `core/panic_stop.py` — reached by 16 test files — the
first four were `test_memory_guard`, `test_cascade`, `test_claude_bridge`,
`test_confirm_self_approval`, and the cap **excluded `test_panic_stop.py`**,
the one test written for the file being edited. Truncating an unordered list
is how a cap turns into a coverage hole. Results are now ordered by
specificity: name match, then direct importers, then one hop out.

**Verified.**

    core/panic_stop.py           -> test_panic_stop.py first
    core/llm/claude_accounts.py  -> test_claude_account_failover.py first
    actions/self_improve.py      -> test_claude_self_improve.py first

`pytest test_self_improve_verifier test_claude_self_improve test_protocol_zero
test_era_gate test_era_manifest` -> **66 passed** (plus the freshness guard,
which passed after regenerating).

---

### 2026-09-29 · Gate proven end to end, and the lint debt it exposed
**Objective:** 10 — acceptance point 5

**Measured.** `era_gate.py --changed core/llm/claude_accounts.py
actions/protocol_zero.py actions/self_improve.py` ->
**GATE PASSED in 101.7s** (ruff format, ruff lint, manifest freshness,
era_doctor, 518+ tests). A narrower change runs in **25s**. Against a
13-minute full suite, that is the difference between a gate that gets run and
one that does not.

**What it exposed.** The first end-to-end run failed on `ruff check .` — the
repo carries **18 pre-existing lint errors and 12 files needing reformat**,
none of it in code changed this session. A gate that fails every run for
unrelated reasons trains people to ignore it, which is worse than no gate.

**Decision.** Lint scope now follows the tier: `--changed` lints exactly the
files you touched (you are answerable for what you changed); the untargeted
tiers still lint the whole repo, so the debt stays visible rather than being
defined away. The cleanup is tracked in `TODO.md`, not silently absorbed.

**Deliberately NOT auto-fixed.** 13 of the 18 are `--fix`-able, but at least
two are load-bearing: `src/fast_local_vision.py:13` (`mss.tools`) and
`actions/screen_processor.py:37` (`fast_local_vision`) look like availability
probes — an import whose only job is to succeed or raise, which ruff correctly
sees as unused and would happily delete, breaking feature detection. Blanket
`ruff check --fix` on this repo is unsafe; each F401 needs looking at.

---

### 2026-09-29 · Prompt synced; the false capability is gone
**Objectives:** 3, 4 — closing the previous entry

Deferred until the baseline finished, because Protocol Zero writes
`core/prompt.txt` and tests read it. Run as maintenance once the suite was
done:

    Protocol Zero synced prompt.txt: removed 1 (spotify_bridge).

**Verified.** `check_drift()` -> `changed: False`, "in sync with all 52 of my
tools". `grep -c spotify_bridge core/prompt.txt` -> **0**. Era no longer
carries a capability she cannot perform. Protocol Zero took its own backup, as
it does on every write. Takes effect on her next session.

---

### 2026-09-29 · The regression gate
**Objectives:** 10 (health after every change), 1 — acceptance point 5

**Problem.** The full suite is 13 minutes. Objective 10 asks for a suite run
after *every* modification; nobody runs a 13-minute gate after a three-line
fix, so in practice the gate did not exist.

**Change.** Added `tools/era_gate.py` with three tiers, all of which also run
the cheap invariants (ruff format, ruff lint, manifest freshness, era_doctor —
a few seconds, and they catch a different class of breakage than any test):

    era_gate.py --changed <files>   only the tests that reach that code
    era_gate.py                     everything but the measured-slow files
    era_gate.py --all               everything

**How targeting works.** It reads the import graph out of
`runtime/manifest.json` — the navigation system paying for itself on day one —
and selects every test that can reach a changed module. Derived from code, not
from a naming convention, so a test that reaches a module through three layers
of imports is still selected.

**The measurement that set the default.** Unbounded closure is the honest
relation and a poor filter, because this repo is tightly coupled. Changing one
leaf module, out of 159 test files:

    depth 1     1 file      depth 3     9 files
    depth 2     2 files     depth 0    99 files  (62% of the suite)

Depth 2 is the default and picked exactly the two files a human would pick.
A bound can miss a deep path, so `--all` still exists and still runs before
work is called done — and when the bound excludes a test that *would* have
covered a changed file, the gate prints that file under "NO TEST REACHES
THESE" rather than reporting a clean pass over code it chose not to run.

**A generator bug this surfaced.** The first targeted run reported that nothing
covered `core/llm/claude_accounts.py`, which its own test file plainly imports.
Cause: `_imports()` in `tools/era_manifest.py` recorded only `node.module`, so
`from core.llm import claude_accounts` produced an edge to `core.llm` and lost
the submodule — most of this repo's coupling. Fixed with a two-pass build: the
first pass learns which dotted names are real modules on disk, the second
resolves each import against that set (and now handles relative imports too).

**Verified.** `pytest tools/test_era_gate.py` -> **17 passed in 0.08s**.
Two of those tests exist to keep the tradeoff honest: one asserts the depth
bound really does still exclude something (if it ever stops, the "fast" tier
is silently running the whole suite again), and one asserts a file no test
reaches is *reported*, never quietly dropped.

**One test of mine was wrong and is worth recording.** The first version
asserted `core/far.py` would be reported uncovered at depth 1 — but a test
imports it directly, so depth 1 covers it. The fixture had no genuinely
orphaned module, so the property could not be tested at all. Fixed by adding
one, rather than by loosening the assertion to whatever the code returned.

---

### 2026-09-29 · Era's prompt claimed a tool she cannot call
**Objectives:** 3 (anti-hallucination), 4 (self-model)

**Found while cross-checking two capability readers.** The new manifest counted
52 tools; `actions/protocol_zero.py` counted 53. One of them was wrong, and the
one that was wrong writes Era's system prompt.

**Root cause.** `app/tool_declarations.py` declares every tool in one literal,
then withholds the ones whose config flag is off — that filter ran at import
time, about 2,000 lines below the literal. Protocol Zero deliberately does not
import the module (it `ast.literal_eval`s the list so nothing is executed and a
running Era sees today's file), so it read the list *before* the gate and never
saw the filter. Result: `spotify_bridge` — dormant, keyless, and never offered
to the live model — was documented in `core/prompt.txt` as a capability Era has.

That is not a cosmetic doc bug. It is the exact failure objective 4 names:
Era believing she can do something she cannot, and objective 3's anti-
hallucination rule broken at the source, in the prompt itself.

**Fix — make the gate data, not a statement.**
- `app/tool_declarations.py`: gates now live in a literal `RUNTIME_GATED_TOOLS`
  (`tool -> (config section, key)`), and the filter is driven from it. A reader
  that parses this file without importing it can now see the gate.
- `actions/protocol_zero.py`: `load_declarations()` reads `RUNTIME_GATED_TOOLS`
  from the same parse and applies the same flags, failing CLOSED when the
  config cannot be read — treating an unreadable flag as "on" would re-create
  the bug it fixes. Still no import, so the module's no-execution property is
  intact.

The point of routing it through one data structure: adding a second gated tool
later updates one place, instead of relying on someone remembering there are
two readers.

**Verified.** Both readers now return **52** and agree exactly (`sorted(disk)
== sorted(imported)`, symmetric difference empty). `check_drift()` now reports
what was previously invisible: `removed 1 (spotify_bridge)` — confirming the
false capability is in the live prompt and must be synced out.

**Still open.** `core/prompt.txt` has not been rewritten yet; the sync is
deferred until the full-suite baseline finishes, because tests read that file
and changing it mid-run would corrupt the measurement. Tracked as item 11a.

---

### 2026-09-29 · Dual Claude account failover, for every path
**Objective:** 9 (multi-account failover), supports 8

**Problem.** Era reaches Claude through the subscription CLI, and which account
answers is chosen by one variable: `CLAUDE_CONFIG_DIR`. `era_dispatch.py:1699`
has failed over `.claude-freelance` -> `.claude-delta` since 2026-09-25, but
**only for the community-management lane**. Every other Claude path —
reasoning, planning, research synthesis, and the Gemini quota failover that
lands on Claude — read a single hardcoded `config_dir` from `config/llm.yaml`.
One account hitting its usage limit stopped all of it.

**Change.** Lifted the proven logic rather than writing a second one; a
subtly-different second definition of "is this account dead?" is how the two
lanes would start disagreeing.
- Added `core/llm/claude_accounts.py`. Ordered account list (primary +
  `fallback_config_dirs`, de-duplicated, `ERA_CLAUDE_ACCOUNTS` overrides), the
  switch predicate, and a 15-minute in-memory cooldown per failed account.
  `ROUTE_FAILURE_MARKERS` is copied from `era_dispatch._ROUTE_FAILURE_MARKERS`
  deliberately.
- `core/llm/adapters/claude_cli.py`: `generate()` now loops over accounts; the
  single-invocation body moved into `_attempt()`. `_build_env()` takes an
  optional account.
- `config/llm.yaml`: added `fallback_config_dirs` pointing at the `.claude-delta`
  profile. `config_dir` is unchanged and still tried first, so this is a pure
  addition — the account that served every call yesterday still serves the
  first attempt today.

**The decision worth keeping.** It switches accounts only on failures the other
account could plausibly survive — out of usage, rate limited, signed out,
credentials rejected. It deliberately does **not** switch on `refusal`,
`unsupported`, `invalid_response`, `context_overflow` or `timeout`: those fail
identically on the second account, so retrying spends another ~3s and ~$0.06 to
lose the same way. "Retry everything everywhere" looks more robust and is
strictly worse. Seven tests hold that half in place specifically.

**Verified.**
- `python -m core.llm.claude_accounts` -> both profiles found, both `ready`.
- `pytest tools/test_claude_account_failover.py` -> **24 passed** (new file).
- Regression: `test_claude_cli_adapter` (46), `test_api_key_failover`,
  `test_ads_dispatch`, `test_exec_ledger`, `test_era_manifest` ->
  **207 passed in 85s**, nothing broken.
- `ruff format --check` + `ruff check` -> clean.
- `python tools/era_doctor.py` -> **READY, 0 errors** (unchanged from baseline).

**Not done here.** The failover is exercised by fakes, never by a real
exhausted account. A live test means actually driving the primary to its limit;
that is item 2 of the end-of-week acceptance test, not something a unit test
can claim.

---

### 2026-09-29 · Codebase navigation system + capability manifest
**Objectives:** 13 (navigation), 4 (self-model), 3 (anti-hallucination)

**Problem.** Two readers kept re-deriving the same facts by scanning 1,289
Python files. Claude paid a full-repo scan before every three-line fix. Era
described herself from prose in `core/prompt.txt` (923 lines) that nothing tied
to her 52 real tools in `app/tool_declarations.py` — so a tool removed in code
stayed claimed in the prompt. No manifest, no index, no `TODO.md` existed, and
`handoff.md` held CM content only, not a technical log.

**Change.**
- Added `tools/era_manifest.py` (generator). AST-walks the source tree and
  imports the two pure-data modules that define Era's abilities. Emits
  `docs/MANIFEST.md` (the navigation map) and `runtime/manifest.json`
  (machine-readable, for Era's self-model). Nothing is hand-written, so nothing
  can go stale the way a maintained doc does.
- Added `tools/test_era_manifest.py` (the guard). Four properties: the
  committed artifacts match a fresh run (anti-drift); every declared tool
  resolves to a file that exists (anti-hallucination); no module fails to parse;
  the build is byte-deterministic.

**Verified.** `python tools/era_manifest.py` → 284 source modules, 106,474
lines, 157 test modules, 52 declared tools, 57 registered actions, **0
unparseable**. `pytest tools/test_era_manifest.py` → **7 passed in 13.5s**.
All 52 tools resolve: 30 to an `actions/*.py` module, 22 to a dispatch site in
`app/era_live/tool_dispatch.py`. Zero unresolved.

**Why it matters beyond the map.** `runtime/manifest.json` is the grounding
source a later objective needs: when Era is asked what she can do, the answer
comes from the generated capability table rather than from the prompt's prose.

---

## Baseline audit (2026-09-29, before any change)

Recorded because "fix the bugs" needs a starting measurement, not an impression.

- `python tools/era_doctor.py` → **READY, 0 errors, 3 warnings.** Warnings:
  api_keys.json holds literal credentials (expected, local-only); one vault
  filing warning; `pyaudio` / `python_vlc` not installed (optional).
- Test suite: **1 failed, 5,105 passed, 1 skipped in 787s (13m07s)**, with Era
  stopped. The single failure was this session's own manifest-freshness guard,
  firing because `app/tool_declarations.py` was edited mid-run — the anti-drift
  mechanism doing its job, not a defect in Era.
- **CORRECTION to an earlier figure in this log.** A first run was killed at
  ~20 minutes with no output and estimated as ">20 min". Measured properly with
  Era stopped it is **13 minutes**; the earlier run was contending with the live
  instance. 13 minutes is still too long to run after every three-line change,
  which is what `tools/era_gate.py` exists for — but the premise "the suite is
  enormous and slow" was overstated and is corrected here rather than left
  standing.
- So the honest headline for objective 1: **Era's test suite is green.** This
  is not a broken system needing rescue; it is a large, healthy one whose real
  risk is drift, not breakage.
- `pytest-timeout` is **not installed**, so no per-test timeout is enforceable.
- Era runs live as `main.py` + `era_supervisor.py`; the suite spawns
  subprocesses, so full runs should be done with Era stopped.

**What was found already built** (recorded so it is not rebuilt):
- Objective 8 is ~80% done. `core/llm/adapters/claude_cli.py` (440 lines) runs
  `claude -p` on the subscription, no API key, `--allowedTools ""` deliberately
  off. Router, autopilot, shadow mode and a $2.00/day ceiling are configured in
  `config/llm.yaml`. Missing: the voice → code → test → hot-reload loop.
- Objective 9 is **already solved for one lane only**. `era_dispatch.py:1699`
  `_routes_for()` plus `_ROUTE_FAILURE_MARKERS` already fails over
  `.claude-freelance` → `.claude-delta` → gateway for CM/ads work, and both
  account dirs exist with credentials. But `config/llm.yaml` pins a single
  `config_dir`, so every other Claude path in Era has no failover. The fix is
  to lift the proven logic into the adapter, not to write a second one.

---

# Earlier handoffs (pre-Phase 0, kept verbatim)

## A) Wardrobe Reel — master-anchored workflow (2026-07-22)

### 1) Goal
Make the CM subsystem produce wardrobe Reels to Karim's exact standard, with
**perfect consistency from image to video** — no drift (layout changing between
shots, missing doors, swapped/generic accessories, "a different wardrobe" in shots
2-3, unreal renders). Then extend the same guarantee to kitchen and Ceram Quartz.

### 2) Current state — wardrobe SOLVED and validated live; confirmed by Karim
- **Root cause found & fixed.** The old Magnific Space generated all six shots as
  independent text-to-image nodes, so identity could never hold. Fix =
  **master-anchored generation**: generate ONE master wardrobe, then DERIVE every
  other shot FROM it (`images_change_camera` for angles, `images_generate` with the
  master as an image reference for door-open/accessory), never a fresh text prompt.
- **Validated live 2026-07-22** with Karim watching: master strong; all 6 derived
  shots held identity with zero drift; all 6 Kling clips held identity, no morphing;
  concatenated to `wardrobe_reel_silent.mp4` (30.2s, 9:16) in the session scratchpad.
- **Locked policies (Karim):** master = `imagen-nano-banana-2` (Nano Banana Pro) 2K;
  shots 1-6 = `imagen-nano-banana-2-flash` 1K (unlimited-friendly); video = `kling-25`
  5s 9:16 720p, keyframe start = image id; music = free `music_generate.py` (Lyria) +
  ffmpeg, **never** Magnific `audio_music_generate`.
- **NEW hard rule this session — CAMERA-ONLY MOTION.** When animating, NOTHING in the
  shot moves (wardrobe/doors/drawers/clothes stay frozen); the only motion is a slow
  camera move. Prompt must be phrased as a camera instruction, never an action verb.
- Existing CM assets untouched and still in place: `core/cm_vault.py`,
  `core/cm_learning.py`, `_build_ads_brief()`, the SELF-CRITIQUE GATE in `build_brief()`,
  and the current Magnific CM Space (kept as-is for the kitchen/Ceram live test).

### 3) Active files
- `.claude/skills/wardrobe-reel-workflow/SKILL.md` — full master-anchored procedure
  (incl. the new camera-only step 3).
- `brands.yaml` → `delta_cuisine.design_rules.wardrobe.generation_method` — concise
  machine copy auto-rendered into every headless brief (`model_policy`, `video_policy`
  with camera-only rule, `music_policy`, `never`).
- `docs/era_roadmap.html` — roadmap updated (wardrobe marked solved).
- Memory: `era-wardrobe-master-anchored.md`, `era-cm-learning-zero-lessons.md`.

### 4) Changes made (this session)
- Added `generation_method` (principle + steps 1-4 + model/video/music policy) to
  `brands.yaml` wardrobe; confirmed it renders into the brief.
- Wrote the `wardrobe-reel-workflow` skill; added the SELF-CRITIQUE GATE to
  `build_brief()` for producing lanes (`tools/test_cm_knowledge.py` green).
- **Today:** added the **camera-only motion** hard rule to `brands.yaml` (`video_policy`
  + `step_3_video`) and the skill (step 3); updated `docs/era_roadmap.html`.
- No generations/tests were run this turn (Karim: "do not run any test").

### 5) Failed attempts / gotchas
- `simulate_cost` for `video_generate` errors ("api field required when slug absent")
  — a simulator quirk; measure real cost via `account_balance` delta instead.
- A derived shot generated too similar to a neighbor duplicates it — slot 2 must be a
  **macro** (material/handle), not another open-door view.
- Never derive from a flawed master — it multiplies the flaw six times. QC the master
  hardest; two re-rolls, then show Karim before spending on derived shots.
- Seedance Mini = ~700 cr/clip vs Kling 2.5 720p = ~140 cr/clip (5x cheaper) — reason
  Kling is the locked video model.

### 6) Next steps
- **DONE 2026-07-22 — wardrobe Reel finished.** Free Lyria music (cinematic-slow,
  faded) muxed under the reel via `music_generate.py` + ffmpeg (no Magnific). Final
  file: `Desktop\Era_Wardrobe_Reel\wardrobe_reel_final.mp4` (30.2s, 9:16, video+aac).
  Awaiting Karim's Gate-2 approval (never write "(approved)").
- **DONE 2026-07-22 — master-anchored Space built** for Karim to test himself:
  "Wardrobe Master-Anchored (Claude)", id `a251c2a9-b6f2-4440-a3aa-e1dc0298d26f`.
  Node graph: master (Nano Banana Pro 2K) → 6 derived shots (Flash 1K, each references
  the master) → 6 camera-only Kling 2.5 720p clips → ALL CLIPS list → FINAL REEL
  combiner. NOT run (no credits spent). Before running, Karim fills the CONCEPTION
  REFERENCE + ACCESSORY PHOTOS creation nodes and the CONFIG material `<fill>`, then
  runs from the MASTER Prompt Architect downstream. Pointer commented in `brands.yaml`
  (reference only — live CM pointer unchanged, per Karim "keep the current space").
- **Karim runs a live test** of the current CM Space for **kitchen + Ceram Quartz**.
  If not what he wants, port the same master-anchored method (a Space like the wardrobe
  one) to those brands for cross-brand consistency.
- When the new wardrobe Space is validated, promote its id into `magnific_space`.
- Widen CM correction capture (`record_feedback` only fires on `revise`/`approve` —
  most corrections are still lost; see `era-cm-learning-zero-lessons.md`).
- Meta Ads Steps 6-7 (reporting PDF, dry-run campaign).

### Known pre-existing issues (not caused by this work)
- 8 stale `_Stub` test failures in `test_hermes_wiring.py` / `test_memory_guard.py`.
- `context7` MCP allowlisted for CM but not registered in the headless brand configs.

---

## B) Telegram two-way channel (previous handoff, still pending)

### 1) Goal
Make Telegram a two-way channel: an order sent from any network must report back in
the chat, so the user knows whether it ran. Full spoken reply for now — brief acks
later, once Era talks through the earpods directly.

### 2) Current state
Implemented in `main.py`, `py_compile` OK. NOT yet run against a live Era session —
no Telegram round-trip has actually been observed. Needs a real end-to-end test.

Confirmed by reading the code (answers the user's second question): the LAN shortcut
is **same-network only**. `launch_network_bridge()` binds Flask to `0.0.0.0:5005`
(`main.py`), reachable only at the laptop's private IP; no inbound port is open.
Telegram works anywhere because it long-polls *outbound* to api.telegram.org.

### 3) Active files
- `main.py` — all changes live here (flag, wrapper, mirror, disarm sites)
- `era_telegram.py` — unchanged; `send_message` / `_tg_notify` are the transport
- `runtime/telegram_offset.json` — getUpdates cursor, untouched

### 4) Changes made
Added `self._tg_reply_pending` (init ~line 1630): armed only while a Telegram-originated
order is in flight, so local spoken asides are never mirrored to the chat.

- `_start_telegram()` (~4410) now wraps `era._on_text_command` instead of passing it
  raw; the wrapper arms the flag before dispatch (dispatch returns immediately, so
  arming after would race the reply) and disarms if dispatch raises.
- At `turn_complete` (~3785), if armed, `_tg_notify(full_out)` — `full_out` is the exact
  transcript already written to the UI as `era: …`. Disarms unconditionally there, even
  on a text-less turn (pure tool call), so it can't leak into the next local turn.
- Disarm + explanatory reply on the two silent early returns in `_on_text_command`:
  standby drop ("I'm in standby — say \"era\"…") and session-not-up. Previously both
  were indistinguishable from Era being offline.
- Disarm without reply on the remote-wake path (~1792) and in both `_run_cm_flow`
  branches (~2209/2217) — those already send their own Telegram message, so mirroring
  would double it.

### 5) Failed attempts / gotchas
- `speak()` is not a usable mirror point: it pushes an *instruction* into the Gemini
  live session; Era's actual words only exist later as `output_transcription`. Hence
  the hook at `turn_complete`, not at `speak()`.
- CM runs take ~30 min, so the flag would stay armed across the whole run and collide
  with CM's existing `_tg_notify` — that's why CM disarms explicitly.
- One `Edit` failed on the wake-path block: the file has mojibake (`rÃ©veillÃ©`) from a
  bad encoding round-trip. Match on surrounding lines, not on that string.
- Only `py_compile` was run. No test covers this path.

### 6) Next steps
- **Test end-to-end**: message the bot with a plain order, confirm the reply arrives;
  then repeat while Era is in standby (expect the standby hint), and after a CM order
  (expect exactly one `[brand] …` message, not two).
- Consider a brief-ack mode flag for when earpod audio lands — the mirror call site is
  a single `_tg_notify(full_out)`, so swapping in a short ack is a one-line change.
- **Carried over from the previous (unrelated) handoff, still pending:** user must run
  `python scripts/cleanup_memory.py` (review), then `--apply`, then restart Era. Also
  still open: name of the 2nd son's "perfect song" (feedback entry 197). Full detail is
  in git history / `docs/memory_feedback_cleanup.md`.
