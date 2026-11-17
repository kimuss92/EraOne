# Claude (dev-build) review of the pinned PF-035/036/037 candidate (qitem-20261116212553-ad8e6103)

Verdict: **NOT ACCEPTED yet**: 2 actionable findings (F1, F2); F3-F5 minor.
Pins: all 16 SHA-256 values match the bytes on disk at review time (`sha256sum -c`).

## Run by me (Windows Python 3.14, this review)
- Focused: test_hud_inactivity, test_era_relaunch, test_media_hud_reliability, test_media_selection, test_youtube_direct_play + PF-034/PF-008/PF-032/PF-006 suites (test_reanchor_sources, test_activate_mic, test_attention, test_session_mode, test_inactivity_mode): **362 passed**.
- `tools/protected_fixes.py check`: REGISTRY OK. `run`: **409 passed**.
- Direct probes of `utils.media_match` (below). Screenshots viewed.
- NOT run by me: the builder's 4,219-test focused run, ruff, Vite build. No live restart, microphone, sound or YouTube.
- PF-034 / PF-008 code intact (`_reanchor_passing`, `_speaking_for_internal_turn`, `_attention_keep_open` internal check, `_ensure_unmute_listener` on every mute). main.py since my commit 3a337b9: only the +17/-1 reboot block.

## F1 (privacy, PF-036): a muted restart still announces itself afterwards
`tool_dispatch._shutdown_era`: when `ui.muted` the pre-exit notice is skipped, but `_arm_relaunch` -> `era_relaunch.arm_ready_ack(source)` still writes the marker. `ui.muted` is not persisted (`app/hud/ui_base.py` / `ui.py` start `_muted = False`), so the new process consumes the marker in `main.py` and speaks "I'm back and ready, sir." Scenario: owner mutes Era, types "reboot Era One" -> silent restart -> Era speaks on return. The brief requires the muted choice to be respected. The test `test_shutdown_era_relaunch_writes_relaunch_not_stop[True]` even asserts `era.relaunch.ready` exists when muted.
Fix: do not arm (or record `muted: true` in the marker and suppress the spoken line on consume, still clearing it). Guard: muted reboot -> marker absent or silent on consume.

## F2 (PF-037): the logged query does not match a Latin-only title
`title_spelling_variant` needs identical consonants AND `SequenceMatcher` >= 0.8. The logged query "Sabr Al Rijal" vs "Saber Elrjel": consonants equal (sbrlrjl) but ratio **0.727**, so False. Probes:
- `Sabr Al Rijal Mikasa Music` vs `Hassen Thabet - Saber Elrjel | صبر الرجال ( Mikasa Music )` -> True, 0.86, **only via the Arabic segment** (صبر الرجال -> "sbr alrjal").
- vs `Hassen Thabet - Saber Elrjel | Mikasa Music` -> False, 0.29.
- vs `Hassen Thabet - Saber Elrjel (Full Version) | Mikasa Music` -> False, 0.24.
- `Sabr El Rijal` vs `Saber Elrjel` -> True (0.818): the only Latin form the tests use.
So the screenshot case works by luck of the Arabic script in the title; the code comment's own example passes only with "El". The Arabic article variants (al/el/il/ul, assimilated forms) defeat the 0.8 ratio.
Suggest: normalise the article (e.g. drop/merge a leading al/el/il/ul) before the ratio, or let an identical >= 6-consonant skeleton pass at a lower ratio (~0.7), keeping the 0.86 cap and ambiguity rules. Add tests with the LOGGED query against a Latin-only title, and a near-miss negative (same consonant count, different title).

## F3 (minor): docstring contradiction
`title_spelling_variant` says "This never transliterates Arabic", but `latinize` transliterates Arabic, and the real match depends on it.

## F4 (minor, PF-035 visual): screenshots (disconnected preview, synthetic badge)
Readable in dark/Neon and light/Aurora. The badge overlaps the top of the core orb and nearly touches the "Reconnecting to the local backend" subtitle. Sentence-case sans text, while neighbouring status labels use uppercase letter-spaced mono. Not blocking. Live online-inactive rendering, the transition and the sound unlock remain unproven.

## F5 (note, PF-036): fixed English phrases
"I'm restarting now, sir." / "I'm back and ready, sir." are hard-coded `speak()` strings. The brief allowed this unless a phrase mechanism exists; not blocking.

## Reviewed with no finding
- PF-035 cue: `transport.ts` cues only on live `active -> inactive`; onclose resets to `unavailable`, so a reconnect or snapshot cannot replay it. `sounds.ts` keeps enable/unlock (trusted gesture)/hidden-tab/speaking gates; the capture bypass applies only to `inactivity` while the backend mode is inactive (the attention gate silences un-named audio then).
- PF-036 marker: 300 s TTL, owner sources only, same PID cannot consume, invalid/stale discarded, armed only when relaunch is recoverable; consumed only after the new Live connection is ready; it replaces the first-boot greeting instead of duplicating it; standby suppresses.
- PF-037 ambiguity: no playback on ambiguity; up to 5 real titles listed; a clear match keeps the exact watch ID and advancing-playback verification.

Live checks still owed: reboot by voice/typed (incl. muted, failed and interrupted relaunch), the HUD transition and sound on the live backend, YouTube "Sabr Al Rijal" end to end.

---

# Re-review of the corrected candidate (qitem-20261116213303-b6d9db66): ACCEPTED

Pins: all 7 SHA-256 values in docs/tasks/hud-reboot-youtube-review2-20261116.md match the bytes on disk.
Run by me (Windows): focused suites (same set as above) 362 passed; registry OK; all protected guards 409 passed.

- F1 resolved: `_arm_relaunch` records `muted` in the marker; `consume_ready_ack` returns "muted"; main.py suppresses both "I'm back and ready" and the first-boot greeting, consuming the marker once. Unmuted keeps the audible path.
- F2 resolved: probes now give "Sabr Al Rijal Mikasa Music" vs Latin-only "Hassen Thabet - Saber Elrjel | Mikasa Music" and "... (Full Version) ..." -> True, 0.86. "Sobre Regir" -> False; a different song -> False; contrived same-skeleton "Sbr Lr Jl Remix" -> False.
- F3 resolved (docstring). F4/F5 left as non-blocking notes.

Observations, not blocking:
- Both Saber Elrjel cards now tie at 0.86, so the screenshot case asks the owner to choose with real titles instead of playing; consistent with the brief's distinct-recording rule.
- Windowed spans let a longer requested title match a shorter title it contains (e.g. "Habibi Ya Nour El Ain" vs "Hbb Ya Nor" -> 0.86); from the original span design, capped at 0.86.
- Era's HUD mute itself is not persisted across a restart (pre-existing): after a muted reboot she is silent on return but speaks normally afterwards.

Still unproven live: muted/unmuted/failed/interrupted reboot, HUD transition + cue on the live backend, YouTube end to end.
