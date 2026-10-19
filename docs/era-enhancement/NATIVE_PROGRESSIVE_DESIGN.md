# Native Gemini progressive delivery

Continue approved Phase 4 using the existing turn owner and playback ledger.
Queue zero-PCM transcript checkpoints at sentence endings, behind the audio
already received for that turn. Reveal each new text suffix only after those
bytes were accepted by the device. The final marker verifies the entire turn
and reveals any remaining suffix, without repeating earlier sentences.

Generation-time reveal would precede playback; waiting until completion is safe
but prevents progressive conversation. Ordered checkpoints retain playback
evidence without adding estimated timers. Gemini supplies no word timestamps:
these are best-effort sentence previews, not exact audio/text alignment.
Text received before the first owned audio frame waits for a later checkpoint
or final marker. Missing audio, failed writes, changed ownership and conflicting
transcripts must not finalize successfully. Already delivered previews cannot
be recalled if later generation fails. Preserve the single HUD transcript.

Verify with mocked transports through real receive/playback loops: preview
before provider completion, no preview before successful writes, final without
duplicate text, missing/dropped audio and ownership cancellation. No live
Telegram messages, device recording, dependency changes or destructive steps.
