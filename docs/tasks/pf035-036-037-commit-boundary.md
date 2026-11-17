# PF-035/036/037 commit boundary — dev-build analysis (2026-11-17)

Request: qitem-20261117011443-d664c511d58531ec (stage only the accepted PF-035/036/037,
no PF-038/039/040, no unrelated edits). HEAD = 3a337b9 (era/task-lessons). Era not restarted.

## Bottom line
Only **PF-036** has an exact boundary against HEAD. **PF-035** and **PF-037** cannot be
committed alone: each modifies or depends on code that has never been committed.

## PF-036 (reboot acknowledgement): stageable
- Exact patch: `docs/tasks/pf036-exact-vs-HEAD.diff` (166 lines; core/era_relaunch.py +67,
  app/era_live/tool_dispatch.py +14/-4, main.py +16/-1). Built in a clean `git worktree` at
  HEAD: the filtered hunks applied cleanly; the 2026-11-05 "rebot / tribute Era One"
  `_MISHEARD` matcher that shares a hunk was REMOVED (it is earlier uncommitted work, see
  handoff.md line ~1131), so `_CMD` stays as at HEAD.
- Judgement call to confirm: the tool_dispatch hunks also make `_arm_relaunch` return
  "recoverable only if the flag was written" and add "I could not schedule my restart."
  I attributed them to PF-036 (your scope: "arm marker only for a recoverable requested
  relaunch"). Drop them if they predate PF-036.
- Checks in the HEAD worktree (Windows Python): the four PF-036 guards, copied into an
  isolated file (`docs/tasks/pf036-isolated-guards.py.txt`, because tools/test_era_relaunch.py
  imports `divert_era_reboot` and therefore cannot import at HEAD): **3 passed**
  (marker one-shot/expiry, non-owner source, spoken only after connect). The dispatcher guard
  `test_shutdown_era_relaunch_writes_relaunch_not_stop` **fails at HEAD for a reason
  outside PF-036**: HEAD's `core/self_healing_router.py` imports `netflix_context_fresh`,
  which HEAD's `core/intent_memory.py` lacks, so `app/era_live/tool_dispatch.py` cannot
  import at HEAD at all.
- That HEAD inconsistency comes from commit 3a337b9 (dev-build, owner-approved scope "my
  files whole + new modules"): it took self_healing_router.py whole, including other
  agents' edits. HEAD was already not runnable alone; this confirms it concretely.

## PF-035 (themed HUD inactivity + cue): depends on uncommitted PF-033
- Its ui_src changes read `listeningMode` (ui_src/src/lib/store.ts) and the backend
  `listening_mode` push (app/hud/status.py, app/websocket_server.py): all PF-033 (Codex),
  none in HEAD.
- app/hud/status.py `listening_mode` now also carries PF-040 (logging + `_busy_reason`).
- `ui/dist/index.html` is a build of the WHOLE ui_src tree, so it embeds any other
  uncommitted UI edits; it cannot be scoped to PF-035.
- tools/test_hud_inactivity.py is wholly untracked (PF-033 + PF-035 tests in one file).
- Minimum closure: PF-033 + PF-035 (+ PF-040's status.py hunk or a hand split) + a rebuild.

## PF-037 (YouTube transliteration + useful ambiguity): depends on earlier YouTube work
- `title_spelling_variant`, `_base_media_score` (utils/media_match.py) and
  `VideoSelectionRequired`, `_start_and_observe` (actions/youtube_video.py) are NOT in
  HEAD. The live 20:17 failure already raised VideoSelectionRequired, i.e. that code
  predates PF-037. actions/youtube_video.py differs from HEAD by +157/-256.
- tools/test_media_hud_reliability.py is wholly untracked; test_youtube_direct_play.py
  differs by +199/-198 (a rewrite).
- PF-037 cannot be separated from that baseline without its original author.

## Options for the owner (decision needed)
1. Commit PF-036 now from `docs/tasks/pf036-exact-vs-HEAD.diff` (`git apply --cached`), and
   accept that HEAD still cannot import tool_dispatch until the router/intent_memory
   baseline is committed.
2. Commit dependency closures per feature: PF-033+PF-035 (+ rebuild ui/dist) and
   PF-037 + the YouTube/media baseline, each with whole files — they will carry other
   agents' edits in those files, as 3a337b9 did.
3. First commit the 11-05..11-16 baseline (all agents), then the features on top: the
   only way to get exact, runnable boundaries.
