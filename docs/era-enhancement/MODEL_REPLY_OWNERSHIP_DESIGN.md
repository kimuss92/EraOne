# Gemini text request / response ownership

Continuation of the approved Phase 4 plan. Capture immutable input context at
the actual SDK send, after lane classification; the latest accepted input or a
shared Telegram flag is not evidence of which request produced a response.

Use a small session-local tracker. A single request may own response turns until
text completion. Empty tool-only turns preserve the owner. Same-request helper
text can retain ownership. A distinct or unowned text send while a request is
active makes attribution uncertain; suppress remote mirroring until the old
boundary drains. New input, voice interruption, output flush and session teardown
invalidate the owner without assigning buffered old output to the newer input.

Alternatives: latest-input attribution leaks delayed local replies to Telegram;
serializing all user input would block natural interruption. The selected
conservative suppression preserves local conversation and logs the reason when
remote delivery cannot be safely attributed. Send errors clear only their own
registration, avoiding races with a newer send.

Wrap existing text SDK sends without changing intent parsing, payloads, tool
dispatch or memory writes. Final Telegram routing requires both a valid captured
owner and a matching latest accepted input. Keep an explicit legacy fallback
only for embedded/test receivers with no input-envelope support. Production
ingress never uses that fallback. Tool responses are continuations, not new
user inputs. Voice audio remains on the existing path, with first transcription
invalidating a pending remote owner. Reconnect never carries the old owner.

This slice fixes final-message attribution. Native Gemini text/audio alignment
and transcript-fed local phrase ownership remain separate; do not expose
generated text as verified spoken progressive captions. Test delayed lane work,
two remote requests, local/voice interruption, tool-only boundaries, same-owner
continuation, send failure races and real receive-loop final routing with mocked
SDK and Telegram transports.
