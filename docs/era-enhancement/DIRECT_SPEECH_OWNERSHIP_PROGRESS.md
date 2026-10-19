# Direct speech / Telegram playback integration — 2026-10-12

Status: implemented and verified for explicitly owned direct Fish/Kokoro
utterances. Phase 4 remains partial; no later phase has started.

## Implemented

- Handler context retains the immutable originating input through threads and
  child tasks. Source is not inferred from the latest accepted input.
- Direct speak() calls from a Telegram-owned handler suppress the old immediate
  final acknowledgement. Ownership crosses onto the audio loop explicitly.
- Sentence synthesis carries ownership into PCM-compatible frames. A zero-PCM
  end marker follows all frames of the owned utterance; it is never written to
  the output device or counted as played audio.
- The real playback loop reveals text at the first successful phrase write and
  schedules the final Telegram message after consuming the end marker.
- New input synchronously invalidates the older owner before queued frames can
  contribute text. The first recognized voice fragment also disarms the legacy
  remote flag and reports an interrupted pending remote reply.
- Playback failure, synthesis failure, flush, native interruption and teardown
  cancel owned delivery. A missing speaker cannot produce a confirmed final
  reply. Cancellation records whether playback failed or the user interrupted.
- Delivery outcomes include request IDs in structured logs. Unconfirmed final
  delivery is surfaced in the UI log. Network work stays outside the audio loop.
- Later direct utterances from the same request still play locally. The existing
  first-direct-utterance Telegram acknowledgement behavior is preserved.

## Verification and failures

**225 distinct focused checks passed**: 171 in the combined new ownership,
delivery, synthesis, playback, input, Telegram voice and integration-gate suite
(23.15 seconds), 45 panic-stop checks (1.04 seconds), and nine existing text
command parity checks (4.10 seconds). Each run reported the existing Google SDK
deprecation warning. Selected Ruff checks and git diff --check passed.

The new tests exercise the actual playback loop with fake output devices,
including complete output-device loss. They also cover context across thread/
task boundaries, owner metadata, end-marker ordering, repeated phrase events,
new local/voice/remote owners, legacy-ack suppression, preserved later speech,
voice interruption notices and immediate flush invalidation.

One initial playback test used a nonexistent FakeDevice constructor option;
corrected to the fixture's fail_opens parameter. Initial Ruff checks identified
three import-order issues, which were corrected. Two narrow reconnaissance rg
calls used PowerShell-incompatible wildcard paths and returned errors; explicit
directory/glob arguments replaced them. No failing verification was skipped.

Regenerated the manifest after all production/test changes: 333 modules, 57
tools, zero unparseable files. Manifest/registry/executor/first-patch/smoke gates
passed as part of the combined run. No live Telegram messages, cloud synthesis,
hardware recording, OS preference changes or Era restart were performed. The
full repository suite was not rerun.

## Remaining and limitations

- Native Gemini responses and transcript-fed local synthesis retain the existing
  final-only Telegram path. Actual outgoing model-request ownership, tool-only
  continuations, stale server response boundaries and reconnects remain open.
- Local offline fallback through local_speak() retains its existing mirror.
- Preview timing is phrase-level: text appears after the phrase's first accepted
  output write. There is no word-level or acoustic synchronization guarantee.
- Final delivery follows device acceptance of all queued frames, not measurement
  that the user heard every word. Submitted HTTP requests cannot be recalled.
- Additional direct utterances in one request play locally without additional
  progressive Telegram replies, matching the old first-ack behavior. Combining
  multi-utterance task results needs an explicit response lifecycle later.
- A draft unsupported by the connected Bot API falls back to final delivery.
  Live private-chat draft acceptance and hardware timing remain untested.
- Seamless Gemini/Fish/Kokoro failover and native caption alignment are pending.

The confirmed glasses microphone preference is preserved. No destructive
cleanup, dependency upgrade, task-database write or CodeMode implementation
occurred during this slice.
