# Phase 4 recovery and hardware acceptance — 2026-10-13

Status: software gates passed; live normal-volume acceptance pending.
Phase 5 has not started.

## Delivered

Standalone Gemini TTS recites only the failed unsaid sentence after a queued
local prefix, preserving request ownership and actual engine metadata. Successful
sentences remain queued once. Interrupt/mode/generation changes discard late
recovery audio. No whole-turn replay is introduced. Transport is bounded to
12 seconds and 16 MiB, has a 30-second failure cooldown, uses the official origin
without redirects/retries and store=false, and validates completed status plus
a single intact 24 kHz mono int16 WAV. Saved voice preferences are preserved.
Google's documented REST surface avoids upgrading the installed SDK.

Fish provider exceptions now use the ready-Kokoro fallback. Kokoro rejects token
overflow rather than silently truncating a phrase. Every-provider failure still
reports incomplete speech; it cannot guarantee speech during total outages.

The one-command runner now includes a Speech pipeline suite, including owned
Telegram delivery, input safety, quiet-input and barge-in regressions.

## Hardware feedback and correction

User confirmed Telegram works. Initial live acceptance failed: repeated quiet
commands and shouting needed for interruption, including quiet-room misses and
a glasses-enrolled voiceprint. Actual saved speaker verification is disabled,
so voiceprint mismatch is not established as the cause.

Added a lower interruption floor for the actual CY03 input, retained adaptive
echo/VAD/sustain checks, and preserved four mic blocks on detected interruption.
These blocks still traverse ordinary speaker/attention/state gates. Stale,
muted or replacement-session callbacks cannot forward them. Clarified the
Gemini instruction: quiet/short/headset speech alone is not evidence of media.
Added actual-endpoint and rate-limited RMS/capture-disposition diagnostics.

The second live retest still missed normal-volume commands. Runtime evidence
confirmed Casque (CY03-CB36), device 1, at 16 kHz. Several capture intervals had
raw RMS peaks approximately 0.0014–0.0032. These aggregates support investigating
low input level, without proving that every interval contains user speech.
Added up-to-8x gain with whole-frame peak limiting only for verified wearable
input. Detection and normal forwarding use the conditioned frame. Diagnostics,
standby and local-unmute listeners retain raw frames. Other/unknown inputs
restore unity gain and the configured interruption floor. Third live retest is
pending; do not claim this has resolved hardware hearing or false triggers yet.

User then linked the regression to the previously added self-talk/room-chat
ignoring feature. Inspection found the attention gate's rejected-utterance state
could persist until sustained silence, even when media stopped or conversation
reopened. It now reevaluates reopened context immediately without releasing held
media, and rearms bounded name checks during continuous ignored background sound.
The latest gain plus attention-state correction needs another live retest.

## Evidence and failures

- Recovery/provider/token/ownership focused checks: 85 passed initially.
- Expanded speech/provider/validation runner regression: 280 passed, one existing
  Google SDK deprecation warning, 36.78 seconds.
- Quiet-input, echo, speaker, mute, attention and recovery: 198 passed before
  gain. After gain/safety refinements: 203 passed in 13.16 seconds.
- First global runner: 30 gates passed, Era One doctor gate timed out at its
  existing 30-second subprocess deadline. An isolated recheck reproduced it.
  Per-check timing then completed with action imports taking about 7.45 seconds;
  a subsequent doctor regression run passed 70 checks. Cause of the transient
  delay is unproven; no timeout threshold was relaxed.
- Fresh global runner writes phase4-global-recheck.txt and uses an isolated
  ERA_LOG_DIR so validation does not overwrite the live diagnostic log.
  All 31 gates passed in 434.5 seconds: GLOBAL_READY YES and ALL_CHECKS_OK.
  Its speech gate passed 414 tests before the last attention-state correction;
  reran the registered speech gate after that correction: 417 passed in 29.24
  seconds. See phase4-final-speech-checks.txt. Other gates required no new changes.
- Ruff initially rejected a blind pytest.raises(Exception); narrowed to the
  expected ValueError contract. Selected final Ruff and diff checks passed.
- Manifest regenerated: 335 modules, 57 tools, zero unparseable files. A currency
  check performed after a later runner edit reported stale; regenerated after
  final code edits. Currency will be checked again before reporting completion.
- Two attention regression tests initially failed because their synthetic
  unknown-only transcript was still in CHECKING, not the required DROPPING
  setup. Corrected to recognizable non-name words; the suite then passed 192
  checks. The final 417-test speech gate includes the held-media safety case.
- A diff check found an extra blank line at EOF in audio_pcm.py; corrected
  formatting without behavior changes and regenerated the manifest again.
- Two targeted searches used nonexistent paths (core/listening_state.py and
  tools/test_voice_input.py); corrected to attention.py and existing tests.

No live recovery API calls or Telegram messages were sent by these tests. User
provided live Era feedback; deterministic suites used fake audio/providers.
No destructive changes, driver edits, pairing resets or saved voice changes.

## Required before Phase 5

The global runner is verified. Complete the normal-volume hardware retest. Preserve
the hardware failure as open until observed success. Acoustic completeness,
exact native word timing, failover latency and ambiguous provider turns remain
explicit evidence limits rather than claims of perfect continuity.

Lessons: preserve the command onset used to detect interruption; measure actual
input levels before attributing misses to identity or media filtering; validate
the entire recovered phrase before exposing PCM; do not truncate token overflow.

API references verified:
- https://ai.google.dev/gemini-api/docs/speech-generation
- https://ai.google.dev/api/interactions-api
