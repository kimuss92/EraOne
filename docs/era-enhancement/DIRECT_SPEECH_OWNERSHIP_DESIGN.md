# Direct local speech ownership — Phase 4 continuation

Incremental implementation of the approved input/playback/delivery design.
Direct Fish/Kokoro utterances from speak() have known literal text and can be
owned independently of Gemini's asynchronous response stream. Implement this
path first. A shared flag or the latest accepted input cannot safely identify a
background task's result; adding a separate parser would duplicate existing
dispatch. Instead, a ContextVar carries the original immutable input through
the existing handler, to_thread and child tasks, and is reset on exit.

Capture that owner when speak() crosses onto the audio loop. Only the current
Telegram request may start an owned direct reply. Suppress its old immediate
Telegram acknowledgement, then carry the owner through sentence synthesis,
PCM frames, and a zero-audio final marker. Playback reveals phrase text on the
first accepted frame and schedules persistent delivery after the final marker.
This is phrase-level timing, not acoustic or word-level alignment.

New typed/remote/Telegram input and the first recognized native voice fragment
invalidate the previous owner synchronously. Flush, failed playback, teardown,
and synthesis failure cancel the owned delivery. Retain the reason in logs.
Submitted HTTP requests cannot be recalled. The network worker stays off the
audio loop, and stale frames cannot contribute text to a newer request.

Maintain existing speech interfaces by making ownership optional and keeping
PCM frames bytes-compatible. Additional direct utterances from the same request
still play locally; the existing first-direct-acknowledgement behavior is retained
for Telegram. Native Gemini responses and transcript-fed local TTS retain the
existing final-only path until actual model-request/response correlation is
implemented. No claim of full Phase 4 completion is permitted.

Verification: explicit context across thread/task boundaries, worker metadata
and final-marker ordering, actual playback with an available or missing fake
speaker, superseding local/voice/remote requests, repeated phrase events, final
delivery, legacy-ack suppression, flush cancellation, and preserved later speech.
Use mocked bot transport and synthesis throughout.
