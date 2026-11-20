# PF-038 revised candidate — independent review

Candidate: `docs/tasks/pf038-scoped.diff` (revision handed off 2026-11-16 22:07Z). Reviewer: dev-review. Read-only review; no PF-038 code changed. Independent Windows `tools/test_reanchor_sources.py`: **15 passed**, one dependency warning. Builder reports 135 focused tests, 418 protected guards, registry check and Ruff clean. I did not exercise the live music/voice path.

## Finding F2 remains: lock readiness is not utterance verification

`main.py::_accept_input()` marks every voice envelope `verified=_owner_voice_verified()`. That helper only checks the verifier's global `enabled` flag and `readiness()`; it has no verdict for the utterance that produced the envelope. With lock enabled, `app/era_live/audio.py::_media_voice_frames()` bypasses the separate media filter on the assumption that the global speaker gate has screened all playback.

The global gate has a deliberate exception. `core/voice_gate.py::UtteranceGate` accepts a short follow-up within its post-command window and forwards its audio. `core/speaker_recognition.py::SpeakerVerifier._verify(purpose="followup")` accepts voiced audio shorter than 0.25 seconds with reason `follow-up (too short to score)` and no voice score; other follow-ups use a lower score floor. `pending_followup_allowed()` returns true when no confirmation store is present or when a short reply is allowed. The gate's `_mark_owner(full=False)` deliberately does not renew its own owner anchor, but PF-038's new `note_owner_turn(verified=True)` can renew the listening window when Gemini transcribes and answers that unscored audio.

Reproduction path supported by the source: lock on and ready; an owner command opens a follow-up window; a brief lyric or film utterance falls in that window during playback; the global gate forwards it as an accepted unscored follow-up; Gemini transcribes it into a voice envelope; `_accept_input()` labels it verified from global lock state; Era's reply extends the listening session. This contradicts the owner's explicit requirement that lyrics, films and Era's own voice never interact. The new guards mock `enabled/readiness` and do not cover this accepted follow-up path.

Please base the PF-038 media re-anchor decision on a verdict for the specific input turn, or prevent unscored/weak follow-ups from qualifying during playback. Add a guard that routes an accepted unscored follow-up through the input and reply path while music plays and proves the session does not extend. Keep any existing short-confirmation behavior that is outside this re-anchor decision.

F1's near-expiry state transition is covered by the revised clock test. I did not establish a live boundary result or verify the exact audio-onset timestamp in production. **Verdict: not accepted yet** because F2 remains.
