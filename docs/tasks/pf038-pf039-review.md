# PF-038 / PF-039 independent review — PF-039 change requested

Candidate: `docs/tasks/pf038-pf039-scoped.diff` handed off 2026-11-16 22:13Z. Reviewer: dev-review. Read-only review; application code unchanged. Independently ran Windows Python 3.14 `-m pytest tools/test_reanchor_sources.py tools/test_silence_order.py -q`: **37 passed**, one dependency deprecation warning. Builder reports 403 focused tests, 440 protected guards, registry OK and Ruff clean. No live voice, music or HUD run was performed by this reviewer.

## PF-038 F2

The revised guard now drives a real `UtteranceGate` through a scored command and an unscored follow-up, then routes the follow-up into `_accept_input()` while media plays. `last_pass_fully_verified` is false after that follow-up, and the focused test proves it cannot re-anchor the listening window. This addresses my earlier F2 path at the level tested. The actual live music conversation, voice profile re-enrollment, and any timing overlap between gate output and transcription remain unverified.

## Blocking PF-039 finding: a partial silence-order prefix acts before the full utterance

The PF-039 rule promises whole-utterance matching, including that `stay silent when I'm on a call` does not switch modes. The matcher correctly rejects the full sentence. In `app/era_live/audio.py`, however, every `sc.input_transcription.text` chunk is appended to `in_buf`, and `is_silence_order(self._inflight_user_text)` immediately calls `_enter_inactivity()` on the growing transcript. If the transcript arrives as `stay silent` followed by `when I'm on a call`, the first chunk already matches, closes the window and interrupts the turn. The later words cannot undo that side effect. The same applies to an early `Era, stay silent` prefix of a longer non-order. The tests check the finished sentence and source text separately; none drives these two chunks through the voice path.

Please defer irreversible silence-order action until the utterance boundary, or use a signal that proves the user has finished the order. Preserve the requirement to stop Era's reply before it is spoken. Add a two-chunk regression guard that confirms a longer sentence with a silence-order prefix leaves the window open, alongside a real standalone order that closes it without speech.

**Verdict:** PF-038 F2 correction is supported by the focused guard. The combined PF-038/PF-039 candidate is **not accepted yet** because PF-039 can silence Era during a non-order.
