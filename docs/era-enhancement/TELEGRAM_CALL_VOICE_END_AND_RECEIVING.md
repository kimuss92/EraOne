# Voice hangup and incoming-call listening — 2026-10-14

The owner confirmed outgoing calls work and asked how to end them by voice and
why calling Era did not receive an answer. Read-only live HUD state showed the
previous call had ended with no active listener. The existing Receive calls action
armed one incoming generation only; native incoming calls were not answered by
default or while Era had no call helper.

## Changes

- Advertise a call-local end_telegram_call tool alongside the existing 27 PC tools.
  The coordinator handles it directly, rather than sending it to a PC task handler.
- Validate that the request is an explicit immediate hangup instruction in the
  observed Telegram transcript, from the current owner and generation, with no
  model-supplied target arguments. Negation, explanations, future requests and
  stopping music do not end the conversation. Examples: "Era, end the call",
  "hang up", "can you hang up now", "please end this call".
- Use the existing owned hangup/cleanup path. Success means hangup was requested;
  remote call termination is still confirmed separately by Telegram.
- Retain pending owner transcript fragments when barge-in arrives between them,
  while invalidating completed approval context. An interruption must not erase
  the beginning of "end the call" and require the user to repeat it.
- Receive calls now stays armed in the running Era process. After a confirmed
  remote end and completed cleanup, rearm with a fresh generation/helper. Never
  reuse the old native handle or old speech context. Failed or unconfirmed endings
  do not trigger an automatic reconnect loop.
- If receiving pauses on a failure or unconfirmed end, Resume receiving permits
  an explicit retry once cleanup is complete; Stop receiving remains available.
- Add Stop receiving. It stops the waiting listener/future answers but preserves
  an already connected conversation. Global Era shutdown disarms receiving.
- Enforce the test expiry: no rearm/network listener when testing is disabled.
  Explicit HUD test-window renewal resumes a previously armed listener.

## Usage and verification

One initial restart installs the new Python handler and updated HUD. In a call,
say "Era, end the call". To call Era from the personal account, press Receive
calls and wait for Waiting for your call, then call Era's separate account
(@EglantineAbdelkefi), not the Telegram bot. Owner-only account checks remain in
force. Stop receiving disables that opt-in; active-call Hang up remains separate.

Tests cover voice hangup through the actual Gemini adapter, rejected requests
without caller authority or explicit immediate intent, fresh listener generations
after remote hangup, ignored stale events, stopping receiving during waiting and
an active conversation, and expiry/explicit renewal. The original 210 call
regressions passed before new cases. The expanded call run passed 222 tests before
the final two expiry/active-conversation cases. Strict mypy found an Any-return
annotation at the imported dispatch boundary; an explicit protocol cast corrected
it. Scoped import/format findings were corrected. No real call was placed by the
agent. The owner's incoming-call/voice-hangup acceptance is still pending.

Final UI TypeScript and production build passed. The two expiry/active-conversation
cases and the added fragmented-barge-in hangup regression passed in focused runs;
the final complete call regression result is recorded below. Existing google-genai
emits one pytest deprecation warning. The complete project-wide suite was not rerun.

Final complete call regressions: 225 passed in 16.56 seconds. Strict mypy on the
coordinator, adapter and runtime passed. Scoped Ruff/Black and compilation passed;
the final HUD TypeScript/production rebuild passed, including Resume receiving.
