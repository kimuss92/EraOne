# CodeMode: silent Gemini voice and missing-source follow-up

The user reported a CodeMode reply that appeared on the HUD but was not spoken,
and that asked them to send `core/voice_mode.py` because it had only an excerpt.
The request is to repair these failures, not implement an unspecified Radio
Intercom feature from the quoted reply.

## Root causes and repair

The ordinary TTS router deliberately produces no PCM when Gemini Live is the
selected voice: the model normally supplies its own native audio. CodeMode's
Claude reply has no matching Gemini Live audio. Sending it through that path
silently yielded no speech, while the normal fallback could only try unrelated
Live audio. Logs show the affected reply reaching the speech worker at 18:31.

CodeMode now marks its text turns as external speech. The speech worker uses
independent text synthesis for those turns, including the configured Gemini TTS
voice when Gemini is selected. Ordinary Live audio keeps its existing path and
the user's selected/preferred voice is not rewritten. Failed external synthesis
produces a visible warning and never replays unrelated Live audio. Cancellation
still discards the generation and clears external-turn bookkeeping.

Source context previously used `source.read`'s line/character excerpt cap and
offered no continuation protocol. CodeMode now prefers whole source files within
its existing eight-file/100KB context window. Claude can return bounded local
`read_requests`; Era validates and reads those files/ranges, updates the context,
and continues the same pinned CLI session. At most four reads per round and two
continuation rounds are allowed, under one 180-second generation deadline.
Private paths, links, credential/state files and approval gates remain restricted.
The prompt explicitly says not to ask the user to upload/paste repository files.
Genuine questions about the desired feature behavior remain legitimate.

## Evidence and activation

- Real Gemini text-synthesis probe generated **72,960 bytes of PCM** successfully.
  This proves synthesis, not that a human heard playback.
- The real runtime `emit` → SpeechOut → TTS router → speaker-queue path is tested
  with Gemini selected, preserving phrase ownership/engine identity and preference.
- Nine focused regression cases passed: external Gemini speaker frames, visible
  failure/no native replay, unchanged ordinary Live routing, complete 406-line
  source, private-path refusals, automatic source continuation and its round limit.
- A broader speech/CodeMode/development/source run passed 206 tests. Final
  current-source speech/context/approval/development/schema/manifest checks:
  **280 passed**, one warning (51.97 seconds). Spotify/personal-control regressions:
  **22 passed**, two warnings, in a separate fresh process (13.00 seconds).
  The combined attempt had 301 passes and a Windows COM apartment initialization
  conflict when importing Spotify after device diagnostics. Separate suites pass;
  no unrelated Spotify implementation was changed to hide that test-order issue.
- Final lint, formatting and whitespace checks passed. Manifest regenerated:
  391 modules, 59 tools, zero unparseable. Doctor READY with zero errors/warnings.
  Frontend/layout was unchanged. No full global-runner exit is claimed.
- Native source reads verified complete, untruncated files: voice_mode.py (406
  lines), speech_out.py (568 lines), audio_output.py (380 lines).
- Current app reports idle/mic off, CodeMode off and Star glass output routing
  verified. Its task queue still reports a running task. The running app was not
  restarted or its work interrupted; these backend changes need an Era restart.

No Radio Intercom behavior was guessed or generated patch applied. The accepted
HUD layout, normal Live voice routing and user's audio preference are preserved.
