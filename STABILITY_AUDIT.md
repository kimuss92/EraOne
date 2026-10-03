# EraOne stability audit

Input: `EraOne-main.zip`, archive revision
`b3ac3555c5aecbea8b093bf25ea5152f348a693d`. Audit date: 2026-10-03.

## Outcome

Implemented concrete startup, response, and reconnect repairs. This is a
tested stability update, not a claim that every feature is operational on a
Windows PC without its keys, model files, applications, or enrollment. No real
cloud response, microphone, speaker, or Windows desktop was available here.

## Findings and repairs

| Finding | Repair |
| --- | --- |
| A local provider exception stayed on its daemon thread; the consumer could receive an apparently empty response. | Forward a normalized error to the turn and clean up the active request. |
| A provider that produced no events could leave the brain waiting indefinitely. | Enforce a total per-pass deadline and observe cancellation even without new tokens. |
| Unbounded streaming queues could accumulate work during slow playback. | Bound the model-to-loop queue and local synthesis queue, with backpressure and cancellation cleanup. |
| Normal exit from `TaskGroup` waits for its workers; a receive task blocked on the socket could prevent requested session renewal. | Explicitly cancel workers on renewal and await their teardown; regression-test the actual engine loop with fake transports. |
| Reconnects allocated SDK clients without explicit transport ownership. | Bound connection setup to 30 seconds and close both async and sync transports on exit. |
| Graceful renewal did not pass through the exception-only continuity capture. | Capture interrupted-turn state after graceful worker teardown too. |
| Missing/unplugged microphone errors propagated through the session TaskGroup. | Retry the input device with capped backoff, keeping the cloud connection and typed input available. |
| Audio callbacks could target a replacement session's queue or overflow the wake queue. | Capture the original outgoing queue and use a bounded enqueue callback. |
| A Live receiver ending without messages could spin immediately. | Raise a reconnectable connection error instead of spinning. |
| Local model and TTS failure together could lose even the error reply. | Return a visible text reply when error-message synthesis fails; await producer cleanup. |
| Many cross-thread coroutine futures were discarded. | Observe failures and publish a HUD/log error; close coroutines rejected by a stopped loop. |
| `get_api_key()` ignored an environment key that Live could use. | Share credential discovery, ignore copied placeholders, accept BOM JSON, and reject non-object config safely. |
| Credentials and the Live model were effectively fixed for the running engine. | Re-read credentials on reconnect and resolve the voice model from the registry or `ERA_LIVE_MODEL`. |
| Import order could choose conflicting COM apartment settings. | Set the COM model before Windows automation imports, matching the existing test bootstrap. Windows runtime verification remains required. |
| A malformed/nonfinite cooldown override could crash initialization or cause pathological retry timing. | Validate timing overrides and fall back to a finite bounded default. |
| Voice verification was enabled with no enrollment/model in the supplied ZIP. | Add explicit startup and Doctor guidance while preserving voice verification. |
| The runtime manifest was absent; repair tooling raised `SystemExit` when loading it. | Regenerate both manifest artifacts, include the runtime manifest, and use an ordinary catchable missing-file exception. |
| Required `rapidfuzz` and `PyYAML` were absent from installation lists; Windows instructions recommended older Python. | Add missing dependencies, correct Python guidance, include optional local voice dependencies, and provide setup/start scripts. |
| Invalid byte paths could be mistaken for junk writes. | Leave undecodable paths out of junk-file quarantine. |
| Windows system paths could be treated as relative POSIX names during validation. | Preserve Windows path semantics in both path validation and the action safety classifier. |
| HUD settings could fail if the Windows `APPDATA` variable was unavailable. | Report unavailable autostart status instead of crashing ordinary settings requests. |
| Three edit tests used the Python 3.13-only `Path.read_text(newline=...)` despite a Python 3.12 target. | Use a context-managed reader with explicit newline handling. |

## Validation

Validation details and final test results are recorded in
`VALIDATION_RESULTS.md`. The focused tests exercise simulated cloud transport
failures, absent microphones, provider stalls/exceptions, cancellation, queue
backpressure, credential loading, and protected-path handling. They do not
prove live provider authorization, audio quality, or hardware compatibility.

The HUD was rebuilt with its existing npm lockfile. Both the Vite production
build and TypeScript check passed. Core version constraints reflect the packages
exercised here; they are not a complete Windows dependency lockfile.

## What still requires the target machine

- A valid Gemini key and an available Live audio model for that account.
- A functioning microphone/output device and voice enrollment when verification is enabled.
- Separate STT/TTS weights and an Ollama model for offline fallback.
- Real application/account setup for Spotify, Claude, Telegram, Gmail, and publishing.
- Windows COM, UI automation, endpoint switching, and a sustained live voice session.

Existing tests include Windows-specific assumptions and some old assertions
that do not match current implementation behavior. They are reported rather
than rewritten to manufacture a fully green suite.

## Archive contents

The updated source, tests, rebuilt HUD, setup instructions, constraints, and
generated manifest are included. Generated caches/logs, `node_modules`, old
Git metadata, and dated phase/`.bak-*` source snapshots are excluded from the
release archive. The original uploaded ZIP remains the rollback reference.
Existing source/configuration and memory data are retained otherwise.
