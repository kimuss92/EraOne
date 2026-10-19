# Transcript-fed local speech — Phase 4 continuation

Extend the approved ownership and playback delivery design to model-generated
Fish/Kokoro replies. Reuse the existing TelegramSpeech and SpeechOut workers.
Attaching the latest accepted input after model completion would misattribute
queued audio; sending both the legacy final and the playback final would duplicate
the reply. Bind a proven send-time owner before the first transcript sentence
enters synthesis, retain that immutable identity in frames, and let the existing
zero-PCM end marker complete Telegram delivery after playback accepts the frames.

Latch attribution once per provider turn. An ambiguous/unowned turn stays
unowned, even if newer input arrives before its later chunks. Reject late owner
binding and changing the owner of a partially buffered speech turn. Repeated
chunks of the same owned reply reuse one delivery worker, including when a
previous direct utterance has already claimed that request and is still open.
New input, flush, synthesis/playback failure and teardown keep existing cancellation
behavior. Provider turn_complete must not finalize or cancel queued local speech.

Suppress the legacy provider-completion Telegram message for a turn assigned to
playback delivery, even if its audio later fails. Native Gemini output and
unowned/legacy local speech retain their current final routing. Tests cover
queued synthesis after provider completion, multiple transcript chunks, owner
immutability, new local/remote input, failure/cancellation, tool-only continuation,
and exactly one persistent final message, with mocked synthesis/SDK/Telegram.

Phrase previews follow the first accepted device write; this is not measured
word-level or acoustic synchronization. Native Gemini alignment and seamless
engine recovery remain separate work.
