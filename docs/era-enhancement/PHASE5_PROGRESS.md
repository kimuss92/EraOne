# Phase 5 progress — 2026-10-13

Status: partial. Started at the user's explicit request, with Phase 4 hardware
acceptance deferred. No Phase 6 work started. DEFERRED_ACCEPTANCE.md records all
remaining physical checks, including the user's untested PC-speaker self-voice.

## Implemented

- Persisted auto-standby preference: off by default; exact enable/disable orders
  wired to typed, native voice, and backup voice. Reuses the existing 300-second
  idle interval. Preferences save atomically without replacing other keys.
- Idle entry blocked while speaking, processing, reconnecting, waiting for
  approval, running a local turn, entering standby, or independently mic-off.
  Repeated standby requests cannot start duplicate transitions.
- Subsequent user instruction implemented: voice mic-off can idle into standby
  when enabled. The standby microphone hears the wake word locally while
  retaining the cloud mute. Backup mode routes standby frames to the same local
  wake recognizer. Automated task-completion resume preserves microphone mute;
  its local unmute listener resumes so "start listening" remains available.
- CM, crew and generic background reports using the existing held-report path
  request one completion wake and drain queued results. A disconnected report
  stays held. Scheduler completion outcomes carry structured report context;
  awaiting-approval and non-completion alerts remain Telegram/UI notifications.
- Media-only identity filtering reuses the enrolled speaker profile and local
  model without changing the global voice-lock preference. Scored non-owner
  speech is suppressed; owner speech is retained in order. Unknown identity is
  diagnosed and retained so model failure does not silently deafen the user.
- Quiet speech: adaptive voiced-frame energy selection replaces the fixed .01
  pre-verification cutoff. Confirmed glasses input gets a lower utterance
  detection floor; PC input restores its prior baseline. Voice-match thresholds
  and enrolled identity samples stay unchanged. Quiet-room input bypasses the
  media filter immediately.

## Verification and failures

- First standby test invocation referenced the CM continuity test under tools,
  but it lives at repository root. No tests collected; corrected the path.
- Initial standby integration: 168 passed. Extended scheduler outcome and
  transition guards: 176 passed. Media/quiet focused checks: 189 passed.
- A later pending-frame preservation change imported VERIFYING, but the
  existing gate names this state PENDING. Both combined runs stopped at
  collection. Corrected the import and state comparison; rerun passed.
- Combined checks before the final backup-wake addition: 327 passed.
- Registered speech pipeline after media/quiet changes: 433 passed, only the
  existing Google SDK deprecation warning. Artifact:
  phase5-final-speech-checks.txt.
- Main tool bridge: 127 passed. Artifact: phase5-bridge-checks.txt.
- Latest combined rerun before the final output/microphone separation check:
  329 passed. A task report may be spoken after automatic wake while its input
  microphone remains voice-muted; input privacy and output speech are distinct.
  Final combined rerun: 330 passed, only the Google SDK deprecation warning.
  Artifact: phase5-final-checks.txt. At that increment the global 32-gate rerun
  was outstanding; the later full validation below completed it successfully.
- Scoped Ruff checks passed. Manifest inventory now has 337 source modules,
  57 tools and no unparseable modules; refreshed after the final edit.
- Two documentation-context patch attempts were rejected before applying their
  combined changes. Retried the intended code/test changes successfully.

## User controls

- "Enable auto-standby": idle entry after the existing five-minute interval.
- "Disable auto-standby": inactivity never enters standby; explicit standby
  still works. Disabled remains the default unless the user enables it.
- Voice "mic off" followed by standby: only local wake listening is enabled.
  The configured wake word resumes the existing normal conversation behavior.
- Automated completion wake leaves input muted, can speak the completed report,
  and keeps the local "start listening" command available.

## Remaining in Phase 5

- Live acceptance of the new Telegram Approve/Cancel buttons (mocked checks
  passed; no real owner messages sent from this coding session).
- Native Telegram calling: research and proposed design recorded in
  TELEGRAM_CALL_DESIGN.md; separate Era account selected but not yet created.
  Account creation/setup, native-build proof, implementation and live acceptance
  remain. The bot text/voice-note bridge is not a native calling facility.
- Acoustic evaluations with real user/media clips and the deferred physical
  listening, soft speech, wake, interruption, and PC-speaker self-trigger tests.
