# Era One — Handoff for Lunar
Updated: 2026-09-27T18:30:00Z
Session count: 2

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

  lane_dispatch.py / lane_router.py — WIRED (typed text only, corrected 2026-10-07):
    _on_text_command (main.py:601) ──main.py:940──> _route_text_through_lanes (cm_flow.py:117)
      ──cm_flow.py:128──> lane_dispatch.handle(text, dispatch=False) ──> lane_router.route()
      └─ category "cm" ──> _arm_cm_handoff() ──> _run_cm_flow ──cm_flow.py:354──>
           lane_dispatch.handle(...) ──> era_dispatch.run_task(...)
      └─ "assistant" ──> normal Gemini flow. Voice is NOT routed through this path.
    main.py:2779 calls lane_router.warm_up() at startup. The dependency on era_dispatch is
    intentionally one-directional (lane_dispatch imports era_dispatch), which is why
    era_dispatch.py still has zero references TO lane_router.
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
| lane_dispatch.py / lane_router.py | 4.2K / 16.2K chars | Lane router (formerly Hermes): classifies typed text cm vs assistant, CM work forwarded to `era_dispatch.run_task` | **live, wired** — called from `app/era_live/cm_flow.py:128,354` via `_route_text_through_lanes`; NOT referenced by era_dispatch.py (dependency is intentionally reversed); tests `test_lane_router.py`, `test_lane_wiring.py`; renamed `hermes_*→lane_*` in 222752f (2026-10-04) |
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
- **git** — DISABLED 2026-09-25 (`.git/HEAD.disabled` confirmed present) — no commits, no push, no `gh`; do not re-enable without the user asking

## What is currently working
- Owner-only voice gate (speaker recognition, fail-closed) gating the mic path
- Gemini Live voice/text session with barge-in (half-duplex mic by design)
- Desktop/browser/media action dispatch through `core/action_registry.py` + `era_dispatch.py`
- CM lane: vault read + two approval gates (plan approval ≠ asset approval) + nightly vault maintenance (03:10)
- Gmail lane (added 2026-09-15) and Telegram bridge, both feeding the same text-intent path as typed input
- Task scheduler with fire-time gating, fingerprint waivers, and opt-in catch-up for missed runs
- `ui.py` (QWebEngineView + React/Vite) as the primary UI shell, driven directly by `main.py` — verified this session, not previously confirmed
- Separate `app/hud/*` at `127.0.0.1:8765` (token-guarded aiohttp server); Phase 1 modernization landed 2026-09-26 — its relationship to `ui.py`'s HUD is unresolved (see open questions)
- Netflix control via the player's own JS API (not DOM clicks — a Brave extension strips buttons)
- Spotify exact control (window-automation lane, NOT the dormant Spicetify bridge): exact per-app volume via pycaw with a cached ISimpleAudioVolume pointer (~0.04 ms warm, live-verified) and track-as-data (title/artist/album/position/duration + exact/relative seek) via WinRT GSMTC through a persistent PowerShell host `actions/spotify_gsmtc_host.py` (live-verified: real track data, `seek_exact` ±0 ms, `spotify_forward`/`spotify_rewind` relative seek) — `actions/spotify_exact.py`, routed through the `spotify_control` tool
- Spotify Liked Songs / New Releases / Made For You playback AND search-and-play now via UI Automation (`actions/spotify_ui.py`), no pixel clicks: collections navigate by URI (Liked Songs = `spotify:collection:tracks`) or Spotify's alt+shift shortcut. SEARCH enters the query into Spotify's search combobox via **UIA `SetValue`** (`set_search_query`) — focus-independent (writes straight to the element, unlike keystrokes which need OS foreground and misfired) AND shows CLEAN text in the box ("chill music", not "chill+music"); Chromium fires the input event so the live search reacts. Then `invoke_search_result` polls the a11y tree and invokes the result whose "Play <title>" best matches (exact > all-query-words > word overlap > substring, with &/punctuation normalized). It requires a real textual match (level>=1) and NEVER grabs a "top result" with zero word overlap — that only happens when the new results have not rendered yet, so it keeps polling (this was the "viva la vida played a stale leftover track" bug). Live-verified: "bohemian rhapsody"→Queen, "viva la vida"→Coldplay, "blinding lights"→The Weeknd, "chill music"→a chill track. Needs a one-time a11y warm-up walk (Chromium builds its accessibility tree lazily on first UIA access). Fallback: if the search box can't be reached via UIA, `_open_search` uses the `spotify:search:<q>` URI (reliable but renders "+").
- `.git` deliberately disabled; confirmed present as `.git/HEAD.disabled`

