# Telegram call-account setup — 2026-10-13

Status: native dependency imports and protected local setup verified; account
authentication and live duplex calling pending. User explicitly resumed the
deferred call step after supplying distinct international Era and owner numbers.
Phone numbers are stored only in a DPAPI-protected user-local payload, not here.

## New feasibility evidence

Current primary-source/PyPI checks found py-tgcalls 3.0.0, ntgcalls 3.0.3 and
Telethon 1.45.0. NTgCalls publishes a CPython 3.14 Windows amd64 wheel. Real
native imports passed in the optional environment. Private-call signaling and
external-frame interfaces exist in inspected sources/runtime. This revises the
earlier third-party feasibility evidence; it does not prove working bidirectional
PCM or accept the production media architecture. Native calls still require a
user-account session, independently of the existing Bot API message bridge.

Sources: [Telegram requestCall](https://core.telegram.org/method/phone.requestCall),
[NTgCalls](https://github.com/pytgcalls/ntgcalls),
[PyTgCalls private connection implementation](https://github.com/pytgcalls/pytgcalls/blob/master/pytgcalls/methods/internal/connect_call.py),
[PyTgCalls external frames](https://github.com/pytgcalls/pytgcalls/tree/master/example/external_capture),
[NTgCalls package metadata](https://pypi.org/pypi/ntgcalls/json),
[PyTgCalls package metadata](https://pypi.org/pypi/py-tgcalls/json).

The pinned optional environment is outside OneDrive at
LOCALAPPDATA/EraOne/telegram-calls/.venv; requirements.telegram-calls.txt records
the direct dependencies. Era's running environment was not changed. Visual Studio
BuildTools installations were found using vswhere despite absent compiler PATH
entries; no native compiler installation/build was required for wheel import.

## Local setup

tools/setup_telegram_calls.py delegates to the isolated environment.
core/telegram_call_setup.py prompts locally for application credentials from
https://my.telegram.org/apps, then a login code/two-step password if requested.
API hash, code and password use hidden prompts; no secrets are passed in command
arguments or requested in this chat. A visible interactive PowerShell setup window
was started and its Python child confirmed running, without inspecting its input.

Telethon uses an in-memory StringSession; the complete saved configuration is
DPAPI sealed. No plaintext SQLite session is created. The private folder ACL
grants the current Windows user and SYSTEM. Setup checks Era's authenticated
phone/non-bot identity, then binds the supplied owner phone and the existing bot
owner ID when available. Only that explicitly supplied owner is imported if
needed for peer resolution; no chats or contacts are enumerated. No personal
Telegram Desktop session is imported. Exception text that could contain secrets
is replaced by its error type in terminal status.

Setup leaves calls_enabled false, including after successful authentication.
Authentication is not media readiness. No Telegram authentication, message,
native call, media capture or voice delivery was verified at this checkpoint.
The phone numbers identify intended accounts; remote account ownership is not
asserted before authenticated read-back. The existing bot bridge remains intact.

## Verification and initial failures

- 96 focused setup/Telegram/warning/runner tests passed, with one existing Google
  SDK deprecation warning. Fake clients never connect to Telegram.
- Setup coverage: 26 tests, 95.83% statement coverage. The initial isolated coverage
  environment lacked aiohttp required by the shared bootstrap; adding it repaired
  the setup errors. The passing coverage run has two config warnings for absent
  asyncio/timeout plugins; the normal focused suite passes.
- Ruff, Black, strict mypy for the two new source modules, compilation and
  whitespace checks passed. An exception-chaining lint finding was fixed.
  Black/mypy were absent in the main environment; isolated uv tool runs supplied
  them without changing Era's dependencies.
- Doctor: zero errors/zero warnings, READY. No full global-suite rerun occurred.
- Native library import and Windows-protected configuration write/read passed.
  Transient uv cache rename errors recovered during dependency installation.

The setup tests were added to the existing confirmation gate. Production API
credentials/session remain protected outside the repository; all test numbers,
sessions and authentication responses are synthetic. Agent-reach CLI update
check remains unavailable on PATH, as previously recorded. Documentation patch
context mismatch was corrected without partially applying that patch.

## Remaining work

Finish local user authentication. Prove bounded external PCM through the chosen
helper before accepting the revised media choice. Implement owner-only signaling,
owned Gemini call input/output, generation-safe cleanup and Hang up/HUD state.
Then coordinate a real inbound and outbound duplex call and opt-in proactive
notification tests. Both directions, echo/interruption and glasses isolation
remain untested. No native-call completion or Phase 5 live acceptance is claimed.
Phase 6 remains partial; this is the user's explicitly resumed Phase 5 call step.

## Post-login verification failure follow-up

User screenshot shows Telegram accepted a login, followed by a generic ValueError.
Protected status inspection confirmed supplied phone settings and application
credentials are present; no session was saved and calls are disabled. The exact
verification condition cannot be recovered from the previous redacted error;
neither an account display name nor a successful login proves owner binding.

Added controlled non-secret diagnostic codes for wrong Era account, wrong owner
ID/phone, bot/self-call and unresolved owner. A verified Era login is now protected
and checkpointed before owner lookup, while owner_bound and calls_enabled stay
false. Wrong-account sessions are never checkpointed. If phone lookup/import cannot
resolve the supplied owner, the local wizard offers an explicit personal username;
hidden-phone usernames require the independently configured bot owner ID and must
match it. No privacy setting or identity guard is weakened.

Verification: 88 setup/Telegram regressions passed (existing SDK warning); 31 setup
tests reached 96% coverage, with the previously recorded isolated plugin warnings.
Ruff, Black, strict mypy, compilation and whitespace passed. Initial lint rejected
a lambda assignment; it was replaced with a typed function. A test expecting only
two writes was updated to assert the new intermediate unbound checkpoint and final
owner-bound state. No real authentication replay or call was performed by the agent.
The corrected local wizard was reopened for user-controlled authentication.

### Failed owner hint persisted — repair

User's next screenshot showed SELF_CALL before a prompt. The account checkpoint
had copied an unverified owner username, so retries reused that failed candidate.
Unbound writes now omit owner usernames/IDs/access hashes; verified account session
and the independent bot-owner expectation remain. Final successful owner binding
can persist the validated username. Updated the local encrypted candidate to the
personal username explicitly supplied by the user; this still requires Telegram
identity read-back and never enables calls. Reopened the corrected wizard.

89 setup/Telegram regressions passed (existing SDK warning); new regression proves
a failed self-call candidate is not retained in the reusable account checkpoint.
Ruff, Black, strict mypy, compile and whitespace passed. No call was placed, and
owner authentication is not claimed before the local wizard's successful read-back.

## Verified account and offline audio foundation

User confirmed successful authentication/owner binding in the local window.
Protected local status read-back agrees; calls are still disabled. User authorized
the next step. Added owner/generation/reply-bound PCM queues, packetization, tail
drain, interruption and bounded backpressure handling. Native external sources at
16 kHz input/24 kHz output and synthetic frame submission/endpoint teardown passed.

Natural native process exit and self-exit both hung. A supervised disposable helper
now reports endpoint teardown and is reaped by its owning parent at a bounded
deadline. This bypasses the native shutdown issue; natural library finalization is
not fixed. See TELEGRAM_CALL_AUDIO_FOUNDATION.md for evidence and limitations.

Expanded ownership/speech regression run passed 148 tests, followed by the final
boolean-sequence regression run: 149 passed in 9.20 seconds, with the existing SDK
deprecation warning. Audio-specific coverage reached 98.86% before
that added case. Ruff, Black, strict mypy, compile and whitespace passed. Doctor
zero errors/zero warnings. No real call, Gemini connection, recording, hardware
route change or full global rerun occurred. Gemini/runtime/IPC/HUD integration and
both-direction live acceptance remain pending; no native-call completion is claimed.

2026-10-13 next checkpoint: dedicated Gemini call adapter and explicit managed
provider entry point implemented and verified offline. Expanded regressions:
323 passed; adapter coverage 97.84%; Doctor zero errors/zero warnings. Calls remain
disabled pending native signaling/IPC, production policy/HUD wiring and attended
duplex acceptance. See TELEGRAM_CALL_GEMINI_PROGRESS.md for corrected test and
typing failures, bounded cleanup behavior and exact remaining work.

2026-10-13 transport checkpoint: owner-bound native helper, private IPC and owned
supervisor implemented. Actual disabled helper exits/reaps without network access;
no configuration or authentication was changed. Installed SDK PCM descriptions and
frame.frame field verified. Expanded regressions: 403 passed; transport coverage
96.02%. Calls remain disabled pending production coordinator/policy/HUD wiring
and attended live acceptance. See TELEGRAM_CALL_TRANSPORT_PROGRESS.md.

2026-10-13 coordinator/HUD checkpoint: generation-owned call lifecycle, runtime
service boundary, live HUD status/captions and exact-generation controls implemented.
Scoped verification: 624 tests across two runs; coordinator/runtime coverage 99.11%;
UI build/type checking and Doctor passed. Browser fixture verified disabled controls
and exact hangup payload without real calls. Production remote policy/provider
bindings and attended acceptance remain pending; calls remain disabled. See
TELEGRAM_CALL_COORDINATOR_PROGRESS.md for the complete checkpoint.

2026-10-13 execution binding checkpoint: separate protected Gemini client, independent
speaker buffers and 27 real headless-executor tools are bound, with generation-owned
approvals, replay protection, cancellation accounting and machine admission. Scoped
regressions and Doctor pass after correcting nested registry-wrapper detection.
The account remains disabled and no call/test window was started. Explicit attended
test-mode tooling is prepared; see TELEGRAM_CALL_TOOL_BINDING_PROGRESS.md.
