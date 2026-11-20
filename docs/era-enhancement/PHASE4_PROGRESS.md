# Phase 4 progress — 2026-10-12

Status: software readiness verified; hardware acceptance deferred by the user
on 2026-10-13, who explicitly authorized Phase 5. See DEFERRED_ACCEPTANCE.md.

## Implemented and verified

- Latest full verification after the listening restoration: all 31 readiness
  gates passed in 466.7 seconds, GLOBAL_READY YES and ALL_CHECKS_OK. Artifact:
  listening-restoration-global-checks.txt. The restarted runtime also accepted
  two glasses voice inputs and produced reply/playback events. User confirmation
  of first-attempt normal-volume commands after idle time remains pending.

- 2026-10-13: the user's voice/text asymmetry was traced to logged pre-model
  attention drops. Disabled the timed name gate for this user's active listening
  preference and repaired raw-only standby wake input for the glasses. Focused
  checks: 114 passed. See LISTENING_RESTORATION.md. Voice reliability and
  self-trigger avoidance remain live acceptance requirements; later phases held.

- 2026-10-13: fixed backup-loop input routing/gain and Gemini switch requests
  while the backup engine is active. Reconnection is reported as pending until
  successful connect. See BACKUP_RECOVERY_PROGRESS.md for evidence, tests,
  initial failures, and remaining live acceptance. The cause of the original
  Gemini transcription outage remains unproven.

- User confirmed interruption works perfectly without yelling and commands are
  heard when addressed to Era. Follow-up name requirement was still premature:
  increased the attention window from 45 seconds at explicit address to 120
  seconds after the associated reply completes, with one-time renewal and
  explicit-transcript tracking. Focused 212 checks passed. See
  CONVERSATION_WINDOW_PROGRESS.md. Live unnamed-follow-up timing remains open.

- Final global readiness rerun: all 31 gates passed, GLOBAL_READY YES and
  ALL_CHECKS_OK. After the last attention correction, reran the registered
  speech pipeline gate: 417 passed. The earlier doctor timeout and all failed
  hardware/test attempts are retained in REMAINDER_SYNTHESIS_PROGRESS.md.
- Ignored speech no longer pins the attention state after media stops or an
  explicit listening context opens. Continuous background sound triggers bounded
  name-check rearming; previously held media is not forwarded on reopening.

- Standalone Gemini sentence recitation now recovers a failed local sentence
  after a voiced prefix without replaying the whole Live buffer. PCM retains
  owner and actual engine metadata; stale or interrupted results are discarded.
  Recovery rejects incomplete/malformed/oversize/incompatible WAV responses and
  uses bounded time and cooldown. Fish exceptions use ready-Kokoro fallback.
  Kokoro rejects token overflow instead of silently truncating. See
  REMAINDER_SYNTHESIS_DESIGN.md and REMAINDER_SYNTHESIS_PROGRESS.md.
- Live feedback confirmed Telegram works but exposed quiet-command misses and
  needing to shout to interrupt. CY03 input now gets a lower interruption floor
  while retaining adaptive echo/VAD checks. Detected interruption PCM is kept
  and forwarded through normal gates; mute/session changes suppress late frames.
  Added level/disposition diagnostics and clarified the media-silence prompt.
  Focused hardware-free ear/recovery checks: 198 passed initially. Peak-limited
  input gain was added after a failed retest and confirmed low input levels;
  final combined speech gate: 417 passed. Live retest is pending.

- Whole-turn Gemini fallback is suppressed after any local PCM was queued for
  the failed turn. Remaining sentences stop with an incomplete-speech report;
  initial failures hand off once. Streamed turns now retain independent IDs
  across direct announcements, and late failures preserve newer partial text.
  Provider exceptions discard incomplete phrase prefixes and take the same
  failure path. See NO_REPLAY_RECOVERY_PROGRESS.md. Automatic remainder-only
  recovery and live acceptance remain open.

- Native Gemini sentence checkpoints now reveal progressive Telegram previews
  after the preceding queued audio is accepted by the device. Final delivery
  appends only the remaining text and verifies whole-turn playback. See
  NATIVE_PROGRESSIVE_PROGRESS.md. Timing is best-effort because the provider
  supplies no word timestamps; recovery and live acceptance remain open.

- Owned native Gemini final Telegram delivery now waits for its queued playback
  marker and complete per-turn byte evidence. Dropped chunks, missing transcripts
  and uncertain ownership suppress success. See NATIVE_PLAYBACK_CONFIRMATION_PROGRESS.md:
  314 regression tests plus 74 registry and 72 smoke checks passed. Progressive
  native alignment, engine recovery and live acceptance remain open.

- Transcript-fed Fish/Kokoro replies now carry send-time ownership into queued
  sentences, frames and final markers. Playback drives progressive Telegram
  delivery; the provider-completion duplicate is suppressed. Internal overlap,
  local interruption and interleaved direct announcements are guarded. See
  TRANSCRIPT_SPEECH_OWNERSHIP_PROGRESS.md: 308 tests plus 74 registry and 72 smoke
  checks passed. Native Gemini timing and live acceptance remain open.

- Gemini text requests now capture the owner at actual SDK submission. Final
  mirroring preserves empty tool-only continuation, rejects overlapping/stale
  attribution, invalidates on interruption, and resets on session teardown.
  See MODEL_REPLY_OWNERSHIP_PROGRESS.md: 300 focused checks passed. Native
  progressive alignment and transcript-fed local frame ownership remain open.

