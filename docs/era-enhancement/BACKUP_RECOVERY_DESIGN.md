# Backup conversation recovery — 2026-10-13

Scope: fix the reported silent backup mode and false Gemini switch confirmation.
The user authorized investigation and repair; no architectural migration is needed.

## Evidence and limits

The latest available `logs/era.log` records a deaf verdict at
2026-10-12T23:29:09.828Z, followed by entry into Claude backup voice mode.
The gateway subsequently returned Claude answers, with one request taking
approximately 19 seconds. The tail records a user-requested relaunch at
23:35:45Z. It does not establish a fatal Python crash or the reason Gemini
stopped transcribing. Do not equate a voice preference with a connected engine.

## Chosen repair

Reuse the existing input resolver and bounded wearable amplification in the
backup microphone path, which previously opened the system default without
either. Identify the opened endpoint before enabling amplification and restore
the detector baseline before each open. Keep owner verification, attention,
mute, standby, and tool confirmation gates.

Intercept standalone Gemini voice orders before the backup brain. Reuse one
thread-safe exit request across voice, UI, and tool paths: close the Claude
window, pause a chosen cascade if necessary, and signal the backup loop to exit.
Preserve the Gemini voice preference. Report retrying until the existing
successful-connect hook reports restoration. A working Live session continues
to use the existing voice switch behavior.

Log actual synthesized engine, audio byte count, text length, and backup mode
without recording microphone audio or reply text in these new diagnostic events.

Alternatives considered: changing only the wording would leave recovery broken;
replacing the entire audio pipeline would create unnecessary risk. Reuse the
existing routing and run-loop handover instead.

## Verification

Regression tests exercise an already-Gemini preference while Claude is active,
spoken/typed/tool reconnection requests, cross-thread exit scheduling, unchanged
live-session behavior, and real backup-loop capture with fake pinned glasses
and PC endpoints. Run existing Claude, local, cascade, voice, glasses, speech,
and main bridge checks. Physical reconnect and speech remain live acceptance
items after restarting Era.
