# Call tool/provider binding and attended-test preparation — 2026-10-13

## Completed

The call runtime now creates its own protected Gemini client and configuration,
using the current era.voice model alias, configured voice and existing core prompt.
It connects through the managed session boundary. A separate UtteranceGate owns
remote speaker-verification buffers and closes with that provider conversation.
When verification is disabled, input remains unfiltered; no media/attention gate
was introduced. PCM buffering from the configured identity policy preserves frame
order rather than treating a list of safe frames as a boolean.

core/telegram_call_tools.py binds immutable CallCommand/InputEnvelope context to
Era's existing headless executor. It does not instantiate EraLive, share local
conversation state, invoke PC speech, or accept model-supplied approval flags as
consent. The existing executor still classifies risk, builds/stores previews and
verifies results. Explicit, speech-grounded memory saves are required on this path.

Only 27 existing executable declarations are advertised: apps, search, weather,
messages, reminders, YouTube, screen inspection, settings/performance, Spotify,
browser, Netflix, Premiere, files, desktop/computer, flights, confirmations, media,
file processing, memory retrieval/save/forget, windows and agent-browser. Crew,
CodeMode/coding delegation, local voice/standby settings, and the remaining main-only
tools are explicitly unavailable through this call path. This is not all 57 tools.

Call confirmations carry a generation-owned task scope in the existing shared
store. Scoped approval checks reject another conversation's action, stale assent,
same-utterance self-approval, and PC speech approving a call-owned action.
Authenticated Telegram button grants still require the exact stored action.
High-risk call actions require those authenticated buttons; lower-risk confirmations
can use a fresh explicit reply. Approval cancellation uses the stored ID and creation
time instead of blindly clearing whichever approval currently occupies the slot.

Call execution claims the existing live-task registry and appears as a real HUD
activity. Provider replays return the stored result; in-flight duplicates report
already running. Worker cancellation requests cooperative stop and suppresses late
success. The real worker retains its lease until it finishes; cancellation does not
claim that an OS action was undone. Completed ledger entries from old generations
are pruned, keeping daily repeated calls from exhausting a lifetime cache.

core/tool_execution_lane.py prevents a remote machine operation from racing local
automation. Local work retains its existing concurrency when no remote lease is
held. Existing main/headless executor entry points participate, and confirmation
tools inherit the resource of their actual stored action. Remote re-entry is allowed
only on the same worker thread, not another worker sharing the call generation.
Reads outside this machine-tool set are not artificially serialized. This does not
claim to govern every independent third-party or crew worker in the project.

## Attended test mode

Production calling remains disabled; the sealed account/owner settings were checked
read-only and still report calling_enabled=false. No credentials were changed.

The new explicit command prepares a 30-minute owner-only manual test window:

```powershell
python -m tools.telegram_call_test_mode --enable
```

It validates the separate account and independently bound owner, preserves sealed
credentials, and places no call. Restart Era and refresh the HUD after enabling.
Use **Call owner** for PC-to-phone. Use **Receive calls**, then call Era's separate
account from the owner's personal Telegram for phone-to-PC. The incoming listener
is manual and handles one generation; re-arm Receive calls after a completed call.

For a clean first acoustic test, keep the PC microphone muted through its normal
HUD control while speaking through the phone, so local and phone conversations do
not both hear the same physical speech. This is a user-controlled test condition,
not an automatic device or privacy preference change. Check glasses routing
afterwards and restore the user's chosen local microphone state.

Verify both sides hear normal and quiet speech, captions remain call-local, “stop”
interrupts speech, a low-risk request such as opening an already-installed app
works with truthful results, and Hang up ends the correct call. Test no high-risk
file/system/message mutation merely to prove calling. High-risk approval behavior
was verified with mocked drivers and exact stored grants.

Expiry disables new starts in the runtime and helper; a waiting/active native helper
also stops at the test-window deadline. No proactive calls or permanent production
enablement is introduced. To disable new calls explicitly:

```powershell
python -m tools.telegram_call_test_mode --disable
```

Hang up any current call and restart Era to refresh controls after disabling.

## Verification and corrected failures

- Expanded call, HUD, Telegram, approval and ownership regressions: 538 passed in
  28.32 seconds before the final added registry-reachability regression.
- Additional remote-tool/local voice-session run: 142 passed (28 remote cases plus
  114 local cases) in 8.67 seconds before that final added regression.
- After the registry fix, all 29 remote-tool cases passed in 7.70 seconds.
- Execution diagnostics: 22/22; main bridge: 127/127; registry validator: 74/74.
- Final scoped tool/lane/activation/runtime coverage before the additional registry
  regression: 61 tests passed, 96.55% statement coverage. Runtime 97%, tool actor
  94%, activation 96%, execution lane 100%. Coverage ran against the installed
  project environment with only the coverage package added to that process's path.
- Strict mypy for new source modules, scoped Ruff/Black, compilation and whitespace
  checks passed. Doctor after the fix: zero errors, zero warnings, READY.

Existing SDK deprecation warnings remain in pytest. The first isolated coverage
attempt lacked Playwright, because the existing approval language helper imports
the main dispatcher; the project environment contains that dependency. Coverage
was rerun against the installed environment without installing/replacing production
packages. Earlier typing/import-order findings were fixed. One existing readiness
test expected the old runtime_policy_pending reason; the completed binding correctly
returns validation_pending until explicit test/validation opt-in, and its expectation
was updated. Two patch context mismatches were rejected atomically and reapplied
against the formatted source.

Doctor initially reported confirmation handlers missing. The registry extractor
followed only one wrapper depth, while the new admission boundary adds a reachable
ownership wrapper. It now follows dispatch-family calls from the actual entry point,
handles recursive references, and ignores unreachable helpers. Both the new
reachability regression and original 74 validator checks pass. The validator's
existing main-only capability warning remains informational for Doctor; it is not
an unresolved missing-handler error and does not mean phone calls expose those tools.

## Still partial/pending

No test window was enabled, Era restart, real Gemini conversation, Telegram call,
recording, external message, destructive action or device-route change was performed.
The full global suite was not rerun. Actual PC startup and attended incoming/outgoing
acceptance are next, including real input format, audibility, echo, quiet speech,
noise/barge-in, local-channel interference, latency and bounded native hangup.

Provider renewal/fallback, automatically re-armed incoming listening, proactive call
reasons/notifications and unanswered fallback remain pending before daily native-call
use. Crew/CodeMode and all remaining main-only tool availability are separate work.
The native library's natural-finalizer hang remains contained by the owned helper,
not repaired. Deferred local hearing checks, Phase 6 work and lost schedules remain
open and were not marked complete.