- Explicitly owned direct Fish/Kokoro speech now feeds progressive Telegram
  delivery from playback events. Context survives threads/tasks; PCM owner
  metadata and a final marker keep delivery attached to the originating input.
  Local/voice interruption, flush and failed playback invalidate old frames.
  See DIRECT_SPEECH_OWNERSHIP_PROGRESS.md: 225 focused checks passed. Native
  Gemini/transcript-fed ownership and live acceptance remain pending.

- Fish distinguishes clean transport completion from failure after an audio
  prefix. Buffered synthesis and the router discard incomplete phrases before
  playback; request-local completion does not rely on shared last_error.
- Fish phrase buffering preserves sentence pipelining. It trades first-phrase
  latency for safe fallback. Native Gemini audio remains on its existing path.
- A failed Fish phrase can use an already loaded Kokoro voice. Cold models do
  not block this recovery, and saved voice preferences are untouched.
- Switching to Gemini during local synthesis discards the pending local audio.
- Long tokens are never split at the soft 320-character sentence limit.
- Speech frames carry phrase identity, sanitized text, actual local engine,
  and first/last boundaries while remaining compatible with raw PCM consumers.
- HUD captions follow successful device writes. Dropped frames produce failure,
  not completion; teardown interrupts an unfinished caption. Caption snapshots
  are detached from bus state. The existing generated transcript is preserved.
- Local playback recovery counts only successfully written bytes. A completely
  unavailable speaker no longer creates false evidence that the reply was heard.
- Speech worker shutdown invalidates its generation and clears pending text;
  a restarted worker cannot replay old-session sentences.
- The HUD uses one transcript surface. The initial floating caption duplicated
  replies and was corrected after user feedback: matching captions are suppressed,
  and otherwise appear as an inline agent row. See HUD_AUDIO_CORRECTION.md for
  the Bluetooth investigation, recovery fixes and subsequent verification.

## Verification and failures

- Four initial regressions reproduced long-token slicing and partial Fish audio
  escaping to buffered callers/playback. Two subsequent regressions reproduced
  missing ready-Kokoro recovery and audio escaping after a Gemini mode switch.
  A playback regression reproduced 640 falsely counted bytes with no speaker.
  These failures were corrected and are retained as tests.
- Final focused run: 199 passed, one existing Google SDK deprecation warning,
  10.90 seconds. See phase4-speech-checks.txt. Mocked synthesis/device transports
  cover normal completion, partial failure, cancellation, stop/restart, routing
  resilience, captions, snapshot ownership, and existing HUD behavior.
- TypeScript checking and final Vite production build passed. ui/dist/index.html
  was rebuilt from the changed source; no dependency upgrade was performed.
- agent-browser QA used an isolated named session and localhost Vite server with
  simulated caption state, not the running Era backend. Active caption text was
  visible; completed state hid it. Transcript height remained 84px in both
  states. Screenshot: phase4-caption-preview.png. This verifies rendering, not
  real hardware audio or live WebSocket/Telegram delivery.
- Selected production/test Ruff checks and git diff --check passed. Initial
  import-order findings were fixed. One Ruff invocation used the frontend cwd
  and failed to locate Python paths; corrected from the repository root.
- After all code/test edits, regenerated the manifest: 330 modules, 57 tools,
  zero unparseable files. Manifest, registry, executor-wrapper, first-patch and
  end-to-end smoke gates passed: 36 checks, one existing SDK warning, 19 seconds.
  See phase4-integration-gates.txt. Final TypeScript checking also passed.
- One test invocation included a nonexistent test_hud_truth.py and collected no
  tests. The corrected explicit suite completed successfully. Browser stdin and
  shell-quoting attempts did not produce valid caption assertions; direct
  module evaluation subsequently verified the actual component.
- The entire repository suite has not been rerun after these Phase 4 changes.
  Phase 3's full-run history and repaired gates remain in PHASE3_PROGRESS.md.

## Remaining work and evidence limits

- Owned Telegram delivery is connected to direct local speech, transcript-fed
  Fish/Kokoro speech and native Gemini audio. Send-time model ownership rejects
  ambiguous/stale responses. Earlier entries above retain historical evidence;
  the current remaining work is listed here.

- Unified voice/UI/Telegram command ownership and equivalent intent/memory paths.
  Explicit text-source envelopes and ordered ingress are now implemented and
  verified; see INPUT_OWNERSHIP_PROGRESS.md. Text reply ownership now survives
  provider completion through owned frames and terminal markers. Native voice
  identity and ambiguous mixed tool/text turns still require live evaluation.
- Progressive Telegram delivery, throttling, final delivery and failure handling
  have mocked verification. No live Telegram messages or cloud synthesis calls
  were sent during this work. Verify draft support for the deployed bot/client.
- Native Gemini caption alignment and Gemini-to-Fish-to-Kokoro recovery. Current
  local recovery is Fish-to-ready-Kokoro, then the existing Gemini fallback.
- Whole-turn Gemini replay after queued local speech is suppressed. Failed
  unsaid sentences now attempt standalone Gemini recitation. If every provider
  is unavailable, the remainder stops and incomplete speech is reported. Live
  cloud recovery, acoustic coverage and its latency budget remain unverified.
- Successful HTTP completion proves transport completion, not acoustic word
  coverage. Successful output writes prove device acceptance, not audibility.
  Kokoro's phoneme-length cap now rejects overflow; multilingual acoustic
  coverage and voice quality need live validation.
- Measure the buffering latency tradeoff with authorized live synthesis/audio;
  current tests do not establish a production latency budget.

No deletion, move, driver change, pairing reset, saved voice preference rewrite,
or production task-database mutation was required. Existing user changes and
backup directories remain preserved.
