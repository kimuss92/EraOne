# Recovery without replay — 2026-10-12

Status: implemented and verified with mocked transports. Phase 4 remains partial.

## Delivered

- Streamed replies allocate one turn ID, independent of direct announcements.
- Per-turn queued-PCM evidence prevents whole-turn Gemini fallback after a local
  prefix has already been queued. Queued sentences drain; unsaid sentences stop.
- Initial synthesis failure preserves Gemini fallback and hands off only once.
- Failed-turn state suppresses owned successful terminal markers and is released
  at turn end or interruption. Existing owned Telegram failure reporting remains.
- Late failure of an older turn preserves a newer turn's partial text.
- Provider exceptions discard phrase prefixes and follow the same failure path.
- Incomplete speech produces a structured voice.reply.incomplete warning and a
  natural HUD notice. Voice preferences remain unchanged by partial failure.

## Verification and failures

Two new regression tests initially failed, reproducing continued synthesis and
whole-turn replay after a prefix, and duplicate initial fallback requests. The
first corrected focused suite passed 79 tests. Expanded checks exposed a stale
chaos test that expected a stalled Fish prefix to remain playable, despite the
earlier complete-phrase transport contract. Two runs failed that same test:
132 then 133 passed, one failed each. Updated the test to assert the real
IncompleteSpeechError and cooldown, preserving timing and prefix assertions.
Its async fixture now closes the client even on assertion failure; the earlier
failure had emitted an unclosed-session diagnostic.

Final evidence:

- Expanded focused recovery suite: 134 passed in 37.55 seconds.
- Broad speech/routing/ownership/cancellation/stability/integration suite:
  373 passed in 90.54 seconds, one existing Google SDK deprecation warning.
- Registry validator: 74 passed, zero failed. Existing main-only tool warning
  and nine default-safety information entries remain.
- Smoke E2E: 72 passed, zero failed. Expected invalid-confirmation and empty-query
  cases emit error logs and are successfully asserted.
- Selected Ruff checks, git diff --check and manifest currency check passed.
  Regenerated manifest: 334 modules, 57 tools, zero unparseable files.

Graph lookup returned older/backup nodes; targeted live source reads supplied
the current implementation. An initial rg invocation used a Windows-invalid
tools/test_speech* path argument; corrected to explicit test paths.
The complete run_all_checks.py suite was not rerun for this slice.

## Limits and next work

This prevents repetition through safe degradation; it does not automatically
finish an unsaid remainder when both local engines are unavailable. Native audio
has no reliable sentence offsets. Remainder-only synthesis and Gemini-to-local
recovery still require design and verification. No live Telegram messages,
cloud synthesis, microphone recording, device playback or Era restart occurred.
Live Phase 4 acceptance remains open; no later phase has started.

Lesson: queued PCM is enough to make whole-turn replay unsafe, even if playback
is delayed. Keep stream identity stable across announcements, and never let an
older failure erase a newer turn's pending text.