## What is currently broken
- `era_telegram.py` — a webhook accidentally set on the bot returns 409 and silently kills long-polling; needs manual `doctor` run, no auto-detection yet
- `era_scheduler.py` — `reload()` wipes all APScheduler jobs; anything that calls it without care loses scheduled tasks
- Root-level `test_*.py` (13 files) — live outside `/tests`, violating this repo's own CLAUDE.md rule; not reorganized
- `.phase1_backups/` through `.phase7_backups/` and `app/era_live/tool_dispatch.py.bak-20260925-pretrace` — stray backup files sitting in the live tree; harmless but not cleaned up

## What is in progress
- Wardrobe Reel generation (Delta Cuisine): master-anchored generation method (`wardrobe-reel-workflow` skill) replacing 6 independent prompt nodes; needs live validation against a real render run — done: the pipeline design; left: confirming it actually holds identity across shots in production
- HUD modernization: Phase 1 (identity vs state color channels) done 2026-09-26 on `app/hud/*`; later phases not started; unclear if this modernization is meant to also cover, or eventually replace, `ui.py`'s HUD
- Lane router (`lane_dispatch.py`/`lane_router.py`) — **live, not stalled** (corrected 2026-10-07): wired for typed text through `_route_text_through_lanes` (`cm_flow.py:117`), dry-classified at `cm_flow.py:128` then dispatched at `cm_flow.py:354`; the old "zero references from era_dispatch.py" note was true but misleading — the dependency is lane_dispatch → era_dispatch by design. Voice path deliberately not routed (Stage 3 covers typed text only)

