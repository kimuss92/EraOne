# Search speech and cancellation repair — 2026-10-13

## Evidence and cause

Inspected frames from the supplied 15.5-second recording and matching local logs.
At 15:45, two Gandhi searches overlapped. The separate grounded-search model hit
429 quota exhaustion; the first DuckDuckGo fallback returned six raw listings,
and the second fallback failed. This was a search-model quota failure, not proof
that the Gemini Live microphone or voice session had disconnected.

The detached-tool reporter truncated the first listing to 600 characters and
wrapped it in a **say exactly** instruction. It then submitted the second report
while the first model request was active. This overrode the normal prompt's
instruction to summarize, and risked interrupting the answer with a second report.
Era's later feedback entry was recorded successfully, but recording feedback did
not repair this code path.

The recording also shows a long mixed-language transcript containing explicit
cancel orders. Local cancellation previously required the entire utterance to be
one order and deferred bare cancellation during partial transcription until turn
completion. That made cancellation inside the observed utterance ineffective.

## Repair design and implementation

Keep the existing nonblocking tool execution and HUD sources. A search-specific
report supplies bounded, JSON-encoded evidence to the live reasoning model;
it requests a brief supported answer, not literal speech. This also works when
the live model's generated output uses Fish/Kokoro. No extra summarization API
call or additional search-model quota is consumed.

Search reports serialize and wait while the user is speaking, playback is active,
another model reply is pending, or a live tool response is in flight. An empty
tool-only turn may receive its awaited search evidence without deadlocking.
The report starts a new turn and clears the retained tool-only flag, so a second
report cannot use that flag to overlap it. Standby completion requests the existing
wake path. The report survives temporary session loss; after 120 seconds without
delivery, it explicitly logs deferral and retains the real result in the HUD.

Cancellation remains available while a completed search is awaiting delivery.
Search workers now carry the cancellation token into the thread; canceled work
does not retry or launch another backend. An already executing network request
cannot be forcibly stopped, but its canceled result is discarded.

A complete spoken cancellation sentence can act during partial transcription,
including within a longer punctuated utterance and with “right now.” Quoted
examples, negated requests, incomplete phrases, bare media stop, and power commands
remain outside this cancellation lane. Ordinary typed parsing is unchanged.

The alternative of adding a prompt reminder alone would leave the contradictory
say-exactly wrapper in place. Adding another cloud summarizer would add latency
and another quota dependency. The repair addresses the existing delivery path.

## Verification and limits

142 focused checks passed: bounded evidence, early voice cancellation, quoted and
negated controls, failures, canceled retries, serialized reports, empty tool turns,
reconnection, and explicit deferral, plus existing cancellation, crew, responsiveness
and input-ownership regressions. Scoped Ruff and compilation passed. Doctor:
zero errors, zero warnings. The Google SDK still emits its existing Python
deprecation warning during pytest; this is separate from Doctor readiness.

Two import-order lint findings were corrected. A new deadline test initially
patched Python's global monotonic clock and hung the event loop; it was stopped
and corrected to replace only the reporter module's clock. Final focused tests
pass. No live Era restart, microphone recording or external messages were performed.

Before this repair, the complete 33-gate readiness suite passed in 401.5 seconds.
That result covers the preceding file-awareness increment, not these later edits.
After the repair, the updated speech gate passed all 629 tests in 23.41 seconds,
including the new search reporting regressions (one existing SDK deprecation
warning). The full 33-gate suite was not rerun after this focused repair.

Live acceptance remains pending: restart Era, ask for a short Gandhi summary,
interrupt with “Era, cancel the task,” and immediately ask an unrelated question.
Confirm that Era gives a brief answer, stops the unwanted topic, and answers the
new question without reciting source listings. Model summarization and speech
recognition still require live confirmation; no deterministic claim of perfect
recognition or elimination of all background-media responses is made.

Search quota availability is external and has not been increased by this repair.
The observed partial transcript included background/mixed-language speech; this
repair adds no media playback gate, in accordance with the user's instruction.
