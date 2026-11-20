# Era Fabric — one nervous system for Era, her tools, the crew and self-improvement

Status: **F2 BUILT** (2026-11-09, pulled ahead as decided in §12.4) and **F0 BUILT**
(2026-11-10). F1, F3 and F4 are still design only. Phased plan in §9; what shipped is
in §3.5 (F2) and §9.1 (F0).
Scope: `app/era_live/*`, `core/*` tool path, `crew/*`, `core/development.py`,
`actions/self_improve*.py`, `core/self_heal*.py`, `core/task_lessons.py`, the HUD.

---

## 1. Why — what actually breaks today

Era already has most of the right *parts*. What she lacks is a shared spine, so
every part invents its own context passing, health check, fallback and reporting.
Each recent incident is one of those seams failing:

| Incident | Seam that broke |
|---|---|
| Crew asked to review staged change `e46dc` answered "the change is missing" | **Context**: the crew got a sentence, not the artifact (patched by hand with `era_access`) |
| "I cannot generate the content of that change myself" when a crew review was asked | **Pathway**: one route (crew), no alternative when it couldn't act |
| Crew ask awaited inline → 1011 keepalive → session died; HUD showed failure, Era never said it | **Isolation**: long work on the receive loop; no durable report |
| Hermes `era_one` drifted to MoA → "no usable reply" on every ask | **Health**: no breaker; every ask paid the full timeout |
| Background dev run finished and sat silent | **Comms**: completion lived in a thread (fixed: `report_outbox`) |
| Tool failure spoken before recovery had a turn | **Comms**: announce-before-recover (fixed: `tool_recovery`) |
| Auto-report loop + false narrative | **Transparency**: prose trusted over the ledger |
| ddgs returning 0 results counted as success | **Fault taxonomy**: empty ≠ ok, but nothing said so |

The fixes so far are correct but **local**. Era Fabric generalises the six that
already work — `ToolResult`, crew `Transport` + `ERROR_TYPES`, `report_outbox`,
`tool_recovery`, `cancel_registry`, `task_trace` — into contracts every component
obeys, instead of building a new framework beside them.

**Design rule zero: FIX, don't rebuild** (Phase 0 rule). Every piece below wraps
or extends an existing module; nothing is replaced wholesale.

---

## 2. The shape

```
            ┌────────────────── Event Bus (typed, persisted to jsonl) ──────────────────┐
            │   task.*    tool.*    crew.*    dev.*    health.*    report.*              │
            └───▲────────────▲───────────▲────────────▲──────────────▲─────────────────┘
                │            │           │            │              │
 input ──▶ ┌────┴─────┐ ┌────┴────┐ ┌────┴─────┐ ┌────┴───────┐ ┌────┴────────┐
 voice     │   Era    │ │  Tools  │ │   Crew   │ │Development │ │ HUD + report│
 typed     │ (EraLive)│ │dispatch │ │ Hermes / │ │self-improve│ │ pump        │
 telegram  └────┬─────┘ └────┬────┘ │ in-proc  │ │ self-heal  │ └─────────────┘
 scheduler      │            │      └────┬─────┘ └────┬───────┘
                └────────────┴──── TaskContext (by reference) ─┘
                                     │
               Pathway Router ── Health Registry (breakers) ── Fault taxonomy
                                     │
                     Safety gates (unchanged, never bypassed)
```

Six small modules in a new `core/fabric/` package (each < 500 lines):

| Module | Generalises | Job |
|---|---|---|
| `faults.py` | crew `ERROR_TYPES`, `looks_like_failure` | One closed fault taxonomy for every component |
| `boundary.py` | crew "never raise" contract | Decorator: exceptions/timeouts → typed faults; nothing crosses a boundary raw |
| `health.py` | `tool_health`, `live_health`, `Transport.health()`, `omniroute.is_up`, quota cooldown | One registry + circuit breakers |
| `context.py` | `Turn.context`, `task_trace`, `era_access` | `TaskContext`: shared, by-reference work item |
| `pathways.py` | TTS router, `quota_failover`, crew backup LLM | Declarative ordered alternatives per capability |
| `events.py` | `task_trace` jsonl, `report_outbox`, HUD pushes | Typed pub/sub, persisted, durable delivery |

