# Recovery without replaying earlier speech

Continue approved Phase 4. Buffered Gemini audio represents a whole turn, with
no reliable mapping from local sentences to native audio offsets. Replaying it
after any local PCM has been queued can repeat speech already heard or about to
play. Estimating offsets would risk missing or repeating words. Remainder-only
cloud synthesis needs a separately verified provider surface.

Preserve whole-turn Gemini fallback when no local PCM was queued. Once any PCM
was queued for the failing turn, suppress whole-turn replay, skip its unsaid
sentences, and report incomplete speech through structured logs and the HUD.
Owned Telegram failure cancellation remains in place; do not send a false final.
Allow already queued local sentences to drain. Preserve voice preferences and
subsequent turns. This is safe degradation, not seamless remainder recovery.

Allocate a streamed turn identity once, independently of direct announcements.
Track turns with queued PCM and failed turns until their terminal marker, then
release that state; interrupt clears both. A late old-turn failure must not
clear a newer turn's pending text. A first-sentence failure hands off only once.

Verify failures before/after the first sentence, interleaved announcements,
later turns, interrupted state, single handoff and failure reporting using
mocked synthesis and queues. No live messages, mode preference changes,
dependency upgrades or destructive steps.
