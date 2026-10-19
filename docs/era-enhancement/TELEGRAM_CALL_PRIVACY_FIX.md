# Telegram call privacy diagnosis — 2026-10-14

## Observed failure

The HUD reported a native call failure, and the owner confirmed the personal
Telegram account never rang. A read-only Telegram GetFullUser query from Era's
authenticated account returned phone_calls_available=true,
phone_calls_private=true and blocked=false for the verified owner. Telegram
therefore reports a call privacy restriction between these accounts.

Independent checks authenticated the native helper, resolved the owner, prepared
the native external audio stream and reached the signaling boundary with the
outgoing request intercepted. A separate Gemini Live connection also opened and
closed successfully. No diagnostic probe placed a call or sent user audio.

## Changes

- Refresh the owner's call privacy metadata in each fresh helper.
- Block outgoing calls locally when Telegram reports them private; emit the
  specific call_privacy status. Owner-initiated incoming calls remain allowed.
- Map Telegram privacy rejection and answer timeout to safe actionable codes.
- Preserve specific native errors when the coordinator also sees a worker error.
- Explain privacy, authentication, expiry and cleanup failures in the HUD.
- Close the owned subprocess input pipe after reaping to avoid the observed
  Python 3.14 Windows pipe-finalizer warnings.

## Owner action and acceptance

On the personal Telegram account, open Settings → Privacy and Security → Calls.
Add Era's separate account (Eglantine Abdelkefi / @EglantineAbdelkefi) under
Always Allow. Do not add the owner's own username as the exception.

Restart Era and refresh the HUD after this code update. If the attended test
window has expired, enable it again with
`python -m tools.telegram_call_test_mode --enable` before restarting. Then use
Call owner and answer on the personal account. Verify two-way audio, interruption
and hangup; incoming calls use Receive calls first.

This change does not alter either account's Telegram privacy settings. Actual
ringing, remote audio and hangup remain unverified until the attended retry.
The prior exception text was discarded, so its exact SDK exception is unavailable;
the privacy restriction is independently verified, rather than inferred from an
exception message. Unknown native failures still use a generic safe code.

## Verification

184 call transport/coordinator/Gemini/tool regressions passed, including privacy
blocking before dialing, owner-initiated incoming calls, safe rejection codes and
preservation of specific errors. TypeScript compilation and the HUD production
build passed. Ruff, strict mypy and Black checks passed on the affected source.

The initial regression run exposed outdated fake-pipe cleanup methods; these were
updated to verify pipe closure. A native startup/cleanup probe then exposed a
remaining Python 3.14 Windows stdout transport finalizer warning. Closing only the
already-reaped child's transport resolved it; the repeated real probe authenticated,
started and reaped the helper without that warning. One existing google-genai
deprecation warning remains in pytest. The full global suite was not rerun.