---

## 3. Shared context — `TaskContext`

### 3.1 Problem
Every hop re-derives context from a sentence. Era tells the crew "review the
change"; the crew must guess *which* change, *which* files, *what the verifier
said*. That is the `e46dc` bug class, and the root of the "I can't" reply.

### 3.2 Contract
One work item per user request, created at the input edge, passed **by id**,
never copied as prose.

```python
@dataclass
class TaskContext:
    ctx_id: str                      # "ctx_20261106_163501_ab12"
    origin: str                      # voice | typed | telegram | scheduler | crew:<bot> | self_heal
    utterance: str                   # the user's own words — grounding for the gates
    intent: str                      # short label, e.g. "review_dev_change"
    artifacts: list[ArtifactRef]     # what this task is ABOUT
    steps: list[StepRef]             # one per tool/crew/dev step: id, component, outcome summary
    parent: str | None = None        # crew handoffs / sub-tasks link back
    deadline: float = 0.0            # monotonic ceiling, inherited by every step
    cancel_token: str | None = None  # core.cancel_registry token id

@dataclass(frozen=True)
class ArtifactRef:
    kind: str     # dev_job | diag_report | vault_note | file | tab | media | tool_result
    ref: str      # job id, diag id, vault-relative path, project-relative path …
    sha: str = "" # content hash when referenced — a reader can detect staleness
```

### 3.3 Rules
1. **Reference, not payload.** Artifacts resolve only through existing read doors:
   `era_access.read_development`, `crew.diagnosis.read`, `vault_bridge.read`.
   No new permission is created — a bot without `development.read` still cannot
   read a dev job, even when its id is in the context.
2. **Context is untrusted data to the crew.** Rendered through crew
   `runtime._fence` exactly like a vault note. An id is never an instruction.
3. **Big tool outputs become artifacts.** A `ToolResult` above ~2 KB is stored as a
   `tool_result` artifact (`runtime/fabric/results/<ctx>/<step>.json`); only its
   summary + ref travels. Kills context bloat; the crew reads the full output on demand.
4. **Persistence = append-only jsonl** in `logs/fabric/ctx-YYYYMMDD.jsonl`, next to
   `task_trace`. `scripts/task_log.py` gains `--ctx <id>`. No database.
5. **Grounding stays with the gates.** `utterance` is what `_note_user_utterance`
   already records; the fabric carries it, it does not replace that gate.

### 3.4 Where it plugs in
- `crew.transport.Turn.context` carries `ctx_id` + `artifacts` (already a dict — no
  contract change).
- `core.development.propose` / `report_finished` attach `ArtifactRef(kind="dev_job")`.
- `task_lessons.observe` keys lessons by `intent` + step outcomes from the context
  instead of re-parsing traces.

