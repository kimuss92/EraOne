# Era One — Protected fixes (READ BEFORE CHANGING ANYTHING)

### PF-040 · A busy exemption can never keep Era listening forever
- **Files:** `app/era_live/audio.py`, `app/hud/status.py`
- **Rule:** `_attention_keep_open` = `AudioIOMixin._busy_reason(self) is not None`. Busy reasons: a pending confirmation; PROCESSING for the owner's request only for `BUSY_PROCESSING_MAX_S` (120 s) after Era last entered PROCESSING (every tool call re-enters and renews it; internal requests never count, PF-034); a running local turn or local speech. Media playing is never busy. An error in the check is logged once and returns None (the window decides), never "busy" forever; an unreadable state history counts as no recent PROCESSING (no exemption). The HUD's `listening_mode` stays read-only (PF-033) and logs a `listening.mode` event with its reason (window open + seconds left, `busy: <reason>`, `window expired`, mic state) whenever the mode OR the reason kind changes (the seconds countdown alone is not logged), so an expired window held `active` by a busy exemption is named.
- **Why:** Live 2026-11-16 22:24:51-22:31 UTC: window set to 60 s by voice, nobody talking; inactivity never came, the HUD never showed it, and the gate kept sending room audio to Gemini (`engine.gemini.unresponsive`: "3 spoken episodes"). The only unbounded way past an expired window was the busy exemption (PROCESSING is left only when Era finishes speaking; the check failed open on any error). No state transitions were logged, so the exact trigger could not be proven; the new `listening.mode` log records it next time.
- **Guards:** `tools/test_busy_bounded.py`
- **Since:** 2026-11-16 · Claude · Windows tests only; live: set 1 min, stay silent, HUD must show INACTIVITY MODE within 1 min, or up to 2 min after Era's last tool call (busy grace); otherwise `listening.mode` names what kept it active

### PF-039 · "Stay silent" puts Era straight into inactivity mode
- **Files:** `core/attention.py`, `main.py`, `app/era_live/audio.py`, `app/era_live/tool_dispatch.py`
- **Rule:** A whole-utterance silence order (`is_silence_order`: "stay/remain/keep/be/go silent|quiet", optional greeting/"Era"/"please" before, "now/please/sir/for now" after; French "reste silencieux/se") calls `EraLive._enter_inactivity`: closes the listening window (`_close_listening_session`: mic and Live session stay warm, never `EraState.STANDBY` even when `standby_enabled` is true), drops Era's reply to that turn (`_barge_in_requested`, cleared at the turn boundary) and speaks nothing; once per turn. Voice is never acted on a partial transcript (dev-review: "stay silent" may begin "stay silent when I'm on a call"): while the growing transcript is a bare order, `_silence_partial` only HOLDS her reply output, in order and without dropping any (`_emit_or_hold` at every reply output site in `_receive_audio`: audio, native checkpoints, spoken text, HUD text), and skips `note_transcript`; more words deliver the held output so she answers in full. `_silence_finished` commits at Gemini's end-of-speech mark (held output discarded, barge-in latch set so the rest of that reply is dropped), with a turn_complete backstop that commits WITHOUT setting the latch (it must never swallow the next reply); one commit per turn. Typed orders are handled before `_STANDBY_INTENT`; an `enter_standby` tool call whose user text is a silence order becomes `_enter_inactivity`. Sentences that merely contain the words ("stay silent when I'm on a call") do not match. Her name reopens the window as usual.
- **Why:** Owner 2026-11-16: "make me able to make Era enter the inactivity mode by saying remain silent or stay silent so it immediately triggers inactivity mode and she responds only when I say her name." `standby_enabled` is currently true, so the old standby path would have gone to wake-word-only STANDBY instead.
- **Guards:** `tools/test_silence_order.py`
- **Since:** 2026-11-16 · Claude · Windows tests only; live "stay silent" by voice (no reply spoken, HUD shows inactivity, "Era, ..." answers) owed

