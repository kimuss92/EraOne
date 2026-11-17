# PF-039 second independent review — change requested

Candidate: `docs/tasks/pf038-pf039-scoped.diff` handed off 2026-11-16 22:18Z. Reviewer: dev-review. Windows Python 3.14 `-m pytest tools/test_reanchor_sources.py tools/test_silence_order.py -q`: **41 passed**, one dependency deprecation warning. Builder reports 451 focused tests, 444 protected guards, registry OK, Ruff clean. I did not run the live voice, music or HUD path. Application code remains unmodified by this review.

The prior premature window close is fixed at the state level: `_silence_partial` does not call `_enter_inactivity`, the two-chunk tests show a longer sentence keeps the window open, and `_silence_finished` commits a standalone order at the speech boundary. PF-038's unscored follow-up guard remains present.

## Remaining PF-039 finding: the temporary hold drops reply audio

The new `_silence_partial("stay silent")` sets `_barge_in_requested=True` while waiting to learn whether more words follow. In `app/era_live/audio.py::_receive_audio`, that same latch makes `response.data` enter the drop branch, prevents `speech_out.feed(delta)` for streamed text, and prevents native speech checkpoints. If the next chunk makes the utterance `stay silent when I'm on a call`, `_silence_partial` clears the latch, but audio/text already discarded cannot be restored. Era can answer the non-order with its first words missing. The builder's queue handoff identifies this as a limit; the new tests only inspect window/latch state and do not inspect the emitted speech.

Please preserve output while the prefix is unresolved, or defer the hold until the end-of-speech marker and then interrupt/flush a true silence order before any reply plays. Add a chunked input plus interleaved output test: `stay silent` -> an output chunk -> `when I'm on a call` must deliver a complete ordinary reply, while a standalone `stay silent` must deliver no reply.

**Verdict:** the combined candidate is **not accepted yet**. The state transition fix is supported by the focused tests; the promised normal response for a longer non-order is not preserved under interleaved output.
