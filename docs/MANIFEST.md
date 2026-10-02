# Era One — Codebase Manifest

> **GENERATED FILE — do not edit by hand.**
> `python tools/era_manifest.py` rewrites it; `--check` fails when it is stale.
> Every fact here is read out of the source tree, so it cannot drift from the code.

This is the map. Find the subsystem, jump to the file, skip the repo-wide scan.

**291 source modules** · 109,631 lines · 169 test modules · 53 declared tools · 57 registered actions

## 1. Where do I go?

| Subsystem | Go here when | Entry points |
|---|---|---|
| **Voice loop / live session** | hearing, speaking, reconnects, barge-in | `app/era_live/__init__.py`, `app/era_live/audio.py`, `app/era_live/audio_output.py`, `app/era_live/cascade.py` _(+11 more)_ |
| **Tool dispatch & declarations** | a tool is not called, called wrongly, or needs adding | `app/era_live/tool_dispatch.py`, `app/tool_declarations.py` |
| **HUD (browser UI)** | anything the user SEES | `app/hud/__init__.py`, `app/hud/api.py`, `app/hud/browser.py`, `app/hud/bus_approval.py` _(+14 more)_ |
| **Safety & confirmation** | gates, approvals, owner-only checks, abort | `core/confirmation.py`, `core/confirmation_preview.py`, `core/input_safety_gate.py`, `core/panic_stop.py` _(+3 more)_ |
| **Model routing & Claude** | which model answers, cost, quota failover, Claude CLI | `core/llm/__init__.py`, `core/llm/adapters/__init__.py`, `core/llm/adapters/claude_cli.py`, `core/llm/adapters/gemini_generate.py` _(+14 more)_ |
| **Memory** | recall, save_memory, long-term facts | `core/intent_memory.py`, `core/memory_intent.py`, `core/turn_memory.py`, `memory/__init__.py` _(+9 more)_ |
| **Actions (what Era can DO)** | desktop, browser, media, files, apps | `actions/_netflix/__init__.py`, `actions/_netflix/js_snippets.py`, `actions/_netflix/player_api.py`, `actions/_netflix/player_js.py` _(+54 more)_ |
| **Vision** | screen understanding | `core/vision_classifier.py`, `core/vision_config.py`, `core/vision_context.py`, `core/vision_ollama.py` _(+6 more)_ |
| **Audio devices & TTS** | which speaker, which voice, volume routing | `core/audio_battery.py`, `core/audio_devices.py`, `core/audio_endpoints.py`, `core/audio_pcm.py` _(+5 more)_ |
| **Community management (CM)** | brands, vault, ads, publishing | `core/cm_learning.py`, `core/cm_vault.py`, `core/daily_note.py`, `core/vault_audit.py` _(+2 more)_ |
| **Self-healing & diagnosis** | Era fixing or reporting on herself | `actions/self_diagnosis.py`, `actions/self_heal.py`, `actions/self_improve.py`, `actions/self_improve_targets.py` _(+6 more)_ |
| **Scheduling** | timed and recurring jobs | `actions/scheduled_task.py`, `core/task_scheduler.py`, `era_scheduler.py` |
| **Health & tests** | is the system OK, and does the suite pass | `conftest.py`, `tools/era_doctor.py`, `tools/run_all_checks.py` |

## 2. Capability manifest — what Era can actually do

The tools the live model is offered, and the file that implements each.
This table is Era's self-model: if a tool is not here, she cannot do it.