### PF-038 · A real conversation keeps the window open; lyrics, films and Era's own voice never do
- **Files:** `core/attention.py`, `main.py`, `core/voice_gate.py`
- **Rule:** A voice input accepted while the window is OPEN (`_accept_input` -> `ListeningState.note_owner_turn(verified=_owner_voice_verified())`) is an owner turn; Era's reply to it re-anchors the window if the reply finishes within `OWNER_TURN_REPLY_S` (60 s) of the turn, even just after expiry (a turn taken after expiry, a stale turn, or an explicit close never re-opens it). While media plays, a voice turn counts ONLY when it was verified as the owner's (`_owner_voice_verified(era)`: voice lock enabled AND ready AND the speaker gate's verdict on the latest utterance it passed was a full scored match, `UtteranceGate.last_pass_fully_verified`; a short follow-up passed unscored or on the low follow-up floor never counts). With the lock off and media playing only an explicit address (name, wake, typing) extends the window. Internal speech neither re-anchors nor spends a turn; the echo hold applies to all her speech; unnamed passing audio during media still never re-anchors (PF-034).
- **Why:** Live 2026-11-16 21:49-21:53 UTC: "Era" once, Spotify on, owner talking every 10-20 s; inactivity switched on mid-conversation (PF-034 refused unnamed re-anchoring during media). dev-review (qitem-20261116215950) then found a turn at 119 s + reply at 121 s still lapsed, and that any transcribed lyric answered by Gemini would extend the window. Owner 2026-11-16: Era must never interact with lyrics, films or her own voice; the logical guarantee is the owner voiceprint, so unverified voice turns cannot extend the window during media.
- **Guards:** `tools/test_reanchor_sources.py::test_a_verified_conversation_with_music_on_does_not_lapse_mid_chat`, `tools/test_reanchor_sources.py::test_an_unverified_voice_turn_during_music_never_extends_the_window`, `tools/test_reanchor_sources.py::test_without_music_an_unnamed_turn_continues_the_conversation`, `tools/test_reanchor_sources.py::test_a_turn_just_before_expiry_survives_a_reply_just_after_it`, `tools/test_reanchor_sources.py::test_a_stale_turn_or_an_explicit_close_is_not_continued`, `tools/test_reanchor_sources.py::test_an_owner_turn_cannot_reopen_a_closed_window`, `tools/test_reanchor_sources.py::test_internal_speech_does_not_spend_an_owner_turn`, `tools/test_reanchor_sources.py::test_a_voice_input_marks_an_owner_turn_with_the_verification_state`, `tools/test_reanchor_sources.py::test_owner_voice_counts_as_verified_only_for_a_scored_turn_with_the_lock_on`, `tools/test_reanchor_sources.py::test_an_unscored_follow_up_lyric_during_music_never_extends_the_window`
- **Since:** 2026-11-16 · Claude · Windows tests only. Live owed: re-enroll the voice with the current glasses mic, turn the voice lock on, then music + 3 min unnamed chat; and with the lock off, music + unnamed chat must need "Era" after 2 min.

### PF-034 · Only real conversation re-anchors the listening window
- **Files:** `core/attention.py`, `app/era_live/audio.py`, `main.py`
- **Rule:** Unnamed passing speech re-anchors the window only when the window is REALLY open (not merely passing through a busy/confirmation exemption) and no media is playing (`AttentionGate._reanchor_passing`). Era's speech for an internal model request (a scheduled report, a background result: `_model_reply_ownership` active, no owner, reason `unowned_model_request`) neither re-anchors the window nor consumes a pending explicit address (`era_finished_speaking(internal=True)`, decided at speech START by `_speaking_for_internal_turn`). An internal request in PROCESSING is not "busy with the owner's request": `_attention_keep_open` stays False for it. Explicit addresses, Era's reply to them, bare follow-ups in a quiet open window (PF-006) and PF-032's rules are unchanged.
- **Why:** Live 2026-11-16 (owner voiceprint verification disabled, so all room audio reaches the attention gate): from 17:59 to 18:07 music played and not one `attention.ignored` was logged; at 18:04:44 Era answered unnamed speech 5 min after the owner's last order, because the 18:00/18:03 scheduled reports put her in PROCESSING, the busy exemption let room audio pass, and the first passing segment re-opened the expired window.
- **Guards:** `tools/test_reanchor_sources.py`
- **Since:** 2026-11-16 · Claude · Windows tests only; live "wait 2+ min, talk without Era -> ignored; music playing -> window still expires" owed

### PF-035 · Inactivity state follows the HUD theme and cues on entry
- **Files:** `ui_src/src/components/CoreStage.tsx`, `ui_src/src/lib/transport.ts`, `ui_src/src/lib/sounds.ts`, `ui_src/src/eclipse.css`
- **Rule:** The online inactive listening state uses the HUD's semantic warning and appearance tokens in both themes and presets. Its short sound plays only on a live active-to-inactive transition through the existing sound enable/unlock/visibility/speech gates; the nonverbal cue may bypass microphone-capture suppression only while the backend reports inactive, where the attention gate sends silence unless a real wake is recognized. Snapshots and reconnects never replay it.
- **Why:** The original amber badge looked detached from the HUD's themes, and inactivity had no entry cue. Replaying from snapshots or outputting ordinary speech-like audio could mislead the owner or wake path.
- **Guards:** `tools/test_hud_inactivity.py::test_inactivity_cue_requires_a_live_entry_transition_only`, `tools/test_hud_inactivity.py::test_inactivity_indicator_uses_theme_tokens_and_static_accessible_markup`
- **Since:** 2026-11-16 · Codex · source guards and isolated visual preview; installed HUD appearance and live timeout/wake exercise owed

### PF-036 · Owner reboot acknowledgement is one-shot and readiness-bound
- **Files:** `core/era_relaunch.py`, `app/era_live/tool_dispatch.py`, `main.py`
- **Rule:** An explicit owner reboot may speak a short pre-exit notice unless voice output is muted. A separate expiring one-shot marker carries the mute choice across the process boundary and is consumed only by a different process after its Live session connects; a muted reboot stays silent, including the ordinary boot greeting. The readiness acknowledgement is suppressed while muted/standby and never replays on later reconnects. Ordinary boot and recovery behavior remains distinct.
- **Why:** Reboot had no correlated return acknowledgement; a generic boot greeting could not prove that the owner's restart had completed and must not be mistaken for one.
- **Guards:** `tools/test_era_relaunch.py::test_relaunch_ready_ack_is_one_shot_and_expires`, `tools/test_era_relaunch.py::test_relaunch_ready_ack_rejects_non_owner_source`, `tools/test_era_relaunch.py::test_ready_ack_is_only_spoken_after_the_new_live_session_is_connected`, `tools/test_era_relaunch.py::test_shutdown_era_relaunch_writes_relaunch_not_stop`
- **Since:** 2026-11-16 · Codex · Windows regression tests; live owner reboot intentionally not exercised

### PF-037 · YouTube matches transliterated titles and names distinct choices
- **Files:** `utils/media_match.py`, `actions/youtube_video.py`
- **Rule:** A conservative Latin/Arabic transliteration match, including the logged Latin-only "Sabr Al Rijal" / "Saber Elrjel" spelling with the same six-plus consonant skeleton, may raise a weak candidate score but never lower an exact match. Distinct close YouTube recordings remain ambiguous; the response names visible candidates and starts nothing until selected. A clear candidate still requires the owned page's requested video ID and advancing playback (PF-005).
- **Why:** The query "Sabr Al Rijal Mikasa Music" did not match the displayed bilingual title "Hassen Thabet - Saber Elrjel | صبر الرجال (Mikasa Music)"; close Arabic/Latin versions also need a choice rather than a search-rank guess.
- **Guards:** `tools/test_media_hud_reliability.py::test_youtube_transliteration_survives_artist_and_music_terms`, `tools/test_media_selection.py::TestSelectBestMediaCandidate::test_transliterated_query_keeps_distinct_youtube_recordings_ambiguous`, `tools/test_youtube_direct_play.py::test_ambiguous_video_choices_name_the_visible_candidates`
- **Since:** 2026-11-16 · Codex · Windows deterministic tests; no browser media was started

### PF-033 · HUD shows the actual inactivity window
- **Files:** `app/hud/status.py`, `app/websocket_server.py`, `ui_src/src/lib/store.ts`, `ui_src/src/lib/transport.ts`, `ui_src/src/components/CoreStage.tsx`
- **Rule:** HUD listening mode reads the existing engine ListeningState without constructing or refreshing its session. Expired window with no busy exemption is inactive; wake is active; standby/muted/off/unavailable remain distinct. WebSocket greeting and the existing two-second push refresh the mode, even in quiet. The HUD shows INACTIVITY MODE with the wake hint only for an online inactive engine, and clears the reading on disconnect.
- **Why:** Automatic inactivity was invisible in the HUD; the owner could not tell whether Era needed her name.
- **Guards:** `tools/test_hud_inactivity.py`
- **Since:** 2026-11-16 · Codex · backend Windows tests and frontend build; live installed-engine validation owed

### PF-032 · Inactivity cannot be bypassed by continuous audio or missing wake recognition
- **Files:** `core/attention.py`, `tools/test_attention.py`, `tools/test_inactivity_mode.py`
- **Rule:** The existing session clock/configuration remains authoritative. A PASSING utterance cannot outlive expiry without an existing busy/confirmation exemption; continuous background frames and their quiet tail cannot reopen it. Outside the active window unavailable/failed local recognition emits only silence, never raw mic audio. A verified leading Era/Era One (or existing configured wake alias, optionally after hey/ok/okay) opens the normal follow-up window; incidental mentions such as "new era" cannot. Active-window audio, existing busy exemptions, manual standby and mic controls retain their existing behavior. The owner requested fail-closed inactivity on 2026-11-16; active-window fail-open is preserved.
- **Why:** PASSING previously waited indefinitely for a quiet boundary; missing/crashed Vosk bypassed expiry entirely. A name in the second arbitrary word also authorized incidental mentions.
- **Guards:** `tools/test_inactivity_mode.py`
- **Since:** 2026-11-16 · Codex · deterministic Windows checks; live microphone exercise and Claude review pending

Every coding agent — Claude Code, Codex, ChatGPT, Lunar, anyone — reads this
file **before** editing Era One. Each entry below is a bug that was fixed and
proven. Several were broken again by work on a different tool. The owner does
not want to fix any of them twice.

## The rules

1. **Before you edit a file**, check whether a fix protects it:
   `python tools/protected_fixes.py show <path> [<path> ...]`
   If it prints an entry, the **Rule** line is a hard requirement. Your change
   must keep it true.
2. **Never delete, rename, skip, `xfail`, or weaken a guard test** to make your
   change pass. A red guard means your change broke a fixed feature: fix your
   change, not the guard. `tools/test_protected_fixes.py` fails the suite if a
   guard disappears.
3. **If the owner explicitly asks to change a protected behaviour**, update the
   entry's Rule and its guard tests in the same change, and say so in
   `handoff.md` ("PF-00N changed at the owner's request: …").
4. **Before you say you are done**, run the gate. It always runs every guard
   below, whatever you changed:
   `python tools/era_gate.py --changed <files you edited>`
   or just the guards: `python tools/protected_fixes.py run`
5. **When you fix a bug or finish a feature, add an entry here** with at least
   one guard test that fails without your fix. Use the next free `PF-` number,
   the exact field format below (it is parsed), and real pytest node ids under
   `tools/`. Then run `python tools/protected_fixes.py check`.

Entry format (parsed by `tools/protected_fixes.py` — keep it exact):

    ### PF-0NN · Short title
    - **Files:** `path/one.py`, `path/two.py`
    - **Rule:** what must stay true, in one or two sentences.
    - **Why:** what broke and why, so nobody "simplifies" it back.
    - **Guards:** `tools/test_x.py::test_name`, `tools/test_y.py::TestClass`
    - **Since:** YYYY-MM-DD · who · live-proven / tests only

## Entries

### PF-029 · The OpenRig team shortcut restores saved conversations after restart
- **Files:** `tools/open_era_one_team.ps1`
- **Rule:** The team shortcut starts a missing OpenRig daemon, inspects the saved `era-one` team and exact live terminal sessions, and restores only that existing team with `rig up era-one --existing` when both sessions are positively absent. An already-running team is only viewed, never relaunched; a partial, unknown or deliberately stopped team is never replaced. Recovery failure never falls back to a spec launch or `--fresh`, and missing sessions never produce empty terminal panes. Native trust/login prompts remain for the owner to answer.
- **Why:** After the owner's Windows restart on 2026-11-16, the original shortcut directly attached to two missing tmux sessions and showed `no sessions` in both panes. The saved histories and queued work still existed in OpenRig.
- **Guards:** `tools/test_openrig_launcher.py::test_cold_start_restores_existing_conversations_once_before_attach`, `tools/test_openrig_launcher.py::test_running_team_is_never_restored_or_restarted`, `tools/test_openrig_launcher.py::test_uncertain_partial_or_deliberately_stopped_team_is_not_replaced`, `tools/test_openrig_launcher.py::test_restore_failure_never_falls_back_to_a_new_conversation`, `tools/test_openrig_launcher.py::test_daemon_failure_stops_before_team_recovery`, `tools/test_openrig_launcher.py::test_restored_native_trust_prompt_can_be_shown_without_answering_it`
- **Since:** 2026-11-16 · Codex · isolated PowerShell recovery tests and live saved-conversation restore; a second physical reboot remains unproven

### PF-001 · Spotify "play my track" uses the saved song (desktop, no API)
- **Files:** `core/personal_media.py`, `actions/spotify_control.py`, `actions/spotify_search_worker.py`
- **Rule:** The owner's saved URI and title override any `play_uri` ID the model invents; an omitted title is recovered only from the matching saved URI; the track page's named Play control is pressed once; success needs matching metadata AND active playback, never "the old song is still playing".
- **Why:** `play_uri` opened the page without pressing Play, then dropping the metadata requirement reported the previous song as success; a model-supplied ID bypassed the saved one. Owner uses Spotify desktop with NO API key.
- **Guards:** `tools/test_spotify_identity_playback.py::test_my_track_overrides_model_uri_with_saved_identity`, `tools/test_spotify_identity_playback.py::test_direct_saved_uri_recovers_omitted_title_without_api`, `tools/test_spotify_identity_playback.py::test_uri_navigation_activates_only_named_track_once`, `tools/test_spotify_identity_playback.py::test_unrelated_playing_audio_never_proves_requested_track_or_triggers_resume`, `tools/test_spotify_identity_playback.py::test_track_page_worker_activates_named_control_without_search_or_uri_replay`, `tools/test_spotify_identity_playback.py::test_desktop_setup_does_not_make_any_api_request`
- **Since:** 2026-11-12 · Codex · live-proven (HUD "play my track", PID 31100)

### PF-002 · Spotify next/previous must prove the track changed
- **Files:** `actions/spotify_control.py`, `core/action_verifiers.py`, `core/live_app_state.py`
- **Rule:** Next/previous compare Windows media metadata before and after; "playing" alone or a skip receipt never verifies a skip. The Spotify verifier is SYNCHRONOUS (the dispatcher calls verifiers synchronously).
- **Why:** An async verifier was registered where `verify_action_result` calls sync functions, and generic OS state "proved" a skip that never happened.
- **Guards:** `tools/test_spotify_identity_playback.py::test_next_waits_for_metadata_change_then_survives_completion_pipeline`, `tools/test_spotify_identity_playback.py::test_skip_receipt_or_paused_new_metadata_cannot_prove_next`, `tools/test_spotify_identity_playback.py::test_registered_spotify_verifier_returns_result_even_inside_async_dispatch`, `tools/test_spotify_identity_playback.py::test_playing_alone_cannot_verify_a_skip_receipt`
- **Since:** 2026-11-12 · Codex · live-proven ("Next track." → VERIFIED)

### PF-003 · Optional Spotify Web API: read back, never replay
- **Files:** `core/spotify_api.py`
- **Rule:** With a token, `play`/`play_uri` send ONE mutation and then read the player back; success only when the requested URI is playing. Timeouts and rate limits are reported, never retried, and never fall through to the desktop path. Without a token the API returns `None` and the desktop path owns everything.
- **Why:** "HTTP 204 accepted" was reported as playing; a timeout could replay a command twice.
- **Guards:** `tools/test_enhanced_directive.py::test_spotify_exact_ids_cache_and_no_mutation_replay`, `tools/test_enhanced_directive.py::test_spotify_rate_limit_and_missing_auth_do_not_use_desktop_replay`, `tools/test_media_hud_reliability.py::test_accepted_play_does_not_mean_requested_track_is_playing`, `tools/test_media_hud_reliability.py::test_timeout_after_play_is_not_replayed`, `tools/test_spotify_identity_playback.py::test_api_result_never_replays_through_native_transport`
- **Since:** 2026-11-11 · Codex · tests only (owner has no API key)

### PF-004 · Spotify search never plays a different song
- **Files:** `actions/spotify_control.py`, `actions/spotify_ui.py`, `utils/media_match.py`
- **Rule:** A named song plays only on an exact (or conservative spelling-variant) title match; a partial match, an ambiguous result, or no result starts nothing. A title the owner says now overrides a stale one from earlier.
- **Why:** Native search accepted partial title matches and blindly retried uncertain clicks, so a nonexistent song played something else.
- **Guards:** `tools/test_media_hud_reliability.py::test_native_specific_title_never_selects_partial_wrong_song`, `tools/test_media_hud_reliability.py::test_reported_spelling_variant_matches_but_other_song_does_not`, `tools/test_media_hud_reliability.py::test_no_exact_or_ambiguous_title_never_starts_a_track`, `tools/test_media_hud_reliability.py::test_explicit_title_overrides_stale_song_identity`
- **Since:** 2026-11-11 · Codex · live negative case proven ("Velvet Comet Espresso 7391" played nothing)

### PF-005 · YouTube "playing" means the selected video is advancing
- **Files:** `actions/youtube_video.py`
- **Rule:** Success needs the owned browser page on the requested video ID with time advancing, not paused/muted/ad/ended. A YouTube URL in the address bar is NOT success. Never click the first card of an unrelated result; never toggle a video already running.
- **Why:** Navigation or an HTTP scrape claimed "Playing" with no proof the video played.
- **Guards:** `tools/test_youtube_direct_play.py::test_selected_video_must_advance_and_running_video_is_not_toggled`, `tools/test_youtube_direct_play.py::test_stalled_paused_muted_ad_or_wrong_video_is_not_success`, `tools/test_youtube_direct_play.py::test_direct_url_still_requires_actual_advancing_playback`, `tools/test_youtube_direct_play.py::test_unrelated_result_does_not_play_first_card`, `tools/test_youtube_direct_play.py::test_organic_watch_link_with_radio_parameters_is_a_video`, `tools/test_browser_media_routing.py::TestYoutubeVideoVerification`
- **Since:** 2026-11-11 · Codex · tests only (positive live search still open, see TODO)

### PF-006 · Listening session: one rolling window, default 2 min
- **Files:** `core/session.py`, `core/attention.py`, `app/era_live/audio.py`
- **Rule:** One wake ("Era"/"shazam") opens a window (default 120 s, `config/session.json`) that every accepted turn re-anchors; inside it bare commands work and every mic frame reaches Gemini in order. The attention gate in `_send_realtime` is INTENTIONAL — do not remove it to satisfy an old test.
- **Why:** Three clocks disagreed about whether Era was listening, so she ignored the owner after short silences or needed "Era" before every task.
- **Guards:** `tools/test_session_mode.py::test_the_default_window_is_two_minutes_and_standby_is_off`, `tools/test_session_mode.py::test_inside_the_window_bare_commands_work`, `tools/test_session_mode.py::test_after_the_window_the_name_is_required_again`, `tools/test_session_mode.py::test_every_accepted_turn_re_anchors_the_window`, `tools/test_open_gemini_input.py::test_media_and_attention_filters_never_consume_live_requests`
- **Since:** 2026-11-11 · Claude · tests only (live acceptance owed, see TODO)

### PF-007 · "Era, standby" closes the window, never enters the STANDBY state
- **Files:** `main.py`, `app/hud/bus_legacy.py`
- **Rule:** With standby disabled, a standby order transitions to `ONLINE_IDLE` and HUD `IDLE`, keeping the mic and Live session warm.
- **Why:** Reusing `EraState.STANDBY` made `_send_realtime` drop every frame and the input gate refuse typed orders, so Era never woke.
- **Guards:** `tools/test_standby_policy.py::test_standby_disabled_closes_the_session_and_keeps_the_mic_warm`
- **Since:** 2026-11-11 · Claude · tests only

### PF-008 · Mic mute: any mute can be undone by voice, typing or the button
- **Files:** `app/era_live/mic_mute.py`
- **Rule:** Every mute (voice, typed, UI button, restored after restart) arms the local unmute listener, so "activate mic" / "start listening" by voice reopens it. The listener hears only the unmute grammar (strict phrases + confidence), skips Era's own speech, and passes the phrase through the configured speaker verifier: when owner verification is ON an unverified voice never reopens a mute; when it is OFF (`enabled: false`, the current setup) the verifier accepts every voice and the owner chose (2026-11-16, on dev-review's finding) to keep voice unmute anyway, so grammar + confidence are then the only protection. A typed mic order is handled locally by `_handle_typed_mic_order` (main.py, before the session check), with no session or model turn. The mic button always works.
- **Why:** Owner rule changed 2026-11-16 ("make sure it can be activated when I ask with any method, vocal or typing"). Live 2026-11-16 18:03: after "microphone off" the owner said "activate mic" for 40 s; the old rule (voice mute arms no listener) let only the mic button reopen it. Earlier history: ambient noise had reopened a mute, which owner verification + strict grammar now prevent.
- **Guards:** `tools/test_activate_mic.py::test_a_voice_mute_is_reopened_by_the_owner_voice`, `tools/test_activate_mic.py::test_an_unverified_voice_cannot_reopen_any_mute`, `tools/test_activate_mic.py::test_a_typed_order_unmutes_a_voice_mute_without_a_session`, `tools/test_activate_mic.py::test_a_ui_mute_is_reopened_by_the_owner_voice`, `tools/test_activate_mic.py::test_repeated_voice_off_repairs_the_listener`, `tools/test_activate_mic.py::test_disabled_verification_still_allows_voice_unmute`
- **Since:** 2026-11-11 · Claude · rule changed by the owner 2026-11-16 · tests only; live "microphone off" -> "activate mic" owed

