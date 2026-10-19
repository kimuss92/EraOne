# Hangup and repeat-call recovery — 2026-10-14

The owner requested calls without restarting Era after hangup. The supplied HUD
reported protocol_failed and instructed a restart. Read-only live state showed
processReaped=true, canStart=true and canHangup=false for the prior failed music
generation. A restart was not actually required to release that helper; the HUD
guidance was misleading. The running Era process still held the older in-memory
call implementation, so installing these Python changes requires one initial
reload/restart; routine subsequent call retries do not.

## Repairs

- Stop pending native output on discard/stopped events so a missing final audio
  acknowledgement does not become a timeout failure after intentional hangup.
- Accept pipe EOF after a known terminal event; continue reporting unexpected EOF
  or malformed protocol while a call is active.
- Do not leave an already remotely discarded native call again. Still disconnect
  and reap only the exact owned helper.
- Distinguish intentional phone/HUD hangup from late provider/native shutdown
  errors. Preserve real failures that occurred before hangup in diagnostics.
- Keep each new call's peer-bound generation, PCM queues, native process and
  Gemini session fresh; clear prior captions/errors when that new call begins.
- Block another call if helper reaping has not been proven. Add Retry cleanup,
  scoped to the exact previous generation/transport, instead of restarting Era.
  Successful reaping restores controls; unknown remote hangup remains explicitly
  unconfirmed. Cleanup retries never place a call or replace account permissions.
- Remove the unsupported Restart Era instruction from protocol/cleanup HUD errors.

The attended test window and unconfigured-account restrictions remain enforced.
No automatic calls, account activation or global voice-session restart were
performed. Existing native DLL finalization hangs remain contained by the owned
child supervisor; this does not claim to repair the external DLL.

## Verification

Tests exercise phone and HUD hangup, pending audio/EOF races, avoiding duplicate
native leave, consecutive outgoing/incoming calls in the same coordinator with
fresh provider and transport objects, and recovery of an unreaped helper without
starting a conflicting call. The recovery control is carried through the existing
authenticated HUD WebSocket boundary. Music/tool/context, audio format, quiet
input, approval ownership and transcript regressions are included in the run.

The initial implementation passed 201 targeted tests. Strict checks then found
nullable shutdown access and a test loop-variable capture; both were corrected.
Atomic patch-context mismatches and formatting findings were corrected. The
expanded incoming/outgoing matrix and final verification are reported below.

Final full targeted call run: 203 passed in 17.34 seconds. Two additional recovery
failure cases were then added; all 41 coordinator tests passed, including those
new cases. Failed cleanup never falsely enables another call. Strict mypy on the
four changed Python source modules, scoped Ruff/Black, compilation, TypeScript
and the final HUD production build passed. Doctor: zero errors, zero warnings,
READY. The existing google-genai pytest deprecation warning remains. The complete
global suite was not rerun.

Actual two consecutive phone calls, PC music playback plus a follow-up, network
stability and phone/HUD hangup are still an attended hardware acceptance check.
The owner has already confirmed two-way speech; this alone does not prove all
remote tools or network conditions. No test call was placed by the agent.
