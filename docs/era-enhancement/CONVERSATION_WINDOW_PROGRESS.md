# Conversation window — 2026-10-13

User confirmed normal-volume interruption now works perfectly. Commands are
heard when addressed to Era, but unnamed follow-ups require her name sooner
than expected. No listening override was saved: the old default was 45 seconds
from explicit address. Reply duration consumed that window.

Changed the default and user's saved listening window to 120 seconds. The first
reply following explicit address refreshes the anchor on completion once.
Recognized input transcripts beginning with Era/wake names also mark explicit
activity while the window is already open. Ordinary speech and autonomous
replies do not renew it. Media-dependent expiry, quiet-room always-open behavior,
echo hold, speaker checks and the repaired ignored-state transitions are retained.
Other fields in config/voice.json are preserved.

Focused tests: 212 passed, one existing SDK deprecation warning, 25.76 seconds.
Initial run: 194 passed, one failed historical test that expected expiry 45
seconds from address even when Era's explicitly requested reply had just ended.
Updated that boundary expectation to the approved completion-based interval;
retained rejection of autonomous renewal and added separate long-reply and
already-open named-transcript regressions. Selected Ruff and diff checks passed.
Registered speech suite: 420 passed, one existing SDK warning, 35.56 seconds;
results are recorded in conversation-window-checks.txt. Manifest currency and
final diff checks passed.

Manifest regenerated after source/config changes. Full 31-gate global run passed
before this bounded timing change; it has not been rerun for this change. The
registered speech gate and focused checks provide current behavior evidence.

Live follow-up timing still needs verification after restarting Era. Phase 4
remains partial at that acceptance gate; Phase 5 has not started. Interruption
success is user-confirmed, rather than merely inferred from synthetic tests.
