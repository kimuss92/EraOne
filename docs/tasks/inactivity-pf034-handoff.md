# PF-034 + PF-008 + PF-031 candidate for independent review (dev-build, 2026-11-16)

Repo: C:\Users\nesri\OneDrive\Documents\EraOne (WSL /mnt/c/Users/nesri/OneDrive/Documents/EraOne), branch era/task-lessons, uncommitted.
Owner asked dev-build directly in its pane (after the Codex reassignment) to investigate and fix "Era answers without her name after the session window". Built ON TOP of Codex's uncommitted PF-032 candidate (core/attention.py), not a replacement.

## Root cause found in live logs (logs/era.log, 2026-11-16 UTC)
- Owner voiceprint verification is DISABLED (every wake/unmute verdict logs "disabled"), so all room audio reaches the attention gate.
- 17:59-18:07: music playing, zero attention.ignored events: each passing segment end called note_activity() and re-anchored the window forever.
- 18:04:44: unnamed speech answered 5 min after the last order (17:59:28). Scheduled reports (18:00, 18:03) sent internal model requests -> PROCESSING -> _attention_keep_open True -> room audio passed -> gate PASSING start called note_activity() -> expired window re-opened.
- PF-032 (Codex) only stops a single PASSING segment outliving expiry; it does not stop re-anchoring.

## Changes (PF-034)
- core/attention.py: AttentionGate._reanchor_passing: passing speech re-anchors only if the window is really open (not via keep_open) and no media plays. era_finished_speaking(internal=True) neither re-anchors nor consumes a pending explicit address.
- main.py: _speaking_for_internal_turn (tracker active, owner None, reason unowned_model_request) decided at speech start; passed to _note_listening("finished", internal=...).
- app/era_live/audio.py: _attention_keep_open: an internal request in PROCESSING is not busy with the owner.
- Guards: tools/test_reanchor_sources.py (6).

## Also in this candidate
- PF-008 (owner rule change, asked directly): every mute (incl. voice) arms the owner-verified local unmute listener; "activate the microphone" / "turn on the (mic|microphone)" added to the grammar. Live 2026-11-16 18:58:57: voice-mute then "start listening" unmuted. Files: app/era_live/mic_mute.py, core/mic_mute.py, tools/test_activate_mic.py.
- PF-031: a screen number spoken by the owner overrides the model's screen arg for move_to_monitor/tab_to_monitor (model sent "primary" = display 2 for "screen one"); minimized window already on target is restored. Files: actions/monitor_move.py, core/self_healing_router.py, app/tool_declarations.py, tools/golden/tool_declarations_anthropic.json, tools/test_monitor_move.py.

## Checks (Windows Python 3.14)
- tools/protected_fixes.py run: 398 passed; registry OK; ruff check + format clean on touched files.
- Router suites (all tests using route_tool_call): 1104 passed. Typed/mic suites: 531+ passed.
- Attention/session/HUD/inactivity suites: 109 passed + 6 new.

## Not checked / owed live
- Wait > 2 min, speak without "Era" -> ignored (with and without music); scheduled report must not reopen.
- Voice-mute -> "activate the microphone".
- "Move Spotify to screen one" -> display 1.
- Note: speaker verification disabled means a stranger saying an unmute phrase can reopen the mic (grammar + confidence only).