### PF-009 · No evidence, no "done": completion contract
- **Files:** `core/completion_contract.py`, `app/era_live/tool_dispatch.py`
- **Rule:** A success with no evidence-based verifier reaches the model as NOT CONFIRMED; the generic "returned without raising" verifier is not evidence; an approved action is verified under its REAL tool name, never `confirm_pending_confirmation`; a failure is never upgraded.
- **Why:** `None` became "Done.", and the one action that needed approval (delete/send/overwrite) was the one never checked.
- **Guards:** `tools/test_sprint1_evidence_gate.py::test_the_confirmed_branch_hands_the_real_tool_to_verification`, `tools/test_sprint1_evidence_gate.py::test_generic_verifier_is_not_evidence`, `tools/test_sprint1_evidence_gate.py::test_unproven_side_effect_reaches_the_model_as_not_confirmed`, `tools/test_sprint1_evidence_gate.py::test_failure_is_never_upgraded_by_the_tag`
- **Since:** 2026-11-11 · Claude · tests only

### PF-010 · Unknown file_controller actions fail closed
- **Files:** `core/safety_guard.py`, `actions/file_controller.py`
- **Rule:** An action outside the declared vocabulary needs confirmation (MEDIUM); known actions keep their existing weight.
- **Why:** `organize_desktop` matched no rule and bulk-moved the Desktop at LOW risk with no confirmation.
- **Guards:** `tools/test_sprint1_evidence_gate.py::test_an_action_outside_the_declared_vocabulary_requires_confirmation`, `tools/test_sprint1_evidence_gate.py::test_known_file_actions_keep_their_existing_weight`
- **Since:** 2026-11-11 · Claude · tests only

