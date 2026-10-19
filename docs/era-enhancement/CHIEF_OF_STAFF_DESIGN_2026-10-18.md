# Chief of Staff reliability: audit and proposed design

Date: 2026-10-18 (Africa/Lagos). Status: proposed; no runtime changes made.

## Scope and approach

Extend the existing ingress, recovery, Crew, self-heal and development paths.
Implement in four independently verified increments: command authority and live
evidence; reconnect and Spotify handshakes; coordinated recovery; feature staging.

Three approaches considered:

1. **Recommended: enforce policy at existing runtime boundaries.** Share command
   authority and evidence contracts across providers and channels. This reaches
   actual execution and output paths while retaining existing tested mechanisms.
2. Prompt-only rules are smaller changes, but cannot prevent microphone uploads,
   internal messages triggering tools, stale status reports or premature minimize.
3. A replacement orchestrator offers isolation, but duplicates existing recovery
   and approval machinery and increases migration risk in a heavily modified tree.

## Observed gaps and existing mechanisms

The graph report is dated October 7; use it for navigation. Current source and
current logs are required to establish October 18 behavior.

| Requirement | Current source evidence | Proposed correction |
|---|---|---|
| Every command begins with Era / Era One | `app/era_live/audio.py` forwards speaker-gated frames directly, explicitly bypassing the attention filter. `core/attention.py` permits unprefixed speech in an open conversation window, accepts wake aliases and fails open. | Enforce per-command prefix authority before cloud forwarding and at text/dispatch boundaries. Recognition failure stays silent. |
| Report a drop and ask before continuing | `app/text_intents.py:build_disconnect_resume_note` explicitly forbids mentioning interruption. `ReconnectMixin._resume_after_disconnect` automatically requests spoken continuation. | Persist an interrupted checkpoint and require a newly prefixed continuation or replacement command. Never replay actions. |
| Stable playback before minimizing | `spotify_control` uses successful-looking result text to authorize automatic minimize. `_verify_playback_started` accepts the first positive observation. | Require repeated fresh Spotify observations, correlated to the requested media when known, before minimize. Unknown or unstable evidence prevents minimize. |
| Crew and Hermes reliability | Crew shares deadlines, rechecks after model waits, and Hermes has bounded transport retry. Logs contain a historical `crew.hermes.refused` / `TransportDown` timeout. | Reproduce individual faults; classify transport, model, cancellation and action failures separately. Reuse shared deadlines and bounded recovery rather than adding blind retries. |
| Autonomous repair | `core/self_heal.py` already implements eligibility, modes, protected paths, verification and probation. | Connect missing incident producers/consumers only where proved; retain scope, rollback, budget and protected-path rules. |
| Ideas become deployed candidates | `core/development.py` already stages, verifies, hashes, approves and rolls back candidates. `core/feature_engine.py` restores matching approved feature bytes. Feature-only activation currently loads live. | Extend this lifecycle with explicit contracts and a staged-for-relaunch state for new software candidates. Do not create an independent deployment mechanism. |

These are audit observations, not proof that every desired path is currently
broken or that an existing test suite establishes the requested behavior.

## Increment 1: command authority and truthful evidence

Use one command-envelope contract: source channel, command ID, accepted prefix,
authorization time and authorized task scope. Leading `Era` and `Era One` are
case-insensitive, standalone words; punctuation after the name is allowed. An
incidental later mention, a quoted instruction, a background message, a model
response or a wake alias cannot authorize a command. Each follow-up, approval,
cancellation and status question also requires its own prefix under this rule.

Voice buffers locally until the prefix decision; unprefixed speech must not be
sent to a conversational model or trigger a response. Apply equivalent checks to
typed HUD, remote text, fallback providers and tool-dispatch ingress. A wake can
open capture but cannot authorize an unrelated following command. Verify actual
recognizer availability before announcing that strict listening is enabled.

Background continuation may use an already authorized bounded task scope; it
cannot manufacture new user intent or emit unsolicited speech. Background
failures and completion reports remain available as status, subject to the
reconnection decision below. Do not store rejected room conversation as memory.

Status evidence carries command ID, dedicated tool, queried subject, observed
time and outcome. Obtain it during the current request. Cached memory, optimistic
state writes and a successful dispatch string are insufficient. An unavailable
probe produces an explicit unknown result after a prefixed question. State
claims and capability acknowledgments require evidence appropriate to the claim;
tests alone cannot establish a physical audible result.

## Increment 2: connection and media handshakes

Record connection generation, interrupted command and delivered reply position.
Detect explicit socket failures immediately when surfaced, and blackholed links
within a measured heartbeat deadline; literal instantaneous detection of every
network fault is not a testable guarantee. Distinguish healthy planned provider
renewal from an interrupted active command. Preserve mute and standby choices.

