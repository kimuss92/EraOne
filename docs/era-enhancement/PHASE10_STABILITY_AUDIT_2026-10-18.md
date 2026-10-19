# Phase 10: existing autonomy, guarded learning and integration readiness

2026-10-18. User authorized continuing all phases and supplying physical/behavior
feedback later. This audit reuses existing implementations; software verification
does not close deferred microphone, glasses or exact live-approval acceptance.

## Existing mechanisms and the verified boundary

| Requirement | Current implementation / actual caller | Acceptance to preserve |
|---|---|---|
| User preferences and corrections | `memory/memory_context.py`; capture in `app/era_live/tool_dispatch.py`; existing long-term facts and vault | Sensitive text excluded from journal; bounded context; forgetting removes journal references; recorded context grants no new tool authority |
| Pattern learning | `core/self_learning.py::format_learning_for_prompt`, called by tool-dispatch prompt construction | Bounded correction/pattern digest; historical text explicitly cannot skip confirmation, send messages or authorize destructive acts |
| Task outcomes | `agent/task_queue.py` calls `memory_context.record_outcome` | Counters describe observed outcomes; success/failure delivery uses existing task and spoken-outcome paths |
| Brand learning | `core/cm_learning.py`, existing brief/vault maintenance | User's own words, brand isolation and bounded lessons; authored vault remains authoritative |
| Model/tool recovery | Existing reconnect/input buffer, `core/tool_health.py`, read-only task recovery and `crew/reliability.py` | Cancellation and original deadline remain authoritative; no replay of ambiguous mutation/consent |
| Source changes | `core/development_submission.py`, `core/development.py`, CodeMode verifier | Observed base hashes, bounded files/tests, exact reviewed candidate and version-bound approval; no implicit patch activation |
| Self-healing | `core/self_heal.py`, started by `main.py`; scoped source eligibility, verifier and probation rollback | Exclude private/state/backup/test/authority paths; only clean verification passes retained; probation rollback checks actual file state |
| Process recovery | `era_supervisor.py` and `core/era_relaunch.py` | Single owner, crash-loop limits, event-loop heartbeat, graceful relaunch and explicit stop respected |

The stored self-heal mode was **auto** during this audit. It was inspected, not
enabled or widened by this session. Existing policy allows verified repairs to
eligible modules in that mode; do not describe all source changes as manually
approved. Development/CodeMode approval authority remains separate. Any future
policy change needs an explicit user choice and must retain rollback.

The Oct 18 stability pass repairs crew deadline classification after model wait:
an expired shared call budget is StepLimit, not LLMUnavailable. Late output
cannot proceed to a capability or final result. Existing cancellation remains.

## Jev integration decision

Current primary docs checked October 18:
[TypeSafe introduction](https://docs.typesafe.ai/introduction) and
[quickstart](https://docs.typesafe.ai/introduction/quickstart).
Jev offers typed judgments; the documented hosted integration requires an API
key. `TYPESAFE_API_KEY` is absent from this diagnostic environment. This audit
did not search secret stores, create an account, install a skill/SDK or send
private Era context to TypeSafe. A configured key in another environment is
not inferred from this observation.

**Decision:** Jev is an optional unfinished Phase 10 dependency, not required for
the existing local learning, approval or recovery behavior. Keep it inactive.
If connected later, review a separate advisory-only design: bounded redacted
input, background/shadow evaluation, strict timeout and local fallback, and no
permission to approve tools, widen grants or apply source. Measure accuracy and
latency against existing routing before enabling any user-facing decision.
No provider result may replace PendingConfirmationStore or authorize an edit.

## Verification and remaining work

The full Windows regression/readiness run is the acceptance source for current
local software. Relevant existing suites include self-heal, self-improve verifier,
development, autopilot, CM knowledge, memory, input/recovery and crew tests.
Final commands/results are recorded in `STABILITY_RELEASE_2026-10-18.md`:
36 readiness gates passed; full Windows run 7,971 passed / four skipped;
subsequent microphone default-identity guard passed 334 focused/protected cases.
The last guard followed the full run, with source-delta evidence retained.

No duplicated memory system or learning executor was added. Multi-day recall,
physical listening and a user-reviewed live change/rollback remain feedback
checks. Jev cannot be called verified or Phase 10 fully complete before its
separate configuration, design and integration acceptance.