### PF-011 · Typed yes/no answers a pending confirmation
- **Files:** `main.py`, `app/hud/approval.py`
- **Rule:** Typed "yes"/"approve" runs the armed action and typed "no"/"cancel" cancels it, both from the single PendingConfirmationStore and without a model turn; a HUD cancel can only consume the confirmation it was shown.
- **Why:** Typed replies went to the model, so approvals did nothing and cancels were silent.
- **Guards:** `tools/test_confirm_self_approval.py::test_typed_yes_after_arming_runs_the_armed_action`, `tools/test_personal_control_repairs.py::test_hud_cancel_cannot_consume_replacement`
- **Since:** 2026-11-12 · Claude · tests only (the `_handle_text_command` short-circuit itself has no direct test yet; live proof owed)

### PF-012 · "Shut down the computer" never kills Era
- **Files:** `core/self_healing_router.py`
- **Rule:** Power words about the machine (computer, PC, laptop) route to machine power control, never to `shutdown_era`.
- **Why:** "Shut down the computer" closed Era instead of the PC.
- **Guards:** `tools/test_memory_safety.py::TestMachinePowerRouting`
- **Since:** ≤2026-09-30 · Claude · guarded by tests

### PF-013 · Panic stop: "abort mission" latches, bare "stop" never does
- **Files:** `core/panic_stop.py`
- **Rule:** Abort orders trip the latch and refuse every queued call; a bare "stop" (a media command) and other ordinary text are left alone.
- **Why:** "Stop" pauses music; tripping the panic latch on it would freeze Era mid-task.
- **Guards:** `tools/test_panic_stop.py::test_abort_orders_are_recognised`, `tools/test_panic_stop.py::test_non_abort_text_is_left_alone`, `tools/test_panic_stop.py::test_latched_abort_refuses_every_call_but_still_answers_them`
- **Since:** ≤2026-09-30 · Claude · guarded by tests