### 3.5 What F2 shipped (2026-11-09)
- `core/fabric/context.py`: `TaskContext`, `ArtifactRef`, `new_context` (explicit
  artifacts first, then ones discovered in the owner's words + task), `add_step`,
  `load`, `dev_job_ref`, `render_for_crew`. Kinds built: `dev_job`, `diag_report`,
  `file`. Discovery: a dev-id prefix needs 5+ hex with a digit AND a letter; "the
  change" with no id implies the newest live candidate, flagged `implicit`;
  credentials and dotfiles are refused through `era_access.resolve`.
- Crew: `run_tool(..., user_text=)` opens a context per ask; the prompt gets an
  ARTIFACTS block naming the capability that reads each one (or saying honestly the
  bot lacks it). Hermes inherits it (same `_user()`); handoffs pass it to the child.
- Development: `propose` opens the job's context and stores `ctx_id` on the job;
  `report_finished` adds the end state as a step on that same context.
- `python scripts/task_log.py --ctx <id>` prints a context, its artifacts and steps.
- **Deviations from this section:** `task_lessons` is NOT re-keyed by intent. It
  already sees every tool call (crew asks included) through `task_trace`, and
  re-keying changes what Era learns; that moves to F4 with the bus. Rule 3 (big tool
  outputs as `tool_result` artifacts) is also deferred to F4. `deadline` and
  `cancel_token` are not on `TaskContext` yet: they belong with `boundary` (F0).
- Tests: `tools/test_fabric_context.py`.

---

## 4. Fault taxonomy — one language for "it didn't work"

Generalise crew `ERROR_TYPES` into a core set, stored in `ToolResult.data["fault"]`
so the dataclass, the tool-declaration golden file and every caller stay unchanged.

| `fault.kind` | Meaning | Retry same? | Try alternative? | Example |
|---|---|---|---|---|
| `Transient` | blip, may work now | yes (bounded) | after retries | 503, socket reset |
| `Unavailable` | component down | no | **yes** | Hermes 8642 refused, OmniRoute down |
| `Exhausted` | quota/budget spent | no (cooldown) | **yes** | Gemini 429 / quota 1011 |
| `Timeout` | deadline hit | no | yes, if time left | crew step budget |
| `EmptyResult` | ran fine, nothing useful | no | **yes** | ddgs 0 hits, "no usable reply" |
| `BadOutput` | unparseable / unverifiable | once | yes | crew non-JSON |
| `NotFound` | target genuinely absent | no | no | path, tab, track |
| `Denied` | a gate said no | **never** | **never** | capability denied, speaker gate |
| `NeedsConfirmation` | waiting on the user | no | no | staged replace, dev activation |
| `BadInput` | caller error | no | no | missing arg |
| `Cancelled` | user / abort stopped it | no | no | abort mission |
| `Internal` | our bug | no | yes | traceback → also feeds self_heal |

```python
fault = {"kind": "Unavailable", "component": "crew.hermes", "retryable": False,
         "alternative": True, "detail": "connection refused 127.0.0.1:8642"}
```

**Rules**
- `Denied`, `NeedsConfirmation`, `Cancelled` are **terminal**: no pathway may route
  around them. This is the line between *graceful degradation* and *gate bypass*.
- Crew `ERROR_TYPES` map 1:1 in one table in `faults.py`
  (`TransportDown→Unavailable`, `LLMUnavailable→Unavailable`, `StepLimit→Timeout`,
  `CapabilityDenied→Denied`, `ConfirmationRequired→NeedsConfirmation`, …).
- `looks_like_failure` stays the single judge of *prose*; `fault` is the judge of
  *structure*. When both exist, structure wins.

---

## 5. Fault tolerance — bulkheads, breakers, pathways

### 5.1 Boundary decorator (bulkhead)
Every adapter that crosses a component line — a tool family, a crew transport, a
model adapter in `core/llm/adapters/`, a network call — is wrapped once:

```python
@boundary(component="crew.hermes", timeout_s=90, fail_mode="open", long=True)
async def ask(turn: Turn) -> ToolResult: ...
```

It guarantees:
- no exception escapes (→ `Internal` fault; traceback goes to `self_heal` incidents);
- the `TaskContext` deadline is enforced through `cancel_registry`;
- blocking work runs via `cancel_registry.offload`, **never on the Live receive loop**;
- one `tool.*` / `crew.*` event per call, with duration and outcome;
- the health registry is told the result.

**Long work is never awaited inline.** Anything that can exceed ~3 s (crew ask,
dev propose/verify, CM dispatch, web research) follows the pattern the crew fix and
the CM lane already use: *ack now → run in background → result event → report
outbox*. `long=True` makes `boundary` return immediately with
`ToolResult.ok("Started — I'll report back", data={"ctx_id": …})`.

### 5.2 Health registry + circuit breakers
One registry, fed by signals that already exist — no new probes unless needed:

| Component | Signal source today |
|---|---|
| `live.gemini` | `core.live_health.LiveHealth` verdict, quota cooldown |
| `crew.hermes` / `crew.inproc` | `Transport.health()`, `crew.reliability.snapshot()` |
| `gateway.omniroute` | `core.omniroute.is_up()` (16–80 s cold start ⇒ HALF_OPEN, never OPEN) |
| `llm.<alias>` | `core.llm.quota_failover`, adapter faults |
| `tts.fish` / `tts.gemini` / `tts.kokoro` | `tts_router` outcomes |
| `tool.<family>` | `core.tool_health` begin/finish, `boundary` outcomes |
| `vault` | `vault_bridge.VaultLockUnavailable` rate |
| `browser.cdp` | stale-session tell (2–4 ms "VERIFIED" tab list) |

Per component: `CLOSED → OPEN` after N faults of kind
`Unavailable / Timeout / Exhausted / EmptyResult` in a window; `OPEN` skips the
component instantly (no timeout paid); after a cool-down one probe goes through
(`HALF_OPEN`); success closes it. `Denied / NotFound / BadInput` never trip a
breaker — they are not health problems. `Exhausted` uses the quota's own reset time.

### 5.3 Pathways — declared alternatives
A capability lists ordered providers. The router tries the first healthy one,
reads the fault, and moves on only when `fault.alternative` is true.

```yaml
# config/fabric/pathways.yaml   (data, like config/crew/*.yaml)
review_change:            # "have the crew review this improvement"
  - crew.hermes:   {bot: cody}
  - crew.inproc:   {bot: cody}                # same BotSpec, same gates, local loop
  - llm.claude:    {mode: review_readonly}    # Era reviews it herself, no tools
  - deterministic: {fn: dev_summary}          # diff stats + verifier result, no LLM
web_search:
  - tool.web_search.ddgs
  - llm.gemini:    {grounded_search: true}
  - deterministic: {fn: say_unavailable}
speak:
  - tts.fish
  - tts.gemini
  - tts.kokoro
voice_brain:
  - live.gemini
  - live.claude_voice                         # app/era_live/claude_voice.py
  - local.loop                                # app/era_live/local_fallback.py
```

**Pathway invariants**
1. **Equal-or-stricter permissions.** A fallback may never hold a capability its
   predecessor lacked. `crew.inproc` runs the same `BotSpec`; `llm.claude` in review
   mode sees read-only artifacts and has no tools. The loader rejects a widening
   pathway (pinned by a test, like `TestAddingABot`).
2. **The last step is always deterministic** — no network, no LLM, an honest answer
   ("the crew and Claude are both unreachable; here is what the verifier found").
   "I can't" is never the output of a working system.
3. **Hermes keeps provider routing.** For the crew the fabric chooses the *transport*
   (Hermes vs in-process). *Which model* answers inside Hermes stays Hermes' job
   ("Era classifies, Hermes routes"). The fabric does not reach in.
4. **Safety-critical paths fail closed** (§5.4) and have **no** pathway.
5. Every hop is recorded on the `TaskContext`, so the final report can say what was used.

### 5.4 Fail-open vs fail-closed — explicit per component

| Fail **closed** (stop, tell the user) | Fail **open** (degrade, carry on) |
|---|---|
| owner voice / speaker gate | app-state pre-command gate |
| `InputSafetyGate`, `safety_guard` | attention-window name check |
| pending confirmation / approvals | crew review (→ next pathway) |
| development **activation** | web search, vision, TTS tier |
| vault **writes** (lock busy → queue, never force) | vault reads, caches |
| Protocol One, panic-stop latch | HUD, report rendering |

`boundary(fail_mode=...)` has **no default**, so every new adapter makes this choice
on purpose.

---

## 6. Unified communication layer

### 6.1 Event bus
In-process `asyncio` pub/sub with thread-safe publish (workers live in executors).
Every event is appended to `logs/fabric/events-YYYYMMDD.jsonl` **before** fan-out,
so a crash never loses the record.

```python
@dataclass(frozen=True)
class Event:
    topic: str            # "crew.result", "dev.verified", "health.open", "report.owed"
    ctx_id: str | None
    component: str        # who emitted it
    outcome: str          # ok | fault | progress | info
    summary: str          # one human line, ≤ 200 chars, already redacted
    fault: dict | None = None
    provenance: dict = field(default_factory=dict)  # provider, model, fallback_from, verified
    ts: float = field(default_factory=time.time)
```

**Topics:** `task.{created,step,done}` · `tool.{start,result}` ·
`crew.{asked,step,result}` ·
`dev.{proposed,verified,failed,activated,rolled_back,rejected,cancelled}` ·
`health.{open,half_open,closed}` · `report.{owed,delivered,requeued}`.

**Subscribers — existing code, re-pointed**

| Subscriber | Listens to | Replaces |
|---|---|---|
| `report_outbox.enqueue` | `*.done`, `dev.*`, `crew.result`, `health.open` for a component the user waits on | scattered enqueue calls |
| `task_trace` | `tool.result`, `task.done` | direct `trace()` calls (kept as a shim) |
| `task_lessons.observe` | `task.done` with faults | trace re-parsing |
| `self_heal` incidents | `fault.kind == Internal` + traceback | log scraping |
| HUD websocket | everything, rate-limited | per-feature HUD pushes |
| `capability_check` | `health.*` | static self-model |

Each subscriber is isolated: one that throws is logged as `Internal` and the others
still receive the event.

### 6.2 Delivery guarantees
- **To the user: spoken exactly once, attempted until it is** — via `report_outbox`
  + `report_pump` (already built). The bus only *feeds* the outbox; it never speaks.
- **Between components: at-most-once in memory, durable in the log.** Missed events
  are recovered from jsonl on restart (the `development.reconcile_reports` pattern,
  generalised).
- **Ordering per `ctx_id`**, not global.
- **Backpressure:** the HUD subscriber drops `progress` events under load, never
  `fault` or `done`.

### 6.3 Request/response over the bus
Crew asks and dev reviews become *commands with a correlation id*:
`crew.asked(ctx_id)` → … → `crew.result(ctx_id)`. Era's tool call returns at
`asked`; the result reaches the user through the outbox. This is the general form
of the 2026-10-06 crew-blocks-receive-loop fix.

### 6.4 Transparency rules ("trust the ledger", enforced)
1. **Era speaks from events, not from prose.** A completion report is rendered from
   `task.done` + its `provenance`, not from what a model *said* it did.
2. **Degradation is disclosed in one clause:** "The crew was unreachable, so I
   reviewed it with Claude — tests pass, two style notes." Built from
   `provenance.fallback_from`.
3. **Unknown is a state.** As `tool_health` already says, stale ≠ cancelled; a step
   with no outcome is reported as "unconfirmed", never as success.
4. **Health is part of the self-model.** "What can you do right now?" reads the
   registry: "Voice and tools are fine; the crew is down, I'll handle reviews myself."
5. **Redaction at emit time** using the `era_access._redact` rules — the event log
   and the HUD never carry a credential.

---

## 7. End to end — "improve X, and have the crew review it before I approve"

```
user: "Era, improve the spotify search, have the crew review it before I approve"
  │
  ├─ TaskContext ctx_A (origin=voice, utterance=…, intent=self_improve+review)
  ├─ self_improve → development.propose                 [boundary long=True]
  │     Era: "Working on it — I'll tell you when it's staged and reviewed."
  │     … dev.proposed(job=e46dc) → dev.verified(41/41 tests)
  │     ctx_A.artifacts += dev_job:e46dc (sha)
  ├─ pathway review_change(ctx_A)
  │     crew.hermes → breaker OPEN (drifted at 08:10) → skipped, 0 s
  │     crew.inproc → cody reads dev_job:e46dc via development.read → verdict
  │     crew.result(ctx_A, provenance={transport: inproc, fallback_from: hermes})
  ├─ report.owed(ctx_A) → outbox → spoken at the next quiet moment:
  │     "The spotify change is staged and verified, 41 of 41 tests. Cody reviewed it
  │      on the local loop because Hermes was down: one concern — the retry has no
  │      ceiling. Approve, or should I fix that first?"
  └─ user "yes" → PendingConfirmation → development.activate → probation
        self_heal watches; a regression → dev.rolled_back → reported
```

Nothing here can approve itself: the crew stays read-only, activation is the user's
own "yes" through `PendingConfirmationStore`, and every fallback had equal or
narrower rights than the step it replaced.

---

## 8. Failure matrix — the system with one link broken

| Broken link | Detected by | What happens instead | What the user hears |
|---|---|---|---|
| Gemini Live deaf / 1011 | `live_health` | key rotation → `claude_voice` → local loop | "Switching to my backup voice." |
| Gemini quota, all keys | `Exhausted` + reset time | Claude haiku via OmniRoute (text); voice → local | once, not every turn |
| OmniRoute down / cold | `is_up`; HALF_OPEN during cold start | `ensure_running` in background; direct `claude -p` with gateway env stripped | nothing unless both fail |
| Hermes gateway down / drifted | breaker on `crew.hermes` | in-process crew transport | disclosed in the review report |
| Whole crew unusable | both crew breakers OPEN | Era self-review → deterministic dev summary | "The crew is offline; here's my own review." |
| Web search empty | `EmptyResult` | grounded Gemini search → say unavailable | honest "couldn't find it" |
| Fish TTS fails | tts fault | Gemini audio → Kokoro | nothing |
| Vault lock busy | `VaultLockUnavailable` | reads continue; writes queued + retried, never forced | "Queued — it'll land shortly." |
| Dev verifier crashes | `Internal` on `dev.verify` | job = *unverified*, cannot be activated | "Staged but NOT verified — I won't apply it." |
| Activated change regresses | `self_heal` probation | automatic rollback | "I rolled the change back; here's why." |
| Browser CDP stale | stale-session tell | reattach once, then report | only if reattach fails |
| Session drops mid-report | `report_pump` confirm | requeue in outbox | report arrives after reconnect |
| A bus subscriber throws | subscriber isolation | others still receive; `Internal` logged | nothing |
| Fabric itself fails to load | startup guard | Era runs on today's direct paths | nothing; diag report flags it |

The last row matters: **the fabric is itself a link that can break**, so it is
strictly additive. Every call site keeps its current behaviour when `core.fabric` is
missing or disabled (`ERA_FABRIC=0`), the same way `ERA_CREW_TRANSPORT` works.

---

## 9. Rollout — each phase shippable, each behind a flag

| Phase | Ships | Behaviour change | Acceptance |
|---|---|---|---|
| **F0 contracts** ✅ built 2026-11-10 | `faults.py`, `boundary.py`, `events.py` (log-only) | none | unit tests; events jsonl appears; ruff + full pytest green |
| **F1 health** ✅ built 2026-11-10 | `health.py` on existing signals; HUD panel; `capability_check` reads it | OPEN breakers skip dead components | chaos: Hermes refused → crew ask answers in < 1 s via in-proc (pinned in tests; live owed) |
| **F2 context** ✅ built 2026-11-09 | `TaskContext` in crew ask, dev propose/report (task_lessons → F4, see §3.5) | crew sees artifacts by ref | replay "review staged change X" → crew quotes the diff, never "missing" (pinned in tests; live replay owed) |
| **F3 pathways** ✅ built 2026-11-10 | `pathways.yaml` for `review_change`, `web_search`, `speak`, `voice_brain` | ordered fallbacks + deterministic floor | chaos matrix (§10) green for the wired pathways (`tools/test_fabric_chaos.py`); live owed |
| **F4 comms** | outbox / HUD / self_heal / lessons subscribe via the bus; completion reports rendered from events | reports from events, not prose | `era_observe` shows zero silent completions over a live day |

### 9.1 What F0 shipped (2026-11-10)
- `core/fabric/faults.py`: the 12 kinds of §4; every crew `ERROR_TYPES` mapped 1:1
  (pinned by a test); exact names, then suffix rules (`*Refused`/`*Denied` → Denied,
  `*Required`/`*Busy` → NeedsConfirmation, `*Quota*`/`*RateLimit*` → Exhausted …);
  exceptions classify by class. An unclassified failure is `Internal`, never success.
  `make()` forces terminal kinds to `alternative: False` whatever the caller asks.
- `core/fabric/events.py`: closed topic set (`dev.rejected` / `dev.cancelled` added
  for the two end states the doc lacked), persisted to
  `<log_dir>/fabric/events-YYYYMMDD.jsonl` before fan-out, redacted at emit,
  subscriber isolation, `bind_ctx()` carries the `ctx_id`, `ERA_FABRIC=0` turns it off.
- `core/fabric/boundary.py`: the decorator of §5.1, sync and async, `fail_mode` with
  no default, `long=True` = ack + background task + result event. Internal faults go to
  `log_crash` (the record self_heal reads). **Wraps no live call site yet** (that is
  F1/F3); a sync function cannot be pre-empted, so for it `timeout_s` is measured
  only (`provenance.overran`).
- Live, log-only emitters: every Era tool call → `tool.result` (from
  `core/task_trace`, the same seam that feeds lessons); crew ask → `crew.asked` +
  `crew.result` on the ask's ctx; Development → `dev.proposed` and its end state.
  Nothing subscribes in production yet.
- Tests: `tools/test_fabric_f0.py`.

### 9.2 What F1 shipped (2026-11-10)
- `core/fabric/health.py`: one breaker per component, `CLOSED → OPEN → HALF_OPEN`.
  Only `faults.BREAKER_KINDS` count; gates and caller errors never trip one. `allow()`
  is the only skip check, and in F1 only two callers use it, both of which already had
  a fallback: the crew transport and the OmniRoute gateway. Thresholds (3 / 2 min,
  60 s, OmniRoute 120 s) confirmed by Karim 2026-11-10, in `POLICY`.
  OmniRoute **can** open: an open breaker sends `claude_cli` straight to the
  subscription accounts it already fell back to, so a cold start costs nothing extra.
- Fed by: `tool.result`, `crew.step`, `crew.result` (subscribed by `install()`, run at
  startup in `main.py`), plus one `health.note()` line each in `quota_failover._latch`
  (`llm.gemini`, Exhausted with the latch's reset time), `LiveHealth`
  (`live.gemini`), `tts_router.stream` (`tts.fish`), `vault_bridge`'s lock-busy branch
  (`vault`, Transient: shown, never trips) and OmniRoute `try_first`
  (`gateway.omniroute`). **Not fed yet:** `tts.kokoro`, `browser.cdp`, `tool_health`'s
  stale state (`tool.*` come from the events instead).
- `crew/fallback.py`: Hermes breaker OPEN → skipped without a call; Hermes refused
  **before** the turn (`TransportDown` / `MissingCredential`) → the same turn runs
  in-process with the same `BotSpec`s. A mid-turn Hermes failure is **not** re-run
  (steps may have run); a terminal fault never is. Disclosed in one clause plus
  `data.provenance` (`fallback_from: hermes_agent`).
- `capability_check` (`summary` / `check`) appends a LIVE HEALTH paragraph only when
  something is not closed; a healthy answer is byte-for-byte as before.
- HUD: Diagnostics drawer → COMPONENT HEALTH, from `/api/diagnostics` `fabricHealth`.
- Cost, stated: until the breaker opens, each refused ask still pays Hermes' probe
  retries (3 × 0.6 s pause ≈ 1.2 s) before falling back. From the 4th ask, 0 s.
- Tests: `tools/test_fabric_f1.py`.

### 9.3 What F3 shipped (2026-11-10)
- `config/fabric/pathways.yaml` + `core/fabric/pathways.py`. The loader **refuses** the
  whole file on any broken rule: a widening scope, a non-deterministic last step, a
  fail-closed component as a pathway or a step, an unknown quality or wiring, or a
  provider listed twice. The runner skips OPEN breakers without calling them. A
  terminal fault ends the pathway. Cancellation propagates. Every hop is a
  `task.step` event (plus a TaskContext step). `data.provenance` keeps an inner
  provenance as `via`.
- **Disclosure (Karim, 2026-11-10: only when quality differs):** a `quality: lower`
  step that answers after a failure gets its `say` clause; `same` steps are silent.
  The F1 crew-transport note stays: the local loop answers with a different model.
- **review_change** (`crew/review_pathway.py`, only for asks whose context carries a
  dev job): crew (F1 transport fallback) → Claude via `claude_cli` (Karim's choice;
  read-only, handed the candidate as text, budget-checked, refused under pytest) →
  a deterministic verifier summary (+/- lines per file, checks, last verifier line).
  When both crew breakers are OPEN, the crew step is skipped at 0 s.
- **web_search** (`actions/web_search.py`): the same Gemini → DDG → honest-empty
  chain as before, now with breakers. A spent Gemini quota is skipped instead of
  paid for on every search. `ERA_FABRIC=0`, or an unusable pathways file, runs the
  old chain unchanged (`_legacy_search`).
- **speak / voice_brain** are `wired: existing`: declared and pinned to the code that
  already implements them (`tts_router`, `claude_voice`, `local_fallback`). **Not**
  re-routed: those are the live voice hot paths, and they already fall back.
- Chaos rows covered by tests: Hermes down, whole crew down, crew and Claude down,
  a held review, both breakers open, Gemini search down, spent quota, nothing found,
  plus the scope and gate rules. The other §8 rows (Live 1011, quota failover,
  OmniRoute, Fish, vault lock, verifier crash, CDP, session drops) keep their
  existing mechanisms and suites; they are not pathways in F3.

F0/F1 carry no user-visible risk. F2 fixes the bug hit this week (crew review of an
improvement) and can be pulled ahead of F1 if wanted.

---

## 10. Verification — chaos tests, not hope

`tools/test_fabric_chaos.py`: one parametrised case per row of §8. Each
monkeypatches exactly one component to raise / time out / return empty / report
quota, runs the user-level request, and asserts:

1. a reply is produced — no exception reaches the caller;
2. the reply discloses the degradation (provenance present);
3. **no gate was bypassed** — a `Denied` / `NeedsConfirmation` fault is never followed
   by another provider (asserted on the event log);
4. the breaker opened and the next call paid no timeout;
5. the event log for the `ctx_id` is complete and ordered.

Plus a scope test: every pathway in `pathways.yaml` is checked for
equal-or-stricter permissions; a widening pathway fails the suite.

Live acceptance (after the tests): stop the Hermes gateway and ask Era for a crew
review of a real staged change; stop OmniRoute and ask a text question; pull the
network for 30 s during a dev run. Judge it with
`python tools/era_observe.py --since last-start`, not with the reports.

---

## 11. Constraints this design respects (do not undo)

- **Golden file:** no new tool declarations in F0–F3. If F4 changes one, regenerate
  `tools/golden/tool_declarations_anthropic.json` deliberately.
- **Action-name collisions:** no generic names (`list`, `restart`, `status`); check
  `action_registry` and `safety_guard` for every new name.
- **Mixins never `from main import`;** `core/fabric` imports nothing from `app/`.
- **Crew never imports `lane_router`** (pinned by `test_crew_registry`).
- **Tests never touch the live Era lock, the real vault or the live browser** — reuse
  the conftest guards; the chaos suite uses fakes only.
- **Degrade, never raise** (CM invariant) becomes the global contract, not a new one.
- **Two approval gates, the user's own "yes"** — the fabric carries approvals, never
  grants them.
- Files < 500 lines; ruff line length 100; `python -m pytest tools/ -q` green per phase.

---

## 12. Open decisions for Karim

1. ~~**Self-review fallback**~~ **Decided 2026-11-10:** Claude via `claude_cli`.
2. ~~**Disclosure verbosity**~~ **Decided 2026-11-10:** only when quality could differ
   (`quality: lower` in `pathways.yaml`).
3. ~~**Breaker thresholds**~~ **Decided 2026-11-10:** 3 faults / 2 min → OPEN, 60 s
   cool-down, OmniRoute 120 s to cover its cold start (in `health.POLICY`).
4. **Order:** land F2 (shared context for the crew-review flow) before F1?
