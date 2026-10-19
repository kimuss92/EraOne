# Progressive Telegram delivery — next Phase 4 slice

Status: design/research; delivery is not implemented by the input migration.

## Supported channel

The official [Bot API draft method](https://core.telegram.org/bots/api#sendmessagedraft)
supports partial private-chat previews keyed by a nonzero draft ID. Drafts expire
after 30 seconds; a final sendMessage is required to persist the full reply.
An [edited-message alternative](https://core.telegram.org/bots/api#editmessagetext)
can reuse a bot message ID, with [retry_after](https://core.telegram.org/bots/api#responseparameters)
provided for flood control. Verified against official documentation on 2026-10-12.
The agent-reach/Jina fetch succeeded but its assumed heading markers did not
match; the official page was then inspected directly. No owner credentials or
live bot endpoint were used for this research.

Prefer drafts plus final delivery where supported, with bounded throttling and
failure reporting. Keep plain text. Do not add rich formatting or an unsupported
scraped client. Evaluate the edit-message path as compatibility fallback.

## Required ownership work before wiring

Capture input identity when an actual model request is sent; associate each
response turn with that identity, rather than the latest accepted command.
Carry the owner through local speech phrases and beyond model turn_complete.
Preserve tool-only continuations. A local/voice interruption must not redirect
an older response into a newer Telegram request or expose a local conversation.

For local TTS, reveal phrases only on successful playback events. Provider arrival
is not playback. Native Gemini lacks phrase/audio alignment; expose that timing
limit and test it before promising word-level synchronization. Complete a remote
reply only after its delivery/owner lifecycle has finished; do not duplicate the
existing final-only _tg_notify path.

## Transport behavior and verification

Use a bounded per-owner delivery worker, coalesce pending updates, honor server
backoff, and record draft/final outcomes. Avoid automatic send retries after an
ambiguous network result that could duplicate a persistent message. Long replies
need deterministic chunking without loss and Unicode-aware limits.

Tests must cover two remote requests, local/voice interruptions, delayed TTS,
tool-only turns, stale playback, reconnect, throttling, flood control, timeout,
final-message failure, long Unicode text and no duplicate final send. Mock bot
transport throughout; explicitly report any required live acceptance separately.

## Delivery component implementation boundary

Implement the transport and per-request worker first, without attaching the shared
reply flag to it. Each worker accepts one immutable Telegram InputEnvelope and an
explicit destination captured by its caller. Only that worker's revealed phrases
contribute to its preview and final text. Coalesce preview updates, serialize its
network calls off the audio loop, and cap retained reply text at 65,536 characters.
Cancellation suppresses queued/final sends; an already submitted HTTP request
cannot be recalled. Split final text without loss at conservative UTF-16 limits.
Only explicit flood-control rejections are retryable for persistent sends, with
at most three attempts. Ambiguous network or acknowledgement failures stop the
send and report an unknown outcome. Unsupported drafts disable previews while
retaining final delivery; other draft failures remain observable.

This component is a prerequisite, not live progressive speech delivery. Wiring
it before model-response and playback ownership are established would preserve
the existing leak. Legacy send_message and approval notifications retain their
current behavior during this isolated slice. Verification uses injected/mock
HTTP and clocks; no real chat messages are sent.