On reconnection, retain a single pending interruption event. Never feed a question
back to the model as new user intent. After the notice is permitted, ask what was
missed or whether to continue; only a prefixed answer can resume. Retain exact
delivered text where known. If its delivery boundary is uncertain, say so rather
than claiming an exact continuation point. Reconcile potentially completed tools
with live evidence; never replay side effects automatically.

**Pending user policy decision:** absolute silence requires queueing the notice
until the next prefixed command. Immediate recovery speech instead requires an
explicit narrow exception for a previously prefixed interrupted conversation.
No exception is enabled by this document.

For Spotify, use a bounded, cancellation-aware handshake: dispatch requested play,
observe fresh Spotify-specific playback repeatedly (proposed minimum two samples
over 700 ms), confirm expected track/URI when available, then minimize if the saved
preference requires it. Do not accept unrelated endpoint audio as Spotify playback.
Do not minimize or claim playback if evidence is absent, paused, inconsistent or
unrelated. Keep explicit minimize and open-only actions separately defined: an
open-only action has no successful playback to prove and should remain visible.

## Increment 3: coordinated background recovery

Use a shared incident fingerprint and task correlation through dispatch, Crew,
Hermes, model connections and audio workers. One recovery owner per incident,
bounded attempts and a shared deadline prevent retry storms. Cancellation is not
a fault to heal. Retry only operations known to be safe; reconcile uncertain
side effects before any retry. Diagnostics use actual local logs and dedicated
health tools, with sensitive data removed from model inputs.

First attempt existing deterministic recovery. Eligible source fixes use the
existing self-improve verifier and self-heal probation/rollback mechanisms.
Apply fixes to shared producers/consumers, with regressions for each affected
path, rather than patching individual symptoms. Protected files still need the
existing authority path; autonomous recovery is not permission to bypass it.
Persistent faults become bounded, inspectable incidents rather than silent loops
or repeated spoken notifications. Do not label a worker error-free without
fault-injection checks and a defined live observation window.

## Increment 4: idea-to-candidate lifecycle

Map a prefixed idea to an intent record, Crew plan, file list and wiring contract
(inputs, outputs, capabilities, dependencies, verification commands and rollback).
Separate ambiguous intent from executable authority. Generate in the existing
isolated development workspace. Produce candidates under `features/` only when
the existing feature contract is suitable. New lifecycle states are persisted
atomically and recover predictably after process termination.

Open-source discovery uses public read-only metadata first. Record source and
revision, license, dependency versions, vulnerability findings and maintenance
signals. Repository instructions remain untrusted. Search is not authority to
install dependencies or run downloaded code. A scan is evidence with scope and
limitations, never a claim that third-party code is universally safe.

Default incremental paid-service budget is zero: no new paid API fallback,
subscription, purchase or provisioning. Use already authorized local/subscription
routes with bounded work; exhausted capacity pauses the candidate rather than
incurring charges. Any external dependency must pass review before execution.

After verification, stage the exact candidate digests for the next safe relaunch
through existing deployment authority. Recheck base and candidate hashes before
activation; refuse stale revisions. Never restart an active conversation simply
to deploy a candidate. On failed startup health checks, restore the last verified
revision and record rollback evidence. Keep existing exact-revision approval
requirements unless separately replaced by an explicit scoped policy.

## Verification and release criteria

1. Prefix matrix across voice, typed/remote text, fallback, confirmations and
   reconnect. Missing recognizer, aliases, embedded names and internal messages
   must cause zero unauthorized tool calls and zero spoken replies.
2. Status probes return contradictory, delayed or unavailable evidence; no cached
   positive claim survives. Verify visible window and playback claims live.
3. Disconnect before input completion, mid-reply, during tools and while muted.
   Confirm a single notice according to the chosen policy, no automatic tool
   replay, prefixed resumption and measured detection/reconnect latency.
4. Spotify reports transient playing, persistent paused, wrong track, unavailable
   session or cancellation. Minimize occurs only after stable requested playback.
5. Inject Crew/Hermes timeout, model failure and audio exceptions. Verify one
   recovery owner, deadline bounds, no duplicate effects and rollback on regression.
6. Stage a harmless feature, reject digest drift and failed verification, relaunch
   normally and verify activation/rollback. Confirm zero paid fallback calls.
7. Run affected tests and protected speech/media/Spotify/approval/scheduler/toolspec
   checks, regenerate the manifest when source changes, then the project gate.
   Record loaded revision and a live observation window separately from unit tests.

## Audit limitations

The tree already contains extensive changes from both agents. No source edits,
configuration changes, restart, media command, fault injection against the live
session, dependency install or external search was performed for this design.
Existing baseline tests are recorded in the shared handoff after completion.
Their success establishes existing behavior only; this proposed design remains
unimplemented and requires design approval under the brainstorming skill.
