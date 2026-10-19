# CodeMode — Claude subscription voice bridge

2026-10-15: the concrete Phase 8 review proposal is [CODEMODE_DESIGN.md](CODEMODE_DESIGN.md).
This early brainstorm remains historical context; use the new design for approval.

Status: research/design proposal only. No CodeMode implementation is authorized
by this document. The directive requires review before implementation.
Provider choice confirmed by the user on 2026-10-11: Claude Code with an existing
Claude subscription. This document is an early Phase 1 feasibility deliverable;
the Phase 8 gate must revisit it against the then-current architecture.

## User experience

“Enter CodeMode” activates it explicitly; “exit CodeMode” returns to ordinary
Era conversation. Coding intent can prompt a suggestion but cannot activate the
mode silently. HUD shows a persistent mode indicator and the current state:
listening, clarifying, gathering context, Claude responding, awaiting approval,
testing, or completed/failed. Era gives a brief audible entry/exit acknowledgment.

The user can interrupt speech, correct the request, or cancel the current coding
turn. Interrupting speech stops queued audio first. Cancellation of a remote
turn is separate and must be acknowledged; do not call a running process
cancelled merely because the microphone captured “stop”. A late result retains
its session/turn identity and cannot execute or speak in a newer turn.

## Integration alternatives

1. **Extend the existing subscription CLI adapter (recommended).** Use the
   installed official Claude Code process, its own authenticated configuration,
   structured output, and explicit session IDs. Keeps credential handling inside
   Claude Code and fits Era's current subprocess boundaries. Process startup and
   interruption on Windows require measured tests.
2. **Official Agent SDK after an authentication compatibility check.** Better
   structured session controls may simplify duplex interaction. Do not assume a
   subscription login is permitted for every SDK embedding scenario or switch
   to API billing without the user's choice.
3. **Desktop automation.** Rejected as the primary transport: screen state and
   delivery acknowledgment are fragile, and the user explicitly requested an
   appropriate authenticated integration.

Official references: [authentication](https://code.claude.com/docs/en/authentication),
[programmatic execution](https://code.claude.com/docs/en/headless), and
[CLI reference](https://code.claude.com/docs/en/cli-reference).
Local version at audit: 2.1.289. Probe local flags and pin tested compatibility.

Use Claude's login; never copy OAuth tokens into Era configuration. The existing
adapter already separates subscription calls from gateway API environment
variables. Preserve that explicit boundary. Authentication expiry produces a
clear reauthentication request; 429/outage handling uses bounded backoff and a
visible paused state. No silent provider or billing substitution.

## Conversation and RTC enrichment

Proposed flow:

1. Voice/UI/Telegram input becomes an authenticated command envelope carrying
   session ID, turn ID, source, timestamp, and cancellation context.
2. Era distinguishes a question, diagnosis, patch request, test request, and
   approval. Ambiguous intent is clarified before dispatch.
3. Hermes retrieves relevant graph nodes and checks graph freshness against the
   current checkout. Read current snippets only where necessary to validate
   context. Include the current uncommitted diff without overwriting it.
4. Build an RTC prompt: Role (coding adviser), Task (desired behavior and acceptance
   tests), Context (selected files/snippets, dependency edges, current revision,
   constraints, previous failures, and evidence).
5. Claude returns structured events. Era validates cited paths and proposed API
   references, streams suitable explanatory text to the mouth and HUD, and
   keeps patch details visible rather than reading raw diffs aloud.
6. Proposed changes become staged development jobs with explicit diffs/tests and
   approval. The existing development promotion path applies accepted changes.

Context selection uses a token budget: direct symbols and their callers/tests
first; one-hop dependencies next; unrelated files excluded. Snippets carry
path, line range, content hash and checkout identity. Retrieved notes and tool
results are data, never authority to grant tools. Do not send credentials,
private logs, or unrelated documents as automatic coding context.

## Team responsibilities

Era owns the user's conversation and approval state. Hermes owns routing,
context collection, task decomposition, progress and handoffs. Cody handles
implementation/testing expertise; Maya, CM and other crew provide domain
knowledge as appropriate. Contributors can read and edit the full project in a
coordinated workspace. Their edits produce reviewable patches; full capability
does not mean concurrent writes to the running checkout without reconciliation.

One job registry prevents duplicate work across voice, HUD and Telegram. Each
job has an owner, participants, base revision, change set, test results, and
terminal outcome. Conflicting files are serialized or explicitly reconciled.
Persist validated outcomes in shared memory with the contributing evidence.

## HUD arrangement

- Main pane: user/Era/Claude conversation with live captions and clear speaker attribution.
- Context pane: selected files, revision/freshness, dependency summary, risks.
- Work pane: Hermes/crew activity, current job, test output and artifacts.
- Review pane: exact diff, tests, Apply/Cancel, approval expiry, conflict warnings.

Reuse the existing conversation, activity, task progress, approval and
development components. Every control dispatches through the same core command
boundary; no independent UI-owned task state.

## Safety and confirmation

Claude's advisory session starts with mutating tools unavailable, not merely
auto-approval disabled. Existing adapter uses `--tools ""` as well as its
approval flags; preserve the distinction. If a later approved design grants
tools in an isolated workspace, document the actual isolation boundary.

Approval binds the exact change digest, base file hashes, job, user, expiry and
test evidence. Voice confirmation is accepted only for the currently presented
proposal and authorized source. Telegram buttons identify that same proposal.
Changes to files or the proposal invalidate approval. A stale “yes” cannot apply
a newer patch. Tests and rollback readiness precede live promotion.

Log selected context, prompt, response, tool actions, interruptions, approvals,
test results and final outcomes with appropriate secret redaction. Store
root-cause uncertainty honestly and allow the user to inspect the full record.

## Phased implementation and acceptance

After design approval and prerequisite phases:

1. Explicit mode state, HUD/audio indication, entry/exit/cancel tests.
2. Graph-grounded context and RTC builder; stale graph, missing file and ambiguous
   request tests; read-only Claude session with auth/rate-limit/outage tests.
3. Incremental response playback/captions; interruption, late output, process
   cleanup and session-resume tests on Windows.
4. Hermes/crew workspace contributions; duplicate submission and conflict tests.
5. Staged patch review/promotion; expired/replayed/denied approval, changed base,
   failed verification and rollback tests.

Acceptance is an actual voice request, follow-up interruption, correct relevant
context, streamed reply, reviewed patch, tests, explicit approval and verified
outcome. A one-shot subprocess demo alone does not satisfy CodeMode.
