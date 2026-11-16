# Exact scope of dev-build's candidate (PF-034, PF-008 change, PF-031)

Patch: `docs/tasks/pf034-pf008-pf031-scoped.diff`. It is filtered to the hunks that
contain my edits, but HEAD is far behind the working tree, so those hunks ALSO show
older uncommitted base work that is NOT mine (2026-11-11 session-window refactor in
core/attention.py + untracked core/session.py, Codex PF-032 lines, the earlier
mic-mute source plumbing). A git-only isolation is impossible; review these exact
units in the CURRENT files instead.

## PF-034 (mine, all new today)
- `core/attention.py`
  - `ListeningState.__init__`: comment only ("media only decides whether unnamed passing speech may re-anchor it").
  - `ListeningState.era_finished_speaking(interrupted, internal=False)`: condition `self._explicit_reply_pending and not internal` + docstring. Was: `if self._explicit_reply_pending:`.
  - `ListeningState._media_playing()`: new (fail-strict True on error).
  - `AttentionGate._reanchor_passing()`: new.
  - `AttentionGate._feed`: the two `self.state.note_activity()` calls (IDLE->PASSING start, PASSING end) replaced by `self._reanchor_passing()`. Nothing else in `_feed` is mine.
- `main.py`: `_speaking_for_internal_turn()` new; `_note_listening(..., internal=False)` param; in `set_speaking`: `self._speaking_internal = ...` at speech start and `internal=` passed + reset at speech end.
- `app/era_live/audio.py` `_attention_keep_open`: the PROCESSING branch now `return not internal` (was `return True`).
- `tools/test_reanchor_sources.py`: new, 6 tests.

## PF-008 rule change (mine today; owner asked directly)
- `app/era_live/mic_mute.py`: in `mute_microphone`, `restore_mic_mute`, `_handle_set_microphone` the `if self._mic_muted_source not in _VOICE_UNMUTABLE_SOURCES:` guard before `_ensure_unmute_listener()` removed (always arm); `_unmute_hint` voice-only branch removed; module docstring + `_VOICE_UNMUTABLE_SOURCES` comment. `_ensure_unmute_listener`, `mic_muted_by_voice`, source persistence were ALREADY in the base.
- `core/mic_mute.py`: 3 phrases added to `UNMUTE_PHRASES`.
- `tools/test_activate_mic.py` (untracked base file): module docstring; replaced `test_a_voice_mute_is_not_reopened_by_a_phrase` and `test_repeated_voice_off_still_arms_nothing` with `test_a_voice_mute_is_reopened_by_the_owner_voice`, `test_an_unverified_voice_cannot_reopen_any_mute`, `test_a_typed_order_unmutes_a_voice_mute_without_a_session`, `test_repeated_voice_off_repairs_the_listener`.

## PF-031 (mine, all new today)
- `actions/monitor_move.py`: `spoken_screen`, `ground_screen` + regex constants; minimized-restore branch in `move_hwnd_to_monitor`. Whole diff of this file is mine.
- `core/self_healing_router.py` `route_tool_call`: the 7-line `ground_screen` block after `ground_explicit_track`.
- `app/tool_declarations.py`: two `screen` descriptions ("'Screen one' means '1'..."); golden regenerated (only the two "Screen one" hunks are mine).
- `tools/test_monitor_move.py`: 4 tests appended.

## Registry
- `docs/PROTECTED_FIXES.md` (untracked): PF-008 entry rewritten, PF-031 and PF-034 added.
