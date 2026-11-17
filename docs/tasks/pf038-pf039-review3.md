# PF-039 third independent review — turn-boundary ordering finding

Candidate: `docs/tasks/pf038-pf039-scoped.diff` handed off 2026-11-16 22:22Z. Reviewer: dev-review. Read-only review. Independent Windows Python 3.14 `-m pytest tools/test_reanchor_sources.py tools/test_silence_order.py -q`: **43 passed**, one dependency deprecation warning. Builder reports 1,039 focused tests, 446 protected guards, registry OK and Ruff clean. No live voice, music or HUD exercise by this reviewer.

The ordinary interleaved case is improved: a partial `stay silent` stores output callbacks, a later non-order chunk replays them in order, and a standalone order discards them at the speech boundary. The added tests exercise those helper methods. PF-038's unscored follow-up guard remains present.

## Blocking finding: reply-end markers can overtake held output

The builder explicitly identifies the no-input-`finished` path. In `app/era_live/audio.py`, `sc.turn_complete` calls `speech_out.end_turn(...)` before `_silence_finished(full_in, at_turn_end=True)`. If a longer non-order has only a partial `stay silent` prefix and no input end-of-speech mark, its `speech_out.feed(...)` callbacks are held until `_silence_finished` runs. `SpeechOut.end_turn()` enqueues `_END_OF_TURN` and resets `_feed_turn_no`; replaying the callbacks afterward starts a **new** speech turn without its own end marker. The reply can be split, delayed or stranded rather than delivered complete. In native audio mode, `NativeSpeechEnd` is likewise queued directly at turn completion before held `NativeSpeechFrame` callbacks replay. The new interleaving test calls only `_emit_or_hold`; it does not exercise either end marker.

Please resolve/discard the hold before speech and native end markers are emitted at `turn_complete`, or include those markers in the ordered buffer. Add a no-`input_transcription.finished` regression that drives `turn_complete` with held output and checks the actual speech/native queue order and completion. Preserve the standalone order's silence and the next turn's normal output.

**Verdict:** the combined PF-038/PF-039 candidate is **not accepted yet**. The missing-finished path is material because the current backstop explicitly handles it, and its output ordering contradicts the promised complete answer for a longer non-order.
