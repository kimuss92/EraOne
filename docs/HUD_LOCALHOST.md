# ERA ONE — Localhost HUD

The ERA ONE interface now runs as a local web application at
`http://127.0.0.1:8765/`, served by the existing Python backend. The backend
keeps all intelligence, tools, voice, memory and credentials; the browser is the
presentation layer only.

Daily use: double-click **ERA ONE** on the desktop.

---

## 1. Architecture before

```
python.exe era_supervisor.py  (Scheduled Task at logon — visible console)
  └─ python.exe main.py        (inherits that console: logs visible)
       ├─ PyQt6 QMainWindow (frameless) + QWebEngineView
       │     └─ ui/dist/index.html  (React/Vite/Tailwind, file://)
       │          └─ ws://127.0.0.1:8765  (`websockets` server, no auth)
       ├─ EraLive engine (asyncio): Gemini Live, mic (sounddevice), speaker gate,
       │   barge-in, TTS (Gemini / Fish / Kokoro), tools, memory, schedulers
       └─ Flask LAN bridge :5005 (token-gated), Telegram, OmniRoute check
```

* The React UI was already web technology, but lived inside a Qt window.
* The browser microphone was used only for a VU meter; real audio I/O was
  always backend-side.
* No single-instance guard for `main.py` (two Eras ran at once on 2026-09-21).
* Some HUD readouts were fabricated (`SYNC %` and `ms/step` computed from CPU,
  "NEURAL CORE v1.1.3"); OS came from the browser user agent; uptime fell back
  to a browser counter; weather came from a hard-coded Tunisian city list.
* Tool activity, transcripts and sources were invisible (only log lines).

## 2. Architecture after

```
ERA ONE.lnk  →  pythonw.exe -m app.hud.launcher          (no console, ever)
                   │  /api/health? → already up: open/focus HUD, exit
                   └─ else: python.exe era_supervisor.py  (CREATE_NO_WINDOW)
                           └─ python.exe main.py  (inherits the windowless console)
                                ├─ aiohttp server 127.0.0.1:8765  (one port)
                                │     GET  /              HUD page (+ session token)
                                │     GET  /api/*         status, activity, memory…
                                │     POST /api/*         message, interrupt, voice…
                                │     WS   /ws            events + commands
                                ├─ HeadlessUI  ── same API the engine always called
                                ├─ HudBus (app/hud/events.py): truthful agent state
                                │     machine + event stream, fed by real signals
                                └─ EraLive engine — unchanged responsibilities
Browser app window (Edge/Chrome/Brave --app, dedicated profile) ──→ http://127.0.0.1:8765/
```

The legacy Qt window still works (`set ERAONE_UI=qt`), loading the same new
HUD from `file://` and authenticating with the same token.

## 3. Files changed

| File | Change |
|---|---|
| `main.py` | UI mode (browser default / qt legacy), single-instance mutex, console log tee, HUD server first, `HeadlessUI`, HUD actions (interrupt, shutdown, restart), auto-open policy, HUD hooks for typed input / wake word / engine restart. BOM preserved. |
| `app/websocket_server.py` | Rewritten on aiohttp: page + REST + WS on one port; token/Host/Origin security; port fallback; same public API. |
| `ui.py` | `EraUI` now inherits the shared `BroadcastUIMixin`; passes token to the page (`hud_query`). |
| `era_supervisor.py` | Single-instance mutex; windowless re-spawn when launched in its own console; `--debug`; `ERA_LAUNCH_REASON`; stands down on exit code 86. |
| `app/era_live/audio.py` | HUD hooks: real mic RMS, partial/final transcript, Gemini `finished` flag, response deltas, model-side interruption, grounding citations; `trigger_barge_in(source)`. |
| `app/era_live/tool_dispatch.py` | `tool.started`/`tool.finished` around the single tool chokepoint; shutdown message points at the launcher. |
| `app/era_live/cm_flow.py` | Routing boundary events; CM job tracked as a background activity. |
| `app/era_live/reconnect_mixin.py` | `set_status` → `connection.status` event. |
| `actions/weather_report.py` | `fetch_current()` — structured data from the existing Open-Meteo source. |
| `register_era_supervisor.ps1` | `install` now registers the silent launcher (`pythonw -m app.hud.launcher --background`). |
| `ui_src/` | New HUD (see §8). |

