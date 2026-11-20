# Phase 3 foundation design

Approved scope: strengthen the existing state, task and approval mechanisms.
The user reported no idle beeps after the audio patch and explicitly instructed
advancement. Audio watcher/profile/listening follow-ups remain tracked; this
does not claim every original audio requirement is complete.

## First increment: ownership at state and approval boundaries

Keep existing interfaces and locks. Copy structured state on ingress and
snapshot reads; copy approval parameters on ingress and approval records on
non-consuming reads and busy exceptions. Consuming confirmation transfers
ownership after atomically clearing the slot. No new approval authority or
change to expiration, cancellation or explicit user consent.

Alternatives: frozen wrapper types would change consumers; caller discipline
alone leaves the demonstrated mutation bugs. Defensive deep copies preserve
the dataclass/dictionary interface and nested values at modest allocation cost.
State payloads are bounded structured data; OS handles/clients must not become
state payloads. Existing tests explicitly documenting unsafe aliasing must be
updated to the new snapshot contract, not silently removed.

Verification: nested ingress/egress mutation, busy exception mutation, snapshot
stability after updates, concurrent confirmation consumption, existing state,
approval, HUD and executor tests. No real system actions in regression tests.

## Subsequent increments

Duplicate prevention compares goal text with only outer whitespace removed.
Case, internal whitespace, paths and code remain significant. The existing
condition lock covers checking and insertion together. Pending, running and
awaiting-approval records block duplicates; cancelled jobs also block while
their executor is still active. Terminal jobs can be requested again. Restored
pending records participate after restart. Live and executor callers report
the existing ID without changing priority or completion callbacks.

This is a single-queue/process contract, not distributed or semantic deduplication.
CM, Hermes and crew dispatch have separate ownership and remain a later team
integration task. Exact matching intentionally does not merge paraphrases.

Still verify the complete approval-expiration/completion-to-job relationship,
persisted transitions and event replay before declaring the whole phase complete.

## State change delivery

Add a monotonically increasing revision to each in-process snapshot and an
async condition-based wait for a newer revision. Updates coalesce into the
latest complete snapshot, so slow consumers do not accumulate an unbounded
event queue. Reads remain detached. Timeout and cancellation must release the
lock. Revisions belong to one StateStore instance; reconnecting consumers take
a fresh snapshot before waiting. This is the foundation API; wiring all OS
watchers and HUD consumers remains in the awareness/HUD phases.

## Approval lifecycle events

Emit approval lifecycle records with sequence, confirmation ID and reason when
stored, consumed, cancelled, replaced or expired. Deliver detached records to
subscribers after releasing the confirmation lock; a subscriber failure cannot
invalidate the approval decision. Bound-method subscribers are weak references
so stopped queues are not retained. Queue-owned approvals that expire, are
cancelled or are replaced become explicit terminal outcomes and notify through
the existing task reporting path. Consumption alone remains insufficient to
mark the multi-step job complete. Continuation must carry a checkpoint and the
approved step's actual result before any remaining steps run.

Continuation uses an internal typed checkpoint containing completed steps,
context observations, the held step, remaining steps and recovery counters.
It is kept process-local; restart retains the existing fail-without-replay policy.
Only a real consumed confirmation can publish its verified tool result, via a
per-call context binding. The queue matches that confirmation to its paused job.
A successful step queues the remaining plan under the same task ID; failed
approved steps terminate the job. A missing checkpoint fails explicitly rather
than inferring whole-job completion. No planner call or completed step is replayed
just because an approval arrived. Subsequent held steps create new approvals.
