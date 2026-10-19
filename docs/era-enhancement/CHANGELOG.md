# Enhancement repair changelog

## 2026-10-18

- Owner-feedback repair: detect undelivered replies independently of wire
  activity/empty turns, recover with a fresh Gemini session without replaying
  commands, and release microphone capture immediately on server interruption.
  570 affected/protected tests and eight manifest cases pass; reloaded PID 24028
  accepted real owner speech and completed a response with playback evidence.
  Physical listening/repeated conversation remain TODO #47; earlier full-gate
  results below predate this follow-up. See RESPONSE_RECOVERY_2026-10-18.md.
- Keep speech active through reported device-buffer latency and defer idle
  provider renewal until playback finishes, preserving forced deadlines.
- Retain a healthy explicitly selected microphone briefly across reconnects;
  discard idle input, close on faults/shutdown/expiry and retire stale callbacks.
  Default-following inputs reopen; old capture cannot clear a newer route.
- Close retained capture before global PortAudio refresh. Native six-lease
  probe used one physical stream with no audio saved/transmitted.
- Read Spotify playback across output devices, retaining desktop-only operation
  and the saved verified favorite URI. Crew respects the deadline after model wait.
- Switch optional HUD tones off for the beep complaint; preserve other choices.
- Resolve stale PCM/standby fixtures without weakening their underlying rules;
  normalize formatting with AST proof and retain source snapshots.
- Full Windows gate 7,971 passed / four skipped; 36 readiness gates passed;
  final default-input guard then passed 334 affected/protected cases. Normal
  restart loaded source with glasses voice, PC media preference and standby.
- Audit local Phase 10 mechanisms and establish a current-machine Phase 11
  baseline. Optional Jev, sustained/fresh-setup and physical acceptance stay open.
  Evidence: `STABILITY_RELEASE_2026-10-18.md`.

## 2026-10-16

- Activate CodeMode from coding intent; add persistent subscription model
  selection, voice switching, bounded model fallback and automatic HUD entry.
- Contain audio ValueError and incomplete PCM frames; retain local recovery and
  visual degradation reporting.
- Add exact-ID Spotify API control, observed-state caching, command timing and
  saved-favourite identity routing; remove pixel settling from native playlists.
- Add bounded crew model waits/retries/backup, preserve cancellation across
  worker threads and expose crew/tool lifecycle diagnostics.
- Preserve unsent inputs through reconnection and add local context/feedback/
  outcome memory over the existing long-term memory and vault.
- Add safe historical file controls, source editing and recent-file history.
- Register Telegram/Development views and route current-task status requests.
- Reuse vision captures, isolate cached results and validate failure paths.
- Resume and verify Netflix playback after next-episode navigation.
- Add bounded/parallel validation, progress output and suite timing profiles.

Evidence, configuration requirements and remaining live acceptance:
[implementation register](REVISION_2026-10-15.md).