## 4. Files created

`app/hud/` — `events.py`, `bus_const.py`, `bus_tools.py`, `bus_legacy.py`,
`tool_adapter.py`, `ui_base.py`, `api.py`, `status.py`, `instance.py`,
`browser.py`, `settings.py`, `shortcuts.py`, `launcher.py`, `launcher_ui.py`;
`core/stdio_log.py`; `scripts/install_shortcuts.py`;
`assets/eraone.ico`, `assets/eraone-192.png`, `assets/eraone-512.png`;
`tools/test_hud_events.py`, `tools/test_hud_server.py`, `tools/test_hud_launcher.py`;
frontend `ui_src/src/lib/*`, `ui_src/src/components/{HudHeader, CoreStage,
ActivityPanel, SystemPanel, ContextPanel, LiveTranscript, CommandBar, Drawer,
ConversationDrawer, DiagnosticsDrawer, SettingsDrawer, CapabilitiesDrawer,
CommandPalette}.tsx`.

## 5. Files removed / archived

Git is disabled in this repository, so nothing was deleted outright. Replaced
frontend files were moved to `.phase7_backups/hud-legacy-20260926/` together
with the previously compiled UI (`ui_dist/index.html`) and pre-migration copies
of the rewritten files — see `ARCHIVE_NOTE.txt` there. Archived components:
`HudOverlay` (fabricated telemetry), `ThinkingIndicator`, `TitleBar` (fake
window controls in browser mode), `GeoWeather` (hard-coded cities, browser
fetch), `SystemMonitor`, `LogPanel`, `VoiceMeter`, hooks `useEra`,
`useAudioInput`, `utils/cn`. The Qt window code (`ui.py`) is kept.

## 6–7. New dependencies

None. aiohttp, psutil, pywin32 and tkinter were already present in Era's
interpreter; the frontend uses only the packages already in `ui_src/package.json`.

## 8. API endpoints

All on `http://127.0.0.1:<port>`. Every `/api/*` route except `/api/health`
requires header `X-EraOne-Token: <session token>`.

