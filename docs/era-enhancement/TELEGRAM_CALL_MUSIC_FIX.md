# Phone tool execution repair — 2026-10-14

The owner confirmed two-way conversation now works, then requested music and
reported a subsequent Telegram "weak signal" warning. The real generation's
diagnostics show successful native input, Gemini input/output and native playback,
followed by a ValueError at the missing completed-transcript guard in _receive.
Spotify dispatch was therefore blocked before reaching the existing driver.

## Repair

- Freeze observed owner-channel transcription when Gemini requests a tool, even
  when its transcription completion flag has not arrived. Retain the first actual
  transcript receipt time for approval freshness; dispatch does not refresh it.
- Preserve context until same-message tools have captured it, then retire context
  at turn completion. New speech replaces the prior command rather than inheriting
  its approvals. Tool command envelopes remain immutable.
- When no observed user transcript exists, reject the action with a structured
  tool response asking for repetition. Keep the conversation alive. A model's
  unsolicited tool request is not accepted as a user instruction.
- Ignore native PCM that arrives after provider failure/hangup. Such a late frame
  must not replace the original failure with a protocol error.
- Preserve the original provider error if a later native error also arrives.
- Record tool-dispatch/missing-transcript milestones without logging speech.

The existing Spotify, ownership, cancellation, memory and confirmation policies
remain in use. Music requested through this tool plays on the PC; system media is
not mixed into the Telegram conversational audio stream.

## Verification

Regression cases cover a missing finished flag, same-message turn completion/tool
dispatch, original receipt timestamps, a fresh partial command replacing stale
context, safe rejection with a working follow-up, and late input after failure.
An integration test runs the phone adapter through RemoteTools and the real
headless executor to a mocked existing Spotify driver; the action is dispatched
once and the call remains alive. It does not prove actual hardware playback.

The existing no-transcript regression previously expected a whole-call failure;
it now verifies blocked execution and continued conversation. One patch initially
inserted a duplicate test signature; this was corrected before running checks.
Scoped import/format findings and patch-context mismatches were corrected.

Telegram's weak-signal message followed the observed conversation failure, but the
logs do not prove whether network quality contributed. Do not label it an internet
problem without evidence. After restarting Era, the attended retry should request
music on Spotify, verify PC playback, ask a follow-up, and confirm that the call
stays connected. Sustained media/network behavior remains a hardware check.
