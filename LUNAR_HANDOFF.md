# Era One — Handoff for Lunar
Updated: 2026-10-02
Session count: 3

> **Read `TODO.md` first.** Since 2026-09-29 the work is driven by a numbered
> board there, with evidence for every closed item in `handoff.md`. This file is
> the map of the system; `TODO.md` is what is actually being worked on.

## What Era One is
Era One is a Windows desktop AI assistant ("Era") with a live voice/text conversation
loop, its own browser and desktop automation, and a separate "Community-Management"
(CM) lane that runs a client's social/ad presence (Delta Cuisine, Brico Kitchen, Ceram
Quartz) from an Obsidian vault. It is for one operator (the repo owner), not a product
for other users yet. It solves two problems at once: (1) a local, speaker-gated voice
assistant that can see and control the machine (windows, browser, media, files) instead
of just chatting, and (2) an automated but human-approved marketing operator that
drafts, revises and publishes brand content without hallucinating brand facts.

No purpose shift since the last update; scope keeps growing at the edges (exact
per-app Spotify control landed today) but the core mission is unchanged.

## Current architecture (one diagram)
```
 Mic/Speaker ──audio──> app/era_live/* (Gemini Live session)
                          │                         │
                          │ text/voice turn         │ tool calls (WS)
                          ▼                         ▼
                 core/input_safety_gate.py ──> core/action_registry.py
                          │                         │
              core/speaker_recognition.py    core/safety_guard.py
              (owner-only gate, fail-closed)  (confirmation gates)
                          │                         │
                          ▼                         ▼
                 era_dispatch.py (single big dispatch table: desktop, browser,
                 media, vision, memory, CM, ads, gmail, scheduler actions)
                          │
        ┌─────────────────┼──────────────────────────────┬─────────────────┐
        ▼                 ▼                              ▼                 ▼
 core/vision_*.py   actions/ (desktop/browser/    core/cm_vault.py /   core/gmail_*.py
 (screen understand, media control impls,          cm_learning.py       (Gmail API,
  Gemini/Ollama)     incl. spotify_exact.py)       ──HTTP/fs──>          OAuth token
        │            [pycaw + WinRT GSMTC]          Era One Obsidian      outside repo)
        ▼                                          vault/ (Markdown,          │
 main.py ──spawns──> ui.py (QWebEngineView          PARA+brand layout)         ▼
   shell) ←WebSocket→ ui/dist (React/Vite HUD)          │                 Gmail API
        +                                               ▼                 (IMAP-less,
 app/hud/* (separate aiohttp localhost:8765        Meta Ads dispatch      REST)
   browser-rendered HUD, token-guarded)              (era-ads-manager /
   — both UI surfaces exist; relationship            era-meta-analyst
   between them is an open question below            skills) ──HTTPS──> Meta Graph API
        │
        ▼
 era_supervisor.py (process supervisor,
 relaunch/watchdog, mutex per ERA_HOME)
        │
        ▼
 core/task_scheduler.py (APScheduler) ──fires──> era_dispatch.py actions (same path)

 core/local_brain.py / core/self_healing_router.py [failover path, mostly OFF]:
   Gemini quota exhausted ──> spare key ──> CodeCraft (disabled by user) ──> Claude Haiku
                                                                    ──> local Gemma (D:\EraOne M4)

 Telegram bridge (era_telegram.py) ──long-polling──> same text-intent path as typed input
 core/claude_bridge.py ──drives Claude Code CLI window via UIA──> reads
   transcripts back into Era (no official Claude Code API used)

 hermes_dispatch.py / hermes_router.py — exist, have dedicated tests, but era_dispatch.py
   has zero references to "hermes" (grep-verified this session) — not shown wired into
   the diagram above because its integration point, if any, is unconfirmed.
```
Corrected since last update: `ui.py` was previously flagged "likely stale" — verified
this session that `main.py` actively imports and drives it (`main.py:2463`, `from ui
import EraUI`); it is the live QWebEngineView shell, not dead code. [NEW] `actions/
spotify_exact.py` (exact per-app Spotify control, landed today).

