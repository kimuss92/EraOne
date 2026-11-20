# Input ownership progress — 2026-10-12

Status: first input migration slice verified; Phase 4 remains partial.

## Implemented

- Immutable input envelopes preserve exact text, source, unique ID and receipt
  time. Desktop, Telegram and LAN adapters name their source explicitly.
- Telegram no longer changes the reply flag on its polling thread before the
  command is accepted. Explicit inputs are ordered on Era's existing loop.
- The existing parser/intercepts, approval checks, memory writes and lane routing
  remain in one handler. Blocking local work runs outside the audio loop; tests
  prove the loop can advance while that worker is blocked.
- A local command superseding pending Telegram input clears remote mirroring and
  reports the interruption. Queued dispatch failures are logged and reported to
  the originating channel. Direct failures preserve adapter-visible exceptions
  and disarm Telegram before the adapter reports them.
- Abort bypasses a blocked input handler. Older waiting commands are cancelled
  through a dispatch epoch, logged and acknowledged where appropriate. This
  does not forcibly terminate already running OS/thread work or undo side effects.
- LAN receipts now report accepted/queued with HTTP 202 and use a received
  notification. They no longer claim completion before dispatch. Owner-token
  authorization is retained, and malformed command payloads are rejected.

## Verification

- 141 input/text/Telegram/lane/scheduler/dedup/tool-bridge checks passed in 10.93s.
  See input-ownership-checks.txt. Tests mock outbound notification/synthesis.
- 45 existing panic-stop checks passed in 1.58s.
- 36 manifest/registry/executor-wrapper/smoke gates passed in 27.43s.
  See input-ownership-gates.txt. Final Ruff and git diff --check passed.
- Manifest refreshed after final source/test edits: 331 modules, 57 tools,
  zero unparseable. No full repository suite rerun was performed for this slice.
- Initial source-specific ingress regressions failed three cases before the
  wrapper supported explicit sources. A later test edit misplaced the malformed
  LAN payload assertions into the worker test, causing NameError; corrected,
  with both contracts subsequently passing. No test was silently removed.
- Existing Google SDK deprecation warning remains. No real Telegram message,
  cloud synthesis, driver/pairing change, or destructive operation was performed.
- New source and docs coexist with prior user edits/backups, which remain intact.

## Compatibility and remaining scope

The LAN response changed from HTTP 200 and executed to HTTP 202 and queued.
This is documented in CM_COMMANDS.md; external shortcuts requiring the former
contract need updating. No in-repository clients were found by the scoped search.
Embedded/legacy callers without an explicit source retain compatibility behavior;
no-loop handling retains the prior offline acknowledgment path.

The shared _tg_reply_pending flag remains temporarily for existing consumers.
This slice fixes input provenance/dispatch ordering, not all response ownership.
Native voice envelopes, actual model-send/turn correlation, delayed phrase owners,
tool-only turn retention and progressive Telegram delivery remain open. In
particular, an immutable command ID alone does not prove which model output
answers it. Correlation must be established before attaching remote delivery to
playback; do not infer owner from the latest input when an older reply is playing.

The user authorized advancing despite the outstanding physical Bluetooth
verification. Its unknown root cause stays tracked in HUD_AUDIO_CORRECTION.md;
no hardware-fix completion claim is made here.
