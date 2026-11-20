# Phase 2 implementation checkpoint — 2026-10-12

Status: core stabilization verified; user reports no idle beeps and explicitly
authorized moving on. Remaining audio acceptance items below stay open as
follow-ups. Phase 3 may proceed under that updated instruction.

## Implemented

- Output endpoint resolution respects direction for exact IDs and generic mic
  aliases; a missing stable ID cannot silently choose among same-name devices.
- Saving a voice preference reports pending playback verification. Status and
  pinned prompt context distinguish requested and observed output. Verification
  checks request generation, device index, name compatibility and fallback state.
- Playback acknowledges its captured routing generation and actual driver
  device; closure invalidates the observation. Repeating a request for the same
  healthy stream acknowledges it without reopening it.
- Preferred-device recovery preserves the working fallback until the candidate
  starts. Failed attempts back off from five to sixty seconds.
- Full-session re-enumeration is limited to three attempts per routing request,
  with increasing cooldowns. Reselecting an output resets that budget. This
  deliberately stops automatic session resets if they cannot restore the route.
- HUD status separates requested and observed output; missing/stale observations
  are unknown. Automatic routing prompt context describes policy, not proof.
- Stream open, close and reclaim events are logged for correlation with beeps
  and model-session reconnects.

## Evidence

The original eight regression reproductions failed against the original source.
The expanded suite now also checks stale acknowledgments, closure, repeated
same-device requests and bounded re-enumeration. The final focused and adjacent suite passed 239 tests with one warning in
22.36 seconds; see `audio-regression-after.txt`. Ruff and diff whitespace checks
passed. The full 6,529-case baseline was not repeated after these runtime edits.

Both real outputs opened successfully at 24 kHz, mono, int16 and accepted a
100 ms silent write without underflow. The driver reported PC speakers at
index 5 and Star Glass at index 4. See `hardware-route-probe.json`. Neither the
system mixer nor stored preferences were changed. A successful silent write
does not establish audibility or sustained Bluetooth stability.

The user confirmed beeps happen while Era is idle, but not when Era is closed.
Existing source opens output and microphone streams per model session. Reclaim
and re-enumeration can also act while idle. This makes lifecycle/profile churn
an investigation target, not a proven diagnosis. The laptop microphone is
currently pinned; no Bluetooth pairing, driver or microphone preference change
was made.

## Remaining acceptance work

- Event-driven endpoint watcher and safe reconciliation of manual changes.
- Stereo/hands-free profile policy and ambiguous-name handling across physical
  devices; the PortAudio bridge still uses names within its device enumeration.
- Dedicated HUD controls for requested/verified/recovery details; backend status
  now exposes them, but the broader widget redesign belongs to its later phase.
- Sustained live-session observation and audible PC/glasses routing tests.
  Correlate a timestamped beep with `audio.output.opened`, `.closed`, `.reclaimed`,
  `.reenum_scheduled` and `engine.reconnect_error`. Do not label the beep fixed
  based on the bounded-retry tests alone.
- Present retry-budget exhaustion through the user-facing device status.

## Lessons

Device enumeration proves availability, not playback. A saved preference proves
intent, not execution. A connected endpoint may still reject stream creation;
keep an operational fallback and back off. A cached device index needs both
identity context and a current request generation. Recovery that rebuilds an
entire session must have a budget across session boundaries.

No files were deleted or moved. The pre-existing change in
`app/era_live/tool_dispatch.py` and user backup directories were preserved.
