# Conversation regression — 13 October 2026

Phase expansion is paused for the user's report of repeated voice commands and
no response to either text or voice. No live Era restart or microphone capture
was performed during diagnosis. The supplied recording was inspected.

## Evidence and causes

- Recording frames at 13:44:10, 13:44:25 and 13:44:40 show PC Boost running,
  then canceled, with no new conversation reply. The HUD's connected indicator
  alone is not proof that the model is answering.
- Logs show a PC Boost call canceled after 221 seconds, another after 61 seconds,
  and a media coordinator call taking 48 seconds. Both tools were absent from
  the existing slow-tool grace table. The receive coroutine awaited their
  execution, delaying transcription and reply processing. Keepalive timeouts
  followed. This is a concrete blocking path; it does not establish that every
  connection timeout has that cause.
- At 13:47:46 a typed "next track" request was sent but no answer arrived.
  A later "era" send failed when the connection timed out. Typed sends were not
  included in the existing silent-response watchdog's reply clock.
- The glasses microphone was open as Casque (CY03-CB36), with input gain 8.
  Media-only speaker checks repeatedly scored captured speech below 0.488.
  Those frames were discarded before Gemini could hear or transcribe them.
  Logs cannot identify every rejected utterance as owner speech rather than
  playback; no voiceprint was recalibrated from mixed recording audio.

## Repairs

- PC/system performance and media coordination now use the existing 1.5-second
  grace-and-background path. Fast results stay inline; slow work returns an
  explicit running acknowledgment, with its actual outcome reported separately.
  Scheduler/autopilot/halting-tool policies are preserved.
- Their worker dispatch now carries the existing cancellation token. PC Boost
  checks cancellation during discovery, before launching/clicking, and while
  waiting. A native UIA call already in progress cannot be forcibly interrupted;
  after it returns the cancellation check prevents further work. A click that
  already occurred cannot be undone by cancellation.
- Cooperative cancellation returns a Cancelled outcome instead of logging an
  engine crash or triggering repair advice for an intentional stop.
- Successfully sent typed/Telegram user requests start the reply watchdog,
  including with the microphone muted. Repeating text does not postpone the
  original timeout. Internal background prompts do not start this user clock.
  Existing configured fallback policy still controls recovery.
- The optional media filter can accept a confident local opening Era/wake-name
  address despite a poor short-utterance voiceprint score. Unaddressed mismatches
  remain filtered; the explicit utterance is allowed to finish, then checked anew
  on the next utterance. Recognition errors cannot override a mismatch. This
  needs the existing Vosk recognizer; missing Vosk retains the previous policy.
  The global security voice lock still runs first and is never bypassed.

## Verification

Focused conversation, cancellation, ownership, speaker, attention, quiet-glasses,
barge-in, mute, watchdog and tool-batch checks: **441 passed**. Additional execution,
foundation ownership and validation-runner checks: **18 passed**. Scoped Ruff and
Python compilation passed. Initial Ruff import-order findings were corrected.
The existing Google SDK Python deprecation warning remains.

Final additional regression checks: **24 passed**, including mic-muted typed
requests, cancellation classification and the global security gate boundary.
Full readiness runner: **33 gates passed in 414.2 seconds**, GLOBAL_READY YES,
VISION_STACK_READY YES, ALL_CHECKS_OK. Output is retained at
`C:/Users/nesri/AppData/Local/Temp/era-responsiveness-full-checks-20261013.txt`.
Test logs are isolated under TEMP so production logs are not overwritten.
No device changes, account actions, destructive cleanup or live PC Boost
execution were used in testing.

## Live acceptance still required

After restarting Era, try normal-volume requests in quiet without repeating her
name; with music playing, start a request with "Era" and verify its whole phrase
reaches her. Type a harmless question while a slow task is running, cancel that
task, and verify a new question gets a reply. Check interruption and any false
response to playback or Era's own voice. Do not treat automated passing checks as
proof of these physical audio results.

## Lesson

Any tool added to the live receive path needs a tested latency policy. A new
convenience voice filter needs an explicit-address recovery route, and health
monitoring must cover both user text and voice, independently of mic state.