## What is next
1. ~~Determine whether `lane_dispatch.py`/`lane_router.py` are called from anywhere~~ — **answered 2026-10-07: yes, wired** (chain in the architecture sketch above). Remaining decision, if wanted: extend Stage 3 to the voice path (today only typed text is lane-routed), or leave voice as-is
2. Clarify the `ui.py` vs. `app/hud/*` relationship: are these two independent UI surfaces serving different purposes (e.g. one is the always-on shell, one is a debug/ops panel), or is one meant to retire the other
3. Move the 13 root-level `test_*.py` files into `/tests` (or confirm they're intentionally root-pinned) — low risk, but touches `conftest.py` fixture paths, so verify ERA_VAULT_DIR/ERA_LOCK_FILE isolation still holds after the move
4. Optional optimization: if a `winrt`/`winsdk` CPython 3.14 wheel later ships (today: `winrt` has no distribution, `winsdk` is sdist-only, build-from-source, banned), the PowerShell GSMTC host in `actions/spotify_gsmtc_host.py` could be swapped for an in-process native path to shave the ~cold spawn — not blocking anything, track verbs already work via PowerShell

## Open questions for Lunar
1. Should the 13 root-level `test_*.py` files be moved into `/tests` now, or is there a reason (CI, tooling path) they were left at root?
2. ~~Is `lane_dispatch.py`/`lane_router.py` a live secondary router... or an abandoned experiment safe to archive?~~ — **answered 2026-10-07: live and wired** (typed text; not safe to archive). Open sub-question: should voice also go through it?
3. `main.py` drives `ui.py` (QWebEngineView + React/Vite) as its UI, while `app/hud/*` is a separate aiohttp browser-rendered HUD that just got a modernization pass — are both meant to be live simultaneously, and if so what is each one for?

## Standing decisions
1. Spicetify replaces SpotX. Do not propose SpotX again.
2. Gemini is the fuzzy-fallback brain; regex fast-path stays local. Do not propose cloud STT as default.
3. `git` is disabled repo-wide (2026-09-25). No commits, no push, no GitHub, no `gh`. Never re-enable unasked.
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
- 2026-10-07 — Corrected the lane-router status: `lane_dispatch.py`/`lane_router.py` are **wired, not parked**. Chain verified in code: `main.py:940` → `_route_text_through_lanes` (`cm_flow.py:117`) → `lane_dispatch.handle` (`cm_flow.py:128`, dry classify) → on CM, `_run_cm_flow` → `lane_dispatch.handle` (`cm_flow.py:354`) → `era_dispatch.run_task`; `main.py:2779` warms `lane_router` at startup; `test_lane_wiring.py` is the proof test. Scope is typed text only (voice untouched, per `cm_flow.py:121`). The earlier "zero references from era_dispatch.py" note was literally true but pointed the wrong way — the dependency is lane_dispatch → era_dispatch by design. Also noted: graphify's AST misses function-local `import x` + `x.f()` call sites, so the knowledge graph showed `lane_dispatch.handle` with no callers and repeated this stale claim as an AMBIGUOUS edge until checked against source.
- 2026-09-27 (later still) — Spotify search fix round 3 (clean box, "one for all"): the search box showed "chill+music" because URI nav renders spaces as "+". Fixed by entering the query via UIA `SetValue` on Spotify's search combobox ("What do you want to play?") — clean text + focus-independent (keystroke typing failed on Windows foreground restrictions; argv nav ignored by a running Spotify). Removed the level-0 "top result" fallback from `_best_search_match` — it grabbed the STALE previous-query results before the new ones rendered (played the wrong Coldplay track for "viva la vida"); now requires a real word match and keeps polling. Live-verified 4/5 exact (5th was GSMTC title-read lag, not a miss).
- 2026-09-27 (later) — Spotify search fix round 2: the "+"-cosmetic fix (Ctrl+L + typewrite) regressed search — Windows foreground restrictions made keystrokes land in the wrong window, leaving stale results (voice "chill music" failed). Reverted search to reliable `spotify:search:` URI navigation (box shows "+", cosmetic) and instead fixed the real gap: `_best_search_match` now does &/punctuation normalization + partial word-overlap scoring + a top-of-screen fallback for vibe queries (so "chill music" plays the top result). Note `minimize spotify after use` is an active standing rule; UIA reads need the window restored (invoke handles this).
- 2026-09-27 — Spotify seek + content-play: added relative `spotify_forward`/`spotify_rewind` on GSMTC (tokens seek_forward/seek_rewind to dodge the router's next/prev rewrite; live-verified ±1 ms). Fixed Liked Songs / New Releases / Made For You: replaced blind pixel-ratio clicks (broke on resize/layout change) with URI/shortcut navigation + UIA `invoke()` of the primary "Play <context>" button (`actions/spotify_ui.py`) — live-verified Liked Songs + New Releases both play. Chose UIA after verifying (as promised) that Spotify's a11y tree IS reachable once warmed (635 elements incl. "Play Liked Songs"); a naive single walk sees only ~4 chrome buttons, so the helper polls until the tree populates.
- 2026-09-27 — Spotify lane hardening: cached the pycaw volume pointer (28–46 ms → ~0.04 ms warm), and shipped GSMTC track-data via a persistent PowerShell host (Step C). Chose C because Step A found no usable Python 3.14 wheel (`winrt`: no distribution; `winsdk`: sdist-only, build-from-source, banned) and Step B was impossible (repo has no comtypes-WinRT helper to clone; `comtypes` is pycaw-only). Live-verified: real track data, seek ±0 ms, no orphan powershell on exit.
- 2026-09-27 — Refreshed handoff: verified `ui.py` is live (main.py:2463 imports `EraUI`, not dead code as previously flagged), confirmed `lane_dispatch.py`/`lane_router.py` have zero references from `era_dispatch.py` despite dedicated tests, added `actions/spotify_exact.py` (new today — exact per-app volume + WinRT track control) to the inventory, and carried forward the two Spotify-bridge standing decisions that were added to the file directly since the last update
- 2026-09-27 — Created LUNAR_HANDOFF.md for the first time; populated from a live pass over the repo (file counts, requirements, integrations) plus verified existing project memory
(older entries trimmed)

## How to resume with Lunar
1. Open a new session with Lunar.
2. Paste this entire file as the first message.
3. Write one sentence: what you want to work on next.
