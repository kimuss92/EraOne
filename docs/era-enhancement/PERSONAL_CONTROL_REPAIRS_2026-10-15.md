# Personal control repairs — 2026-10-15

User approved the concrete repair design in chat. Existing MASTER_PLAN authority
also covers Phase 6 reliability repairs and Phase 7 HUD control integration.
Preserve the user's HUD layout and appearance. Speed is the primary optimization
goal, with truthful playback verification and exact approval ownership retained.

## Findings

- The production Spotify search at 13:53:05 UTC was still running when cancelled
  from the HUD after 45,843 ms. Its UIA operations had stacked independent waits
  and potentially blocking cross-process COM calls. An already-running reply
  indicated local duplicate suppression, not proof of Spotify API congestion.
- The favourite was saved as `Sawal El Reem by Mikasa`; the user's supplied
  original title is `سواد الغيم by Mikasa Music`.
- PowerShell's default console encoding changed Arabic media titles to question
  marks. Correctly started Arabic tracks could therefore fail identity checks.
- Search verification accepted any changed track. The audio-only fallback could
  also report success without identifying the requested song.
- Native Telegram call control existed in the HUD/runtime but had no model-facing
  declaration. The model only saw Telegram text messaging.
- HUD Approve/Cancel sent ordinary `yes`/`no` messages. Completion depended on the
  model interpreting them and invoking the right confirmation tool.
- The generated capability manifest was stale before this work.

## Implementation

Spotify UIA search runs in an owned, hidden subprocess, polled every 100 ms.
Cancellation or deadline expiry kills and reaps that process, preventing its
worker from issuing subsequent clicks. An OS action already dispatched cannot
be undone by cancellation. Search uses Spotify's desktop URI directly,
batch-cached accessible names/rectangles, and actual element clicks first. Warm
search targets a 12-second aggregate budget; cold launch targets 20 seconds.
Native media reads/cleanup can add bounded overhead; these are not guarantees
about Spotify/network response times. No service API or new credentials are used.

GSMTC reads now explicitly use UTF-8, and waiting for their shared lock is bounded.
Success requires the requested title/artist and active audio. Merely changing
tracks or finding an active audio session cannot establish search success.
Equivalent search aliases/argument names share an atomic in-flight claim.
Already-playing matches return immediately. Failure avoids post-action minimize.

The favourite is retained ahead of the trimmable prompt-memory budget. Personal
song references in live media tool calls resolve from current saved memory before
execution. Prompt instructions require retrieval before asking for missing
personal information and preserve original spelling. This improves grounded
recall; prompt compliance for every arbitrary personal question is not claimed.
After all tests completed, the memory-manager API corrected the favourite to
`سواد الغيم by Mikasa Music` and saved the user's speed-first and memory-before-
clarification instructions. The favourite was read back and verified exactly.

`telegram_call` advertises owner-only status/dial/hangup/reception controls and
uses the existing live coordinator. Accepted/dialing never means connected.
The headless executor explicitly reports LiveSessionRequired; native call
control is offered through Era's live session/HUD, not all remote/headless lanes.
The generated manifest and Anthropic schema snapshot contain 58 tools.

HUD decisions use authenticated `POST /api/approval` with the displayed ID,
creation time and approve/cancel decision. Approval reuses existing classified,
atomic, verified exact-action execution and parked-job continuation. Duplicate
in-flight clicks share one submission; stale cards cannot approve replacements.
No new spoken assent is needed. The layout is unchanged; only button behavior
and decision feedback change.

## Verification

- Initial diagnostic baseline: 102 passed, three skipped.
- First repair regression group: 96 passed, three skipped.
- Updated focused group: 107 passed, three skipped.
- Additional focused endpoint/worker/approval checks: 20 passed.
- Frontend production build and `npx tsc --noEmit` passed.
- Live first probe: hung UI worker stopped after 9.08 seconds; no false success.
- Live UTF-8 reader returned the exact Arabic title and Mikasa Music artist.
- Live already-playing favourite request: verified response in 0.63 seconds.
- Live paused favourite -> search -> actual playback: verified in 4.44 seconds,
  with native state changing from paused to playing and position restarting.
