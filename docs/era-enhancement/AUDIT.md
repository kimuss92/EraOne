# Phase 1 audit — 2026-10-11

## Scope and evidence quality

Intent: stabilize the existing assistant, preserve working behavior, and identify
the smallest verified changes before broader architectural work.

Inventory: 1,773 tracked files; 632 scoped source/configuration/test/documentation
files across root, actions, agent, app, config, core, crew, memory, scripts,
tools, UI, utilities, and Windows helpers. 585 of those files occur in the
knowledge graph. 215 tracked paths changed since the graph commit. The full
inventory is `audit-inventory.json`; `inventory_only` explicitly does NOT mean a
line-by-line review. Historical backups are not live implementation evidence.

All 519 scoped tracked Python files parse successfully. The React/TypeScript
application passes `tsc --noEmit`. `python -m pip check` reports no broken
requirements. These are structural checks, not proof of behavioral correctness.
The targeted production Ruff scan (`E9,F63,F7,F82,B006,B023`) found no issues.

## Current architecture and coverage

| Area | Existing implementation / evidence | Gap against directive | Verification scope |
|---|---|---|---|
| Startup and wiring | `main.py`, `app/era_live/*`, tool declarations and action registry | Large mixins and multiple input/lifecycle paths remain regression-sensitive | Graph, recent changes, selected current wiring, whole-suite baseline |
| Audio routing | `core/audio_endpoints.py`, `voice_output.py`, `voice_input.py`, `audio_policy.py`, `app/era_live/audio_output.py` | Requested vs observed route confused; direction bypass by exact ID; retry churn; no established event-driven endpoint lifecycle | Direct inspection, 135 existing focused tests, 8 new failing regressions, live read-only endpoint inventory |
| TTS | Gemini Live; Fish; restored `tts_kokoro.py`; sentence queue in `speech_out.py` | Router uses chosen Fish/Kokoro with Gemini fallback, not the requested global priority; provider and playback completion differ | Current router and worker inspection; previous audit used only as historical evidence |
| Captions / Telegram mirroring | HUD transcript events; `_tg_reply_pending` and completion mirroring | Full playback-correlated phrase lifecycle/progressive remote delivery not established | Wiring search; requires Phase 4 tests |
| Telegram | `era_telegram.py` authorizes updates; text and voice notes reach command callback | No callback-button lifecycle or live calling in inspected integration | Current function inventory and entry-point wiring |
| HUD | React/Vite; sequenced events, snapshots, approvals, task/development surfaces | Need component-by-component interaction coverage, schema-aware widgets, gap reconciliation | TypeScript check, bus/transport source, existing tests |
| Visual customization | `core/visual_engine.py`, `VisualWorkspace.tsx` already exist | Reuse and verify capabilities instead of assuming widgets are absent | Graph/current manifest and prior audit; runtime behavior still unverified |
| Standby | Explicit triggers, wake/resume, `_idle_monitor` in `reconnect_mixin.py` | No persisted vocal auto-standby switch found; notification gates defer some completion speech | Current idle and CM completion paths |
| Ear | VAD, attention, speaker recognition, media gating, pinned mic | False-accept/reject and own-TTS rejection need actual audio evaluation | Graph, recent commits, existing tests; no live acoustic claim |
| Awareness | StateStore, process/window/media helpers, file watchdog | Mixed polling/event mechanisms; `StateStore.snapshot()` returns mutable internal state | Current source and graph; observer coverage is incomplete |
| Jobs | SQLite `agent/task_store.py`, task queue, scheduler store, cancellation registry | Separate job/schedule stores are not automatically duplicates; global atomic deduplication not established | Current contracts and tests; map ownership before consolidating |
| Reporting | `ToolResult`, task traces, scheduler reports, hive insights, HUD | Unified end-to-end evidence and lesson reporting not established for every task family | Current report functions and graph |
| Approvals | PendingConfirmationStore, HUD cards, scheduler fingerprints | General action immutability and Telegram callback binding need explicit tests | Existing guarded paths must remain enforced |
| Hermes / CM / crew | HermesAgentTransport, in-process transport, capability grants, CM flow | Cody's current code capabilities propose/check only; full crew read/write collaboration is not implemented by those capabilities | Current capability handlers and transport contracts |
| Self-improvement | Staged development workspace, source discovery, verification/rollback, self-heal | Need unified authorization, health policy and promotion evidence; do not build a second edit system | Current development module and existing tests |
| PC/files/browser/media | Substantial adapters and tests; file watcher; API-free media controls | Reliability must be established task family by task family; no broad replacement justified | Graph and full baseline; live acceptance outstanding |
| Memory | Persistent stores, audit, semantic recall, feedback | Validate provenance and promotion of lessons; historical assumptions can become stale | Graph and test infrastructure; seeded test data noted |
| CodeMode | Existing Claude subscription CLI provider and headless helper | Dedicated mode, context enrichment, duplex sessions, HUD UX, coordinated patches still need reviewed design | Current CLI adapter plus official documentation |
| Jev | No existing integration identified in scoped inventory | User confirmed TypeSafe AI; decision API must be evaluated before use | Official introduction and API quickstart |
| Cleanup/configuration | Existing README, env/config surfaces and backups | Dynamic imports, scripts and historical references prevent deletion based on graph degree alone | Inventory only; no deletion approved or performed |