- Broader notification delivery failure/retry coverage and final phase gate.

No physical recognition accuracy, no native calling, and no complete Phase 5
claim is made. No real Telegram messages or microphone recordings were sent
from this coding session. Earlier uncommitted work and backups were preserved.

## Telegram approval increment

Implemented private-owner Approve/Cancel cards over the shared pending store.
Each card uses a random process-local token, pending generation, and Telegram
message binding. Sender and private-chat checks reject outsiders, forwarded or
inline cards, stale requests, and replay. Store consumption/cancellation checks
the creation timestamp atomically, including reuse of a short confirmation ID.
Resolved buttons are cleared best effort; restart invalidates all old tokens.

Approved actions use existing classification and verification with a task-local
authenticated grant, not a synthetic “yes”. They cannot be rerouted by unrelated
recent speech. Classification still blocks unsafe actions. Results resume parked
jobs through the existing continuation. Interrupted approved execution now marks
that job failed instead of leaving it waiting forever. Remote approval wakes
standby silently while preserving microphone privacy. The current-engine provider
survives engine replacement; Telegram text uses it too.

Network delivery runs on a dedicated worker with restricted API methods and
sanitized diagnostics. Unknown sends are not retried. A missing/reconnecting
engine leaves the approval pending and retryable. Cards mask message bodies and
credentials and direct users to the full HUD action when abbreviated. Receipt
is separate from execution and verification. HUD activity receives actual results.
Failed actions are not rerun automatically. Delivery failures are recorded;
voice/HUD approval remains available. Group approvals are disabled.

Initial tests: 193 passed, 2 failed. One exposed the existing display helper's
missing message_text mask; fixed in the card. One test incorrectly passed
verification_status to ToolResult.ok; corrected the fixture. Rerun: 198 passed.
Expanded lifecycle/HUD/standby run: 297 passed before final interruption/wake
tests. Final results are in phase5-telegram-checks.txt. The only test warning is
the existing Google SDK deprecation. Initial lint test formatting/import issues
were corrected. One combined documentation patch failed context verification;
no changes from that patch applied, and it was retried with matching context.
No full global 32-gate readiness or live Telegram acceptance is claimed here.

Final focused run: 299 passed. Scoped Ruff and git diff whitespace checks passed.
The global runner's existing Confirmation flow gate now includes authenticated
button and model-self-approval regressions. Manifest refreshed for the new module.

## Native call research increment

Checked Telegram's official user-call API, TDLib call signaling, current tgcalls
custom-audio boundary, application credential setup, and repository license
metadata. Native calls require an authenticated user account; the existing bot
token cannot provide them. A separate Era account is recommended to provide a
caller/callee identity distinct from the owner. The user selected that approach
but has not yet created the second account. No account access, dependency install,
or call was attempted. Native build and fake-transport work can precede login.

TELEGRAM_CALL_DESIGN.md proposes an isolated official TDLib/tgcalls helper, maps
Era's current input/output ownership constraints, compares alternatives, and
defines staged proof for Windows, privacy, duplex speech, interruption, call
deduplication and unanswered-message fallback. This is a proposed architecture,
not a working native transport. Windows build and actual custom PCM remain
unproved. Live acoustic and Telegram acceptance remain deferred until reported
by the user. Two documentation amendments failed context verification without
applying changes, then were retried against the actual text.

## Full Phase 5 software regression

tools/run_all_checks.py passed all 32 checks in 404.8 seconds, including compile,
registry, logging, memory, execution, verification, tool bridge, authenticated
confirmation flow, standby, speech, browser/media, vision, Netflix, YouTube and
doctor. The runner reports VISION_STACK_READY: YES, GLOBAL_READY: YES and
ALL_CHECKS_OK. Artifact: phase5-global-checks.txt. Tests ran with ERA_LOG_DIR
isolated under the temporary directory to preserve live production logs.
The doctor still reported zero errors and three nonblocking warnings; those
details remain in the full artifact rather than being hidden by READY status.
Whitespace validation passed after documentation updates. This is software gate
evidence; it does not certify physical hearing accuracy or native calling.

User subsequently authorized Phase 6 and explicitly deferred native calls until
the separate Era account is prepared. Preserve this return point and all earlier
live/acoustic acceptance; Phase 6 entry is not an implicit physical/calling pass.
