# Native Telegram call transport checkpoint — 2026-10-13

## Completed

The approved disposable-helper design now has three source modules:

- core/telegram_call_protocol.py: bounded, versioned JSON with strict generation,
  owner, reply and frame validation. PCM travels only over inherited private pipes;
  no network IPC listener, session string, API hash or access hash crosses them.
- core/telegram_call_helper.py: optional native process, reuse of the sealed local
  session without interactive login, account/owner revalidation, owner-only incoming
  notifications, explicit dial/answer, external mono 24 kHz microphone and 16 kHz
  speaker sources, paced output frames, controlled error codes and teardown receipts.
- core/telegram_call_process.py: hidden child launch, isolated log directory,
  CallAudio input/output queues, exact packet receipts, bounded cleanup and reaping
  through the owned process handle. No PID search or unrelated process termination.

The helper handles one generation once. Unknown callers are refused; known incoming
calls are never automatically answered by SDK play(). An incoming event for the
owner during an active generation terminates that helper rather than treating a
second call as the first one. This is necessary because public SDK callbacks expose
peer IDs rather than exact call IDs. Live collision behavior still needs acceptance.

The installed SDK was checked directly. Its decoded Frame uses frame.frame,
not frame.data. tools/probe_telegram_call_sdk.py verifies that actual field and
the external stream descriptions without constructing NTgCalls, connecting an
account, recording a device or placing a call. Both checks passed.

Output accepts one 20 ms frame in flight and paces native submission. Old reply
frames/receipts cannot account for new replies. Already submitted remote speech
cannot be undone by clearing a local queue; audible interruption/echo behavior
must still be measured. Submission is not a remote playback receipt.

The known native-finalizer hang remains contained, not fixed. Teardown attempts
call leave/disconnect before emitting a structured receipt; the supervisor then
reaps only its owned process. A successful leave request is distinct from observing
Telegram's discard event. Missing or failed teardown stays cleanup_incomplete.
Process exit also releases the supervisor's wait when no receipt can arrive.

## Verification and corrected failures

- Expanded call, Telegram, speech ownership and approval regressions: 403 passed
  in 23.99 seconds; one existing Google SDK deprecation warning.
- Transport tests: 80 passed; aggregate statement coverage 96.02% (helper 97%,
  supervisor 93%, protocol 98%). The isolated environment reported its known
  missing pytest-timeout configuration warning.
- Actual isolated SDK descriptions/Frame probe passed with network_opened=false.
- Actual protected configuration + disabled child guard passed: calls_enabled=false,
  no network opened, owned process reaped. Credentials were neither changed nor
  printed, and calling remains disabled.
- Scoped Black, Ruff, strict mypy, compilation and whitespace checks passed.
- Doctor: zero errors, zero warnings, READY.

The first coverage pass failed its 90% target (69.43%) because the SDK-boundary and
child paths had not yet been exercised. Added network-free SDK/stdio/auth-failure
tests raised coverage above the target; no production paths were excluded.
Strict mypy initially rejected the untyped SDK decorator. Explicit callback
registration fixed it. Ruff found an import ordering issue, corrected before the
final checks. A shell-quoted SDK diagnostic failed to parse before execution;
the checked-in probe replaced it and passed. One patch failed its context check
without changing files, then was reapplied against the formatted source.

Graph lookup returned historical HUD backups. Two guessed current HUD paths and
two guessed SDK internal method paths did not exist; targeted imports/source search
located the actual files. No deletion, production dependency installation, account
login replay, Era restart, recording, live call or full global-suite rerun occurred.

## Remaining integration and acceptance

This is a transport checkpoint, not a completed calling feature. Era's startup and
HUD do not yet instantiate these modules. CallAudio already provides the common
PCM boundary with the tested Gemini adapter, but a production coordinator must
bind both lifecycles, provider readiness, timeout/hangup handling and optional
speaker policy. Era's actual tool/approval/job/memory policies must be attached
without sharing mutable local conversation state. HUD controls/status/captions
must bind to that coordinator rather than simulate readiness.

Then conduct one attended incoming and one outgoing call. Verify real input PCM
format, quiet speech, output audibility, interruption, remote echo, latency,
caption ownership, hangup confirmation, helper crash, and local glasses privacy.
Provider renewal/fallback, proactive call policies and unanswered-message fallback
remain pending. Calls stay disabled until these integration/acceptance gates pass.
Deferred local hearing checks, Phase 6 work and previously lost schedules remain
separate open items; this checkpoint does not close or skip them.
