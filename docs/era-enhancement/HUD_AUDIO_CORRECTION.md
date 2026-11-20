# HUD text and glasses recovery correction

User requested one text surface and investigation of physical Bluetooth loss
before further phases. The prior caption overlay duplicated/overlapped the
conversation. Remove the floating caption; use the existing conversation row,
with an inline caption only when its text is not already represented there.
Keep captions in the event bus for playback evidence and later remote delivery.

Read-only evidence, 2026-10-12: at 15:49:03 local time the output write failed
with MME error 6 (device/driver unavailable); at 15:49:09 the preferred open
failed with MME error 2 (invalid device ID), then the fallback opened. This
followed set_voice(gemini) at 15:49:02, which does not itself reconnect or alter
Bluetooth. The user confirmed Windows also showed the glasses disconnected.
The pin remains Star glass CY03-CB36, the mic pin is the laptop Microphone Array,
and read-only current enumeration finds the glasses active again. Most Windows
Bluetooth diagnostic channels are disabled; System has one adapter link-key
storage warning at 13:00, which does not prove the later disconnect cause.

Safe implementation: reconcile the live resolved target during recovery,
including startup with no output index. Adopt a returned target without closing
the working PC stream until the preferred stream opens. Preserve backoff,
generation ownership, user preference and the existing renewal budget. Add
structured open/write errors and actual/requested endpoint names. Do not reset
pairing, change drivers, turn the radio off/on, or claim the physical fault fixed.

Verification: mocked startup-unavailable/return, failed reclaim/backoff,
outdated probe rejection; browser transcript+caption duplicate checks; normal
audio/HUD regressions, type/build checks, refreshed manifest. A physical repeat
may still be needed to establish the Bluetooth/driver root cause.

The recovery now also recognizes PortAudio invalid-device errors and structured
MME invalid-ID errors. If the pinned endpoint is connected but its listed index
is unusable, it schedules the existing bounded idle renewal. Device enumeration
is forced only after old audio tasks/streams have unwound, and only for the same
routing generation. Ordinary outages do not force enumeration. This addresses
the logged invalid-ID recovery gap; it does not establish why Bluetooth dropped.

## Result and verification

- HUD: passed. Removed the floating caption. Browser fixtures with recorded
  conversation plus active caption gave one matching response, zero overlays,
  and four history rows. A live generated reply plus matching caption rendered
  one agent row; a local notification without transcript rendered one inline
  agent row. Screenshot: hud-single-text-preview.png.
- Recovery: passed in mocked runtime checks. Returning glasses are adopted even
  when the initial preferred index was unknown. Failed attempts keep the working
  default stream and backoff. A stale background probe cannot override a new
  user route. Invalid-ID faults schedule the existing bounded idle refresh;
  generic faults do not force it. No enumeration occurs with audio streams open.
- 244 audio/TTS/HUD checks passed (14.05s); two affected scheduler/race checks
  passed again after binding the background closure's captured values. One Ruff
  finding about loop-variable capture was fixed. Final Ruff/diff checks passed.
- 36 manifest/registry/executor-wrapper/smoke gates passed (18.94s). Regenerated
  manifest: 330 modules, 57 tools, zero unparseable. TypeScript checking and
  final production Vite build passed; ui/dist/index.html was rebuilt.
- Existing Google SDK deprecation warning remains. The entire repository suite
  was not repeated; hardware reconnection and acoustic output are not proved by
  the mocked tests. See hud-audio-correction-checks.txt and
  hud-audio-correction-gates.txt for the retained results.
- Read-only diagnostics found current glasses endpoints active. The default
  localhost HUD port did not answer, so its private runtime status could not be
  inspected. Historical numeric indices vary between processes; new observed
  route logs include endpoint names and requested stable ID for correlation.
- Initial guessed config paths did not exist; actual pins were read through
  the project's preference APIs. No preferences were written. Browser fixtures
  ran in an isolated local preview; preview server/browser were closed afterward.

Physical Bluetooth cause: unresolved. The device-loss and invalid-ID sequence is
confirmed, but disabled Windows Bluetooth diagnostic channels do not supply the
disconnect reason. No automatic Bluetooth-disconnect command appears in the
inspected incident's tool calls. The preceding voice switch is correlation,
not proof of causation. The set_voice handler keeps the AUDIO session and does
not request Bluetooth changes. No radio/pairing/driver/power-setting change was
made, and no live cloud synthesis or Telegram message was sent by these checks.

Reload the rebuilt HUD and restart Era to load Python changes. Verify the glasses
remain connected and recover to the saved output after a real interruption before
closing this hardware acceptance item. Further phases remain queued, as requested.
