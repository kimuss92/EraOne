# Deferred live acceptance

2026-10-13: the user explicitly requested testing voice responsiveness later
and authorized advancement to Phase 5. Phase 4 software gates passed (31/31);
hardware acceptance is deferred, not passed. Do not remove this item or claim
first-attempt microphone reliability from software tests.

- [ ] Normal-volume glasses commands on first attempt, without repeating Era.
- [ ] First unnamed request after more than two minutes of quiet.
- [ ] Ordinary command with music playing on the direct Gemini path; assess
      unintended replies to lyrics or Era's voice in the actual room setup.
- [ ] Quiet interruption and configured wake word in explicit standby.
- [ ] Claude backup conversation and explicit Gemini reconnection on hardware.
- [ ] Soft glasses speech while media plays: request reaches Gemini without the
      removed media/attention gate. Observe background response behavior separately.
- [ ] Voice mic-off, enabled auto-standby, then normal-volume local wake word.
- [ ] Self-voice rejection on PC speakers: explicitly not tested by the user yet.
- [ ] Telegram approval card: Cancel a disposable proposed action; confirm it
      does not execute, then verify the resolved button cannot be reused.
- [ ] Telegram Approve for a reviewed disposable action: one execution, honest
      result/verification, and standby wake retaining any microphone mute.

Keep these checks in phase progress and final acceptance until the user reports
results. No reminders or external messages were created.

2026-10-13: user explicitly authorized moving to Phase 6 and deferring native
Telegram calls until they prepare a separate Era account. The native call design,
account/setup and duplex acceptance remain open; this is not a native-call pass.
Earlier acoustic and live-button checks remain tracked, not silently accepted.

2026-10-13: the user explicitly removed media/attention voice gates from Gemini.
Earlier local-filter acceptance expectations are superseded, not passed. Confirm
normal first-attempt commands after silence, quiet sustained speech interruption,
no noise-only interruptions, Gemini session recovery and an input-only bottom
meter. Era's output may still be captured acoustically by a PC microphone; the
input meter must not be driven directly by output events.

- [ ] After restarting Era, create/rename/delete a disposable file in a usual
      user folder and confirm one meaningful HUD update and accurate state.
- [ ] Confirm watcher roots/failed roots reflect real broad-drive access; skipped
      and inaccessible areas must not be described as observed.
- [ ] Native adapter-change event delivery and remaining device/task checks.

File events were proven on a disposable native Windows TEMP fixture; these live
user-folder/HUD checks remain pending. Native Telegram calls still await the
separate Era account.
