# Gemini communication repair — open listening

The user explicitly requested removal of media-playback voice filtering and
other automatic listening blockers, better interruption discrimination, and an
input-only bottom meter before further phase work. This supersedes the previous
optional media-filter fallback policy.

## Findings

1. The live send path applied a media identity filter and attention filter after
   the security voice lock. The optional filters could discard owner speech
   before Gemini received it. The persistent security voice lock was already OFF.
2. The active Python 3.14 environment had no webrtcvad module. Barge-in therefore
   used amplitude alone. The glasses' gain and low floor, with a 150 ms trigger,
   made noise look like an interruption.
3. CommandBar deliberately drew Era's output amplitude while she spoke, instead
   of drawing the microphone's signal. This was a wiring choice, not proof of
   an incorrect microphone route.
4. Slow-tool grace previously covered selected tool names only. An omitted live
   tool could still hold up reception. The health watchdog previously preferred
   an automatic Claude handover when Gemini stopped answering.

## Final behavior

- Removed attention and media filtering from the live Gemini send path. There
  is no media-triggered identity check or wake-name requirement, and no fixed
  post-reply microphone hold. Normal input, including silence for server
  endpointing, reaches Gemini in order.
- Preserved intentional microphone off/mute, standby/wake operation and the
  explicitly toggleable security voice lock (currently OFF). No voiceprint was
  deleted or recalibrated, and action confirmation rules were not weakened.
- Installed webrtcvad-wheels 2.0.14.post1, with its native Python 3.14 Windows
  wheel. Added it to both requirements files and Doctor dependency checks.
  Production interruption requires the speech classifier; a missing classifier
  no longer becomes an amplitude-only trigger. The compatibility detector mode
  remains available to existing explicit callers/tests.
- Interruption requires 300 ms of sustained speech, with a majority of voiced
  subframes, adaptive echo protection and a broadband/DC noise check. Non-speech
  resets accumulated interruption credit. These checks apply only while Era is
  speaking, not to normal microphone audio sent to Gemini.
- Retain up to 768 ms of input when confirming an interruption, so onset and
  confirmation do not discard the opening of the user's command.
- Voice settings override legacy main-config audio settings. Gemini automatic
  VAD is retained with high start sensitivity, low end sensitivity, 80 ms prefix
  and 600 ms silence endpointing. This reduces the previous 300/800 ms settings
  while allowing pauses. Google's continuous-input and configurable-VAD behavior
  is documented in its [Live API capabilities guide](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
  and [API reference](https://ai.google.dev/api/live).
- All ordinary live tools have a bounded grace, including newly added tools.
  Existing scheduler/autopilot/CM and turn-halting exceptions remain. A running
  acknowledgment never claims successful completion; the real outcome follows.
- The response watchdog reconnects Gemini instead of automatically switching to
  Claude under the saved Gemini-first preference. Explicit manual Claude mode
  remains available. This does not replay actions or bypass provider quota limits.
- The bottom waveform reads only micLevel, including while Era speaks. It no
  longer reads Era's output amplitude, and stale microphone readings expire after
  one second. Output-driven sphere animation remains independent. A microphone
  can still physically pick up speaker sound; the meter truthfully reflects that
  input rather than synthesizing it from output audio.

## Verification and transparency

Before implementation, checked the project graph first. Its query resolved to
backup components rather than the current live path, so targeted current-file
reads were necessary.

- Initial selected checks: 261 passed, one legacy test expected automatic Claude
  recovery. Updated it to assert Gemini reconnection.
- Broader selected checks: 577 passed, one endpointing test still read the actual
  user's new voice settings while expecting defaults. Isolated the voice-config
  source in the test. Final broad selected run: **587 passed**, with SDK and
  pywinauto COM-mode warnings, no test failures.
- A real WebRTC synthetic broadband-noise probe initially interrupted once.
  Added an interruption-only spectral flatness guard and regression coverage.
  Final native probe: **zero noise interruptions**. Post-change focused suite:
  **258 passed**, one Google SDK deprecation warning.
- UI build and TypeScript check passed, output 508.62 kB. Scoped Ruff, compilation
  and whitespace checks passed. An initial Python probe was run from ui_src and
  could not import core; rerunning from the repository root succeeded.
- Native detector probe confirms Vad backend, speech detection required,
  300 ms sustain, and Gemini endpointing 80/600 ms.

Full readiness: **all 33 gates passed in 385.0 seconds**, GLOBAL_READY YES,
VISION_STACK_READY YES, ALL_CHECKS_OK. Doctor: **zero errors, zero warnings**.
Full output retained at
`C:/Users/nesri/AppData/Local/Temp/era-open-mic-full-checks-20261013.txt`.
Production logs were preserved by isolating test logs under TEMP. No live microphone recording, Era
restart, device reroute, PC automation task, credential change or Telegram send
was performed.

## Pending live acceptance

Restart Era so the running process imports the new speech classifier and config.
Speak normally without repeating her name, including while music plays. While
Era gives a long reply, try normal-volume "stop", keyboard noise and a brief
background noise. Verify input meter response while Era speaks and when she is
silent. Report false interruptions and any missed first word. Real speech/music
overlap and speaker bleed still require the user's glasses/room test: automated
tests do not establish perfect speaker identity or cloud availability.

Phase expansion remains paused for this communication acceptance. Lessons:
dependency availability must not silently change interruption semantics; a
convenience media filter must not obstruct the user's chosen listening policy;
input and output meter ownership must stay separate.
