# Phase 6 — observed state and task reliability

Authorized 2026-10-13. Phase 5 global software gate passed all 32 checks; user
explicitly deferred Telegram calls until a separate account is ready and asked
to move to Phase 6. Acoustic and live-button acceptance remain in the register.

## First increment: read-only media observations

Existing StateStore supports sequenced, detached snapshots and event waiting,
but BackgroundManager is not wired to Era startup. Its old Windows media loop
polls, guesses session identity, silently ignores failures, and can pause other
sessions. Do not start that mutating loop as an awareness feature.

Choose a read-only Windows GSMTC observer through a hidden persistent PowerShell
helper, matching the existing Spotify transport-host approach. Python WinRT and
winsdk are absent here. A compiled .NET callback adapter can register the native
session and playback events without running PowerShell on callback threads.
PowerShell's standard Register-ObjectEvent cannot subscribe to these events;
an on-machine probe confirmed the .NET adapter registers successfully. It does
not pause, play, change defaults, or reconnect Bluetooth.

The helper publishes an initial full snapshot, snapshots on session/playback
events, and a 15-second reconciliation heartbeat. A full list replaces the old
list atomically, so closed sessions disappear. Session identities are scoped to
the helper generation; process restart cannot merge old and new IDs. Ambiguous
browser sessions stay unattributed instead of being guessed as YouTube/Netflix.
Fresh playback observations and watcher health are distinct from requested state.
Unavailable, stale, and unsupported telemetry are unknown, not paused or success.

Reuse StateStore for media observations. One process-wide lifecycle service feeds
its detached snapshot to HUD events/snapshots and provides a synchronized read
view for reasoning and the legacy media cache. The legacy cache retains intended
state for compatibility, but OS evidence is separate and expires. Its is_playing
helper uses fresh known OS evidence when available. Requested actions do not
overwrite an observed manual change or claim verification.

Session configuration reads fresh observations rather than inferring playback
from a window title. Live changes update the queryable reasoning cache and HUD;
an already connected model must still query a live status tool. Do not inject
unsolicited model turns or reset voice sessions to refresh context. Treat
external source labels as data, never as instructions. Preserve existing media routing and Spotify/Netflix
API-free control. Browser DOM attribution, foreground/app/file/network watchers,
device event unification, selector/workaround upgrades, and latency measurement
remain subsequent increments.

Alternatives: reconnecting the legacy poller risks automatic playback mutation;
adding a Python WinRT dependency changes the runtime without proving this Python
version's compatibility. A small observer/helper reuses an established transport
pattern and has a read-only on-machine verification path.

## Verification

Fake transport: manual playing → paused; replacement/removal; empty sessions;
duplicate/ambiguous sources; restart identity; malformed messages; unavailable or
stale observations; clean stop/restart; no silent failures; HUD detached snapshots
and event updates; observed state superseding intention; no playback mutation.
Read-only native probe: helper registers manager/session events, reads one
snapshot, and exits cleanly. No playback, live Era restart, or recordings.
Run existing state, media, HUD and tool-routing regressions. Report what is
software-verified and what needs actual manual play/pause acceptance.

## Browser increment

Extend only already-connected browser sessions. Attach a read-only top-document
observer to exact YouTube/Netflix domain families, with native media DOM events,
coalesced element replacement observation, and a 15-second reconciliation timer.
Event callbacks trigger a fresh DOM read; ignore all claimed page payloads.
Verify both the page URL and sampled document origin, reject child-frame callbacks,
and store only browser/source/session ID, state and freshness (no full URLs,
titles, page bodies, cookies or credentials). Remove observations on close,
navigation away or driver disconnect. Guard in-flight reads against navigation
and generation changes, and never bring pages to the foreground to observe them.

A shared provider cache supplies source-specific evidence, HUD events/snapshots,
and session-context reads. Multiple tabs remain distinct. Aggregate playing if
any verified tab is playing; do not call a source paused if another tab is unknown.
Expired, failed, absent-player and buffering evidence remain unknown. After live
browser observation begins, old intended playback flags cannot substitute for
missing evidence. Existing browser control remains the only mutating path.

Fix false pause confirmations where the background fallback currently returns
success merely because pause() was called. Use read-back and sampled origin.
Verify with fake pages, malicious payloads/origins, stale reads, navigation,
multiple tabs, interrupted/failed subscription and driver replacement. Execute
the JS observer against an isolated local test harness without opening or
changing the user's tabs. Real media acceptance remains separately tracked.

Alternatives: continuously reading all browser tabs costs latency/privacy;
labeling generic GSMTC browser sessions guesses the platform. Reusing connected
page hooks with a narrow provider is the chosen incremental approach.

API contracts: https://playwright.dev/python/docs/api/class-page#page-expose-binding
and https://playwright.dev/python/docs/api/class-page#page-add-init-script.

- [Microsoft SessionsChanged](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssessionmanager.sessionschanged)
- [Microsoft PlaybackInfoChanged](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession.playbackinfochanged)

Only native system events drive immediate media changes; periodic reconciliation
recovers missed events and supplies freshness evidence. This does not claim
literal zero latency or universal knowledge of every app.

## Playback recovery increment

Keep existing API-free controllers and target selection. A missing target,
rejected playback or interrupted DOM command cannot safely justify a global
keyboard toggle: its destination and previous outcome are unknown. Return the
failure with an explicit unverified result instead. Confirmed DOM controls remain
unchanged. This trades an unsafe fallback for a recoverable, truthful failure.
Adding a new targeted keyboard controller would require separate selection and
read-back work; blindly repeating the command is rejected.

The legacy exclusivity sweep must not mark another source paused merely because
it attempted a pause. Use the connected browser's targeted pause/read-back path,
and fresh native paused evidence for Spotify after its existing pause-only action.
Retain unknown/failed state and log incomplete enforcement. Preserve callers and
the intended-state contract for the active source; migrating every caller's
before/after-play timing remains a subsequent task-family increment.

Verify missing targets, rejected/ambiguous commands, successful read-back,
failed pause state retention, partial sweep reporting and browser routing
regressions with isolated fakes. Do not control the user's live playback.
