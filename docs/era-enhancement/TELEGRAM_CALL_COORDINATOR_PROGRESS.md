# Call coordinator and HUD checkpoint — 2026-10-13

## Completed

core/telegram_call_coordinator.py now joins the verified native CallProcess and
GeminiCall adapters through CallAudio. One loop owns one owner-bound call and one
provider conversation. An explicit dial or incoming listener reserves a fresh
generation; overlapping calls and incomplete cleanup prevent replacement.

Gemini is prepared before dial/answer. Incoming listening does not open a provider
until the owner rings. Call audio, captions, completed command envelopes and tool
callbacks remain separate from local PC audio and conversation state. Stale native
events, provider captions and hangup commands cannot affect another generation.
No media/attention gates were added and local microphone mute is unchanged.

Cleanup tracks provider workers separately from the native process. Unsettled
provider cleanup blocks another call until it completes. Native teardown and process
reaping remain distinct from Telegram's remote discard confirmation. HUD shows
"Session stopped" when remote hangup is unconfirmed, never a false "Call ended".

app/telegram_call_runtime.py creates the service on Era's loop and safely degrades
if call settings cannot be read. main.py owns its shutdown alongside the existing
local session. Cross-thread HUD controls resolve the current engine and marshal
actions to that loop, including exact-generation Hang up. No current Era process
was restarted during this work.

The HUD bus stores replayable call status separately from the main transcript.
Instance IDs and revisions prevent an old engine's late events from replacing
the rebuilt engine's state. React's TelegramCallPanel receives WebSocket updates
and reconnect snapshots; it introduces no polling. Call owner, Receive calls and
Hang up are wired to backend controls. Disabled readiness is visibly explained;
call captions appear only in this panel. The production UI bundle was rebuilt.

## Important remaining gate

The runtime deliberately supplies no production remote provider/tool/speaker-policy
bindings yet. Even manually changing calls_enabled to true cannot bypass those
missing bindings. The coordinator is tested with injected provider/native/tool
boundaries, but the production call feature remains disabled. There is no runtime
conversation-only pilot pretending to support Era's tools.

Attaching EraLive's current mutable tool dispatcher to a second conversation would
mix local and remote intent, approvals and reply ownership. Complete an isolated
remote execution context using existing risk, confirmation, deduplication, jobs,
verification and memory policies before activation. Then bind the actual protected
Gemini client/config and optional configured biometric policy. No second EraLive
instance, shared physical queue or microphone-default change is acceptable.

## Verification

- Expanded call, Telegram, HUD, ownership and approval regression: 510 passed
  in 23.63 seconds; one existing Google SDK deprecation warning.
- Additional local session/Gemini/open-input regression: 114 passed in 8.48 seconds
  with the same SDK warning. Combined scoped runs: 624 tests passed.
- Coordinator/runtime coverage: 33 tests, 99.11%; isolated environment reports the
  existing SDK warning and missing pytest-timeout plugin configuration warning.
- Strict mypy, scoped Ruff/Black, compilation, whitespace checks, TypeScript
  checking and production Vite build passed.
- Doctor: zero errors, zero warnings, READY.

agent-browser verified the built UI against an isolated localhost fixture. Disabled
buttons were actually disabled; call captions rendered once in the call panel;
Hang up sent the displayed generation exactly. The fixture had no native transport,
Era engine, credentials or external platform access, so the simulated connected
state was a UI test only. Screenshots were visually inspected. Browser error check
returned no application errors. The dedicated browser session was closed and the
owned fixture stopped; Era and the user's browser were not restarted.

## Corrected failures and limits

Initial strict mypy findings concerned skipped-import return typing, mixed task
result types and optional teardown receipts; explicit types/narrowing fixed them.
TypeScript caught a nullable previous-status value, corrected before rebuilding.
Initial coverage was below 90%; failure/cleanup tests raised it to 99.11% without
excluding production paths. The expanded isolated coverage environment then lacked
NumPy for the WebSocket test; adding it to that temporary environment restored the
full pass. A transient uv cache rename warning recovered automatically.

Two browser automation clicks failed (PowerShell interpreted an unquoted @ref;
a text selector did not match). A fresh quoted ref succeeded and the fixture
recorded the exact hangup payload. Stopping the fixture produced expected
KeyboardInterrupt and a Windows connection-reset callback diagnostic; its process
exited. These did not occur in Era or the native call helper. Graph lookup returned
unrelated/history results and several guessed paths did not exist; targeted current
imports located WebSocketServer and the React HUD files. No source files were moved
or removed, and no production dependency environment was changed.

No native call, real provider conversation, account login replay, microphone
recording, device-route change or full global-suite rerun occurred. Actual PC
runtime startup/UI reload and real incoming/outgoing call acceptance remain pending.
Provider renewal/fallback, echo/quiet speech/latency, proactive calling and unanswered
fallback also remain open. Deferred local hearing checks, Phase 6 completion and
lost-schedule restoration were neither performed nor marked complete.
