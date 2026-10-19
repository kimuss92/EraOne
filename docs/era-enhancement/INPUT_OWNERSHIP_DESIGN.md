# Phase 4 input ownership

Incremental extension of the approved plan. Preserve the existing intent,
approval, memory and lane-dispatch paths; do not introduce a competing parser.

Telegram currently arms a shared boolean on its polling thread before calling
the common text handler. UI and LAN callbacks infer source from that boolean.
This lets unrelated input inherit Telegram provenance. Source identity must be
captured at ingress, not guessed when a queued callback eventually runs.

Selected approach: an immutable envelope with unique ID, original text, explicit
source and receipt time. Desktop, LAN and Telegram adapters provide their source.
Commands are accepted on the existing running Era loop before source-dependent
state changes. No-loop handling retains the current offline acknowledgments.
All accepted text continues through the same existing handler. A new local
command superseding a pending Telegram request reports that interruption, and
must not mirror its local reply. Exceptions during queued dispatch must be logged
and reported to the originating channel rather than disappearing in callbacks.

Alternatives: a lock around the current flag would not identify queued input;
separate channel parsers would duplicate intent/memory behavior and drift.

Compatibility: retain legacy inferred-source calls for test/embedded callers;
all production adapters are made explicit. No credentials, preferences, driver
settings or production database are changed. Tests use mocked dispatch and
notification hooks plus the existing lane/text parity regressions.

This first slice establishes command ownership only. Native voice ownership,
model-turn/phrase correlation and progressive Telegram delivery are separate
remaining slices. The shared flag is retained temporarily for existing consumers;
it must not be presented as a completed response-ownership migration.

## Execution and priority

Explicit-source adapters queue delivery even when already on the Era loop.
An asyncio lock orders acceptance and the existing handler; blocking local
intercepts run through asyncio.to_thread, preserving the audio loop's progress.
Tracked delivery tasks remain alive until completion. Legacy in-loop/no-loop
calls retain direct dispatch for compatibility.

Abort bypasses that lock. A dispatch epoch invalidates older waiting commands,
which are logged as cancelled and acknowledged on Telegram when relevant.
Already running OS/thread work is not forcibly terminated; existing panic guards
and task cancellation still handle ongoing actions. This must not be described
as rollback of side effects already performed.

The LAN receipt notification now says received, not executed. Its response is
HTTP 202 with accepted/queued fields, documented in CM_COMMANDS.md. This is an
intentional API contract correction: external shortcuts requiring the former
HTTP 200/ executed field need updating; no in-repository clients were found in
the scoped reference check. Owner-chat and LAN-token authorization are retained.