### PF-014 · Voice device pin never touches the Windows default; bare volume = system
- **Files:** `core/voice_output.py`
- **Rule:** Pinning Era's voice to a device never calls set_default; MME names are matched through 31-char truncation; a bare "volume up" changes the SYSTEM volume, only "glass volume" targets the glasses.
- **Why:** set_default moved all PC audio; truncated names failed to match; bare volume hit the glasses.
- **Guards:** `tools/test_voice_output.py::test_pinning_the_voice_never_touches_the_windows_default`, `tools/test_voice_output.py::test_pin_matches_through_mme_name_truncation`, `tools/test_voice_output.py::test_bare_volume_up_still_hits_the_system_not_the_glasses`
- **Since:** ≤2026-09-30 · Claude · guarded by tests

### PF-015 · Owner voice gate is fail-closed
- **Files:** `core/voice_gate.py`
- **Rule:** Only the enrolled owner's voice reaches Gemini; no profile, a model mismatch, or an embedder error rejects (never passes) audio.
- **Why:** A fail-open gate lets any voice in the room command the machine.
- **Guards:** `tools/test_speaker_gate.py::test_owner_accepted_stranger_rejected`, `tools/test_speaker_gate.py::test_fail_closed_without_profile`, `tools/test_speaker_gate.py::test_fail_closed_on_model_mismatch`, `tools/test_speaker_gate.py::test_fail_closed_on_embedder_error`
- **Since:** ≤2026-09-30 · Claude · guarded by tests

### PF-016 · Netflix: drive the player API, never write currentTime
- **Files:** `actions/netflix_control.py`
- **Rule:** Seek/volume/episode use Netflix's own player API (times in ms), every seek is verified, generated JS must parse, and no script writes `video.currentTime`.
- **Why:** `querySelector('video')` is null on the watch page and element seeks silently failed; Python-built JS passed every Python check and broke in the page.
- **Guards:** `tools/test_netflix_profile_and_player.py::TestPlayerUsesNetflixOwnApi`, `tools/test_netflix_profile_and_player.py::TestNeverWriteCurrentTimeOnNetflix`, `tools/test_netflix_profile_and_player.py::TestGeneratedJavaScriptIsValid`
- **Since:** ≤2026-09-30 · Claude · guarded by tests (test film: The Wild Robot)

### PF-017 · Shared media matcher: an exact title always scores 1.0
- **Files:** `utils/media_match.py`
- **Rule:** `score_media_candidate` computes the normal score first; a transliteration spelling variant ("Sabr El Rijal" / "Saber Elrjel") may only RAISE a weak score to 0.86, never lower an exact (1.0) or artist-dash match. This file is shared by Spotify, YouTube AND Netflix — a change for one platform must pass all three.
- **Why:** The variant check was placed before the exact-match check, and an exact title is trivially its own variant, so every exact match was capped at 0.86 across all media tools (found 2026-11-12 by the full suite, 3 reds in `test_media_selection.py`).
- **Guards:** `tools/test_media_selection.py::TestScoreMediaCandidate::test_exact_match_scores_one`, `tools/test_media_selection.py::TestArtistSongBoost::test_artist_song_dash_boost`, `tools/test_media_selection.py::TestArtistSongBoost::test_artist_song_boost_works_with_dash`, `tools/test_media_hud_reliability.py::test_reported_spelling_variant_matches_but_other_song_does_not`
- **Since:** 2026-11-12 · Claude · tests only

### PF-018 · Semantic recall: a cold embedding model is loaded, not written off
- **Files:** `memory/semantic_recall.py`
- **Rule:** `available()` asks Ollama's `/api/tags` + `/api/ps` (metadata, never loads a model) first. Installed-but-unloaded starts ONE background warm-up and answers False at once (no 1.5 s probe in the spoken turn); when the warm-up lands, recall is semantic immediately, and a warm-up result is never overwritten by a stale False. Not installed = no probe, no warm-up. Unknown metadata = the old probe, unchanged. Every embed sends `keep_alive` (default 30m, `ERA_EMBED_KEEP_ALIVE`).
- **Why:** The 1.5 s probe met a model Ollama had idle-unloaded (cold load measured 16.3 s), read it as ABSENT, and cached that for 60 s, so after every idle period Era recalled by words only, silently, while also spending 1.5 s of the turn on a probe that could not succeed.
- **Guards:** `tools/test_semantic_recall.py::test_a_cold_model_is_loaded_in_the_background_not_written_off`, `tools/test_semantic_recall.py::test_a_cold_model_costs_the_conversation_no_probe_wait`, `tools/test_semantic_recall.py::test_a_warm_up_that_finishes_first_is_not_overwritten`, `tools/test_semantic_recall.py::test_a_model_that_is_not_installed_is_never_warmed`, `tools/test_semantic_recall.py::test_unknown_metadata_falls_back_to_the_old_probe`, `tools/test_semantic_recall.py::test_recall_falls_back_to_words_when_embedding_fails`, `tools/test_semantic_recall.py::test_every_embed_asks_ollama_to_keep_the_model_resident`
- **Since:** 2026-11-12 · Claude · live-proven against local Ollama (cold call 0.11 s, next call semantic, model resident 30 min)

