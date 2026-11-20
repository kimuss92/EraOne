# Phase 5 — standby and remote conversation

User authorized advancing on 2026-10-13 with Phase 4 hardware tests deferred.
The deferred acceptance register remains mandatory. Reuse existing voice,
approval, scheduler, and Telegram components; preserve working flows.

## First increment: intentional standby and completion wake

Persist an explicit auto-standby preference, default off. Use the existing
300-second idle timeout when enabled. Exact enable/disable utterances are
handled identically in text, native voice, and backup voice. A preference change
does not itself enter or exit standby. Disabled means the inactivity monitor
cannot enter standby. Processing, speech, an active local turn, pending
approval, or an independent hard mic-off prevents idle entry. Per subsequent
user instruction, voice mic-off can idle into standby when auto-standby is
enabled. Standby enables local wake detection while retaining cloud input mute;
an explicit wake returns the existing conversation behavior.

Reuse the existing standby resume and held completion report path. A CM/crew
completion during standby remains queued until the event-loop resume drains
it; schedule only one completion wake at a time. Scheduler reports always
mirror to Telegram, as already implemented; a completion report during standby
also requests wake. Preserve explicit mic-off state on automated resume, avoid
a redundant wake greeting. Microphone privacy controls input; a completion
report can still be spoken after resume while input remains muted. Keep results
visible and held through a reconnect.

Alternatives: keeping unconditional idle standby contradicts the user's toggle;
replacing the session controller creates unnecessary risk. A persisted policy
plus the existing monitor and resume is the chosen incremental design.

## Media and quiet-voice steering

The user reported playback being mistaken for them and requested improved soft
voice recognition. Reuse the existing enrolled profile and local speaker model
only while another application is making sound. Quiet-room input passes
immediately, without an inactivity/name requirement. The global voice-lock
preference remains unchanged; an enabled security gate remains authoritative.
The media convenience filter rejects a scored non-owner utterance and retains
unknown identity with an explicit diagnostic. It buffers and releases complete
owner speech. Leaving media mode preserves a pending request's onset.

Adapt the voiced-frame selector's old fixed .01 RMS cutoff to quiet speech, and
configure the utterance detector's lower energy floor only on confirmed
wearable input. Retain identity-match thresholds and enrolled samples; never
train the profile from unverified audio. Verification adds the existing
approximately 1.2-second voiced window during media, not during quiet-room
conversation. Short/unscorable speech and failed local verification remain
limits, so physical media rejection and soft owner speech are deferred tests.

## Remaining increments

### Telegram button increment (authorized by “proceed with next step”)

Use one process-wide adapter subscribed to the existing pending store, with a
worker for network delivery and the current Era engine provider for execution.
Each private-owner card has an unpredictable process-local token bound to its
pending ID, creation timestamp, and acknowledged message ID. Require matching
sender, private chat, message, token, and unexpired pending generation. Consume
the generation atomically, not an inferred spoken “yes”. Replayed, replaced,
expired, forwarded, or pre-restart cards cannot execute. Group approvals are
disabled; existing group text behavior is unchanged.

Run the stored action through the existing classifier, tool dispatcher, result
verification, and background-job continuation using a task-local authenticated
grant. Retain the voice/HUD approval path. Report receipt separately from the
execution outcome, including unverified results; do not retry ambiguous sends
or automatically rerun a failed action. Remove resolved buttons best effort.
Deliveries and decisions receive sanitized structured diagnostics. No credentials
or arbitrary tool payloads belong in these diagnostics.

An approved action may silently resume standby, using the existing resume path
while preserving microphone mute. A paused/reconnecting engine defers acceptance
and leaves the card retryable. HUD activity observes the actual call and outcome.
Cancellation after atomic consumption must resolve the parked job as interrupted,
without claiming completion or retrying the side effect.

Alternatives: synthetic Telegram “yes” could authorize the wrong slot; a second
approval store could drift. Reusing the atomic store with a scoped grant avoids
both. Verify fake transport, owner checks, expiry/replacement, voice/button races,
duplicate clicks, engine unavailability, blocked classification, verification,
and queue continuation before reporting this increment complete.

API contracts: https://core.telegram.org/bots/api#inlinekeyboardbutton,
https://core.telegram.org/bots/api#callbackquery, and
https://core.telegram.org/bots/api#answercallbackquery (checked 2026-10-13).

Extend existing Telegram authorization and pending-confirmation lifecycle with
owner-bound Approve/Cancel callbacks, expiry, single execution, sanitized
delivery logs, and stale/replayed-button rejection. Do not create a parallel
approval store or bypass existing action classification.

Evaluate hearing on a reproducible audio set and verify duplex behavior after
the deferred hardware test. Separate microphone controls from standby policy.

Native Telegram calls require a supported authenticated channel. The current
Bot API bridge supplies text and voice notes, not native phone calls. Calling
integration remains research/design work; do not emulate a call with messages
or claim native calling is complete.

## Verification and reporting

Test saved toggles, malformed config, exact-intent matching, all input hooks,
idle state/approval matrix, duplicate completion wakes, microphone privacy,
held reports, and disconnected sessions. Mock Telegram transport; do not send
real owner messages from this coding session. Phase 5 stays partial until its
remaining increments and explicit hardware acceptance are completed or the
user authorizes further deferral.
