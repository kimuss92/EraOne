# Daily incoming calls and conversational hangup

2026-10-14. Supersedes the manual initial Receive calls requirement and the
confirmed-end-only automatic rearm policy in TELEGRAM_CALL_VOICE_END_AND_RECEIVING.md.

The owner explicitly requested incoming calls while away from the PC. Daily call
consent is separate from measured duplex validation: enabling it does not mark
duplex_validated true or pretend that live acceptance tests have passed.

## Behavior

- Protected daily settings enable calls and automatic owner-only listening on Era
  startup. No 30-minute test window applies. No call is placed by configuration.
- After a call, listening returns only after the owned helper has been reaped.
  Local shutdown without a remote-discard receipt remains unconfirmed in logs;
  subsequent listening uses a fresh helper and generation.
- Temporary failures retry at 2, 4, 8, 16, 32 and at most 60 seconds. Authentication,
  identity, privacy, disabled access and test expiry do not loop. Cleanup retries
  target only the exact owned helper, never another process.
- Stop receiving persists the incoming opt-out across restarts. Receive calls
  restores it. An active conversation is preserved when future reception stops.
- Call owner can replace an idle listener with an outgoing call, then resume
  incoming listening after cleanup. Completion/schedule-triggered outbound calls
  are still outside this change.
- The PC must remain awake and online with Era running. One initial restart loads
  the new code; subsequent calls do not require a HUD click or test renewal.

## Voice hangup correction

The old handler required an exact full-transcript regex and a model-issued tool.
Conversational prefixes could fail validation and return a misleading instruction
to repeat the same words. Completed owner-channel transcriptions now directly end
the current connected call for immediate hangup requests and short goodbyes:
"end the call", "okay goodbye", "bye", and "thanks, Era, hang up now please".
Incomplete fragments do not trigger hangup. Negations, explanations, quoted
goodbyes, media titles, future requests and stale generations remain rejected.
The model tool retains matching owner/channel/generation checks and the same
intent recognition, with instructions never to demand a fixed magic phrase.

## Verification and limits

247 targeted transport, coordinator, Gemini, tool and diagnostic tests passed.
Scoped Ruff, Black, compilation and strict mypy passed. HUD TypeScript checking
and production build passed. The Google SDK emits an existing pytest deprecation
warning; the complete project-wide runner was not rerun for this change.

Daily mode was saved to the Windows-protected account store and read back as
enabled, automatically receiving, with the testing limit removed. Credential
values were neither printed nor changed. No live call or Era restart was performed
by the coding agent. Away-from-home incoming calls, natural goodbye hangup and
subsequent incoming-call rearm still require live owner acceptance after restart.

Local controls: `python -m tools.telegram_call_daily_mode --enable` or `--disable`.
These update protected configuration only; restarting Era applies them.
