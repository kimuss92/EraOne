# Phase 2 audio stabilization design

Status: proposed implementation within the user's approved audio phase.

## Problem and approach

The existing routing layer saves stable endpoint preferences and recovers from
device failures, but conflates intent with observed playback. Preserve this
architecture and repair its contracts. No driver changes, pairing changes,
PortAudio replacement, or live-preference migration is proposed.

Alternatives: replace the audio backend (largest regression risk); patch only
response strings (leaves false state and recovery churn); repair route evidence
and stream lifecycle together (recommended).

## First patch: verified route state and bounded recovery

1. Respect input/output flow even for an exact endpoint ID. Refuse an ambiguous
   same-name replacement when a stored ID is no longer available.
2. A routing command reports preference accepted/pending until a playback stream
   acknowledges the requested generation and device. Status distinguishes
   unknown, mismatched/fallback, and matching observed routes.
3. Carry the generation captured for the stream open, so a late acknowledgment
   from an older request cannot validate a newer request. Clear observed route
   state on stream closure/failure. Report the stream's actual device index when
   the driver exposes it, not merely the requested default sentinel.
4. System-prompt context must describe the requested pin and observed route
   separately. A stored setting is never presented as proof of audibility.
5. Attempt preferred-device recovery without first closing a usable fallback.
   Failed preferred opens back off exponentially with a cap. Explicit new user
   choices reset that recovery delay. Cleanly close failed candidate streams.
6. Keep source compatibility for current helpers/callers; additive result fields
   include requested generation, route status and routing verification.

## Remaining Phase 2 acceptance work

- Test stereo versus hands-free profile policy against the actual machine and
  preserve explicit stable-ID selections. Do not resolve multiple physical
  glasses by arbitrary list order.
- Add/validate endpoint notifications and off-loop reconciliation using existing
  playback generation handling; manually changing media must not silently erase
  an explicit voice pin.
- Bound repeated full-session re-enumerations when they cannot restore a route.
- Verify actual PC/glasses stream binding and manual changes, plus a sustained
  observation for the reported beep. Report gaps without labeling them fixed.

## Test matrix

New reproductions cover accepted-before-open, unknown route, wrong nonfallback
route, reused index/name mismatch, misleading prompt facts, wrong-flow IDs,
ambiguous replacements, and fallback preservation/backoff. Add success, closed
stream, stale acknowledgment, explicit reroute and recovery cases. Rerun existing
audio, voice, media-persistence, TTS, and cancellation regression suites.

Driver stream-open evidence is distinct from the user hearing speech. A physical
listening test remains part of phase acceptance. No later phase should be called
started while this phase's required acceptance checks remain incomplete.
