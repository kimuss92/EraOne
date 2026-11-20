# PF-038 independent review — changes requested

Candidate: `docs/tasks/pf038-scoped.diff` on commit `3a337b9`. Reviewer: dev-review. Windows `tools/test_reanchor_sources.py`: 10 passed, 1 dependency warning. Builder reports 130 focused and 413 protected guards passed. No live PF-038 exercise was run by the reviewer.

## F1: a real owner turn near expiry still lapses mid-conversation

`ListeningState.note_owner_turn()` only sets `_turn_reply_pending`; it does not update the session anchor. `era_finished_speaking()` uses that pending turn only when `self._session.open()` is still true. With a 120 s window, an owner voice turn accepted at t=119 s and Era finishing at t=121 s leaves the window closed. PF-034's media rule also prevents unnamed audio from re-anchoring during music. The owner therefore still sees inactivity during an ongoing exchange. The new ten-turn test keeps every reply comfortably inside the window and misses this boundary.

Please add a clock-controlled guard for an accepted voice turn at the edge of expiry and its reply after expiry, and make the accepted turn count as activity without allowing an internal turn to do so. Keep explicit close/standby from being reopened.

## F2: music can impersonate the accepted owner turn

`main.py::_accept_input` calls `note_owner_turn()` for every `envelope.source == "voice"`. `InputEnvelope` has no speaker/owner identity field, and this owner currently has voiceprint verification disabled. If a lyric inside the open window is transcribed into a voice input and Gemini replies, `_turn_reply_pending` plus `era_finished_speaking()` extends the window even while Spotify plays. This bypasses PF-034's deliberate `_reanchor_passing()` media exclusion—the live failure PF-034 fixed. The builder already called this risk out in the handoff; the current guard tests do not model it.

Please establish what evidence makes a turn owner activity before using it to extend the window while media plays, or return this tradeoff to the owner. A typed/explicitly addressed turn is distinguishable; an arbitrary voice envelope is not. Do not mark the lyric path as proven safe from the current tests.

Verdict: **not accepted yet**. The candidate improves ordinary mid-chat timing, but F1 leaves the stated outcome unmet and F2 can regress the protected music behavior. PF-038 code remains unmodified by this review.
