# Conversation attention timing — 2026-10-13

User confirmed interruption works without shouting. Unnamed follow-ups still
need Era's name after roughly ten seconds of inactivity. Actual listening config
has no override: the default is 45 seconds, anchored at explicit address, so
reply duration consumes the interval.

Increase the default and this user's saved listening window to 120 seconds.
An explicit address marks one pending conversational reply. Its completion
refreshes the explicit anchor once, allowing the full window after that reply.
Ordinary activity and subsequent autonomous replies cannot keep media listening
open indefinitely. Preserve the quiet-room always-open rule and echo hold.
This is the existing attention filter, not a standby transition.

Verify long-reply timing, exact expiry, non-explicit reply non-renewal, configured
window overrides, background-media rejection and existing safety/voice behavior.
Preserve other config fields. Phase 4 live conversational acceptance remains open.