### PF-019 · Live telemetry: close actions must poll for process termination
- **Files:** `core/live_app_state.py`
- **Rule:** When a close action is performed, `refresh_after_command` must poll `_processes` until the process report reflects termination, preventing stale "process still running" telemetry.
- **Why:** The `_expected()` function had no close action handler and no polling loop for close, so `expected is None` caused immediate loop exit after one attempt. The live_app_state reported the process as still running because the OS hadn't yet fully terminate it when the single read occurred. Fresh post-command observations must wait for actual process state, not just command receipt.
- **Guards:** `tools/test_live_app_state.py::test_close_action_polls_until_termination`, `tools/test_live_app_state.py::test_close_verified_when_process_gone`
- **Since:** 2026-11-12 · Codex · live-proven against real window close (HUD telemetry regression)

### PF-020 · Protocol One reports the truth, and Era can read it
- **Files:** `core/protocol_checks.py`, `actions/protocol_one.py`
- **Rule:** The report must state what it measured and never under-count: identical lint hits in one file are ONE finding with a true count and every line; the suite runs pytest's own testpaths (`tools/` AND the repo root, not a hand-picked subset); each test failure carries its real cause (junit XML, not the width-truncated console line); an autofix that was skipped or rolled back says why on the finding; a section cap is stated, never silent; the report says when its log scan ran and what changed since the previous cycle. Era reads it with `protocol_one action=report` (read-only), which states the report's age and how many source files changed since, and `status` points at it.
- **Why:** The 2026-11-14 report listed a rolled-back autofix as simply "open" with no reason, merged two I001 hits into one, cut every failure reason to "Asser…", and its "full suite" skipped 91 root-level tests. Era's only window onto it was a hive briefing that drops any detail over 120 characters, so she could not answer "what did Protocol One find?" from the real report.
- **Guards:** `tools/test_protocol_one.py::TestReportAccuracy::test_identical_lint_hits_in_one_file_are_counted_not_merged_away`, `tools/test_protocol_one.py::TestReportAccuracy::test_a_rolled_back_autofix_says_why`, `tools/test_protocol_one.py::TestReportAccuracy::test_an_ineligible_autofix_says_why`, `tools/test_protocol_one.py::TestReportAccuracy::test_junit_carries_the_real_cause_not_a_truncated_one`, `tools/test_protocol_one.py::TestReportAccuracy::test_the_suite_run_covers_the_root_tests_too`, `tools/test_protocol_one.py::TestReportAccuracy::test_a_section_cap_is_stated_not_silent`, `tools/test_protocol_one.py::TestReportAccuracy::test_read_report_returns_one_section_with_age_and_currency`, `tools/test_protocol_one.py::TestReportAccuracy::test_status_now_points_at_the_report`, `tools/test_protocol_one.py::TestReportAccuracy::test_report_is_a_declared_read_only_action`
- **Since:** 2026-11-15 · Claude · tests only (live cycle owed, see handoff)

### PF-021 · A mood or genre request is not a song title
- **Files:** `core/personal_media.py`
- **Rule:** `explicit_track` grounds a spoken request as a literal title only when it is one: "play me X" drops the indirect object, and a generic request ("some chill music", "a chill rap mix", a restatement across sentences) returns None so the model's own query stands. The saved-song / `play_uri` identity rules (PF-001) are untouched.
- **Why:** Live 2026-11-14 17:46: "Play me some chill music. chill rap music" was grounded as the literal title "me some chill music. chill rap music", overriding the model's query "chill rap mix"; the search could never match and was reported PlaybackNotVerified.
- **Guards:** `tools/test_media_hud_reliability.py::test_generic_or_negated_speech_is_not_invented_as_a_title`, `tools/test_media_hud_reliability.py::test_play_me_a_real_title_drops_the_indirect_object`, `tools/test_media_hud_reliability.py::test_a_mood_request_keeps_the_models_own_query`
- **Since:** 2026-11-15 · Claude · tests only

### PF-022 · Era knows what her tools really did
- **Files:** `core/intent_memory.py`, `core/self_healing_router.py`, `core/task_trace.py`, `actions/protocol_one.py`, `app/era_live/tool_dispatch.py`, `actions/computer_control.py`
- **Rule:** "Netflix is the active page" expires (`NETFLIX_CONTEXT_TTL_S`) and is renewed only by real Netflix use, never by a failed call or the router's own `Netflix state: unknown` reply; every Netflix key/type/click guard asks `netflix_context_fresh`, never the raw `last_media_app` flag. When the router runs a different tool than the model asked for, the reply carries a ROUTING NOTE and `data["routed"]` (success, verdict and errors are never touched). `protocol_one action=recent` (read-only, LOW) answers "did you do it?" from the task trace: asked vs. ran, CONFIRMED / NOT CONFIRMED / FAILED, never upgraded. The recent outcomes also ride in the session context. A detached call logs `tool.execution.detached` and its own `tool.execution.completed`; tool time feeds `turn.latency`. `computer_control` accepts `key` for `press`.
- **Why:** Live 2026-11-15: a stale `last_media_app: netflix` turned "type instagram.com in Brave" into a 49 s Netflix search and "press enter" into `Netflix state: unknown` reported as a success; the hijack's own reply renewed the flag. Slow calls were detached with no outcome event (8 of 14 calls had none), `turn.latency` always said `tool_count: 0`, and Era had no way to read back what had happened, so she could not tell the owner a task had not been done.
- **Guards:** `tools/test_tool_truth.py::TestNetflixContextExpires::test_old_use_expires`, `tools/test_tool_truth.py::TestNetflixContextExpires::test_the_hijacks_own_unknown_reply_does_not_renew_the_flag`, `tools/test_tool_truth.py::TestRouterLeavesOtherAppsAlone::test_stale_netflix_does_not_hijack_a_keypress`, `tools/test_tool_truth.py::TestRouterLeavesOtherAppsAlone::test_live_netflix_still_gets_the_guard`, `tools/test_tool_truth.py::TestRoutingNoticeReachesTheModel::test_a_swapped_tool_is_named_and_the_outcome_is_untouched`, `tools/test_tool_truth.py::TestRoutingNoticeReachesTheModel::test_a_failure_stays_a_failure`, `tools/test_tool_truth.py::TestEraCanReadBackWhatHappened::test_outcomes_are_never_upgraded`, `tools/test_tool_truth.py::TestEraCanReadBackWhatHappened::test_era_reads_it_through_protocol_one`, `tools/test_tool_truth.py::test_recent_is_a_declared_read_only_action`, `tools/test_tool_truth.py::test_the_model_can_say_key_for_a_keypress`
- **Since:** 2026-11-15 · Claude · replayed against the live router state (all three hijacked calls now stay on computer_control); live proof owed after restart

