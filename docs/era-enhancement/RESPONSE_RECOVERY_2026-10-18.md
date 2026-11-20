# Response delivery recovery — October 18, 2026

Owner reports Era completely ignores speech. Live process 29836 is connected,
not muted/standby, with all six session workers alive. Input is captured and
transcribed, but a receive buffer accumulates repeated requests without a final
answer. The health watchdog treats tool calls, generated/dropped audio and empty
turn boundaries as successful replies; partial transcripts can keep moving its
timeout. This is not evidence that the physical glasses are healthy.

Repair plan: track a separate delivery deadline starting with a real user
transcript or successfully sent text. Transport activity/empty turns must not
clear it. Clear it after successful output writes. Respect active speech, tools,
CodeMode and explicit mic/standby choices. A stalled delivery requests normal
Gemini renewal with a fresh server session; retain local recap/memory and never
replay side-effecting commands automatically. Add deterministic regressions for
repeated transcripts/empty turns, valid delivery, long utterances, paused work,
muted typed input and recovery orchestration, then protected area checks, Ruff
and manifest freshness. Recover the current stalled instance through its normal
renewal event immediately; verify a benign check after loading the repair.

## Implemented and verified

- `core/live_health.py`: separate delivery deadline survives partial transcripts,
  empty turns, tool notifications and generated-but-dropped audio. Successful
  output clears it. Existing configured patience remains 60s on this machine;
  recent user speech delays the delivery verdict until a quiet gap. Typed requests
  remain pending through incidental voice input, including with the mic muted.
- `app/era_live/claude_voice.py`: active tools and CodeMode keep their existing
  budgets. Deliberate microphone mute/standby remain respected. A mute/undelivered
  verdict requests normal renewal, clears the stuck server handle, marks the old
  turn interrupted, flushes stale output and logs a visible request to repeat.
  Local recap, memory and preferences are retained; actions are not replayed.
- `main.py`: consumes the fresh-session flag before constructing the next
  connection, so a last old-socket token cannot undo the recovery decision.
- `app/era_live/audio.py`: successful output writes acknowledge delivery. Native
  server interruption immediately releases the speaking state, matching local
  barge-in; suppression of stale output remains intact. This second defect was
  reproduced independently with an interrupted event lacking turn_complete.

Regression evidence: seven delivery/watchdog cases first failed against the old
behavior (one existing-compatible case passed); the missing-turn interruption
case separately failed first. Final overlapping affected/protected run:
**570 passed, two warnings, 25.68s**, including the six required speech/media/
Spotify/approval/scheduler/toolspec suites. Manifest cases separately **8 passed**
(20.66s). Root Ruff lint/format and regenerated manifest pass. No UI build,
tool-declaration/prompt edit, API setup, device/driver change or dependency upgrade.
The prior 7,971-test full gate predates these repairs and was not repeated here.

Evidence: `runtime/response-delivery-red-20261018.log`,
`response-interruption-red-20261018.log`,
`response-delivery-final-tests-20261018.log` and
`response-recovery-manifest-tests-20261018.log`. Backups:
`.phase7_backups/response-recovery-20261018/`.

## Live result and remaining acceptance

Recovered the stalled process through its normal renewal event without command
replay, then loaded the tested source through the authenticated HUD restart.
New PID **24028**, connected, microphone active, voice on Star Glasses. A benign
typed check was superseded by the owner's actual speech, so it is not claimed as
an isolated typed-check success. The owner then gave normal spoken instructions
about concise routine responses; the HUD recorded a completed assistant reply,
and a matching turn recorded playback and completion (one sample 3,737ms total).
The running instance's new delivery methods were confirmed by read-only runtime
inspection. The owner resumed talking after the earlier standby; their new
listening state was retained rather than restoring an older standby snapshot.
The owner subsequently said **"Stand by now."**; Era entered standby normally.
Final read-only check: connected PID 24028, standby, verified glasses voice route.
That deliberate standby remains in effect; wake via the HUD or configured wake
word before expecting conversational replies.

Artifacts: `runtime/response-recovery-restart-20261018.json`,
`response-recovery-reloaded-20261018.json`,
`response-recovery-live-probe-20261018.json` and
`response-recovery-live-inspection-20261018.json`. Diagnostic attach used the
installed Python 3.14 inspection mechanism; no dependency was installed. Raw
private diagnostic snapshots remain local under ignored runtime, not shared logs.

This proves response delivery through the software/device boundary, not that a
person heard every word or that Bluetooth can never fail. Do not deliberately
stall/reconnect the now-active conversation to manufacture another live test.
Owner listening and repeated normal conversation, including interruption and
network/provider faults, remain feedback under TODO #47/#46. A resumed server
session and an isolated clean typed turn still need separate live acceptance.
