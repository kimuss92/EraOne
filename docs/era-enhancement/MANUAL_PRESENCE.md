# Manual presence without phone location

2026-10-14. Supersedes Telegram live-location requirements for this owner's
selected mode. The owner requested no extra iPhone app, no shared location,
natural English declarations, and automatic recognition of returning to the PC.

## Behavior

Natural, whole-utterance departure/return intent is handled locally, without
depending on a model choosing a tool. Examples: "I'm heading out now", "I'm off
to work", "I'm going out shopping", "I just got home", "I'm back at my place".
French variants and /away, /home remain supported. The parser rejects questions,
negations, quoted/explained statements, hypothetical and tomorrow's plans; it is
a bounded recognizer, not an unlimited semantic interpretation guarantee.

Voice (Gemini completed local input), HUD text, owner Telegram text and owner
native-call completed input can set presence. Telegram text receives a direct
confirmation, local input writes the confirmation and speaks it; phone input
updates the call caption and the provider acknowledges the declaration naturally.
Only accepted user input is used; notification/tool data cannot set presence.

Away enables existing automatic notification calls for eight hours. Home blocks
them. Expiry becomes Unknown and blocks calls; no guessing from PC inactivity.
Existing quiet hours, deduplication, owner binding, cooldowns, daily attempt limit
and final pre-dial presence guard remain. Saying departure does not itself dial.

State and absolute expiry persist encrypted across restarts. Manual mode removes
the saved home coordinates and ignores subsequent GPS shares. /presence explains
manual state and expiry. /autocalls on|off still works. /sethome explicitly allows
returning to location-based setup if later requested; it is not needed now.

## Return-home safeguard

Windows low-level keyboard and mouse hooks report only an activity signal, never
store keys, key text or pointer locations. Key-down, click and scroll qualify;
mouse movement alone does not. Injected flags exclude software-generated input
from Era's PC control. Callbacks only post a throttled signal to the owning loop;
state writes happen outside the hook. Owned hooks are removed on shutdown.

If the selected state is manual Away, local input changes it to Home after a
30-second departure grace. This also clears an expired Away declaration. It
blocks queued automatic dialing through the existing final presence checks.
Other people using the PC can conservatively trigger Home; this is presence
evidence, not biometric identification. If native hooks are unavailable, Era
logs that limitation and explicit return commands remain available.

## Verification and activation

571 targeted call, presence, approvals, task reporting, scheduler, standby and
input-ownership regressions passed. Added cases cover natural wording, rejected
contexts, encrypted restart persistence with no coordinates, eight-hour expiry,
manual calling without a home point, return-home veto and injected-input flags.
Scoped Ruff/Black, strict mypy and compilation passed. A read-only native hook
probe registered and stopped successfully; its callback did not change presence.

Verification caught a mocked call missing its generation and an import-boundary
Any return annotation; both were corrected. Existing Google SDK emits one pytest
deprecation warning. The complete project-wide runner was not rerun.

The protected configuration was switched to manual Home and read back: coordinate
storage removed, automatic calls blocked. No call was placed or Era restarted by
the coding agent. One owner restart loads the new handlers/watcher. Live voice
recognition and automatic Home restoration still need owner acceptance.

The owner should stop the existing Telegram live share once. This code cannot
press Stop Sharing on the iPhone or erase the location messages already stored in
Telegram. The earlier reminder shortcuts may be deleted on the iPhone.

Win32 injection flag definitions verified against Microsoft's KBDLLHOOKSTRUCT
and MSLLHOOKSTRUCT references:
https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-kbdllhookstruct
https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-msllhookstruct
