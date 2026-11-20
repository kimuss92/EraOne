# Telegram call Gemini adapter — 2026-10-13

## Completed offline integration

The approved separate-conversation design now has a typed Gemini Live adapter in
core/telegram_call_gemini.py. It accepts an already verified CallAudio generation,
forwards 16 kHz remote PCM to the supplied Live session, and sends only 24 kHz
provider PCM to the call-owned output queue. It opens no local microphone or
speaker. The explicit connect_call entry point uses Era's managed connection
boundary with a caller-supplied protected client, model and configuration.
Importing the module does not connect, authenticate, enable calls or send messages.

Provider receive() ends after each reply. The adapter re-enters it across turns.
Call-local caption events carry immutable generation identity; they are separate
from the HUD's existing local transcript. Interruption clears queued speech and
invalidates in-flight native receipts. A bounded provider queue and native drain
deadlines prevent unlimited delayed speech. Successive replies wait for the prior
native queue to drain instead of clearing completed speech.

Tool callbacks receive immutable CallCommand objects with peer, call generation,
provider request ID and a completed Telegram-source InputEnvelope. ContextVar
ownership propagates to the dispatched coroutine. Partial new transcripts cannot
authorize actions using an earlier command. Tool calls are serialized, deduplicated
and bounded. No tools are advertised unless declarations are explicitly supplied;
the production callback must retain existing approval, execution, job and memory
policies. Without a callback, a tool request reports call_tools_unavailable.

Input policy is explicitly injected: Telegram account ownership is not biometric
voice verification. The production coordinator must honor the configured speaker
policy if enabled. This adapter introduces no media/attention gate and does not
consult or change local microphone mute or glasses routing.

## Failure handling and lessons

Tool timeouts report outcome unknown; cancellation does not prove that native or
background work stopped. Dispatch operations are tracked separately from their
wrapper tasks. Bounded shutdown reports worker_cleanup_incomplete if an operation
ignores cancellation. This was caught by a new regression and corrected before
the checkpoint. Provider failure invalidates only the matching call generation.
Local provider closure is never labeled as proof of remote Telegram hangup.

The first isolated coverage attempt failed during fixture setup because its
temporary environment lacked aiohttp. Adding the required test dependencies fixed
that environment; no production dependency or running Era environment changed.
Strict mypy then caught an Awaitable/create_task mismatch. The dispatch contract
now explicitly accepts an async coroutine callback; strict checking passes.

## Verification

- Focused audio/Gemini tests: 64 passed after the cleanup fix.
- Expanded call setup, audio, Gemini, Telegram, speech ownership and approval
  regressions: 323 passed in 16.11 seconds; one existing Google SDK deprecation warning.
- Gemini adapter coverage: 37 tests, 97.84%; isolated coverage had the existing SDK
  warning and a missing pytest-timeout plugin configuration warning.
- Scoped Black, Ruff, strict mypy, compilation and whitespace checks passed.
- Doctor: zero errors, zero warnings, READY.

No real Gemini Live conversation, native incoming/outgoing call, physical recording
or device-default change was performed. Doctor's provider reachability probe is
separate from conversation acceptance. The full global suite was not rerun.

## Still pending before activation

Calls remain disabled. This is a tested adapter, not a complete live calling feature.
The production coordinator must connect authenticated native signaling and IPC,
pace external output frames, validate incoming frame formats, flush native output
on interruption, and confirm exact-generation transport teardown. It must also bind
Era's actual tool/approval/memory policies and call events to HUD controls/captions.

Provider renewal currently ends the adapter with an explicit failure; seamless
renewal, fallback voices and remote echo behavior remain unverified. Never enable
calling solely because offline tests pass. Complete the transport/coordinator,
then conduct one attended call in each direction and verify speech, interruption,
latency, captions, hangup and preservation of the local glasses route.