## Confirmed defects and priorities

### High: voice route claims precede playback evidence

`set_pin()` returns “now plays ... from this sentence” after saving a preference.
No stream acknowledgment has happened. `status()` treats a missing stream as a
match, and `_route_disagrees_with_pin()` accepts any nonfallback numeric index.
`summary_line()` turns the preference into a factual system-prompt assertion.
Four distinct regressions reproduce this plus a prompt-grounding regression.

### High: device selection can violate identity or direction

`audio_endpoints.resolve()` resolves exact IDs across all endpoints before
respecting the requested flow. A microphone ID can therefore satisfy an output
request. `_live_endpoint()` chooses the first same-name replacement when the
stable ID disappears, even if two replacements share that name.

### High: recovery tears down a usable fallback

`reclaim_preferred()` closes the working fallback before proving that the
preferred endpoint can open. `_play_audio()` may request this every 1.5 seconds
while Core Audio says the endpoint is present. Presence does not imply that the
audio stream can open. The regression proves unnecessary closure; it does not
prove this is the physical cause of the user's beeps.

### High: default pytest discovery enters nested worktrees

Bare pytest aborted during collection in
`.claude/worktrees/blissful-antonelli-9b3938/test_verify_changes.py` because that
manual script calls `sys.exit(1)`. The existing comment says vendored `.claude`
content is excluded, but `norecursedirs` omitted `.claude` itself. Added that
specific exclusion; the complete baseline then began executing normally.

### Medium: externally mutable state snapshot

`core/state_store.py:StateStore.snapshot` returns `self._state` after releasing
its lock. Callers can mutate it outside the lock. Trace consumers and add an
isolation regression in Phase 3 before deciding on immutable snapshots/deep copies.
A disposable empty StateStore reproduced this: modifying `snapshot.open_apps`
was visible in a subsequent snapshot without calling an update method.

### High: approval payload is retained by reference

`PendingConfirmationStore.store()` assigns the supplied parameters dictionary
directly, and `get()`/`peek()` return the stored mutable object. A caller can
change an armed action without a new approval ID. Phase 3 must copy/freeze the
approved payload and bind approval to its immutable identity; tests should
mutate both the original argument and a returned preview to prove isolation.
A disposable local store reproduced mutation of the armed parameters through
the original argument; no real action or shared approval store was used.

### Confirmed functional gaps

- `TaskQueue.submit()` unconditionally creates a UUID and enqueues a task; no
  same-goal running-job check exists in that path.
- `_monitor_idle_state()` enters standby at the idle timeout without consulting
  a persisted auto-standby toggle.
- `_deliver_or_queue_cm_report()` queues reports while in standby instead of
  waking Era to deliver them. This preserves the report but does not meet the
  requested completion-wake behavior.

## Live audio observations

Read-only enumeration found five active Core Audio endpoints: PC speakers,
glasses stereo output, glasses hands-free output, laptop microphone array, and
glasses capture. The stereo and hands-free endpoints have distinct stable IDs.
PortAudio also exposes the outputs through multiple host APIs.