| Tool | Does | Required args | Implemented in |
|---|---|---|---|
| `open_app` | Opens an APPLICATION on the computer system. If the application is already running or matching open windows on | `app_name` | `actions/open_app.py` |
| `web_search` | Searches the web for any information. | `query` | `actions/web_search.py` |
| `capability_check` | What you can actually do, computed from your own live tool declarations. Call this INSTEAD OF answering from m | — | `app/era_live/tool_dispatch.py:2707` |
| `weather_report` | Gives the weather report to user | `city` | `actions/weather_report.py` |
| `send_message` | Sends a text message via WhatsApp, Telegram, or other messaging platform. | `receiver`, `message_text`, `platform` | `actions/send_message.py` |
| `reminder` | Sets a timed reminder using Task Scheduler. | `date`, `time`, `message` | `actions/reminder.py` |
| `scheduled_task` | Creates and manages the user's SCHEDULED TASKS: a reminder, one Era tool call, or a CHAIN of tool calls run in | `action` | `actions/scheduled_task.py` |
| `youtube_video` | Controls YouTube. Use for: playing videos, pausing/resuming playback, summarizing a video's content, getting v | — | `actions/youtube_video.py` |
| `screen_process` | Captures and analyzes the screen or webcam image. MUST be called when user asks what is on screen, what you se | — | `app/era_live/tool_dispatch.py:2219` |
| `computer_settings` | Controls the COMPUTER ITSELF: volume, brightness, window management, keyboard shortcuts, typing text on screen | — | `actions/computer_settings.py` |
| `audio_control` | Audio DEVICES: list every speaker/headphone/microphone with its live level, switch the default output or input | `action` | `actions/audio_control.py` |
| `claude_bridge` | Talk to the Claude Code desktop app (the GUI) and read its answers. Use when the user asks you to ask/tell Cla | `action` | `actions/claude_bridge.py` |
| `system_performance` | Controls laptop performance profiles and system optimization. Switches the MSI Center user scenario (extreme p | `action` | `actions/system_performance.py` |
| `spotify_control` | Controls Spotify playback directly. Use for: play, pause, next/previous track, search and play a song, like/un | — | `actions/spotify_control.py` |
| `browser_control` | Controls web browser features, active tabs, and monitor layouts. Supports listing all open tabs, cycling navig | `action` | `actions/browser_control.py` |
| `netflix_control` | Controls Netflix end to end: opening it, finding and playing a title, and every playback control once somethin | `action` | `actions/netflix_control.py` |
| `premiere_control` | Controls Adobe Premiere Pro timeline and video editing. Use this exclusively when the user asks to edit video, | `command` | `actions/premiere_control.py` |
| `file_controller` | Manages files and folders: list, create, delete, move, copy, rename, read, write, find, open, last_download, d | `action` | `actions/file_controller.py` |
| `desktop_control` | Controls the desktop: wallpaper, organize, clean, list, stats. | `action` | `app/era_live/tool_dispatch.py:2813` |
| `code_helper` | Writes, edits, explains, runs, or builds code files. | `action` | `actions/code_helper.py` |
| `dev_agent` | Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and f | `description` | `actions/dev_agent.py` |
| `agent_task` | Executes complex multi-step tasks requiring multiple different tools. Examples: 'research X and save to file', | `goal` | `app/era_live/tool_dispatch.py:2831` |
| `computer_control` | Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen. | `action` | `actions/computer_control.py` |
| `game_updater` | THE ONLY tool for ANY Steam or Epic Games request. Use for: installing, downloading, updating games, listing i | — | `actions/game_updater.py` |
| `flight_finder` | Searches Google Flights and speaks the best options. | `origin`, `destination`, `date` | `actions/flight_finder.py` |
| `enter_standby` | Mutes the microphone and puts era into standby mode. Call this ONLY on one of these four exact commands: 'stan | — | `app/era_live/tool_dispatch.py:2399` |
| `set_microphone` | Turns era's OWN microphone off or on (privacy mute). Call with state='off' when the user tells you to stop hea | `state` | `app/era_live/tool_dispatch.py:2409` |
| `set_wake_word` | Changes era's OWN wake word — the word that wakes her from standby. Call this ONLY when the user explicitly as | `wake_word` | `app/era_live/tool_dispatch.py:2412` |
| `voice_lock` | Turns era's voice recognition (the owner-only voice check) ON or OFF in real time. While ON, era only responds | — | `app/era_live/tool_dispatch.py:2439` |
| `set_voice` | Switches era's OWN speaking voice between her Fish Audio voice (her primary, cloned voice) and her Gemini voic | `voice` | `app/era_live/tool_dispatch.py:2442` |
| `manage_schedule` | View or change a brand's recurring community-management schedules: the weekly content plan and the auto-publis | `action` | `app/era_live/tool_dispatch.py:2445` |
| `manage_offer` | View or change a brand's standing promotions (discounts/offers) that the community-management agent may use in | `action` | `app/era_live/tool_dispatch.py:2448` |
| `community_manager` | Hand a social-media / content order to the community-management agent, which runs it in the BACKGROUND while y | `order` | `app/era_live/tool_dispatch.py:2451` |
| `shutdown_era` | Shuts down ERA HERSELF — the assistant program only. It does NOT touch the computer. ONLY call this when the u | — | `app/era_live/tool_dispatch.py:2919` |
| `confirm_pending_confirmation` | Confirm and execute the currently pending high-risk action after the user clearly says yes, confirm, proceed,  | — | `app/era_live/tool_dispatch.py:2633` |
| `cancel_pending_confirmation` | Cancel the currently pending high-risk action after the user says no, cancel, stop, don't do it, or never mind | — | `app/era_live/tool_dispatch.py:2688` |
| `media_coordinator` | Orchestrates media playing between Spotify, YouTube, and Netflix. Pauses conflicting sources when a new stream | `target`, `action` | `actions/media_coordinator.py` |
| `file_processor` | Processes any file that the user has uploaded or dropped onto the interface. Use this when the user refers to  | — | `actions/file_processor.py` |
| `save_memory` | Save an important personal fact about the user to long-term memory. YOU decide when to call it — the user does | `category`, `key`, `value` | `app/era_live/tool_dispatch.py:2520` |
| `retrieve_memory_context` | Retrieve memories relevant to the current conversation context. Use when you need to remember user-specific pr | `query` | `app/era_live/tool_dispatch.py:2602` |
| `list_memory_keys` | List all keys stored in long-term memory, grouped by category. Optionally filter by a specific category. Suppo | — | `app/era_live/tool_dispatch.py:2613` |
| `forget_memory` | Delete one specific key from long-term memory by exact category and key name. BOTH category and key are requir | `category`, `key` | `app/era_live/tool_dispatch.py:2624` |
| `window_control` | Advanced window management. Use to list all open windows, focus a specific window by title, move/resize, minim | `action` | `actions/window_control.py` |
| `agent_browser` | Advanced browser automation for complex multi-step web tasks. Use for web research, form filling, data extract | `query` | `app/era_live/tool_dispatch.py:2218` |
| `record_feedback` | Record user feedback, corrections, preferences, or future behavior instructions to feedback_memory.jsonl. Use  | `feedback_text` | `app/era_live/tool_dispatch.py:2933` |
| `vault_note` | Write something into Era's Obsidian vault — her SECOND BRAIN and the only source of truth for community manage | `content` | `app/era_live/tool_dispatch.py:2220` |
| `manage_preference` | Persist or revoke a STANDING user preference that must survive restarts. Use when the user grants or cancels a | `action` | `app/era_live/tool_dispatch.py:2981` |
| `self_diagnosis` | Runs a deep diagnosis of Era's OWN code and health: syntax sweep of her source files, the era_doctor healthche | — | `actions/self_diagnosis.py` |
| `self_heal` | Era's autonomous self-healing loop. It runs by itself in the background: it finds faults in her log that Pytho | — | `actions/self_heal.py` |
| `self_improve` | Fixes or improves Era's OWN source code, cautiously: every change is backed up, syntax/lint/test verified, and | — | `actions/self_improve.py` |
| `protocol_one` | Protocol One, Era's project health guardian. action=launch runs a full maintenance cycle in the background: de | `action` | `actions/protocol_one.py` |
| `protocol_zero` | Protocol Zero, Era's prompt-synchronisation auditor. action=execute compares every tool Era has against her sy | `action` | `actions/protocol_zero.py` |
| `gmail_control` | The user's Gmail. Use for ANY request about their email: checking the inbox, finding or reading a message, wor | `action` | `actions/gmail_control.py` |

## 3. Highest-traffic modules

Most-imported first. A change here has the widest blast radius.

| Module | Imported by | Purpose |
|---|---|---|
| `core.utils` | 79 |  |
| `actions.netflix_control` | 25 |  |
| `core.logger` | 25 |  |
| `core.tool_result` | 25 |  |
| `core.user_prefs` | 25 | Persistent user-granted preference exceptions and behavior rules. |
| `core.safety_guard` | 24 |  |
| `core.llm.types` | 23 | Provider-neutral request/response types for Era's LLM layer. |
| `core.llm.registry` | 20 | Alias registry — the one place that maps Era's internal model aliases to a provider and a  |
| `actions.browser_control` | 19 | Era One Browser Controller — Playwright-based, tab-aware, Brave-first. |
| `core.ai_provider` | 19 | Unified Gemini provider for Era One. |
| `app.era_live.tool_dispatch` | 18 | Tool dispatch + tool result handling + session config for EraLive (from main.py). |
| `app.text_intents` | 17 | Typed/spoken text-intent parsing + path helpers (extracted from main.py). |
| `core.llm` | 17 | Provider-neutral LLM layer for Era One. |
| `core.self_healing_router` | 17 | Self-healing tool router for Era One. |
| `era_dispatch` | 16 | era_dispatch.py — Era One → Claude Code dispatcher. |
| `main` | 15 |  |
| `app.hud` | 14 | Localhost HUD layer: event bus, HTTP/WebSocket server glue, launcher helpers. |
| `core.intent_memory` | 14 | Short-term intent memory for Era One. |
| `core.vision_types` | 14 |  |
| `core.browser_grid_state` | 13 | In-memory short-term grid snapshot + scroll-context state. |
| `actions.self_improve` | 12 | actions/self_improve.py — Era cautiously fixes and improves her OWN code. |
| `app.hud.events` | 12 | The HUD's single, truthful event stream and agent state machine. |
| `core.action_registry` | 12 | Unified action registry for Era One routing. |
| `core.tts_fish` | 12 | Fish Audio TTS client Ã¢ÂÂ Era's PRIMARY speech synthesis backend. |
| `actions.screen_processor` | 11 |  |

## 4. Module index

Every source module, its size, and what it is for.

### Voice loop / live session

| File | Lines | Purpose |
|---|---:|---|
| `app/era_live/__init__.py` | 2 | EraLive behavioral mixins (extracted from main.py). |
| `app/era_live/audio.py` | 1311 | Audio I/O loops for EraLive (extracted from main.py). |
| `app/era_live/audio_output.py` | 292 | Self-healing speaker output for Era's Live voice. |
| `app/era_live/cascade.py` | 183 | Cascade voice mode — Era WITHOUT the Gemini Live session. |
| `app/era_live/cm_flow.py` | 575 | Community-management flow mixin for EraLive (extracted from main.py). |
| `app/era_live/dispatch.py` | 360 | Dispatch — being ANSWERED by a model other than the live voice session. |
| `app/era_live/local_fallback.py` | 520 | Local fallback voice loop — Era keeps hearing and acting while Gemini is down. |
| `app/era_live/local_guards.py` | 70 | Security gates for the local fallback loop (app/era_live/local_fallback.py). |
| `app/era_live/mic_mute.py` | 274 | Voice-controlled microphone mute for EraLive (see core/mic_mute.py for the model). |
| `app/era_live/model_route.py` | 322 | Era's session-scoped model preference, and the routing decision it feeds. |
| `app/era_live/panic.py` | 150 | Runtime half of "abort mission" for EraLive (model in core/panic_stop.py). |
| `app/era_live/reconnect_mixin.py` | 779 | Reconnect / standby / status-reporting mixin for EraLive (extracted from main.py). |
| `app/era_live/speech_out.py` | 336 | Speech output — turns the model's text into audio, sentence by sentence. |
| `app/era_live/tool_batch.py` | 281 | One-response-per-call execution of a model's tool-call batch. |
| `app/era_live/tool_dispatch.py` | 3346 | Tool dispatch + tool result handling + session config for EraLive (from main.py). |

### Tool dispatch & declarations

| File | Lines | Purpose |
|---|---:|---|
| `app/era_live/tool_dispatch.py` | 3346 | Tool dispatch + tool result handling + session config for EraLive (from main.py). |
| `app/tool_declarations.py` | 2138 | Gemini Live tool declarations for Era. |

### HUD (browser UI)

| File | Lines | Purpose |
|---|---:|---|
| `app/hud/__init__.py` | 6 | Localhost HUD layer: event bus, HTTP/WebSocket server glue, launcher helpers. |
| `app/hud/api.py` | 351 | REST endpoints for the localhost HUD. |
| `app/hud/browser.py` | 216 | Open (or re-focus) the HUD as a Chromium app window. |
| `app/hud/bus_approval.py` | 87 | Approval half of the HUD bus: mirrors Era's single pending confirmation. |
| `app/hud/bus_const.py` | 95 | Constants and pure helpers shared by the HUD event bus modules. |
| `app/hud/bus_legacy.py` | 170 | Legacy-UI observation and the read side of the HUD bus. |
| `app/hud/bus_tools.py` | 168 | Tool-execution half of the HUD bus: real tool calls in, activity rows out. |
| `app/hud/events.py` | 500 | The HUD's single, truthful event stream and agent state machine. |
| `app/hud/instance.py` | 214 | Single-instance guards and the backend's discovery file. |
| `app/hud/launcher.py` | 353 | ERA ONE launcher — the one thing the desktop shortcut runs. |
| `app/hud/launcher_ui.py` | 216 | Launcher splash and startup-error dialog (tkinter: stdlib, no console). |
| `app/hud/schedule_view.py` | 96 | Read-only view of the user's scheduled tasks for the HUD (GET /api/schedule). |
| `app/hud/settings.py` | 81 | HUD preferences, persisted in runtime/hud_settings.json. |
| `app/hud/shortcuts.py` | 203 | Windows shortcuts for the launcher, and the opt-in "start with Windows" switch. |
| `app/hud/status.py` | 317 | Real status for the HUD: health, providers, integrations, diagnostics. |
| `app/hud/tool_adapter.py` | 416 | Turn a real tool call into what the HUD may show about it. |
| `app/hud/ui_base.py` | 217 | The UI API the engine talks to, without Qt. |
| `ui.py` | 513 | ui.py — Transparent QWebEngineView wrapper for ERA ONE. |

### Safety & confirmation

| File | Lines | Purpose |
|---|---:|---|
| `core/confirmation.py` | 39 |  |
| `core/confirmation_preview.py` | 95 | core/confirmation_preview.py — resolve what a risky call will actually DO, before the user is asked to approve |
| `core/input_safety_gate.py` | 720 |  |
| `core/panic_stop.py` | 239 | core/panic_stop.py — "abort mission": the order that always wins. |
| `core/pending_confirmation.py` | 193 |  |
| `core/safety_guard.py` | 1083 |  |
| `core/voice_gate.py` | 487 | Voice gate — only the enrolled owner's voice reaches Era. |

### Model routing & Claude

| File | Lines | Purpose |
|---|---:|---|
| `core/llm/__init__.py` | 8 | Provider-neutral LLM layer for Era One. |
| `core/llm/adapters/__init__.py` | 15 | Provider adapters. |
| `core/llm/adapters/claude_cli.py` | 491 | Claude as a reasoning oracle, over the subscription `claude -p` CLI. |
| `core/llm/adapters/gemini_generate.py` | 257 | Gemini generate/JSON as an LLMProvider — a wrapper AROUND existing behaviour. |
| `core/llm/adapters/ollama_chat.py` | 315 | Local LLM over Ollama's /api/chat — the brain that needs no cloud. |
| `core/llm/adapters/openai_compat.py` | 479 | OpenAI-compatible chat completions — the cloud brain of the cascade mode. |
| `core/llm/claude_accounts.py` | 256 | Which Claude subscription account runs a call, and what to do when it can't. |
| `core/llm/codecraft_failover.py` | 77 | CodeCraft as a text step in the Gemini quota failover (core/llm/quota_failover.py). |
| `core/llm/exec_ledger.py` | 327 | Idempotency ledger for model-requested tool execution. |
| `core/llm/provider.py` | 99 | The provider interfaces every adapter implements. |
| `core/llm/quota_failover.py` | 526 | Gemini quota failover: exhausted key -> spare Gemini key -> CodeCraft -> Claude -> local. |
| `core/llm/registry.py` | 494 | Alias registry — the one place that maps Era's internal model aliases to a provider and a concrete model id. |
| `core/llm/router.py` | 433 | Which model serves this request, and why. |
| `core/llm/shadow.py` | 260 | Shadow mode — what automatic routing WOULD have done, recorded but not acted on. |
| `core/llm/spend.py` | 267 | Daily spend ledger for metered model calls. |
| `core/llm/taskclass.py` | 159 | Deterministic task classification — which `routing.by_task` class is this? |
| `core/llm/toolspec.py` | 375 | Canonical tool schema for Era, plus one emitter per provider dialect. |
| `core/llm/types.py` | 319 | Provider-neutral request/response types for Era's LLM layer. |

### Memory

| File | Lines | Purpose |
|---|---:|---|
| `core/intent_memory.py` | 128 | Short-term intent memory for Era One. |
| `core/memory_intent.py` | 224 | Memory-save authorization + speech-grounding helpers (extracted from main.py). |
| `core/turn_memory.py` | 119 | Interrupted-turn memory for contextual resumption (Phase 2 barge-in). |
| `memory/__init__.py` | 6 | Memory package for MARK-XXXIX. |
| `memory/config_manager.py` | 57 |  |
| `memory/feedback_memory.py` | 162 |  |
| `memory/injector.py` | 45 |  |
| `memory/memory_audit.py` | 107 | Memory audit log — records every memory mutation to logs/memory_audit.jsonl. |
| `memory/memory_manager.py` | 1176 |  |
| `memory/persistence.py` | 59 |  |
| `memory/safe_store.py` | 264 |  |
| `memory/semantic_recall.py` | 304 | memory/semantic_recall.py — recall by MEANING, on top of the lexical scorer. |
| `memory/transient_filter.py` | 49 | Shared transient-key filter for long-term memory. |

### Actions (what Era can DO)

| File | Lines | Purpose |
|---|---:|---|
| `actions/_netflix/__init__.py` | 2 | Internal implementation package for actions/netflix_control.py. |
| `actions/_netflix/js_snippets.py` | 539 | JavaScript / data snippets extracted verbatim from netflix_control.py. |
| `actions/_netflix/player_api.py` | 511 | Netflix player control, driven through Netflix's own player API. |
| `actions/_netflix/player_js.py` | 211 | Netflix profile-gate and player JavaScript, built at call time. |
| `actions/_netflix/profile_gate.py` | 259 | Get past Netflix's profile picker onto the configured profile, reliably. |
| `actions/agent_browser_use.py` | 99 | Agent-browser CLI integration for Era One. |
| `actions/audio_control.py` | 285 | audio_control - list / switch audio devices and control any ONE device. |
| `actions/browser_control.py` | 2597 | Era One Browser Controller — Playwright-based, tab-aware, Brave-first. |
| `actions/browser_driver_health.py` | 118 | Liveness checks for the Playwright driver behind a _BrowserSession. |
| `actions/browser_intelligence.py` | 1496 | Browser Intelligent Automation for YouTube and Netflix grid/card selection. |
| `actions/claude_bridge.py` | 164 | claude_bridge tool: Era talks to the Claude Code desktop app and reads its answers. |
| `actions/claude_self_improve.py` | 148 | Claude-Code-as-generator for self_improve. |
| `actions/code_helper.py` | 732 |  |
| `actions/computer_control.py` | 693 |  |
| `actions/computer_settings.py` | 1943 |  |
| `actions/design_studio.py` | 288 | actions/design_studio.py — natural language -> professional prompt -> image / video. |
| `actions/desktop.py` | 459 |  |
| `actions/desktop_ops.py` | 487 | actions/desktop_ops.py — the vetted operation set behind `desktop_control action=task`. |
| `actions/desktop_preview.py` | 263 | actions/desktop_preview.py — planning a desktop task, and describing the plan. |
| `actions/dev_agent.py` | 680 |  |
| `actions/feature_inspire.py` | 47 | FeatureInspire — web-search similar AI-agent projects and propose improvements. Uses existing Gemini grounded  |
| `actions/file_controller.py` | 1964 |  |
| `actions/file_indexer.py` | 846 | Persistent file system index for Era One instant file finder. |
| `actions/file_processor.py` | 1076 | file_processor.py — Era One Universal File Processor |
| `actions/flight_finder.py` | 421 |  |
| `actions/game_updater.py` | 1153 |  |
| `actions/gmail_control.py` | 549 | Era One's Gmail tool — the single entry point Gemini routes mail work through. |
| `actions/legal_automation.py` | 32 | Legal automation scaffold — EXPLICIT DEFINITION ONLY — NOT autonomous legal practice. Service defined: AUTO-ND |
| `actions/media_coordinator.py` | 196 |  |
| `actions/monitor_move.py` | 439 | Move a window to another connected monitor ("move this window to screen 2"). |
| `actions/netflix_control.py` | 5048 |  |
| `actions/open_app.py` | 444 | High-performance Windows Application Launcher for Era One. Features: - Ultra-fast Start Menu Shortcut (.lnk) i |
| `actions/page_refs.py` | 310 | Text-first page reading for browser_control: `page_snapshot` + `click_ref`. |
| `actions/path_anchors.py` | 142 | Resolve bare folder/file names against paths Era already knows. |
| `actions/platform_tab.py` | 176 | Strict platform tab ownership for YouTube and Netflix. |
| `actions/premiere_control.py` | 195 | actions/premiere_control.py — voice/text bridge to the Adobe Premiere Pro MCP server. |
| `actions/protocol_one.py` | 674 | actions/protocol_one.py — Protocol One, Era's project health guardian. |
| `actions/protocol_zero.py` | 427 | actions/protocol_zero.py — Protocol Zero, the prompt-synchronisation auditor. |
| `actions/reminder.py` | 325 |  |
| `actions/scheduled_task.py` | 425 | actions/scheduled_task.py — the `scheduled_task` tool: create, list, modify, cancel, approve and waive the use |
| `actions/screen_processor.py` | 933 |  |
| `actions/self_diagnosis.py` | 633 | actions/self_diagnosis.py — Era deep-diagnoses her own code and proposes fixes. |
| `actions/self_heal.py` | 52 | actions/self_heal.py — the `self_heal` tool: Era's view of her own healing loop. |
| `actions/self_improve.py` | 665 | actions/self_improve.py — Era cautiously fixes and improves her OWN code. |
| `actions/self_improve_targets.py` | 136 | actions/self_improve_targets.py — feature → file resolution for self_improve. |
| `actions/self_improve_verifier.py` | 445 | actions/self_improve_verifier.py — independent second pass after a self_improve apply. |
| `actions/send_message.py` | 409 |  |
| `actions/spotify_control.py` | 1306 |  |
| `actions/spotify_exact.py` | 243 | Exact Spotify control that window-automation cannot do, using APIs already in this repo — no new dependencies, |
| `actions/spotify_gsmtc_host.py` | 236 | Persistent PowerShell host for Windows GSMTC (media transport) reads. |
| `actions/spotify_ui.py` | 262 | Resize-proof Spotify content playback via UI Automation (accessibility tree). |
| `actions/system_performance.py` | 1090 | MSI Center user-scenario switching and Microsoft PC Manager "PC Boost". |
| `actions/text_edit.py` | 226 | text_edit.py — precise, verified edits to plain-text files for file_processor. |
| `actions/vault_writer.py` | 424 | vault_writer.py — how Era writes into her own Obsidian vault, live, in conversation. |
| `actions/weather_report.py` | 299 |  |
| `actions/web_search.py` | 208 |  |
| `actions/window_control.py` | 678 | Advanced window management for Era One. Features an aggressive Windows 11 foreground stealer and class-based m |
| `actions/youtube_video.py` | 1563 | Era One YouTube Controller — tab-aware, browser-integrated. |

### Vision

| File | Lines | Purpose |
|---|---:|---|
| `core/vision_classifier.py` | 341 |  |
| `core/vision_config.py` | 136 |  |
| `core/vision_context.py` | 114 |  |
| `core/vision_ollama.py` | 86 |  |
| `core/vision_openai.py` | 60 |  |
| `core/vision_policy.py` | 138 |  |
| `core/vision_resilience.py` | 138 | Failure-loop guards for the screen/camera vision path. |
| `core/vision_service.py` | 298 |  |
| `core/vision_types.py` | 85 |  |
| `core/vision_worker.py` | 268 |  |

### Audio devices & TTS

| File | Lines | Purpose |
|---|---:|---|
| `core/audio_battery.py` | 239 | Battery telemetry for connected audio devices (Windows SetupAPI). |
| `core/audio_devices.py` | 149 | Dynamic audio-device detection for Era — Star Glass routing. |
| `core/audio_endpoints.py` | 657 | Per-endpoint audio control for Era (Windows Core Audio via pycaw). |
| `core/audio_pcm.py` | 160 | PCM/WAV helpers for Era's audio path. |
| `core/audio_policy.py` | 417 |  |
| `core/local_tts.py` | 143 | Local voice: Piper (French + English) via sherpa-onnx, fully offline. |
| `core/tts_fish.py` | 315 | Fish Audio TTS client Ã¢ÂÂ Era's PRIMARY speech synthesis backend. |
| `core/tts_router.py` | 64 | Speech-synthesis router — Fish Audio primary, Gemini fallback. |
| `core/voice_output.py` | 573 | Where Era's SPOKEN VOICE plays - the single source of truth. |

### Community management (CM)

| File | Lines | Purpose |
|---|---:|---|
| `core/cm_learning.py` | 390 | cm_learning.py — the community-management lane's memory of your feedback. |
| `core/cm_vault.py` | 724 | cm_vault.py — read/write bridge between Era's community-management lane and the Obsidian vault at obsidian_era |
| `core/daily_note.py` | 276 | daily_note.py — Era's entry in the shared daily log. |
| `core/vault_audit.py` | 382 | vault_audit.py — what is WRONG with the era_brain vault. Read-only, always. |
| `core/vault_maintenance.py` | 440 | vault_maintenance.py — the routine that ACTS on what vault_audit.py found. |
| `era_dispatch.py` | 2450 | era_dispatch.py — Era One → Claude Code dispatcher. |

### Self-healing & diagnosis

| File | Lines | Purpose |
|---|---:|---|
| `actions/self_diagnosis.py` | 633 | actions/self_diagnosis.py — Era deep-diagnoses her own code and proposes fixes. |
| `actions/self_heal.py` | 52 | actions/self_heal.py — the `self_heal` tool: Era's view of her own healing loop. |
| `actions/self_improve.py` | 665 | actions/self_improve.py — Era cautiously fixes and improves her OWN code. |
| `actions/self_improve_targets.py` | 136 | actions/self_improve_targets.py — feature → file resolution for self_improve. |
| `actions/self_improve_verifier.py` | 445 | actions/self_improve_verifier.py — independent second pass after a self_improve apply. |
| `core/handled_exceptions.py` | 266 | core/handled_exceptions.py — see exceptions Era's tools CATCH, not just the ones that escape. |
| `core/self_heal.py` | 499 | core/self_heal.py — Era repairs her own code defects, unattended, with rollback. |
| `core/self_heal_incidents.py` | 227 | core/self_heal_incidents.py — turn era.log into fixable incidents. |
| `core/self_heal_patch.py` | 298 | core/self_heal_patch.py — a small, scoped, checkable patch for one incident. |
| `core/self_healing_router.py` | 2588 | Self-healing tool router for Era One. |

### Scheduling

| File | Lines | Purpose |
|---|---:|---|
| `actions/scheduled_task.py` | 425 | actions/scheduled_task.py — the `scheduled_task` tool: create, list, modify, cancel, approve and waive the use |
| `core/task_scheduler.py` | 920 | core/task_scheduler.py — the user's own scheduled tasks ("run X every monday at 9"). |
| `era_scheduler.py` | 214 | era_scheduler.py — runs each brand's recurring jobs (weekly plan, auto-publish) at the times set in brands.yam |

### Health & tests

| File | Lines | Purpose |
|---|---:|---|
| `conftest.py` | 269 | Pytest bootstrap. |
| `tools/era_doctor.py` | 670 | Era One Doctor — healthcheck tool. Read-only, no side effects. |
| `tools/run_all_checks.py` | 256 | One-command validation runner for Era One. |

