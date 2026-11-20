# Cut-off speech and Star Glasses reconnects

Investigated 2026-10-18. Use this with the dated stability report and current
handoff. A complete conversation entry proves generated text, not completed
synthesis, driver playback or physical listening.

## What was found

| Possible cause | Evidence / current position | Repair or next check |
|---|---|---|
| Speaking ends while final PCM remains buffered | Application queue-empty logic previously ignored device output latency | The turn now remains speaking through the reported buffer tail; provider renewal respects that state unless its hard deadline forces renewal |
| Routine provider reconnect closes the glasses capture stream | Logs show repeated audio close/open around Gemini rotations | Healthy identical microphone handle retained up to 15s; idle input discarded; expired/faulted/changed/shutdown capture closes; refresh first closes retained capture |
| User/model interruption, including possible acoustic pickup | Repeated audio_flush/interrupted turns in live logs; some recognized fragments unrelated to the apparent request | Preserve deliberate barge-in and existing user policy. Test actual room/mic conditions; an interruption marker alone does not prove a human intentionally spoke |
| Driver/device output loss | Existing local retry/fallback/degraded-output path handles short faults; longer outages can still lose audio | Compare output-error/recovery events with the exact missing phrase; never report full audible completion from text alone |
| Optional HUD tones | Persisted hud_sounds was true; UI emits ready/error/notification tones | Switched the existing HUD-sounds setting off for the reported beep issue; all other settings preserved. Remaining beeps can be compared with Bluetooth/stream events |
| Bluetooth radio/driver/power/battery conditions | Current Windows inventory OK; installed Intel Bluetooth driver 22.180.0.2 dated 2022-09-23; no continuous radio trace proving the reported events | Windows hardware comparison below; driver age is a lead, not a confirmed cause |
| Provider synthesis/network delay or partial streaming | Existing provider, synthesis and interruption diagnostics; not established as the sole current cause | Compare generation/synthesis failures, audio delivery and reconnect timestamps before changing providers or increasing buffering |

Confirmed native capture test: six sequential leases used **one physical stream**;
initial open 131.5ms and reuse 0–0.2ms. Stream closed at end; audio was discarded,
never saved or transmitted. This proves handle reuse, not audible beep elimination
or sustained Bluetooth reliability.

## Checks to perform when giving feedback

1. Keep the current device choices: glasses microphone / Era voice as selected,
   PC speakers for media. Charge the glasses, stay near the PC, and start with
   one quiet-room reply of two or three sentences. Note the exact last word heard
   and whether the glasses beeped at the same time. Do not infer full playback
   from the transcript.
2. Leave Era connected through a routine provider rotation. Check whether the
   microphone is reused and whether beeps stop. Then try a deliberate quiet
   interruption separately: expected interruption is not a missing-phrase defect.
3. Compare a short audio session with Era fully quit. If connecting/reconnecting
   or beeps persist without Era, record that observation for the Bluetooth/device
   path. If another paired device may be taking the connection, temporarily
   disconnect that device as a controlled comparison; this glasses model's
   multipoint behavior has not been established.
   Optional HUD sounds are now off; they can be restored later in Settings if
   desired. This setting does not mute Era's spoken answers or change devices.
4. Run Windows Get Help's Bluetooth troubleshooter. Check Battery/Energy Saver
   and the Bluetooth adapter's Power Management option, if Windows exposes it.
   Microsoft recommends preventing adapter power-off as a troubleshooting step.
   Record the prior setting so a comparison is reversible.
5. Check the PC manufacturer's supported Bluetooth driver, then Windows optional
   driver updates. The currently installed driver is dated 2022; no driver was
   installed or service reset during this repair. Schedule disruptive Bluetooth
   service/pairing changes outside an active Era conversation.

Windows guidance source, checked October 18:
[Microsoft: Bluetooth keeps disconnecting in Windows](https://support.microsoft.com/en-us/windows/hardware/bluetooth/bluetooth-keeps-disconnecting-in-windows).
Device battery, range and secondary-device comparisons are diagnostic proposals,
not confirmed explanations of this model's beeps.

## Useful feedback format

```text
Approximate local time:
What I asked:
Full text shown / last word actually heard:
Glasses beeped or reconnected? When:
Voice output / microphone / media output:
Music playing? Quiet room? Deliberate interruption?:
Does the issue also occur with Era quit?:
How long until Era began answering?:
```

Match that time against voice.input.opened/reused, audio.output open/closed/
write_failed/fallback/recovered, engine rotation/network events and interruption
records. Keep logs local and redact credentials/private conversation before
sharing. Do not use a device's connected label as proof of audible playback.

## Speed and acceptance

Keep native state checks and handle reuse on the short path; avoid name-search,
pixel inspection and repeated device opens when a verified identity/healthy
handle exists. The output-tail check adds no fixed first-audio delay. Model and
vision work can take seconds; the command-acknowledgment, first-audio, completion
and device-recovery timings must be measured separately. A single successful
sample is not a p95 or whole-system sub-500ms claim.