## File inventory
Real source (excludes `.git/`, `__pycache__`, `.phase*_backups/`, the vault, and the
1,359 files under `.claude/` which are tooling/skill config, not app source) is ~460
files. Below: every root-level file, then per-directory summaries with the load-bearing
files named explicitly. Line counts are `wc -l`-order approximate.

| Path | Lines | Purpose | State |
|---|---|---|---|
| main.py | — | Primary entry point; imports and drives `ui.EraUI` | working — verified this session |
| era_dispatch.py | ~124K chars / large | Central action-dispatch table for all tool calls | working, actively edited |
| era_supervisor.py | ~24K chars | Process supervisor: launch, relaunch, watchdog, mutex | working |
| era_telegram.py | ~28K chars | Telegram long-polling bridge into text-intent path | working (needs `doctor` if webhook set — see broken) |
| era_scheduler.py | ~8K chars | Thin wrapper around APScheduler; `reload()` wipes jobs | working, fragile (see broken) |
| era_offers.py | small | CM/ads offer logic | untested this session |
| hermes_dispatch.py / hermes_router.py | 4.2K / 16.2K chars | Secondary router (Hermes); NOT referenced by era_dispatch.py (0 grep hits, verified) | has dedicated tests (`test_hermes_router.py`, `test_hermes_wiring.py`) but wiring/entry point unconfirmed; last touched Jul 24 — looks parked |
| music_generate.py | — | Background-music generation for Reels | working per `cm-lane` skill |
| media_control.py / media_host.py | — | Media/device control host process | untested this session |
| ui.py | 21.6K chars | QWebEngineView wrapper hosting the React/Vite HUD (`ui/dist/`); WebSocket bridge to main.py; title-bridge exposes window controls | **live** — imported by main.py, verified this session |
| final_standalone.py | — | Unclear purpose, name suggests a fallback/standalone runner | untested — verify before touching |
| audio_detect.py / calibrate_spotify.py | — | Audio device / Spotify calibration utilities | untested this session |
| clean_zero_kb.py | — | Deletes stray 0-byte junk files (see `era-root-file-guard` memory) | working |
| conftest.py | 11.3K chars | pytest fixtures; must set ERA_VAULT_DIR/ERA_LOCK_FILE for isolation | working, has had past gaps (memory: vault + lock leaks) |
| test_*.py (13 files, root) | — | Ad-hoc regression tests, NOT under `/tests` (violates repo's own layout rule) | mixed — not re-run this session |
| brands.yaml | 87K chars | Authoritative brand config (ads accounts, design rules, magnific flows) — needs ruamel to edit, plain yaml dump strips comments | working, comments load-bearing |
| requirements.txt / requirements.windows.txt | — | Python deps (pip, no lockfile beyond pip's own) | working |
| package.json | 2 deps | `@claude-flow/aidefence`, `agentic-flow` — Node side is minimal, not the app | working |
| **core/** (92 files) | | Shared engine: safety, voice, vision, memory, task scheduling, vault I/O | see below |
| core/safety_guard.py | 42.9K chars | Confirmation-gate / action-authorization core | working — reviewed by `security-reviewer` agent after changes |
| core/action_registry.py | — | Maps action names → handlers; collision-prone (memory: generic names get hijacked) | working, needs care on every new action |
| core/speaker_recognition.py | — | Owner-only voice gate (TitaNet-large), fail-closed | working |
| core/input_safety_gate.py | — | Gates only typed input, not ASR (per `text-voice-parity` memory) | working, known asymmetry |
| core/vision_*.py (10 files) | — | Screen understanding (Gemini/Ollama), resilience/backoff | working with guards added 2026-09-17 |
| core/cm_vault.py, cm_learning.py, vault_maintenance.py, vault_writer(via daily_note.py) | — | CM lane's vault read/write + nightly maintenance | working, precedence rules documented in `cm-lane` skill |
| core/gmail_*.py (4 files) | — | Gmail lane: auth, service, watcher, priority | added 2026-09-15, working |
| core/task_scheduler.py, task_store.py, task_chain.py, task_catchup.py, task_reports.py, task_trace.py | — | User-facing scheduled-task system | working, own APScheduler instance |
| core/claude_bridge.py, claude_error_router.py, claude_transcript.py | — | Drives Claude Code via UIA, routes errors, reads transcripts | working, unverified reply-detection path |
| core/root_file_guard.py | — | Quarantines stray 0-byte root files, logs traceback | working |
| core/local_brain.py, self_healing_router.py | — | Gemini-quota failover chain | working, CodeCraft leg disabled by user choice |
| (79 more core/*.py) | — | audio device/endpoint/battery routing, panic-stop, protocol-checks/spies, session/state stores, tool_result classification, etc. | not enumerated per-file — ask Lunar/Explore-agent for a specific one |
| **actions/** (54 files) | | Concrete implementations dispatched by era_dispatch.py (desktop, browser, media ops) | mostly working |
| actions/spotify_exact.py | [NEW, today] Exact per-app Spotify control: volume via pycaw with cached pointer (`spotify_set_volume_exact`/`get`/`volume_delta`/`mute_exact`/`unmute_exact`/`toggle_mute_exact`, ~0.04 ms warm), track-as-data via WinRT GSMTC delegated to `spotify_gsmtc_host.py` (`spotify_current_track`/`playback_state`/`progress`/`seek_exact`) | all verbs live-verified working |
| actions/spotify_gsmtc_host.py | [NEW, today] Persistent PowerShell host driving WinRT GSMTC (media session data/seek) — one warm process for the Era lifetime, JSON over stdin/stdout, reaped by atexit and by the supervisor's `taskkill /T` | working; ~cold spawn ~0.5 s, warm 1–21 ms/op |
| actions/page_refs.py | — | Text-first browser control (`page_snapshot`/`click_ref`) | working, newest browser-control addition before today |
| **app/** (43 files) | | Live-session runtime + a second HUD surface | see diagram |
| app/era_live/* (13 files) | — | Gemini Live session plumbing: audio, cascade, dispatch, panic, reconnect | working; `tool_dispatch.py.bak-20260925-pretrace` is a stray backup file, not source |
| app/hud/* (14 files) | — | Separate localhost:8765 aiohttp HUD, token-guarded, event bus | working, Phase 1 of modernization done 2026-09-26; relationship to `ui.py`'s HUD is an open question |
| **tools/** (153 files) | | Largest single directory; not inventoried per-file this pass | untested this session — flag for Lunar if this needs auditing |
| **config/** | | brands.yaml companion configs, API keys (`api_keys.json`, gitignored), speaker enrollment, voice/wake-word prefs | working; `api_keys.json` is permission-denied by design |
| **memory/** (9 .py + state files) | | Two-tier pinned-memory system, feedback capture, learned patterns | working per `era-memory-architecture`/`era-memory-gate-window` memory |
| **docs/** | | ADRs, CM commands, HUD/localhost notes, safety notes, operations, `spotify_bridge_csp_probe.md` [NEW today] | current |
| **Era One Obsidian vault/** | | CM lane's actual content store (not code) | working, PARA+brand layout since 2026-07-21 |

## Stack
- Python 3.14.7, no virtualenv (memory: `audioop` removed in 3.14, uses numpy instead)
- Windows-only runtime target (win32gui, pycaw, WinRT via comtypes, PyQt6)
- Package manager: pip (`requirements.txt`, `requirements.windows.txt`); no Python lockfile
- Node side: npm, minimal (`package.json` — `@claude-flow/aidefence`, `agentic-flow`); a separate React/Vite build produces `ui/dist/` for `ui.py`'s QWebEngineView shell
- Key libraries actually in use: google-genai/google-generativeai (Gemini Live + vision),
  playwright (browser control), pyautogui/pywinauto (desktop control), sounddevice/comtypes/pycaw
  (audio device routing), WinRT/GSMTC (media session data, pending a Python 3.14 wheel),
  APScheduler (task/CM scheduling), aiohttp (secondary HUD server), ruamel.yaml
  (brands.yaml — must use this, not PyYAML, or comments are lost)
- Models: local (Gemma via D:\EraOne M4), remote (Gemini, Claude Haiku failover), Fish Audio + Piper/Kokoro TTS

## External integrations
- **Gemini Live** — WebSocket, `app/era_live/*` — working — needs `GOOGLE_API_KEY`(s); multi-key failover on quota (per-key, not per-session)
- **Gmail API** — REST, `core/gmail_*.py` — working — OAuth token stored outside repo; minimal scopes; `label_ids=[]` means ALL mail
- **Telegram Bot API** — long polling, `era_telegram.py` — working — a webhook set on the bot silently breaks it (409); run `doctor` if replies stop
- **Meta Graph API (Ads)** — HTTPS, dispatched via `era-ads-manager`/`era-meta-analyst` skills + `era_dispatch.py` CM lane — working — always-paused review gate before spend; USD not TND
- **Fish Audio TTS** — HTTPS, `core/tts_fish.py` — working — primary voice backend, no reconnect needed; Gemini TTS is the held fallback
- **Claude Code CLI** — local UIA automation, `core/claude_bridge.py` — working, unverified — drives the desktop window, reads replies from transcripts (no accessible API for replies)
- **CodeCraft (quota failover leg)** — disabled by user (2026-09-25); do not re-enable unasked
- **Obsidian vault (local filesystem)** — `Era One Obsidian vault/`, `core/cm_vault.py` — working — source of truth for CM lane
- **Star Glass / Bluetooth audio devices** — WinRT via comtypes, `core/audio_*.py` — working — battery read via SetupAPI, not `Get-PnpDevice` (64s vs ~20ms)
- **Spotify** — window automation + pycaw (exact volume, cached pointer → ~0.04 ms warm) + WinRT GSMTC track data via a persistent PowerShell host (`actions/spotify_gsmtc_host.py`), `actions/spotify_exact.py` — all working, live-verified — the older Spicetify bridge integration is dormant, not this path
- **git / GitHub** — **LIVE since 2026-09-30**, reversing the 2026-09-25 disable. `.git/HEAD.disabled` is gone (verified 2026-10-02). Remote is the PRIVATE repo `kimuss92/EraOne`; development happens on a `claude/*` branch and Karim pulls it to `C:\Users\nesri\OneDrive\Documents\EraOne` with `git pull origin <branch>`. **`git checkout` is NOT a substitute — once the branch exists locally it is a silent no-op and he sees no new code.** Commit and push are expected, not forbidden; `config/api_keys.json` stays gitignored and blocked by the guard hook

## What is currently working
- Owner-only voice gate (speaker recognition, fail-closed) gating the mic path
- Gemini Live voice/text session with barge-in (half-duplex mic by design)
- Desktop/browser/media action dispatch through `core/action_registry.py` + `era_dispatch.py`
- CM lane: vault read + two approval gates (plan approval ≠ asset approval) + nightly vault maintenance (03:10)
- Gmail lane (added 2026-09-15) and Telegram bridge, both feeding the same text-intent path as typed input
- Task scheduler with fire-time gating, fingerprint waivers, and opt-in catch-up for missed runs
- `ui.py` (QWebEngineView + React/Vite) as the primary UI shell, driven directly by `main.py` — verified this session, not previously confirmed
- Separate `app/hud/*` at `127.0.0.1:8765` (token-guarded aiohttp server); Phase 1 modernization landed 2026-09-26 — its relationship to `ui.py`'s HUD is unresolved (see open questions)
- **Phase 0 stabilization (2026-09-29 → ongoing)**, tracked item-by-item in `TODO.md` with evidence in `handoff.md`. Landed since the last update to this file:
  - `memory/semantic_recall.py` — recall by MEANING as well as words, via Ollama `all-minilm`. Floors are measured against a FROZEN synthetic corpus (`tools/golden/semantic_recall_store.json`), never the live store, because the live store grows every time Era learns something
  - `core/turn_latency.py` — one `turn.latency` line per turn breaking the wait into endpoint wait / model think / playback / tool time, so "she responds too late" can be measured instead of argued about
  - `core/tool_recovery.py` — a failed tool no longer announces itself mid-turn before a workaround can be tried; the notice is held to the turn boundary and cancelled if a retry succeeds
  - `core/claude_error_router.py` — repeated tool failures are reported to Claude Code by code, with a pending queue so a report is never silently dropped
  - `actions/self_diagnosis.py` — a provider 5xx is no longer counted as an Era crash (it was forcing a DEGRADED verdict off upstream outages)
- Netflix control via the player's own JS API (not DOM clicks — a Brave extension strips buttons)
- Spotify exact control (window-automation lane, NOT the dormant Spicetify bridge): exact per-app volume via pycaw with a cached ISimpleAudioVolume pointer (~0.04 ms warm, live-verified) and track-as-data (title/artist/album/position/duration + exact/relative seek) via WinRT GSMTC through a persistent PowerShell host `actions/spotify_gsmtc_host.py` (live-verified: real track data, `seek_exact` ±0 ms, `spotify_forward`/`spotify_rewind` relative seek) — `actions/spotify_exact.py`, routed through the `spotify_control` tool
- Spotify Liked Songs / New Releases / Made For You playback AND search-and-play now via UI Automation (`actions/spotify_ui.py`), no pixel clicks: collections navigate by URI (Liked Songs = `spotify:collection:tracks`) or Spotify's alt+shift shortcut. SEARCH enters the query into Spotify's search combobox via **UIA `SetValue`** (`set_search_query`) — focus-independent (writes straight to the element, unlike keystrokes which need OS foreground and misfired) AND shows CLEAN text in the box ("chill music", not "chill+music"); Chromium fires the input event so the live search reacts. Then `invoke_search_result` polls the a11y tree and invokes the result whose "Play <title>" best matches (exact > all-query-words > word overlap > substring, with &/punctuation normalized). It requires a real textual match (level>=1) and NEVER grabs a "top result" with zero word overlap — that only happens when the new results have not rendered yet, so it keeps polling (this was the "viva la vida played a stale leftover track" bug). Live-verified: "bohemian rhapsody"→Queen, "viva la vida"→Coldplay, "blinding lights"→The Weeknd, "chill music"→a chill track. Needs a one-time a11y warm-up walk (Chromium builds its accessibility tree lazily on first UIA access). Fallback: if the search box can't be reached via UIA, `_open_search` uses the `spotify:search:<q>` URI (reliable but renders "+").
- Git + private GitHub backup (`kimuss92/EraOne`), live since 2026-09-30
- **A 3-tier regression gate**: `tools/era_gate.py` (`--changed` for the blast radius of a diff, `--all` for ~5,300 tests, ~10-12 min) built on an import-graph reachability map, plus `tools/era_doctor.py` for environment health and `tools/era_manifest.py` for the module map

## What is currently broken
- `era_telegram.py` — a webhook accidentally set on the bot returns 409 and silently kills long-polling; needs manual `doctor` run, no auto-detection yet
- `era_scheduler.py` — `reload()` wipes all APScheduler jobs; anything that calls it without care loses scheduled tasks
- Root-level `test_*.py` (13 files) — live outside `/tests`, violating this repo's own CLAUDE.md rule; not reorganized
- `.phase1_backups/` through `.phase7_backups/` and `app/era_live/tool_dispatch.py.bak-20260925-pretrace` — stray backup files sitting in the live tree; harmless but not cleaned up

## What is in progress
- Wardrobe Reel generation (Delta Cuisine): master-anchored generation method (`wardrobe-reel-workflow` skill) replacing 6 independent prompt nodes; needs live validation against a real render run — done: the pipeline design; left: confirming it actually holds identity across shots in production
- HUD modernization: Phase 1 (identity vs state color channels) done 2026-09-26 on `app/hud/*`; later phases not started; unclear if this modernization is meant to also cover, or eventually replace, `ui.py`'s HUD
- Hermes router (`hermes_dispatch.py`/`hermes_router.py`) — has dedicated tests but zero references from `era_dispatch.py` (verified this session); last source change Jul 24 — looks stalled rather than actively worked, but not confirmed abandoned

## What is next

`TODO.md` is authoritative and ordered. In short, as of 2026-10-02:

1. **Two live checks are owed by Karim, not by code** — board items 19 and 41.
   Both mechanisms are built, tested and pushed; what neither can prove from a
   Linux container is that they behave on his Windows machine. The test scripts
   are written in plain language in the chat handoff and summarized in `TODO.md`.
2. **Board item 20** — continuous learning pipeline. The next unstarted item.
3. **Board item 35b** — one full `python tools/era_gate.py --all` on Windows.
   Every session so far has run on Linux, and Era is a Windows program.
4. **Board item 42** — a fresh clone is RED (26 tests) until `tools/era_manifest.py`
   is run, because `runtime/` is gitignored. The fix is a DECISION — document it as
   a required first step, or have `conftest.py` generate the manifest when missing.
   The second is a behaviour change (a test fixture writing a build artifact) and
   should not be made as a side effect.
5. **Board item 20b** — the quota ledger was proposed, approved in principle, then
   NOT built because the measurement came back `engine.quota.exhausted = 1`. Three
   real defects behind it stand and are recorded. Revisit when that count moves.
6. **Deliverable 9** — a full `README.md` generated from `runtime/manifest.json`.
   Unblocked, not started.

Older entries from the 2026-09-27 pass, still open: the `hermes_*` wiring question,
the `ui.py` vs `app/hud/*` relationship, and the 13 root-level `test_*.py` files.

## How work is done now (changed since the last update)

- Develop on the `claude/*` branch named at the top of `TODO.md`. Commit and push
  to `kimuss92/EraOne`; Karim pulls with `git pull`, **never `git checkout`**.
- A board item moves to Done only when something RAN and proved it. "Written" is
  not "done" — that rule is at the top of `TODO.md` and is enforced in practice.
- A test is not trusted until it has been seen to FAIL. Revert the fix, watch the
  test go red, restore. Where that is impractical, mutate the code instead and
  record how many mutations were caught.
- A gate failure is ATTRIBUTED, never waved at: run the same suite on a baseline
  worktree and diff the failure lists. Generate the manifest in that worktree
  first, or 26 tests fail for its absence and look like your doing.
- Regenerate the manifest **after** the last edit, not before. A manifest built
  mid-edit is stale by the time you commit, and `test_committed_artifacts_are_fresh`
  will catch it.

## Open questions for Lunar
1. Should the 13 root-level `test_*.py` files be moved into `/tests` now, or is there a reason (CI, tooling path) they were left at root?
2. Is `hermes_dispatch.py`/`hermes_router.py` a live secondary router with an entry point outside `era_dispatch.py`, or an abandoned experiment safe to archive?
3. `main.py` drives `ui.py` (QWebEngineView + React/Vite) as its UI, while `app/hud/*` is a separate aiohttp browser-rendered HUD that just got a modernization pass — are both meant to be live simultaneously, and if so what is each one for?

## Standing decisions
1. Spicetify replaces SpotX. Do not propose SpotX again.
2. Gemini is the fuzzy-fallback brain; regex fast-path stays local. Do not propose cloud STT as default.
3. ~~`git` is disabled repo-wide~~ — **REVERSED 2026-09-30.** Git and the private GitHub repo `kimuss92/EraOne` are the normal workflow now: develop on the `claude/*` branch named in `TODO.md`, run the gate green, commit with a message that says what was wrong and how it was verified, push. Never commit `memory/*.json` or secrets.
4. CodeCraft quota-failover leg is off by user choice (2026-09-25), independent of the Cascade voice-mode toggle.
5. `brands.yaml` must be edited with ruamel (round-trip), never plain PyYAML load+dump — plain dump deletes the comments that justify every account id.
6. Voice-mode preference (Fish vs. Gemini) is a deliberate user choice stored in `voice.json`; automatic fallback must never persist over it (`set_mode(persist=False)`).
7. CM lane has two separate approval gates — plan approval and asset approval are not the same signature.
8. Ads dispatch: no auto-greeting on messaging ads (Meta's Conversations template is off); only new campaigns are touched automatically, live campaigns are left alone unless named.
9. Never force-kill the MSI Center DCv2 process or use `Stop-Process` on Era (kills the supervisor too).
10. Spotify renderer resolves localhost, not 127.0.0.1 (probe finding, `docs/spotify_bridge_csp_probe.md`). Bridge stays in POLL mode when reactivated; SERVER/require() mode is dead.
11. Spotify bridge remains dormant until Spicetify ships 1.3.x support. Window-automation is the live path in the meantime.
12. Volume verbs cache the ISimpleAudioVolume pointer (per Era-process, invalidated on COMError); do not remove the cache to "simplify" — the cache is the latency budget (~0.04 ms warm vs ~30 ms uncached).
13. Track-data verbs route through a persistent PowerShell WinRT host (Step C shipped) because no `winrt`/`winsdk` CPython 3.14 wheel exists and the repo has no comtypes-WinRT helper to clone. Do not switch lanes without a measured reason.
14. Spotify Liked Songs / New Releases / Made For You play via UI Automation `invoke()` on the primary "Play <context>" button (`actions/spotify_ui.py`), NOT pixel-ratio clicks — the pixel clicks broke on every layout change/resize. Spotify's Chromium UI exposes its a11y tree only after a first UIA access, so the helper polls until the tree populates; keep that warm-up loop. Relative forward/rewind seek uses tokens `seek_forward`/`seek_rewind` (NOT `forward`/`rewind`, which the spotify_control router rewrites to next/previous track).

## Standing constraints
1. No API keys hardcoded; secrets live in `config/api_keys.json`, which is gitignored and permission-denied by the guard hook.
2. No cloud calls except the ones listed under "External integrations" above.
3. Windows-only (Win32/WinRT APIs throughout core/audio, core/window_awareness, etc.).
4. Voice latency budget: regex fast-path <400 ms, Gemini path <1.5 s (per architecture, not re-benchmarked this session).
5. Files should stay under 500 lines per this repo's CLAUDE.md — several root files (`era_dispatch.py`, `brands.yaml`, `core/safety_guard.py`) already violate this; do not treat that as license to keep growing them.
6. Never delete 0-byte files automatically — an empty file may be an in-progress write, not junk (`clean_zero_kb.py`/`root_file_guard.py` both respect this).
7. Mic path is half-duplex by design (barge-in) — do not "fix" this into full-duplex without the user asking.

## Session log
- 2026-10-02 — Phase 0 session. Closed board item 36 (recall floors measured on a frozen corpus: top-1 8/12, top-3 11/12, noise 0/8), board item 19 (a failed tool used to announce itself out loud BEFORE any workaround could be tried — the prompt also promised a recovery layer that structurally cannot exist, since `route_tool_call` is pre-dispatch only and has no parameter a result could arrive in), and board item 41 (Ollama unloads an idle model after 5 min, a cold load costs ~4s, the probe allowed 1.5s — so memory silently dropped to word-matching after every pause and never recovered; fixed with `keep_alive`, a background warm-up off the conversational path, and logging the transition). **Corrected this file's standing decision #3: git is NOT disabled** — it has been the live workflow since 2026-09-30 and the old entry would have stopped the next session pushing anything. Added a "Talking to Karim" rule to `CLAUDE.md` (plain language, always include how to test) and a skills/plugins auto-use table.
- 2026-09-27 (later still) — Spotify search fix round 3 (clean box, "one for all"): the search box showed "chill+music" because URI nav renders spaces as "+". Fixed by entering the query via UIA `SetValue` on Spotify's search combobox ("What do you want to play?") — clean text + focus-independent (keystroke typing failed on Windows foreground restrictions; argv nav ignored by a running Spotify). Removed the level-0 "top result" fallback from `_best_search_match` — it grabbed the STALE previous-query results before the new ones rendered (played the wrong Coldplay track for "viva la vida"); now requires a real word match and keeps polling. Live-verified 4/5 exact (5th was GSMTC title-read lag, not a miss).
- 2026-09-27 (later) — Spotify search fix round 2: the "+"-cosmetic fix (Ctrl+L + typewrite) regressed search — Windows foreground restrictions made keystrokes land in the wrong window, leaving stale results (voice "chill music" failed). Reverted search to reliable `spotify:search:` URI navigation (box shows "+", cosmetic) and instead fixed the real gap: `_best_search_match` now does &/punctuation normalization + partial word-overlap scoring + a top-of-screen fallback for vibe queries (so "chill music" plays the top result). Note `minimize spotify after use` is an active standing rule; UIA reads need the window restored (invoke handles this).
- 2026-09-27 — Spotify seek + content-play: added relative `spotify_forward`/`spotify_rewind` on GSMTC (tokens seek_forward/seek_rewind to dodge the router's next/prev rewrite; live-verified ±1 ms). Fixed Liked Songs / New Releases / Made For You: replaced blind pixel-ratio clicks (broke on resize/layout change) with URI/shortcut navigation + UIA `invoke()` of the primary "Play <context>" button (`actions/spotify_ui.py`) — live-verified Liked Songs + New Releases both play. Chose UIA after verifying (as promised) that Spotify's a11y tree IS reachable once warmed (635 elements incl. "Play Liked Songs"); a naive single walk sees only ~4 chrome buttons, so the helper polls until the tree populates.
- 2026-09-27 — Spotify lane hardening: cached the pycaw volume pointer (28–46 ms → ~0.04 ms warm), and shipped GSMTC track-data via a persistent PowerShell host (Step C). Chose C because Step A found no usable Python 3.14 wheel (`winrt`: no distribution; `winsdk`: sdist-only, build-from-source, banned) and Step B was impossible (repo has no comtypes-WinRT helper to clone; `comtypes` is pycaw-only). Live-verified: real track data, seek ±0 ms, no orphan powershell on exit.
- 2026-09-27 — Refreshed handoff: verified `ui.py` is live (main.py:2463 imports `EraUI`, not dead code as previously flagged), confirmed `hermes_dispatch.py`/`hermes_router.py` have zero references from `era_dispatch.py` despite dedicated tests, added `actions/spotify_exact.py` (new today — exact per-app volume + WinRT track control) to the inventory, and carried forward the two Spotify-bridge standing decisions that were added to the file directly since the last update
- 2026-09-27 — Created LUNAR_HANDOFF.md for the first time; populated from a live pass over the repo (file counts, requirements, integrations) plus verified existing project memory
(older entries trimmed)

## How to resume
1. Open a new session on the repo.
2. Say: **"Read CLAUDE.md, TODO.md and handoff.md, then continue the Phase 0 board."**
   Those three files carry the current state; this file is the system map behind them.
3. Add one sentence saying what you want next, or let the board's order decide.