| Method | Path | Returns / does |
|---|---|---|
| GET | `/api/health` | `{app:"eraone", status:"starting"|"ready", pid, port, agent, voice, model, hud_clients, build, uptime_s}` — no token |
| GET | `/api/status` | agent state, model link, voice session model, STT/TTS provider, mic, voice lock, audio output, tools, memory, internet, integrations |
| GET | `/api/system` | backend system metrics (psutil/NVML) + OS |
| GET | `/api/capabilities` | the live tool registry grouped as skills, + integration status |
| GET | `/api/conversation?limit=` | conversation record of this backend session |
| GET | `/api/activity` | tool runs, context cards, sources |
| GET | `/api/events?limit=` | recent agent events |
| GET | `/api/memory` · `/api/memory/search?q=` | memory counts / read-only ranked search |
| GET/POST | `/api/settings` | HUD preferences (+ `autostart`) — validated |
| GET | `/api/weather` | current weather for the configured city (Open-Meteo, cached) |
| GET | `/api/schedule` | READ-ONLY view of the user's scheduled tasks (`app/hud/schedule_view.py`): active tasks, awaiting-approval first then soonest; next run from the live APScheduler job (`nextRunLive`), stored value as flagged fallback. No write route exists — tasks are created/changed through ERA ONE. |
| GET | `/api/diagnostics` | status + measured latencies, failures, recent events |
| POST | `/api/message` `{text}` | same path as a typed command |
| POST | `/api/interrupt` | stop speaking now (barge-in path) |
| POST | `/api/voice/start` · `/api/voice/stop` | backend microphone on/off |
| POST | `/api/standby` `{on}` | enter / leave standby |
| POST | `/api/shutdown` `{confirm:true}` · `/api/restart` `{confirm:true}` | the existing `shutdown_era` path |
| POST | `/api/reveal` `{cardId}` | show a file from a context card in Explorer (path from the backend's own store; never executed) |

Static: `/` (HUD), `/favicon.ico`, `/icon-192.png`, `/icon-512.png`,
`/manifest.webmanifest`.

## 9. WebSocket / event specification

`ws://127.0.0.1:<port>/ws?token=<session token>` (legacy path `/` also accepted).

Server → client:

* `{"type":"snapshot","data":{state,label,connection,tools,cards,sources,conversation,transcript,latency,…}}` on connect
* `{"type":"event","event":AgentEvent}` — `AgentEvent = {id, seq, ts, sessionId, type, state?, label?, message?, toolName?, toolCallId?, duration?, metadata?}`
* `{"type":"voice_level","level","active"}` — real mic RMS (≤13 Hz)
* legacy, unchanged: `state`, `log`, `amplitude` (+`bands`), `mute`, `browser_mic`, `system_metrics`, `model`, `model_catalog`, `theme`, `pong`

Event types: `agent.state.changed`, `connection.status`, `voice.wake_detected`,
`voice.listening.started|stopped`, `voice.transcript.partial|final`,
`agent.routing.started|completed`, `agent.request.sent`,
`agent.response.started|delta|completed`, `tool.started|progress|completed|failed|cancelled`,
`context.added`, `source.added`, `memory.retrieved`,
`tts.started|completed|interrupted`, `conversation.entry`, `error`,
`approval.requested|resolved` (Era's single pending confirmation — `app/hud/bus_approval.py`).

Client → server: `command {text}`, `interrupt`, `mic_toggle {active}`,
`mute {muted}`, `model_select {alias}`, `file_upload {name,mime,data}`,
`ping {t}`, `window_control {action}` (Qt host only), `mic_volume` (legacy).

## 10. Agent states

`idle · wake_detected · listening · transcribing · routing · thinking ·
tool_running · synthesizing · speaking · interrupted · complete · error ·
offline · standby · awaiting_approval`

Priority: standby > speaking > transient (wake/interrupted/complete/error) >
foreground tool > phase (listening/transcribing/routing/thinking/synthesizing)
> offline (model link down) > awaiting_approval > idle ("Ready" / "Mic off").

`awaiting_approval` ("ACTION REQUIRED") opens only when a tool result is
`ConfirmationRequired` AND the process-wide `PendingConfirmationStore` really
holds a live confirmation; the bus re-reads the store every 2 s while it is
shown and closes it the moment the store no longer holds it (confirmed,
cancelled, expired, replaced). Only the tool, its safe one-line description,
the gate's reason, risk and expiry are published — never raw parameters. The
HUD's Approve / Reject buttons send the bare words `yes` / `no` through the
normal typed-command path, so the backend gate (`_user_approved_pending`)
decides exactly as if the user had typed them.

Each comes from a real signal — mic RMS above ~−35 dBFS (listening), Gemini
input-transcription chunks (transcribing) and its `finished` flag (thinking),
the lane-router classify step (routing), a request handed to the live session
(thinking), `_execute_tool` entry/exit (tool_running), response transcript
before audio (synthesizing), legacy `SPEAKING` (speaking), barge-in (interrupted).
Pending phases expire (listening 1.3 s, transcribing 6 s, routing 20 s,
thinking 25 s, synthesizing 12 s, tool 240 s), so no state can stick.

## 11. URL / port

`http://127.0.0.1:8765/` by default. `ERAONE_PORT` overrides it. If the port is
held by another program (detected by probing, never by killing it) the backend
takes the next free port up to +10 and records it in `~/.era/eraone_backend.json`;
the launcher reads that file, and the HUD uses its own origin.

## 12–16. Startup, launchers, shortcuts

* **ERA ONE** (`pythonw -m app.hud.launcher`): checks `/api/health`; if ERA ONE
  is up it opens or focuses the HUD and exits; otherwise it clears an
  intentional-shutdown flag, starts the supervisor with `CREATE_NO_WINDOW`
  (a real console with no window, inherited by every child — verified: a
  flag-less grandchild attaches to it with no window), shows a splash of real
  startup stages if it takes more than ~1 s, waits for health `ready`, then
  opens the HUD. Failures show a dialog with Retry / Open Logs / Diagnostics.
* **ERA ONE Debug** (`python -m app.hud.launcher --debug`): same backend and
  HUD. If ERA ONE is running it tails `logs/eraone.log` live; otherwise it runs
  the supervisor in the console (logs streaming) and opens the HUD when ready.
* **Shortcuts**: Desktop and Start Menu → `ERA ONE`, `ERA ONE Debug`
  (`python scripts/install_shortcuts.py`). Pin either to the taskbar from its
  context menu. Icon: `assets/eraone.ico`.
* **Logon autostart**: the existing `EraOneSupervisor` task still starts the
  supervisor; it now detaches from its console at once. Re-running
  `register_era_supervisor.ps1 -Action install` (admin) switches the task to
  the fully silent launcher. The HUD's "Launch ERA ONE when Windows starts"
  switch manages a Startup-folder shortcut (off by default).
* **Browser**: the user's default browser if Chromium-based, else Edge, else
  Chrome/Brave, found via the registry; `--app=` window on a dedicated profile
  under `~/.era/hud-browser`; the default browser tab is the fallback.

## 17. Health check

`GET /api/health` answers as soon as the server binds (`status:"starting"`)
and reports `ready` once the engine object exists. The launcher polls it every
300 ms — no fixed sleeps.

## 18. Single instance

Named mutexes released by the kernel however the process dies:
`Local\EraOne.Backend` (main.py) and `Local\EraOne.Supervisor`. A second
`main.py` opens the running HUD and exits with code 86; the supervisor treats
86 as "someone else runs Era" and stands down. Names are suffixed with a hash
of `ERA_HOME` when it is overridden, so test instances never collide with the
live one.

## 19. Shutdown

HUD → palette → *Shutdown ERA ONE* → confirm → `POST /api/shutdown` → the
existing `shutdown_era` path: checks for a running CM job (asks by voice if
one is running), writes `~/.era/era.stop`, says goodbye, lets speech finish,
exits; the discovery file is cleared; the supervisor sees the stop flag and
exits; mutexes release with the processes. Closing the browser window does not
stop ERA ONE. *Restart ERA ONE* uses the relaunch path; an open HUD reconnects
(it reloads itself once to pick up the new session token).

## 20. Logs

`logs/eraone.log` and `logs/eraone-error.log` (console output with timestamp,
level, component; secrets masked; 5 MB × 5 rotation), `logs/launcher.log`,
`logs/era_crash.log` (supervisor), `logs/era_lifecycle.jsonl`, existing
`logs/era.log`.

## 21–22. Build and development

```
cd ui_src && npm run build        # → ui/dist/index.html (single file, offline)
python main.py                    # backend + HUD at http://127.0.0.1:8765/
python -m app.hud.launcher --debug
```

Frontend iteration: rebuild and reload the HUD — the backend re-reads
`ui/dist/index.html` whenever it changes, so no backend restart is needed. (The
Vite dev server alone cannot authenticate: the session token is injected by
the backend when it serves the page.)

Gate: `python -m ruff format . ; python -m ruff check --fix . ; python -m pytest tools/ -q`.

## 23. Tests performed

Automated (new): `tools/test_hud_events.py` (20 — state machine walks real
signals, phases expire, barge-in, gate outcomes ≠ failures, sources only from
real URLs, memory counts, secret masking, first-audio latency),
`tools/test_hud_server.py` (14 — token/Host/Origin/CSP, registry-backed
capabilities, message/shutdown/settings validation, WebSocket snapshot/events/
commands, port fallback + conflict reporting, HeadlessUI),
`tools/test_hud_launcher.py` (10 — mutexes, discovery file, startup errors,
foreign-listener detection, stop/halt flags, settings, log tee masking and
rotation). Updated: `tools/test_model_indicator.py` (2 tests moved to the new
server internals), manual `tools/test_websocket_bridge.py` (8/8).

Full suite with the live Era running: **4,846 passed, 3 failed, 1 skipped,
2 xfailed** — the 3 failures are the pre-existing `test_lane_wiring.py` stub
(see §26). Ruff check/format clean on every touched file.

Live, against the real backend on this machine (2026-09-26):

| # | Test | Result |
|---|---|---|
| 1 | Startup via the shortcut target | HUD window open 7 s after launch; health `ready` 8.8 s; **no console window** before or after (window enumeration) |
| 2 | Triple launch | one supervisor, one backend; launchers exit; existing window focused; no second window |
| 3 | Debug launcher | attaches to the running backend and streams `logs/eraone.log` live |
| 4–5 | Voice / barge-in | **not verified by hand** — needs a person speaking. Real mic → HUD level stream verified (~8 frames/s); transcript/interrupt paths covered by unit tests |
| 6 | Real tool | "weather in Tunis": ROUTING (477 ms) → THINKING → `weather_report` 1.0–1.3 s → context + source cards → SYNTHESIZING → SPEAKING |
| 7 | Multi-tool / meeting prep | multi-tool flow verified in the harness; no calendar integration exists → shown as NOT CONNECTED |
| 8 | Spotify | real `spotify_control` pause, 101 ms; Era reported Spotify not running (true) |
| 9 | System metrics | backend psutil/NVML values; N/A where unreadable; reset to N/A when the link drops |
| 10–11 | Model / internet down | OFFLINE + banner verified with the backend stopped; model-link states come from `set_status` (unit-tested classification) |
| 12 | Sources | only URLs present in real results / Gemini grounding metadata |
| 13 | New capability | capabilities and the core's node ring are built from `TOOL_DECLARATIONS`; unknown tools get a generic row (unit-tested) |
| 14 | Shutdown | `POST /api/shutdown` → goodbye spoken → clean exit in 6.7 s; stop flag written; discovery file removed; both mutexes released |
| 15 | Restart | clean start 11.4 s; stop flag cleared; the HUD window left open reconnected by itself (token-rotation reload) |
| — | Duplicate backend/supervisor started by hand | supervisor refused in 0.4 s; `main.py` refused with exit 86 before creating an engine |

## 24. Performance

| Measure | Value |
|---|---|
| Launch → HUD window (cold) | 7 s (restart: 11.4 s to ready) |
| HUD WebSocket round trip | 1 ms |
| Core draw workload | ~5.1 ms/frame (1,344 sprite blits + 4 batched strokes, 1.5× DPR, software-rendered) |
| HUD frame rate | adaptive 60/30/20 fps; measured 30 fps at idle (headless Edge) |
| Lane routing (typed) | 477–718 ms |
| Time to first token (typed) | 1.89 s (avg 3.04 s over 2 turns) |
| Response duration | 6.7 s (avg 8.7 s) |
| Tool latency | weather_report 1.26 s, spotify_control 101 ms |
| HUD bundle | 429 KB single file (187 KB gzip), fonts embedded, works offline |

## 25. Screenshots

`docs/hud-screenshots/`: `01-idle`, `02-listening`, `03-transcribing`,
`04-thinking`, `05-tool-execution`, `06-tool-complete-context-sources`,
`07-speaking`, `08-error`, `09-standby`, `10-command-palette`, `11-offline`
(dev harness: the real server, bus and HUD driven by a stand-in engine so each
state can be held long enough to capture), and `real-ready`,
`real-diagnostics`, `12-real-1366x768` (the live backend).

## 26. Known limitations

* Voice conversation and barge-in were not exercised by a human in this pass.
* No calendar integration exists, so meeting preparation can only use memory,
  files, Gmail and the web; the HUD says so.
* TTS volume is the output device's volume (existing `audio_control`); there is
  no separate software gain for Era's voice.
* The logon task registered before this change still starts a console
  `python.exe` for a moment before the supervisor detaches; re-run
  `register_era_supervisor.ps1 -Action install` (admin) for a fully silent logon.
* `ruff format --check` flags `main.py` only for its UTF-8 BOM (pre-existing;
  formatting strips it — restore it if you reformat).
* Three tests in `test_lane_wiring.py` fail on a stale stub that predates the
  "abort mission" feature (pre-existing, unrelated to the HUD).
* Browsers throttle hidden windows; a minimised HUD catches up when shown.

## 27. Remaining migration work

* Hands-on voice verification (wake word, partial transcript, barge-in).
* After a period of parity, retire the Qt window path (`ui.py`, `ERAONE_UI=qt`)
  and the legacy `mic_volume`/`window_control` messages.
* Optional: system tray icon; HUD control for TTS device volume.