### PF-023 · A Netflix failure never swallows Enter in another app
- **Files:** `core/self_healing_router.py`
- **Rule:** After a real Netflix failure or vision cooldown, a bare Enter (no explicit "press enter") is held only when it would land on Netflix: the foreground window title decides when readable (Netflix title → held as `netflix_control status`, any other title → passes through immediately); only an unreadable foreground falls back to fresh Netflix evidence (scroll context, `netflix_context_fresh`, a Netflix URL or mention). The 120 s failure window itself is unchanged.
- **Why:** Owner 2026-11-15: after a Netflix failure, every bare Enter anywhere (Notepad, WhatsApp, another browser tab) was turned into `Netflix state: unknown` for 120 s. Changed at the owner's request; the same-app protection stays.
- **Guards:** `tools/test_browser_media_routing.py::TestNetflixEnterOnlyGuardsNetflix`, `tools/test_browser_media_routing.py::TestNetflixEnterFallbackGuard::test_netflix_enter_fallback_suppressed`
- **Since:** 2026-11-15 · Claude · tests only (live foreground proof owed after restart)

### PF-024 · A relaunch never leaves the previous Era running
- **Files:** `era_supervisor.py`, `core/era_relaunch.py`, `app/era_live/tool_dispatch.py`
- **Rule:** Era's one exit path ends with `era_relaunch.hard_exit` (flush, then `TerminateProcess` on Windows, `os._exit` fallback), never a bare `os._exit` whose DLL teardown can hang. The supervisor never starts a replacement while the previous Era lives: `kill_tree` is bounded, never raises and reports whether the process is gone; `--wait-pid` waits `ERA_PREVIOUS_WAIT` (60 s), then kills the tree only when `is_era_process` verifies the pid is a Python running this repo's `MAIN_PY` (a reused pid is left alone), and halts instead of starting a second Era if the old one survives or cannot be verified.
- **Why:** Live 2026-11-15 20:53: Era called `os._exit(0)` on "reboot Era One" yet was still alive 26 s later (exit code already 0) and had to be force-killed. A hand-started Era that did not exit within 60 s got a second Era launched next to it, and a second `wait()` timeout in `kill_tree` raised and crashed the watchdog, leaving the old process unreplaced.
- **Guards:** `tools/test_era_relaunch.py::test_wait_pid_kills_a_stubborn_era_before_starting_one`, `tools/test_era_relaunch.py::test_wait_pid_never_kills_a_python_that_is_not_era`, `tools/test_era_relaunch.py::test_kill_tree_is_bounded_and_never_raises`, `tools/test_era_relaunch.py::test_era_exit_terminates_itself_after_flushing`, `tools/test_era_relaunch.py::test_the_one_exit_path_uses_hard_exit`
- **Since:** 2026-11-15 · Claude · Windows subprocess tests; live "reboot Era One" proof owed (Era is currently stopped by the owner's 21:05 stop flag)

### PF-025 · A mood search is verified by what it started, not by the query words
- **Files:** `actions/spotify_control.py`, `actions/spotify_ui.py`, `core/personal_media.py`
- **Rule:** For a mood/genre search (`is_mood_request`: "chill music", "night music", "chill rap mix", "some jazz"; never an "X by Y" request), success is decided only after the worker reports the control it pressed (`"clicked: Play <name>"`), by observation, without repeating any command: a pressed TRACK row must be that exact track playing; a pressed playlist/album/artist (or unnamed control) must have changed the playing track. The pre-command song still playing, or nothing playing, is `PlaybackNotVerified`. The result is a `spotify_native` ToolResult carrying `previous_media_observation`, so the query-word re-check in `refresh_after_command` cannot overrule it. Named-song searches keep PF-004's strict query match.
- **Why:** Live 2026-11-15 20:56 and 20:57 (after PF-021): "chill music" reached Spotify intact, the worker pressed the "Chill Hits" result, and verification demanded a track whose title or artist contained both "chill" and "music". No real song does, so every mood request was reported PlaybackNotVerified even when music started.
- **Guards:** `tools/test_spotify_search.py::test_mood_playlist_that_started_a_new_song_is_verified`, `tools/test_spotify_search.py::test_mood_search_with_the_old_song_still_playing_is_not_verified`, `tools/test_spotify_search.py::test_mood_search_with_nothing_playing_is_not_verified`, `tools/test_spotify_search.py::test_mood_track_row_must_be_that_exact_track`, `tools/test_spotify_search.py::test_named_song_search_stays_strict`, `tools/test_spotify_search.py::test_the_worker_names_the_control_it_pressed`
- **Since:** 2026-11-15 · Claude · tests only (live "play some chill music" owed after restart)

### PF-026 · Gemini Live G0: a gated mic never idles the socket; drops are named and unanswered speech survives once
- **Files:** `app/era_live/session_stability.py`, `app/era_live/audio.py`, `main.py`, `core/session_manager.py`
- **Rule:** **Keepalive:** while a PRIVACY gate (muted, mic muted by voice, mic off, standby without wake) holds the mic shut, `_send_realtime` sends only constant zero frames (`SILENT_FRAME`), `ERA_LIVE_KEEPALIVE_FRAMES` (5 × 40 ms) every `ERA_LIVE_KEEPALIVE_S` (10 s; 0 disables). It never sends mic data and never sends while reconnecting. **Chunks:** 40 ms at 16 kHz (`CHUNK_SIZE` 640). **Resumption handle:** kept for Google's 2 h lifetime minus 5 min. Only a server refusal (`handle_rejected`) or an expired handle discards it, never a count of failed attempts. **Close record:** every `engine.connection.closed` carries `close_kind` (1008-inactivity / 1008-lifetime / 1008-other), `conn_age_bucket`, `keepalive_frames` and `continuity_frames_kept`. **Replay:** the last ≤ 3 s of SENT owner audio is replayed once into the next session, ahead of live audio, only if the server produced no audio, text or tool call after that utterance began and no tool was running, and only if no gate is closed at replay time. **Untouched:** the mute/standby gates, the speaker and attention gates (PF-006) and audio ordering.
- **Why:** 246 of 255 closes over 5 days were Google's 120 s client-inactivity 1008 while Era's own mute/standby gate sent nothing at all, giving 2–5 s of deafness each time. A handle the server still honoured was dropped after 15 min or after two failed reconnects, and every 1008 was logged as an anonymous "rotation".
- **Guards:** `tools/test_gemini_session_stability.py`
- **Since:** 2026-11-15 · Claude · mock/guard tests only. The idle-timer effect of sparse silence is UNVERIFIED; the 24 h muted soak (0 idle-1008 target) and the 50 forced drops are owed.

### PF-027 · Era says what was asked and whether it really happened, from a reader that works
- **Files:** `core/live_app_state.py`, `app/era_live/speak_once.py`, `core/user_prefs/minimize_rule.py`, `actions/spotify_control.py`, `actions/open_app.py`
- **Rule:** Every live-state verified app/media command (open, close, minimize, play, pause, stop, resume; Spotify, YouTube, Netflix, any app) leads with `outcome_sentence`: what was asked and whether the fresh observation shows it done; never the raw telemetry, which follows after "Observed:" for the model. `spoken_outcome` speaks that sentence as-is. "Open X" is verified only by a window that is not minimized; `open_app` close expects the process gone. A probe that asked nothing of the app (window move/focus) keeps the tool's own result FIRST, adds `Now: <status_sentence>` as context and never claims an app "is not running" (a window title may match no process). Spotify playback is read through the warm GSMTC host when the `winrt` binding is missing (it has no CPython 3.14 wheel); an unreadable playback adds `playback_unreadable` to `errors`. A playback check spreads its 3 reads over 3.5 s (1 s apart); other checks are unchanged (PF-019). The minimize-after-use rule never hides an app `open_app` opened in the last 60 s.
- **Why:** Live 2026-11-15 23:06: "Open Spotify" was answered "spotify: process running; window minimized, sir — though I couldn't confirm it took effect." The minimize rule had hidden the window the owner asked to open, and the playback reader needed `winrt`, which Era's Python cannot import, so every playback check read "unknown" and nothing could ever be confirmed (measured: the host reads "paused" in 0.37 s cold, 3-8 ms warm).
- **Guards:** `tools/test_tool_outcome_sentences.py`
- **Since:** 2026-11-15 · Claude · tests and a read-only Windows probe (Era's probe now reads the real "paused"); live "Open Spotify" owed

### PF-028 · Era never starts music nobody asked for
- **Files:** `app/era_live/tool_dispatch.py`
- **Rule:** Outside scheduled runs, `spotify_control` play/resume and `media_coordinator` play/resume (target Spotify, music or none) run only when an utterance from the last 60 s (`_user_utterances`, voice, typed, HUD or Telegram) matches `_MEDIA_START_ORDER` (play, music, song, resume, put on, listen, playlist… in EN/FR/AR). An app's name alone ("Open Spotify") is never an order to play; the refusal tells the model playback was NOT started. Pause, minimize, open and other targets are untouched.
- **Why:** Live 2026-11-15 23:06: "Open Spotify" made the model add `spotify_control play`; the owner decided on 2026-09-25 that opening Spotify must not start music. Owner approved this gate on 2026-11-16 ("proceed").
- **Guards:** `tools/test_tool_outcome_sentences.py::test_play_nobody_asked_for_is_refused`, `tools/test_tool_outcome_sentences.py::test_play_the_owner_asked_for_goes_through`, `tools/test_tool_outcome_sentences.py::test_other_actions_are_untouched`, `tools/test_tool_outcome_sentences.py::test_the_music_gate_runs_beside_the_state_gate_and_spares_scheduled_runs`
- **Since:** 2026-11-16 · Claude · tests only (live "Open Spotify" owed)

### PF-030 · "Couldn't confirm it took effect" only when there was an effect to confirm
- **Files:** `core/completion_contract.py`, `core/live_app_state.py`, `core/action_verifiers.py`, `core/task_trace.py`, `actions/window_evidence.py`, `actions/window_control.py`, `actions/spotify_exact.py`, `actions/spotify_control.py`, `actions/audio_control.py`, `actions/netflix_control.py`, `actions/computer_settings.py` (its routed-device reply stays text)
- **Rule:** A READ action on any tool (`READ_ACTIONS`: status, list, report, recent, current_track_data, list_devices…; plus per-tool `TOOL_READ_ACTIONS`) and a tool that only switches Era's own in-process state (`ERA_STATE_TOOLS`: mic, voice, voice lock, shutdown…) are `informational`, never `unverified`. `search` is never a generic read (Spotify/Netflix search starts playback); `protocol_one` launch/confirm stay side-effecting. Spotify `liked_songs` / `new_releases` / `made_for_you` expect fresh playback `playing` like play, and their `outcome_sentence` names the collection ("Spotify is playing your Release Radar."). `window_control` minimize/maximize/move_to_monitor results that the handler read back from Windows, and focus/show/restore confirmed by the real foreground window, return a `window_native` verified ToolResult. Focus proof is the EXACT acted-on HWND (`window_control._note_acted`, thread-local, reset before every action) being the foreground window or its root owner; the same process or a caption containing the name never counts. That proof survives the generic probe. When the probe finds no process (a title may match none, PF-027), the message is the handler's own result with no "not running" claim. Spotify `volume_up`/`volume_down` read the Windows session volume before and after the keystroke (`confirm_volume_step`); only a move in the asked direction is `spotify_native` verified. `audio_control` device switches, levels and mutes keep `core.audio_endpoints`' hardware read-back as an `audio_endpoint` verified result (a quantised level that missed the request stays text); pinning Era's own voice output (`set_voice_output`/`clear_voice_output`) is her own state. Netflix fullscreen confirmed by CDP `Browser.getWindowBounds` is a `browser_window` verified result; the blind F11 fallback stays unconfirmed. "Open Netflix" is verified only when the tab's own address, read AFTER navigation, is netflix.com and the profile gate settled (`selected`/`no-gate`); a visible browser alone never proves it. Only a read-back success becomes a ToolResult: a failure stays the handler's string, because the dispatcher does `r or "Done."` and a failed ToolResult is falsy.
- **Why:** Live 2026-11-16 10:33: "new releases" played Release Radar and the fresh telemetry said "playing", yet it was tagged NOT_VERIFIED because playlist actions had no expected state. Live 2026-11-16 10:44: "What's news about Protocol One?" (a report READ) was spoken "… sir — though I couldn't confirm it took effect." Over 2026-11-15/16, all 11 window move/focus calls were unconfirmed, though move_to_monitor already asked Windows which screen the window ended up on.
- **Guards:** `tools/test_completion_evidence.py`
- **Since:** 2026-11-16 · Claude · tests on Windows Python; live "new releases", "move Brave to screen one" and "what's new about Protocol One" owed after restart

### PF-031 · "Screen one" means screen one
- **Files:** `actions/monitor_move.py`, `core/self_healing_router.py`, `app/tool_declarations.py`
- **Rule:** For `window_control move_to_monitor` and `browser_control tab_to_monitor`, a screen NUMBER in the owner's sentence ("screen one", "monitor 2", "second screen"; the last mention wins) replaces the model's `screen` argument in `route_tool_call` (`ground_screen`). Sentences without a number ("other", "primary", "main", "left") keep the model's value. A window already on the target screen but minimized is restored there, and only a read-back that it is no longer minimized is reported as moved.
- **Why:** Live 2026-11-16 17:54-17:55 (and owner corrections 2026-10-03, 10-14): "move Brave/Spotify to screen one" reached the tool as `screen="primary"`; the primary monitor is Windows display 2, so Era answered "already on screen 2" four times running, and a minimized Brave was left minimized.
- **Guards:** `tools/test_monitor_move.py::test_spoken_screen_number_is_read_from_the_sentence`, `tools/test_monitor_move.py::test_screen_one_is_never_sent_as_primary`, `tools/test_monitor_move.py::test_grounding_leaves_other_calls_alone`, `tools/test_monitor_move.py::test_the_router_applies_screen_grounding`
- **Since:** 2026-11-16 · Claude · tests only; live "move Spotify to screen one" owed after restart