The generic glasses alias is ambiguous with both profiles present. An explicit
stable pin should be honored; automatic profile policy needs an explicit,
tested rule and must never choose PC speakers for a glasses alias.
At a later read, the voice-specific glasses helper resolved successfully and
the saved stereo pin resolved to PortAudio index 4. This is time-sensitive
device state, not a permanent alias failure. The configured microphone pin is
`Microphone Array`, not the Bluetooth hands-free capture endpoint.

Log counters in `audio-log-counts.json` show 17 scheduled audio re-enumerations,
43 engine reconnect errors, and 20 legacy MME error-6 occurrences across the
inspected files. Those are historical counts, not proof of correlation or a
newly reproduced physical disconnect loop. No driver, pairing, mixer, voice
preference, or running Era process was changed by the inventory.

## External integration findings

- [AgentOS reference](https://agentos.guide/system.html): useful ideas are agent
  status, unified chat, searchable outputs, inline previews, and shared memory.
  The retrieved page is a product overview, not a complete technical protocol
  specification. Adapt the useful interaction patterns to the existing stack.
- [Telegram Bot API](https://core.telegram.org/bots/api): use its message,
  draft/edit, voice-note and inline callback surfaces for the existing bot.
  [phone.requestCall](https://core.telegram.org/method/phone.requestCall) is
  explicitly user-only. Real calls require a separate supported user-client
  transport and account/session provisioning; a bot token alone is insufficient.
- [Claude authentication](https://code.claude.com/docs/en/authentication) supports
  claude.ai subscription login. [Programmatic Claude Code](https://code.claude.com/docs/en/headless)
  supports structured streaming and session metadata. Installed CLI is 2.1.289;
  do not assume features documented for newer releases are available locally.
  Extend the existing subscription CLI boundary; do not extract OAuth tokens.
- [TypeSafe introduction](https://docs.typesafe.ai/introduction) describes Jev's
  typed Choice, Score, and Noul judgments. Its
  [quickstart](https://docs.typesafe.ai/introduction/quickstart) documents
  `POST https://api.typesafe.ai/v1/systemone`, bearer authentication and typed
  questions. Proposed uses: advisory intent/urgency classification and confidence
  signals, evaluated in shadow mode first. Model output must not authorize edits.

## Verification ledger

| Check | Result |
|---|---|
| Original focused audio suite | 135 passed, 1 warning, 9.55 seconds |
| Original full discovery | Failed before execution: nested worktree manual script |
| Isolated full baseline | 6,523 passed, 2 failed, 4 skipped, 3 warnings in 1,006.43 s; see `baseline-pytest-isolated.txt` |
| Baseline repairs | Both failures repaired: Windows subprocess fixture and stale manifest. 13 targeted checks passed in 31.71 s; `baseline-repairs.txt` |
| New routing regression reproduction | 8 failed as expected; `audio-reproduction-before.txt` |
| Python syntax | 519 parsed, 0 errors; `python-syntax-audit.json` |
| Frontend TypeScript | Passed, `tsc --noEmit` |
| Installed Python dependency consistency | Passed, `pip check` |
| agent-reach update helper | Unavailable on PATH; conda fallback also unavailable. Jina and Exa retrieval worked |
| Phase 2 focused/adjacent regressions after patch | 239 passed, 1 warning in 22.36 s; `audio-regression-after.txt` |
| Hardware stream binding | Both PC/glasses opened at 24 kHz mono int16, silent write without underflow; `hardware-route-probe.json` |
| Acoustic correctness / sustained stability | Not yet verified |
| Bluetooth beep root cause | Unconfirmed |
| UI/browser/remote/provider live end-to-end | Not yet tested |

## Positive foundations and verdict

Preserve stable endpoint IDs, form-factor detection, task-store lifecycle,
thread-safe HUD events, approval checks, staged development verification,
speaker-fault containment, memory/log/vault test isolation, and existing media
automation. The strongest defects are at the boundaries between those pieces.

Verdict: targeted stabilization required. No broad rewrite or cleanup deletion
is justified. Phase 1 audit/baseline verification is complete, subject to the coverage
limits above. Phase 2 implementation has started against AUDIO_DESIGN.md. Later
phases remain unimplemented; no physical audio acceptance is claimed.