- These latency measurements are individual local samples, not percentile claims.
- Era Doctor: READY, zero errors, zero warnings.
- Broader targeted regression gate: 4,032 passed, three skipped, one manifest
  freshness failure in 889.16 seconds. The approval helper was edited while this
  run was in progress; the generated manifest was stale at the assertion. This
  run exited unsuccessfully and is not reported as a green gate.
- Regenerated the manifest after all source changes. A clean final process ran
  the complete manifest, personal-control, tool-schema, registry and HUD approval
  groups: 52 passed in 44.52 seconds. See
  `runtime/personal-control-repair-final.log`. The exact broad-run failure is
  resolved; combined evidence covers 4,033 passing cases and three skips.
- A final approval-test assertion was changed to await an explicit completion
  event and mock verification/queue dependencies, replacing a timing-dependent
  20 ms sleep. The updated test verifies one execution and one successful report
  for duplicate clicks; it passed in the final clean run.

Existing tests initially caught the intentional tool-count change (57 -> 58) and
the changed approval-button contract. Their expectations and reviewed schema
snapshot were updated; those tests passed in the 107-test rerun.

## Remaining work and phase order

Load the new backend and prompt with one Era restart and reload the HUD. Check a
natural spoken owner-call request and a disposable real HUD approval after restart.
Do not place a test Telegram call without the owner's request. Earlier acoustic,
glasses, away-call, device and folder acceptance remains tracked separately.

Phase 7 retains the user's accepted HUD. Its remaining scope is live voice/mail
acceptance, richer functional widgets and broader agent-driven customization.
Phase 8 is Hermes/CM/crew ownership and coordination plus the separately reviewed
CodeMode design. Phase 9 implements the approved Claude Code subscription bridge.
Phase 10 covers guarded self-improvement, validated learning and Jev integration.
Phase 11 covers reviewed cleanup, documentation, setup and final acceptance.
These later phases have not been silently marked implemented by this repair.

## Follow-up: literal search text and authoritative personal recall

The user reported visible plus signs in Spotify's search field and requested
memory verification before personal clarification or an unknown answer.

Native UIA read-back confirmed literal `+` characters from the Spotify search URI.
The worker now first enters and verifies the literal query in the native search
field. Cached ComboBox/Edit discovery avoids a full descendant walk. Chromium
ignored SetValue on this installation; the fallback verifies foreground/focus,
selects the old text and sends escaped Unicode keyboard input without changing
the clipboard. It reads the resulting value back before reporting success.
The existing bounded worker remains in use. URI navigation is retained only when
literal field entry is unavailable; its cosmetic encoding can still appear then.

Live field read-back now exactly matches `سواد الغيم by Mikasa Music`, with real
spaces, in 0.53 seconds. This was a search-field check; no new playback was started.

Read-only memory probing found a saved instruction mentioning "song" above the
actual favourite in relevance results. Explicit own-favourite queries now return
the authoritative preferences/favorite_song fact directly, bypassing embeddings,
instructions and a child's song. Explicit son's-song lookup resolves the child's
own stored fact. Missing slots still use the existing retrieval path and never
invent a value. Existing prompt and saved operational rules require relevant
memory/context lookup before personal clarification or a claim of ignorance.

All three live owner queries (my song / my favourite song / favorite song)
returned the exact saved title and artist first; their combined read-only ranking
time was 37.64 ms including first import. Focused memory, semantic retrieval,
Spotify, cancellation, personal control and manifest checks: 165 passed, three
skipped in 39.89 seconds. Scoped Ruff lint/format passed. Source was stable before
manifest generation and the test run. Evidence:
`runtime/spotify-memory-followup-tests.log`. Restart Era to load the revised
in-process memory ranking; the subprocess search implementation reads fresh code.
