"""Tool dispatch + tool result handling + session config for EraLive (from main.py).

The tool router (_execute_tool_raw), result verification/confirmation, live-session
config builder, and memory-save authorization. Operates on self.* state.
"""

import asyncio
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from google.genai import types

from actions.agent_browser_use import agent_browser
from actions.browser_control import browser_control
from actions.code_helper import code_helper
from actions.computer_control import computer_control
from actions.computer_settings import computer_settings
from actions.design_studio import design_studio
from actions.desktop import desktop_control
from actions.dev_agent import dev_agent
from actions.file_controller import file_controller
from actions.file_processor import file_processor
from actions.flight_finder import flight_finder
from actions.game_updater import game_updater
from actions.media_coordinator import media_coordinator
from actions.netflix_control import netflix_control
from actions.open_app import open_app
from actions.premiere_control import execute_premiere_voice_command
from actions.reminder import reminder
from actions.screen_processor import screen_process
from actions.self_diagnosis import run_self_diagnosis
from actions.self_improve import self_improve
from actions.send_message import send_message
from actions.system_performance import system_performance
from actions.weather_report import weather_action
from actions.web_search import web_search as web_search_action
from actions.window_control import window_control
from actions.youtube_video import youtube_video
from app.cm_intents import (  # noqa: F401  (re-export: preserves public + internal names)
    _ADS_INTENT,
    _APPROVE_INTENT,
    _CAMPAIGN_INTENT,
    _PUBLISH_INTENT,
    _REVISE_INTENT,
    _RUSH_INTENT,
    _infer_cm_task_type,
)
from app.era_constants import (  # noqa: F401
    _CM_HANDOFF_WINDOW_S,
    _PENDING_ASK_ALLOWED_TOOLS,
    _RESUME_ACK,
)
from app.system_prompt import (  # noqa: F401  (re-export: preserves public + internal names)
    _load_system_prompt,
)
from app.tool_declarations import TOOL_DECLARATIONS  # noqa: F401  (re-export)
from app.wake import (  # noqa: F401  (re-export: preserves public + internal names)
    _DEFAULT_WAKE_DECOYS,
    VOSK_MODEL_PATH,
    WAKE_CFG,
    WAKE_WORD,
    WAKE_WORD_CONFIG_PATH,
    WakeWordConfig,
    init_vosk,
)
from core.confirmation import ConfirmationManager
from core.era_state import EraState
from core.logger import log_crash, log_event, main_logger
from core.main_tool_bridge import build_function_response, fc_args_to_dict
from core.memory_intent import (  # noqa: F401  (re-export: preserves public + internal names)
    _AUTO_SAVE_CATEGORIES,
    _AUTO_SAVE_MAX_PER_WINDOW,
    _AUTO_SAVE_WINDOW_S,
    _ERA_OWN_ECHO_MARKERS,
    _GROUNDING_STOPWORDS,
    _MEMORY_AFFIRMATION_RE,
    _MEMORY_CONFIRM_WINDOW_S,
    _MEMORY_REQUEST_WINDOW_S,
    _MEMORY_TRIGGER_RE,
    _MEMORY_TRIGGERS,
    _grounding_tokens,
    _is_era_own_echo,
    is_explicit_memory_request,
    is_memory_affirmation,
    is_value_grounded_in_user_speech,
)
from core.process_awareness import format_running_apps
from core.safety_guard import classify_action
from core.self_healing_router import record_tool_result, route_tool_call
from core.tool_result import ToolResult
from core.tts_voicebox import VOICEBOX
from core.verification import verify_tool_result
from core.voice_mode import (
    LOCAL_VOICE_MODES,
    MODE_KOKORO_AUDIO,
    MODE_KOKORO_TEXT,
    VOICE_CFG,
)
from core.window_awareness import format_context as format_window_context
from memory.memory_audit import audit_event
from memory.memory_manager import (
    format_memory_for_prompt_ranked,
    load_memory,
    update_memory,
)
from memory.transient_filter import is_transient_memory_key


class ToolDispatchMixin:
    def _spawn_background_task(self, coro, label: str) -> "asyncio.Task":
        """Start a fire-and-forget coroutine WITHOUT letting it dangle.

        Two problems with a bare ``asyncio.create_task(...)`` whose result is
        thrown away, both of which applied to every long-running tool below:

        1. The event loop keeps only a WEAK reference to a task, so an
           unreferenced one can be garbage-collected mid-execution and simply
           stop running. For self_improve — the sanctioned self-edit path —
           vanishing halfway through is not an acceptable failure mode.
        2. An exception in a task nobody awaits surfaces only as "Task exception
           was never retrieved" at GC time, on stderr, absent from era.log.

        Holding a strong reference until completion fixes (1); the done-callback
        fixes (2). The set is created lazily because this is a mixin and
        EraLive.__init__ does not know about it.
        """
        tasks = getattr(self, "_background_tasks", None)
        if tasks is None:
            tasks = set()
            self._background_tasks = tasks

        task = asyncio.create_task(coro, name=label)
        tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                print(f"[era] [BG] background task '{label}' failed: {type(exc).__name__}: {exc}")
                try:
                    log_crash(
                        main_logger,
                        f"background:{label}",
                        type(exc),
                        exc,
                        exc.__traceback__,
                        event="engine.background.failed",
                        level="ERROR",
                    )
                except Exception:
                    pass

        task.add_done_callback(_done)
        return task

    def _build_config(self) -> types.LiveConnectConfig:

        memory = load_memory()
        mem_str = format_memory_for_prompt_ranked(memory, top_k=15)
        sys_prompt = _load_system_prompt()

        now = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y â€” %I:%M %p")

        # â”€â”€ UPGRADED REAL-TIME OS VISION LAYER (ANTI-POLLUTION FILTER) â”€â”€â”€â”€â”€â”€â”€
        foreground_window = "Desktop"
        running_applications = []
        spotify_active = False
        youtube_active = False

        try:
            import ctypes

            import win32gui

            def is_window_really_visible(hwnd):
                if not win32gui.IsWindowVisible(hwnd):
                    return False
                rect = win32gui.GetWindowRect(hwnd)
                if (rect[2] - rect[0]) <= 0 or (rect[3] - rect[1]) <= 0:
                    return False
                # Filter out cloaked/suspended background instances (UWP/stale tabs)
                cloaked = ctypes.c_int(0)
                try:
                    ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                    )
                    if cloaked.value != 0:
                        return False
                except Exception:
                    pass
                return True

            def _enumerate_windows(hwnd, extra_list):
                if is_window_really_visible(hwnd):
                    title = win32gui.GetWindowText(hwnd).strip()
                    class_name = win32gui.GetClassName(hwnd)

                    if title:
                        title_lower = title.lower()
                        # Ignore self-referential system engines
                        if any(
                            kw in title_lower
                            for kw in ["mark-xxxix", "j.a.r.v.i.s", "main.py", "prompt.txt"]
                        ):
                            return True
                        extra_list.append((title, class_name))
                return True

            visible_windows = []
            win32gui.EnumWindows(_enumerate_windows, visible_windows)

            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd:
                foreground_window = win32gui.GetWindowText(fg_hwnd).strip()

            for title, class_name in visible_windows:
                title_lower = title.lower()

                # Crucial Fix: Exclude active text streams inside your IDE or code files
                is_code_workspace = any(
                    ide in title_lower
                    for ide in [
                        "visual studio",
                        "vscode",
                        "code -",
                        ".py",
                        ".txt",
                        ".ahk",
                        "github",
                        "terminal",
                        "cmd.exe",
                    ]
                )

                # Spotify detection: match by class OR by title containing "spotify"
                # The window title changes dynamically: "Song - Artist" when playing,
                # "Spotify" or "Spotify Premium" when paused/idle.
                if class_name == "SpotifyMainWindow" or (
                    not is_code_workspace and "spotify" in title_lower
                ):
                    spotify_active = True
                    # Extract current track info from window title if available
                    # Format when playing: "Song Name - Artist - Spotify" or "Song Name - Artist"
                    if " - " in title_lower:
                        # Remove " - spotify" suffix if present
                        track_part = (
                            title[: title_lower.rindex(" - spotify")].strip()
                            if " - spotify" in title_lower
                            else title.strip()
                        )
                        running_applications.append(f"- {track_part} [Spotify â€” Playing]")
                    else:
                        running_applications.append("- Spotify [Paused/Idle]")
                elif "youtube" in title_lower and not is_code_workspace:
                    youtube_active = True
                    running_applications.append(f"- {title} [Target: Browser Media Tab]")
                else:
                    if not is_code_workspace:
                        running_applications.append(f"- {title}")

            # Final fallback: check for Spotify process via psutil if no window was found
            if not spotify_active:
                try:
                    import psutil

                    spotify_procs = [
                        p
                        for p in psutil.process_iter(["name"])
                        if p.info["name"] and "spotify" in p.info["name"].lower()
                    ]
                    if spotify_procs:
                        spotify_active = True
                        running_applications.append(
                            "- Spotify [Running â€” window hidden/minimized]"
                        )
                except Exception:
                    pass

        except Exception:
            # Isolated fallback path
            pass
        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        os_vision_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n\n"
            f"[REAL-TIME SYSTEM EYE (OS AWARENESS)]\n"
            f'User Foreground Focus: "{foreground_window}"\n'
            f"All Open Applications & Active Web Tabs:\n"
            f"{chr(10).join(running_applications) if running_applications else '- None'}\n\n"
            f"[LIVE MEDIA TELEMETRY STATES]\n"
            f"- Spotify Application Open/Running: {spotify_active}\n"
            f"- YouTube Browser Content Present: {youtube_active}\n"
            f"- Note: Spotify window title reflects current playback state â€” when a track is playing, the window shows 'Song - Artist â€” Spotify'. When paused/idle, it shows just 'Spotify'.\n\n"
            f"CRITICAL CONTEXTUAL ROUTING RULES:\n"
            f"1. Spotify is your absolute default media application. When the user issues general playback or music controls ('play', 'pause', 'poz', 'resume', 'stop', 'music') WITHOUT naming a specific platform, you MUST always choose Spotify (computer_settings) and trigger 'spotify_play' or 'spotify_pause'.\n"
            f"2. NEVER auto-route generic commands to YouTube and NEVER ask 'Spotify or YouTube, sir?' regardless of the window telemetry states. Spotify is the strict baseline fallback.\n"
            f"3. ONLY route commands to youtube_video if the user explicitly specifies 'youtube', 'the video', 'clip', or 'watch'.\n"
            f"4. For track navigation commands ('next', 'skip', 'previous', 'back', 'next song', 'next track'):\n"
            f"   - You MUST call computer_settings with action='spotify_next' or action='spotify_previous'.\n"
            f"5. If the user says 'close this app' or 'minimize this', extract \"{foreground_window}\" as the target parameter for window_control.\n"
            f"6. NEVER use screen_process (vision) to verify media state. Vision costs 12+ seconds and is unnecessary:\n"
            f"   - To check if Spotify is playing, a playlist/song actually started, or the window state: call spotify_control with action='status'. It reads the real Windows audio session instantly.\n"
            f"   - spotify_control already verifies playback internally before it reports success, so trust its returned message. If it says playback did not start, report that honestly instead of screenshotting.\n"
            f"7. Window operations on Spotify ('minimize spotify', 'maximize spotify', 'focus spotify') target the DESKTOP APP window. Use spotify_control action='minimize' for minimize, or window_control with title='Spotify' for other window actions. NEVER open the Spotify web player (open.spotify.com) for these commands — the desktop app is already running.\n"
            f"8. Trust window_control's returned message: it verifies the real window state after acting. Do not follow a successful window action with another tool call to 'check' it.\n\n"
        )
        try:
            awareness_ctx = (
                "\n[PHASE-1 AWARENESS LAYER]\n"
                + format_window_context()
                + "\n\nRunning process snapshot:\n"
                + format_running_apps(limit=35)
                + "\n"
            )
            os_vision_ctx += awareness_ctx
        except Exception as _awareness_error:
            os_vision_ctx += f"\n[PHASE-1 AWARENESS LAYER]\nUnavailable: {_awareness_error}\n"

        # ── Anti-hallucination system instruction (second safety layer) ──
        ANTI_HALLUCINATION_INSTR = (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "CRITICAL ANTI-HALLUCINATION ENFORCEMENT\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "You are era, a precise voice automation assistant.\n\n"
            "This is a safety layer. Apply a THREE-TIER response model:\n\n"
            "TIER 1 - ORGANIC NOISE (ABSOLUTE SILENCE)\n"
            "For these inputs, say NOTHING. Do not ask clarification. Do not respond.\n"
            "- filler words: 'hmm', 'uh', 'um', 'ah', 'oh', 'er', 'mm', 'huh'\n"
            "- fragments: 'vá', 'va', 'ha', 'he', 'eh', 'bah'\n"
            "- breathing, coughing, throat clearing\n"
            "- background noise, random syllables\n"
            "- accidental speech fragments\n"
            "- stale audio from reconnect\n\n"
            "TIER 2 - REAL WORD, VAGUE INTENT (SPECIFIC CLARIFICATION)\n"
            "If the user says a real word with ambiguous intent, ask a specific question.\n"
            "Examples:\n"
            "- 'volume' → 'Volume up or down?'\n"
            "- 'go' → 'Where should I go?'\n"
            "- 'read' → 'What should I read?'\n"
            "Do NOT execute any tool until the user clarifies.\n\n"
            "TIER 3 - UNCLEAR SPEECH (GENERIC CLARIFICATION)\n"
            "If the speech is clearly an attempt but unrecognizable, say:\n"
            '"Sorry, I did not catch that. Can you repeat?"\n\n'
            "TIER 3B - HEARD SOMETHING, DIDN'T UNDERSTAND THE COMMAND (MANDATORY CLARIFICATION)\n"
            "This is different from TIER 3: here the transcript is made of real, recognizable "
            "words, but you cannot tell what the user actually wants done with them (garbled "
            "phrasing, a word that sounds like a command but doesn't fit any known action, a "
            "sentence that trails off, or ASR mangling that leaves the intent ambiguous). "
            "DO NOT guess and DO NOT execute any tool in this case, even if one of the words "
            "resembles a known command or app/platform name. Ask for clarification instead, e.g.:\n"
            '"Sorry, I heard something but did not understand — what would you like me to do?"\n'
            "Never silently pick the closest-sounding known command and run it. A best guess "
            "that turns out wrong (e.g. opening the wrong app or the wrong tab) is worse than "
            "asking once.\n\n"
            "RULE 4 - RESPECT CURRENT STATE\n"
            "If the provided state says era is muted, standby, or reconnecting, "
            "do not execute tools.\n"
            "If era is in standby, only respond after a valid wake word.\n"
            "If era is reconnecting, wait until stable before taking action.\n\n"
            "RULE 5 - NO PARAMETER GUESSING\n"
            "Never guess missing parameters.\n"
            "If a required parameter is unclear, ask for clarification.\n"
            "Examples: unclear city for weather, unclear browser tab, "
            "unclear app name, unclear volume target, unclear message recipient.\n\n"
            "RULE 6 - SHORT COMMAND SAFETY\n"
            "Clear short commands may be executed only when context and state allow it:\n"
            "next, pause, resume, play, stop, mute, unmute, volume up, volume down\n\n"
            "Vague short inputs must not trigger actions:\n"
            "volume, go, va, read, open, that\n"
            "Ask clarification or stay silent.\n\n"
            "RULE 7 - TOOL RESULT GROUNDING\n"
            "After a tool call, your response must match the actual tool result.\n"
            "Do not say success if: the command failed, the state was already true, "
            "nothing was playing, no tab/app was found, the command timed out, "
            "or an error occurred.\n"
            "For no-op results, report the actual state concisely.\n\n"
            "RULE 8 - NEVER REPEAT STARTUP GREETINGS ON RESUME\n"
            "On resumed sessions, do not say full greetings like:\n"
            '"era online", "At your service", "What is the objective?"\n'
            "Only acknowledge reconnect if instructed by session state, for example:\n"
            '"Back online, sir."\n'
        )

        parts = [os_vision_ctx]
        if mem_str:
            parts.append(mem_str)

        # ── Standing user preferences + self-learning digest ──
        # This is what makes recorded feedback survive a relaunch: the stores
        # are re-read on every session build and injected into the prompt.
        try:
            from core.user_prefs import format_prefs_for_prompt

            prefs_str = format_prefs_for_prompt()
            if prefs_str:
                parts.append(prefs_str)
        except Exception as _prefs_err:
            print(f"[era] [PREFS] prompt injection skipped: {_prefs_err}")
        try:
            from core.self_learning import format_learning_for_prompt

            learning_str = format_learning_for_prompt()
            if learning_str:
                parts.append(learning_str)
        except Exception as _learn_err:
            print(f"[era] [LEARN] prompt injection skipped: {_learn_err}")

        parts.append(ANTI_HALLUCINATION_INSTR)
        parts.append(sys_prompt)

        # ── Session resumption token (if available) ──
        token = self.session_manager.get_resumption_token()
        resumption_config = types.SessionResumptionConfig()
        if token:
            resumption_config.handle = token

        # ── Voice mode decides the SHAPE of the session ──
        # response_modalities is fixed at connect time, so a voice switch is
        # necessarily a reconnect (see core/voice_mode.py). In kokoro_text Era's
        # speech is synthesized locally from this session's TEXT, so asking for
        # AUDIO too would bill audio-output tokens for sound nobody ever hears —
        # and the whole point of that mode is to stop spending them.
        #
        # input_audio_transcription stays in BOTH: the "You:" log,
        # _note_user_utterance, _maybe_record_feedback and the voice-theme
        # intercept all read it, and none of that is about output.
        if VOICE_CFG.is_kokoro_text():
            return types.LiveConnectConfig(
                response_modalities=["TEXT"],
                input_audio_transcription={},
                system_instruction="\n".join(parts),
                tools=[{"function_declarations": TOOL_DECLARATIONS}],
                session_resumption=resumption_config,
            )

        # kokoro_audio falls through to the AUDIO config below on purpose. It is
        # byte-for-byte the gemini_audio session — same modality, same
        # output_audio_transcription — because that transcription is exactly
        # what Kokoro speaks. The ONLY difference is downstream, in
        # _receive_audio: the cloud audio is dropped instead of played. Keeping
        # the configs identical is what makes switching between the two
        # instant, with no reconnect.
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=resumption_config,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Algenib"  # Algenib #Rasalgethi #Enceladus #Leda #Algieba #Despina
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name or "unknown"
        print(f"[era] [TOOL] {name}")
        log_event(main_logger, "tool.call.received", "INFO", tool=name, params=fc_args_to_dict(fc))
        self.ui.set_state("THINKING")
        self.session_manager.transition_to(
            EraState.PROCESSING, reason="tool_call", metadata={"tool": name}
        )

        result = await self._execute_tool_result(fc)

        if not result.success and result.error:
            error_type = result.error_type or ""
            if error_type not in (
                "BlockedAction",
                "ConfirmationRequired",
                "UnknownTool",
                "ActionError",
                "ConfirmationFailed",
            ):
                self.speak_error(name, result.error)

        print(f"[era] [OUT] {name} -> {result.short_message(80)}")
        if result.success:
            log_event(
                main_logger,
                "tool.execution.completed",
                "INFO",
                tool=name,
                duration_ms=result.duration_ms,
                verification_status=result.verification_status,
            )

        try:
            record_tool_result(name, fc_args_to_dict(fc), result=result.message)
        except Exception as memory_error:
            print(f"[era] [WARN] intent memory record failed: {memory_error}")

        if not self.ui.muted and not self._standby_active:
            self.ui.set_state("LISTENING")

        return build_function_response(fc, result, types.FunctionResponse)

    def _handle_set_voice(self, args: dict) -> str:
        """Switch Era's voice between the cloud (Gemini) and local (Kokoro) one.

        A reconnect is needed ONLY when response_modalities changes, since that
        is fixed at connect time. "local" resolves to kokoro_audio, which is an
        AUDIO session like gemini_audio — so the common switch applies instantly,
        mid-conversation, with no session bounce. Only the kokoro_text mode
        (TEXT session) still costs a reconnect, and that goes through the
        existing GoAway path rather than a second teardown route.
        """
        requested = str(args.get("voice") or "").strip()
        voice_name = str(args.get("voice_name") or "").strip()
        wants_local = requested.lower() in (
            "local",
            "kokoro",
            "offline",
            MODE_KOKORO_TEXT,
            MODE_KOKORO_AUDIO,
        )

        # Only kokoro_TEXT needs the text-only session this model refuses.
        # kokoro_audio runs on the ordinary AUDIO session, so the local voice
        # stays available even after a TEXT rejection — refusing it here was
        # what left the user with no local voice at all.
        wants_text_mode = requested.strip().lower() == MODE_KOKORO_TEXT
        if wants_text_mode and getattr(self, "_text_modality_unsupported", False):
            return (
                "I can't use the text-only local mode — this model rejects text-only "
                "sessions, so I'd lose my connection. Tell the user I can still use my "
                "local voice over the normal session, and ask if they want that instead."
            )

        # The local voice is no longer preloaded at boot (that cost every launch
        # several seconds it usually did not need), so the first request starts
        # it. Kick that off in the background and let the switch complete when
        # Kokoro is ready — blocking here would freeze the live session for the
        # duration of a model load.
        if wants_local and not self.tts_supervisor.model_ready:
            self._spawn_background_task(self._start_local_voice_then_switch(), "start_local_voice")
            return (
                "My local voice isn't loaded yet — I'm starting it now. Tell the user "
                "it will take a few seconds and that I'll switch as soon as it's ready. "
                "Say nothing else."
            )

        if not wants_local and VOICE_CFG.is_gemini():
            return "I'm already using my normal voice. Tell the user, and do nothing else."
        if wants_local and VOICE_CFG.is_kokoro() and not voice_name:
            return "I'm already using my local voice. Tell the user, and do nothing else."

        needs_reconnect = VOICE_CFG.needs_reconnect_to(
            VOICE_CFG._resolve_alias(requested) or VOICE_CFG.mode
        )

        try:
            if voice_name and wants_local:
                VOICE_CFG.set_kokoro_voice(voice_name)
                # The preset voice is baked into the profile, so a new voice
                # needs a new profile — drop the cached id or Era keeps the old one.
                VOICEBOX.invalidate_profile()
            applied = VOICE_CFG.set_mode(requested)
        except ValueError as e:
            return f"I couldn't switch my voice: {e}. Ask the user which voice they meant."

        # Drop audio queued in the OLD voice, so a half-played cloud sentence
        # does not finish in Gemini's voice after the switch.
        self._flush_audio_in_queue()

        label = "local, on-device" if applied in LOCAL_VOICE_MODES else "normal cloud"
        print(f"[VOICE] Switching to {applied} (reconnect={needs_reconnect})")
        self.ui.write_log(f"SYS: Voice switched to {label}.")

        if not needs_reconnect:
            # Same session shape — the change is already live.
            return (
                f"Voice switched to the {label} voice, effective immediately. "
                "Tell the user it is done. Say nothing else."
            )

        # Modality actually changed: reconnect to apply it.
        self._go_away_event.set()
        self.session_manager.prepare_reconnect()

        return (
            f"Voice switched to the {label} voice. Tell the user this is done and that "
            "there will be a brief pause while the connection re-establishes. "
            "Say nothing else."
        )

    def _note_user_utterance(self, text: str) -> None:
        """Record a complete user utterance for tool routing AND the
        save_memory authorization window. Every site that accepts a full
        user input must go through here, not assign _last_user_text raw."""
        self._last_user_text = text
        if text and text.strip():
            self._recent_user_texts.append((time.time(), text))
            if len(self._recent_user_texts) > 8:
                del self._recent_user_texts[:-8]

    def _memory_save_authorized(self) -> bool:
        """save_memory gate: True when the user asked for a save recently.

        Checks, in order: the current utterance; the rolling window of recent
        utterances (the ask and the save call are often several tool-turns
        apart on voice); and, after a blocked save, a plain affirmation to
        Era's spoken "want me to remember that?" follow-up question.
        """
        if is_explicit_memory_request(self._last_user_text):
            return True
        now = time.time()
        for ts, txt in reversed(self._recent_user_texts):
            if now - ts > _MEMORY_REQUEST_WINDOW_S:
                break
            if is_explicit_memory_request(txt):
                return True
        if self._blocked_memory_save is not None:
            blocked_ts = self._blocked_memory_save[0]
            if now - blocked_ts <= _MEMORY_CONFIRM_WINDOW_S and is_memory_affirmation(
                self._last_user_text
            ):
                return True
        return False

    def _autonomous_memory_save_allowed(self, category: str, key: str, value: str) -> bool:
        """save_memory gate, second path: Era decided by herself that this is
        worth remembering (the user never said "remember this").

        Allowed only when all of these hold:
          1. the user has auto-memory on (live-toggleable — user_prefs);
          2. the category is one of the personal-fact categories, so brand
             routing / config / lock values can never take this path;
          3. the value is grounded in the user's own recent speech, which is
             what keeps prompt-injected page or document text out of memory;
          4. the unprompted-save rate cap has not been hit.
        """
        if category not in _AUTO_SAVE_CATEGORIES:
            return False
        if not value or not str(value).strip():
            return False

        try:
            from core.user_prefs import is_auto_memory_enabled

            if not is_auto_memory_enabled():
                return False
        except Exception:
            return False  # fail closed: no prefs, no self-initiated writes

        now = time.time()
        self._auto_memory_saves = [
            ts for ts in self._auto_memory_saves if now - ts <= _AUTO_SAVE_WINDOW_S
        ]
        if len(self._auto_memory_saves) >= _AUTO_SAVE_MAX_PER_WINDOW:
            print("[MEMORY] [BLOCK] autonomous save rate cap reached")
            return False

        recent = [
            txt for ts, txt in self._recent_user_texts if now - ts <= _MEMORY_REQUEST_WINDOW_S
        ]
        if not is_value_grounded_in_user_speech(str(value), recent):
            return False

        self._auto_memory_saves.append(now)
        return True

    async def _execute_tool_result(self, fc) -> ToolResult:
        tool_name = fc.name
        parameters = fc_args_to_dict(fc)
        original_name, original_params = tool_name, dict(parameters)

        try:
            user_text = getattr(self, "_last_user_text", "") or ""
            tool_name, parameters = route_tool_call(tool_name, parameters, user_text)
            if (tool_name, parameters) != (original_name, original_params):
                print(
                    f"[era] [ROUTE] routed {original_name} {original_params} -> {tool_name} {parameters}"
                )
        except Exception as route_error:
            print(f"[era] [WARN] routing layer skipped: {route_error}")

        print(f"[era] [ARGS] {tool_name}  {parameters}")

        start = time.perf_counter()

        def duration():
            return int((time.perf_counter() - start) * 1000)

        # ── JIT vision capture for mutation actions ──
        try:
            from core.vision_context import jit_vision_capture

            _jit_action = parameters.get("action", "")
            if isinstance(_jit_action, str):
                await jit_vision_capture(tool_name, _jit_action.lower().strip())
        except Exception:
            pass

        safety = classify_action(tool_name, parameters)
        risk_level = safety["risk_level"]
        requires_confirmation = safety["requires_confirmation"]
        reason = safety["reason"]
        print(f"[era] [SAFETY] safety: {risk_level} - {reason}")
        log_event(
            main_logger,
            "tool.safety.classified",
            "INFO",
            tool=tool_name,
            risk_level=risk_level,
            requires_confirmation=requires_confirmation,
            reason=reason,
        )

        if risk_level == "BLOCKED":
            return ToolResult.fail(
                message="Blocked unsafe action.",
                error=reason,
                error_type="BlockedAction",
                tool_name=tool_name,
                action_name=parameters.get("action"),
                duration_ms=duration(),
            )

        if requires_confirmation:
            print(f"[era] [WARN] confirmation required ({risk_level})")
            confirmed = bool(parameters.get("confirmed", False))
            confirmation_id = parameters.get("confirmation_id", "")

            if confirmed and confirmation_id:
                pending = self._pending_store.confirm(confirmation_id)
                if pending is None:
                    return ToolResult.fail(
                        message="Invalid or expired confirmation.",
                        error="Confirmation ID not found or has expired.",
                        error_type="ConfirmationFailed",
                        tool_name=tool_name,
                        action_name=parameters.get("action"),
                        duration_ms=duration(),
                    )
                stored_params = {
                    k: v
                    for k, v in pending.parameters.items()
                    if k not in ("confirmed", "confirmation_id", "auto_confirm")
                }
                call_params = {
                    k: v
                    for k, v in parameters.items()
                    if k not in ("confirmed", "confirmation_id", "auto_confirm")
                }
                if pending.tool != tool_name or stored_params != call_params:
                    self._pending_store.cancel(confirmation_id)
                    return ToolResult.fail(
                        message="Confirmation mismatch.",
                        error="The confirmed action does not match the pending action.",
                        error_type="ConfirmationFailed",
                        tool_name=tool_name,
                        action_name=parameters.get("action"),
                        duration_ms=duration(),
                    )
            elif confirmed and not confirmation_id:
                return ToolResult.fail(
                    message="Confirmation rejected: missing confirmation_id.",
                    error="Confirmed=True requires a valid confirmation_id.",
                    error_type="ConfirmationFailed",
                    tool_name=tool_name,
                    action_name=parameters.get("action"),
                    duration_ms=duration(),
                )
            else:
                expired = self._pending_store.expire_all()
                if expired:
                    print(f"[era] [CLEAN] expired {expired} stale pending confirmation(s)")
                cid = self._pending_store.store(
                    tool=tool_name,
                    parameters=dict(parameters),
                    risk_level=risk_level,
                    reason=reason,
                )
                print(f"[era] [WAIT] pending confirmation: {tool_name} id={cid}")
                log_event(
                    main_logger,
                    "tool.confirmation.created",
                    "INFO",
                    tool=tool_name,
                    risk_level=risk_level,
                    confirmation_id=cid,
                )
                return ToolResult.fail(
                    message="Confirmation required before executing this action.",
                    error=reason,
                    error_type="ConfirmationRequired",
                    tool_name=tool_name,
                    action_name=parameters.get("action"),
                    confirmation_id=cid,
                    duration_ms=duration(),
                    data=dict(parameters),
                )

            ConfirmationManager().request_confirmation(
                tool=tool_name,
                action_name=parameters.get("action", ""),
                message=reason,
                risk_level=risk_level,
                details=parameters,
                speak=self.speak,
                confirmed=True,
                auto_confirm=False,
            )

        try:
            raw = await self._execute_tool_raw(tool_name, parameters, fc)
            self._last_tool_name = tool_name
        except Exception as e:
            dur = duration()
            traceback.print_exc()
            return ToolResult.fail(
                message=str(e),
                error=str(e),
                error_type=type(e).__name__,
                tool_name=tool_name,
                action_name=parameters.get("action"),
                duration_ms=dur,
            )

        dur = duration()

        if isinstance(raw, str) and ("Unknown tool:" in raw or "Unknown action:" in raw):
            return ToolResult.fail(
                message=raw,
                error_type="UnknownTool",
                tool_name=tool_name,
                action_name=parameters.get("action"),
                duration_ms=dur,
            )

        result = ToolResult.from_any(
            raw,
            tool_name=tool_name,
            action_name=parameters.get("action"),
            duration_ms=dur,
        )

        result.risk_level = risk_level
        result.requires_confirmation = requires_confirmation

        result = verify_tool_result(result, tool=tool_name, parameters=parameters)

        if not result.success:
            print(
                f"[era] [ERROR] {tool_name} failed: {result.error_type} - {result.short_message(200)}"
            )
            log_event(
                main_logger,
                "tool.execution.failed",
                "ERROR",
                tool=tool_name,
                error_type=result.error_type,
                duration_ms=result.duration_ms,
            )

        return result

    async def _execute_tool_raw(self, tool_name: str, args: dict, fc=None) -> str:
        # ── Final safety guard before any tool execution ──
        snapshot = self.session_manager.get_state_snapshot()
        if snapshot.get("is_muted", False):
            return "Action blocked: era is muted."
        if snapshot.get("is_reconnecting", False):
            return "Action blocked: era is reconnecting."
        if snapshot.get("standby_active", False) and not snapshot.get("wake_word_active", False):
            return "Action blocked: era is in standby."
        # While a brand-ask is open, Era should just ask and wait — not guess or
        # act. Allow-list: block EVERY tool except the minimal escape-hatch set so
        # a future tool is protected by default. Lifts automatically the moment
        # _pending_cm_ask_active() goes False (reply, non-match, or 45s timeout).
        if tool_name not in _PENDING_ASK_ALLOWED_TOOLS and self._pending_cm_ask_active():
            print(f"[CM-ASK] Holding '{tool_name}' — awaiting the user's brand answer.")
            return (
                "I've just asked the user which brand this is for, and I'm waiting "
                "for their answer before doing anything else. Please hold on this — "
                "don't call other tools or guess the brand; simply wait for their reply."
            )
        # A CM utterance was just claimed by the headless dispatch lane, which owns
        # it end-to-end. Gemini overheard the same words on the open mic — hold every
        # tool (same allow-list as the brand-ask) so it can't run off and act on a
        # request it isn't handling. Lifts automatically when the window expires.
        if tool_name not in _PENDING_ASK_ALLOWED_TOOLS and self._cm_handoff_active():
            print(
                f"[CM-HANDOFF] Holding '{tool_name}' — the community-management lane owns this request."
            )
            return (
                "That request has already been handed to the community-management "
                "pipeline, which is handling it end-to-end. Do not act on it, call "
                "any tool, or describe doing it yourself — just wait; I'll report the "
                "result when it finishes."
            )
        # ─────────────────────────────────────────────────

        if tool_name == "enter_standby":
            self.enter_standby()
            # Do NOT ask the model to speak here — core/prompt.txt already
            # tells it to stay silent around this tool, and the code plays
            # its own "Standby mode active" line in wait_and_mute(). Telling
            # the model to also say a sign-off raced two spoken utterances;
            # the second one would sometimes only finish streaming after
            # standby had already muted, and leak out on the next wake.
            return "Standby mode activated. Say nothing — the system plays its own offline notice."

        if tool_name == "set_wake_word":
            # Era editing her own wake word. Only allowed while she's AWAKE — never
            # from standby. That guarantee is already enforced by the standby check
            # at the top of this method (a tool call in standby-without-wake is
            # rejected before reaching here); this is a belt-and-suspenders guard so
            # the intent stays explicit even if that top gate ever changes.
            if self._standby_active:
                return "I can only change my wake word while I'm awake, not from standby."
            # WAKE_CFG.update() validates the word, rebuilds the Vosk grammar + match
            # regex, and persists to config/wake_word.json (Era's own state). The new
            # word arms automatically the next time she enters standby.
            requested = (args.get("wake_word") or "").strip()
            try:
                applied = WAKE_CFG.update(requested)
            except ValueError as e:
                return (
                    f"I couldn't change the wake word to '{requested}': {e}. "
                    "Pick a single spoken word and I'll switch to it."
                )
            global WAKE_WORD
            WAKE_WORD = applied
            print(f"[WAKE] Wake word changed to '{applied}'")
            return (
                f"Wake word changed to '{applied}'. Next time I'm in standby, say "
                f"'{applied}' to wake me. Tell the user this is done."
            )

        if tool_name == "set_voice":
            return self._handle_set_voice(args)

        if tool_name == "manage_schedule":
            return self._handle_manage_schedule(args)

        if tool_name == "manage_offer":
            return self._handle_manage_offer(args)

        if tool_name == "community_manager":
            # Voice/text CM order routed by Gemini itself. Kick the (possibly
            # 30-min) job off in the BACKGROUND and return an immediate ack so
            # Era stays free for new tasks — the CM lane reports the outcome back
            # via _run_cm_flow's own speak() when it finishes. No _arm_cm_handoff
            # here: Gemini IS the router in this path (it decided to call the tool),
            # so there is no parallel utterance to mute — muting would gag the ack.
            order = (args.get("order") or "").strip()
            if not order:
                return (
                    "Ask the user what community-management task they want — e.g. "
                    "'publish Ceram Quartz's approved posts' or 'make a reel for Delta Cuisine'."
                )
            task_type = (args.get("task_type") or "").strip().lower()
            if task_type not in (
                "publish",
                "weekly",
                "campaign",
                "rush",
                "revise",
                "approve",
                "ads",
                "adcreative",
            ):
                task_type = _infer_cm_task_type(order)
            brand_in = (args.get("brand") or "").strip()
            brand_key = None
            try:
                import era_dispatch as ed

                if brand_in:
                    brand_key = ed.resolve_brand(brand_in) or (
                        brand_in if "_" in brand_in else None
                    )
                if not brand_key:
                    brand_key = ed.resolve_brand(order)
            except Exception as e:
                print(f"[CM-TOOL] brand resolve failed: {e}")
                brand_key = None
            if not brand_key:
                # Don't guess and don't dispatch blind — bounce back so Era asks.
                return (
                    "This is community-management work, but I couldn't tell which "
                    "brand it's for. Ask the user aloud: 'Which brand is this for — "
                    "Delta Cuisine or Ceram Quartz?' Then call community_manager "
                    "again with the brand set. Do not call any other tool meanwhile."
                )
            payload = {"text": order, "task_type": task_type, "brand_key": brand_key}
            task = asyncio.create_task(self._run_cm_flow(payload, announce=False))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            print(
                f"[CM-TOOL] dispatched brand={brand_key} task_type={task_type} order={order[:60]!r}"
            )
            nice_brand = brand_key.replace("_", " ")
            verb = {
                "publish": "publish",
                "approve": "approval",
                "revise": "revision",
                "rush": "rush job",
            }.get(task_type, "content run")
            return (
                f"Tell the user, in your own words: you've handed the {verb} for "
                f"{nice_brand} to the community-management agent and it's now running in "
                f"the background; you'll report back the moment it finishes, and they can "
                f"keep giving you other tasks in the meantime. Then stop and stay available "
                f"— do not call any other tool for this order; the CM lane owns it."
            )

        if tool_name == "save_memory":
            category = args.get("category", "notes")
            key = args.get("key", "")
            value = args.get("value", "")

            # Three-condition authorization. A save is allowed when:
            #   1. the user asked for it (_memory_save_authorized — current
            #      utterance, the recent-utterance window, or an affirmation
            #      after a blocked save);
            #   2. a deliberate CM-pipeline step opened the gate
            #      (self._memory_write_authorized); or
            #   3. Era decided by herself that it is worth remembering AND the
            #      value is grounded in the user's own recent speech
            #      (_autonomous_memory_save_allowed).
            # Path 3 is what lets "I love this film" or a mentioned date be kept
            # without the user having to say "remember that". It cannot launder
            # page/document text into memory (grounding), and it cannot write
            # brand routing data (category allowlist) — the two abuses the
            # original strict gate was built for.
            user_asked = self._memory_save_authorized() or self._memory_write_authorized
            autonomous = False
            if not user_asked:
                autonomous = self._autonomous_memory_save_allowed(category, key, str(value))
                if autonomous:
                    audit_event(
                        operation="save",
                        category=category,
                        key=key,
                        new_value_summary=str(value)[:120] if value else "",
                        source_tool="main",
                        reason="autonomous_grounded_save",
                        allowed=True,
                    )
                    print(f"[MEMORY] [AUTO] self-initiated save (grounded): {category}/{key}")

            if not (user_asked or autonomous):
                audit_event(
                    operation="block",
                    category=category,
                    key=key,
                    new_value_summary=str(value)[:120] if value else "",
                    source_tool="main",
                    reason="no_explicit_user_trigger",
                    allowed=False,
                )
                print(
                    f"[MEMORY] [BLOCK] save_memory refused (no explicit user request): {category}/{key}"
                )
                # Arm the confirmation window and turn the refusal into a
                # question instead of a dead end: a spoken "yes" within
                # _MEMORY_CONFIRM_WINDOW_S authorizes the retry.
                self._blocked_memory_save = (time.time(), category, key, str(value))
                return (
                    "Not saved yet — I only write to memory when the user asks. "
                    "Ask the user aloud, in one short sentence, whether they want "
                    f"you to remember '{str(value)[:80]}'. If they say yes, call "
                    "save_memory again with the same category, key and value."
                )

            # â”€â”€ Issue 7: Block transient media/browser state from long-term memory â”€â”€
            if is_transient_memory_key(category, key):
                audit_event(
                    operation="block",
                    category=category,
                    key=key,
                    new_value_summary=str(value)[:120] if value else "",
                    source_tool="main",
                    reason="transient_key",
                    allowed=False,
                )
                print(
                    f"[Memory] [SKIP] Blocked transient key from long-term memory: {category}/{key}"
                )
                return "Runtime media state updated."

            if key and value:
                update_memory({category: {key: {"value": value}}})
                self._blocked_memory_save = None
                masked = value[:60] + "..." if len(value) > 60 else value
                print(f"[Memory] [SAVE] save_memory: {category}/{key} = {masked}")
            return "ok"

        if tool_name == "retrieve_memory_context":
            query = args.get("query", "")
            if not query:
                return "No query provided."
            # Reinforced retrieval: bumps access_count on the entries returned,
            # so memories the user actually asks about rank higher over time.
            from memory.memory_manager import retrieve_context_reinforced

            result = retrieve_context_reinforced(query, top_k=5)
            return result if result else "No relevant memories found."

        if tool_name == "list_memory_keys":
            from memory.memory_manager import list_memory_keys

            return list_memory_keys(
                category=args.get("category"),
                full=args.get("full", False),
                limit=args.get("limit", 50),
                offset=args.get("offset", 0),
                show_counts=args.get("show_counts", True),
            )

        if tool_name == "forget_memory":
            category = args.get("category", "")
            key = args.get("key", "")
            if not category or not key:
                return "Error: both category and key are required."
            from memory.memory_manager import forget

            return forget(key, category)

        if tool_name == "confirm_pending_confirmation":
            confirmation_id = args.get("confirmation_id", "")
            # Single-slot store: confirm the sole pending action even if the model
            # passed a stale/invented id (e.g. 'magnific_upload_permission'). The
            # real id is a random hex the model never sees, so requiring an exact
            # match only produced spurious "no longer valid" failures.
            pending = self._pending_store.confirm(confirmation_id, allow_sole=True)
            if pending is None:
                return "This confirmation is no longer valid."
            if confirmation_id and confirmation_id != pending.confirmation_id:
                print(
                    f"[era] [WARN] confirmation id mismatch (got {confirmation_id!r}, "
                    f"confirming sole pending {pending.confirmation_id})"
                )
            stored_params = dict(pending.parameters)
            stored_params["confirmed"] = True
            stored_params["confirmation_id"] = pending.confirmation_id
            print(
                f"[era] [OK] pending confirmation accepted: {pending.tool} id={pending.confirmation_id}"
            )
            log_event(
                main_logger,
                "tool.confirmation.accepted",
                "INFO",
                tool=pending.tool,
                confirmation_id=pending.confirmation_id,
            )
            return await self._execute_tool_raw(pending.tool, stored_params, fc)

        if tool_name == "cancel_pending_confirmation":
            if self._pending_store.is_pending():
                self._pending_store.clear()
                print("[era] [ERROR] pending confirmation cancelled")
                log_event(main_logger, "tool.confirmation.cancelled", "INFO")
                return "Pending action cancelled."
            print("[era] [INFO] no pending confirmation to cancel")
            log_event(main_logger, "tool.confirmation.cancelled", "INFO", reason="no pending")
            return "There is no pending action to cancel."

        loop = asyncio.get_event_loop()
        result = "Done."

        if tool_name == "open_app":
            r = await loop.run_in_executor(
                None, lambda: open_app(parameters=args, response=None, player=self.ui)
            )
            result = r or f"Opened {args.get('app_name')}."

        elif tool_name == "weather_report":
            r = await loop.run_in_executor(
                None, lambda: weather_action(parameters=args, player=self.ui)
            )
            result = r or "Weather delivered."

        elif tool_name == "browser_control":
            r = await loop.run_in_executor(
                None, lambda: browser_control(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "file_controller":
            r = await loop.run_in_executor(
                None, lambda: file_controller(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "send_message":
            r = await loop.run_in_executor(
                None,
                lambda: send_message(
                    parameters=args, response=None, player=self.ui, session_memory=None
                ),
            )
            result = r or f"Message sent to {args.get('receiver')}."

        elif tool_name == "reminder":
            r = await loop.run_in_executor(
                None, lambda: reminder(parameters=args, response=None, player=self.ui)
            )
            result = r or "Reminder set."

        elif tool_name == "youtube_video":
            r = await loop.run_in_executor(
                None, lambda: youtube_video(parameters=args, response=None, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "screen_process":
            r = await loop.run_in_executor(
                None,
                lambda: screen_process(
                    parameters=args,
                    response=None,
                    player=self.ui,
                    session_memory=None,
                    speak=self.speak,
                ),
            )
            result = r or "I could not analyze the screen."

        elif tool_name == "computer_settings":
            r = await loop.run_in_executor(
                None, lambda: computer_settings(parameters=args, response=None, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "system_performance":
            r = await loop.run_in_executor(
                None, lambda: system_performance(parameters=args, response=None, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "desktop_control":
            r = await loop.run_in_executor(
                None, lambda: desktop_control(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "code_helper":
            r = await loop.run_in_executor(
                None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak)
            )
            result = r or "Done."

        elif tool_name == "dev_agent":
            r = await loop.run_in_executor(
                None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak)
            )
            result = r or "Done."

        elif tool_name == "agent_task":
            from agent.task_queue import get_queue
            from agent.types import TaskPriority

            priority_map = {
                "low": TaskPriority.LOW,
                "normal": TaskPriority.NORMAL,
                "high": TaskPriority.HIGH,
            }
            priority = priority_map.get(args.get("priority", "normal").lower(), TaskPriority.NORMAL)
            task_id = get_queue().submit(
                goal=args.get("goal", ""), priority=priority, speak=self.speak
            )
            result = f"Task started (ID: {task_id})."

        elif tool_name == "web_search":
            r = await loop.run_in_executor(
                None, lambda: web_search_action(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "file_processor":
            if not args.get("file_path") and self.ui.current_file:
                args["file_path"] = self.ui.current_file
            r = await loop.run_in_executor(
                None, lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
            )
            result = r or "Done."

        elif tool_name == "computer_control":
            r = await loop.run_in_executor(
                None, lambda: computer_control(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "netflix_control":
            r = await loop.run_in_executor(
                None, lambda: netflix_control(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "spotify_control":
            from actions.spotify_control import spotify_control

            r = await loop.run_in_executor(
                None, lambda: spotify_control(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "game_updater":
            r = await loop.run_in_executor(
                None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak)
            )
            result = r or "Done."

        elif tool_name == "flight_finder":
            r = await loop.run_in_executor(
                None, lambda: flight_finder(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "media_coordinator":
            r = await loop.run_in_executor(
                None, lambda: media_coordinator(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "window_control":
            r = await loop.run_in_executor(
                None, lambda: window_control(parameters=args, player=self.ui, speak=self.speak)
            )
            result = r or "Done."

        elif tool_name in ("shutdown_era", "close_era", "exit_era"):
            if self._shutdown_requested:
                return "era shutdown is already in progress."

            # Is a brand/community-management job running right now? current_job()
            # just reads ~/.era/era_status.json. If era_dispatch can't even import,
            # no job could have been dispatched through it -> treat as nothing running.
            job = None
            try:
                import era_dispatch

                job = era_dispatch.current_job()
            except Exception as e:
                print(f"[era] [SHUTDOWN] job check unavailable ({e}); assuming nothing running.")
                job = None

            # Honor `confirmed` ONLY if we actually asked first (gates a stray true).
            confirmed = bool(args.get("confirmed", False)) and self._shutdown_awaiting_confirm

            if job and not confirmed:
                # A job is running and the user hasn't confirmed: ask, do NOT exit,
                # do NOT write era.stop.
                self._shutdown_awaiting_confirm = True
                brand = job.get("brand", "a brand")
                ttype = job.get("task_type", "") or "job"
                order = (job.get("order") or "").strip()
                order_clip = (order[:120] + "...") if len(order) > 120 else order
                self.ui.write_log(
                    f"SYS: Shutdown requested but {brand}/{ttype} job running; asking to confirm."
                )
                self.speak(
                    f"Heads up, sir - a {brand} {ttype} is running right now"
                    + (f': "{order_clip}".' if order_clip else ".")
                    + " Shutting down will interrupt it. Say 'confirm shutdown' to go ahead,"
                    " or 'cancel' to keep it running."
                )
                return "Shutdown paused: a job is running, awaiting the user's confirmation."

            # -- Proceeding to shut down: nothing running, OR the user confirmed. --
            self._shutdown_requested = True
            self._shutdown_awaiting_confirm = False

            # Tell the watchdog this exit is INTENTIONAL, BEFORE exiting, so it does
            # not relaunch Era. Same path era_supervisor.py watches: ~/.era/era.stop.
            try:
                import os as _os

                era_home = Path(_os.getenv("ERA_HOME", str(Path.home() / ".era")))
                era_home.mkdir(parents=True, exist_ok=True)
                stop_flag = era_home / "era.stop"
                stop_flag.write_text(
                    f"Intentional shutdown by user at {datetime.now().isoformat(timespec='seconds')}.\n"
                    "The supervisor will NOT relaunch Era while this file exists.\n"
                    "Resume with:  register_era_supervisor.ps1 -Action wake\n",
                    encoding="utf-8",
                )
                print(f"[era] [SHUTDOWN] wrote stop flag {stop_flag} (watchdog will not relaunch).")
            except Exception as e:
                print(
                    f"[era] [SHUTDOWN] WARNING: could not write era.stop ({e}); watchdog MAY relaunch Era."
                )

            self.ui.write_log("SYS: Shutdown confirmed; era.stop written.")
            self.speak(
                "Goodbye, sir. When you want me back, run register era supervisor, "
                "dash Action wake, in PowerShell."
            )

            def _shutdown():
                import os

                for _ in range(30):
                    if self._is_speaking:
                        break
                    time.sleep(0.1)
                while self._is_speaking:
                    time.sleep(0.1)
                time.sleep(1.2)
                os._exit(0)

            threading.Thread(target=_shutdown, daemon=True).start()
            result = "Shutting down..."

        elif tool_name == "record_feedback":
            from memory.feedback_memory import FeedbackEvent, append_feedback, extract_feedback
            from memory.memory_audit import audit_event as _audit_fb

            fb_text = (args.get("feedback_text") or "").strip()
            fb_category = (args.get("category") or "").strip() or None
            if not fb_text:
                result = "No feedback text provided."
            elif _is_era_own_echo(fb_text):
                # Era's own spoken status line looped back through Gemini — not a
                # user instruction. Drop it (benign ack so Gemini doesn't retry).
                print(
                    f"[Memory] [FEEDBACK] Skipped Era's own echo, not user feedback: {fb_text[:60]!r}"
                )
                result = "Noted."
            else:
                mistake_cat, signature, expected = extract_feedback(fb_text)
                if fb_category and not mistake_cat:
                    mistake_cat = fb_category
                event = FeedbackEvent(
                    timestamp=datetime.now(UTC).isoformat(),
                    user_text=fb_text[:500],
                    assistant_last_tool=getattr(self, "_last_tool_name", None),
                    mistake_category=mistake_cat,
                    signature=signature,
                    expected_correction=expected,
                )
                append_feedback(event)
                _audit_fb(
                    operation="feedback",
                    category=mistake_cat or "generic",
                    key=signature or "unknown",
                    new_value_summary=fb_text[:120],
                    source_tool="record_feedback",
                    reason="ai_tool_feedback",
                    allowed=True,
                )
                print(
                    f"[Memory] [FEEDBACK] Feedback recorded via tool: {mistake_cat or 'generic'}/{signature or 'unknown'}"
                )
                result = "Feedback recorded."
                # SECURITY: no auto-promotion to standing preferences here.
                # feedback_text is model-supplied and can be steered by
                # prompt-injected content; only _maybe_record_feedback (real
                # user transcription) auto-promotes. Durable preferences via
                # this path must go through manage_preference, whose waiver
                # grants require explicit confirmation.

        elif tool_name == "manage_preference":
            from core.user_prefs import (
                add_behavior_rule,
                format_prefs_for_prompt,
                remove_behavior_rule,
                set_auto_memory,
                set_close_confirmation_waiver,
                set_file_creation_confirmation_waiver,
            )
            from memory.memory_audit import audit_event as _audit_pref

            pref_action = (args.get("action") or "").strip().lower()
            pref_app = (args.get("app") or "").strip()
            pref_rule = (args.get("rule") or "").strip()
            if pref_action == "waive_close_confirmation":
                result = set_close_confirmation_waiver(pref_app, True)
                _audit_pref(
                    operation="preference",
                    category="close_waiver_grant",
                    key=pref_app or "unknown",
                    new_value_summary=result[:120],
                    source_tool="manage_preference",
                    reason="user_granted_close_waiver",
                    allowed=True,
                )
            elif pref_action == "require_close_confirmation":
                result = set_close_confirmation_waiver(pref_app, False)
                _audit_pref(
                    operation="preference",
                    category="close_waiver_revoke",
                    key=pref_app or "unknown",
                    new_value_summary=result[:120],
                    source_tool="manage_preference",
                    reason="user_revoked_close_waiver",
                    allowed=True,
                )
            elif pref_action == "waive_file_creation_confirmation":
                result = set_file_creation_confirmation_waiver(True)
                _audit_pref(
                    operation="preference",
                    category="file_creation_waiver_grant",
                    key="file_creation",
                    new_value_summary=result[:120],
                    source_tool="manage_preference",
                    reason="user_granted_file_creation_waiver",
                    allowed=True,
                )
            elif pref_action == "require_file_creation_confirmation":
                result = set_file_creation_confirmation_waiver(False)
                _audit_pref(
                    operation="preference",
                    category="file_creation_waiver_revoke",
                    key="file_creation",
                    new_value_summary=result[:120],
                    source_tool="manage_preference",
                    reason="user_revoked_file_creation_waiver",
                    allowed=True,
                )
            elif pref_action in ("enable_auto_memory", "disable_auto_memory"):
                enable = pref_action == "enable_auto_memory"
                result = set_auto_memory(enable)
                _audit_pref(
                    operation="preference",
                    category="auto_memory_grant" if enable else "auto_memory_revoke",
                    key="auto_memory",
                    new_value_summary=result[:120],
                    source_tool="manage_preference",
                    reason="user_enabled_auto_memory" if enable else "user_disabled_auto_memory",
                    allowed=True,
                )
            elif pref_action == "add_rule":
                result = add_behavior_rule(pref_rule)
            elif pref_action == "remove_rule":
                result = remove_behavior_rule(pref_rule)
            elif pref_action == "list":
                result = format_prefs_for_prompt() or "No standing preferences saved yet."
            else:
                result = f"Unknown preference action: '{pref_action}'."
            print(f"[Memory] [PREFS] manage_preference {pref_action}: {result}")

        elif tool_name == "premiere_control":
            command_text = args.get("command", "")
            self._spawn_background_task(
                execute_premiere_voice_command(command_text, era_instance=self),
                "premiere_control",
            )
            result = "Timeline task dispatched to background processor."

        elif tool_name == "design_studio":
            # Generation takes 10s-minutes: run in a worker thread and speak the
            # outcome when it lands, so the live loop stays free meanwhile.
            design_args = dict(args)

            async def _run_design_studio():
                try:
                    r = await asyncio.to_thread(design_studio, design_args, self.ui, self.speak)
                    self.speak(r or "The design task finished.")
                except Exception as e:
                    print(f"[era] [DESIGN] background generation failed: {e}")
                    self.speak("The design generation hit an error — check the log.")

            self._spawn_background_task(_run_design_studio(), "design_studio")
            result = (
                "Design generation started in the background — I'll announce it when it's ready."
            )

        elif tool_name == "self_diagnosis":
            diag_args = dict(args)

            async def _run_self_diagnosis():
                try:
                    r = await asyncio.to_thread(run_self_diagnosis, diag_args, self.ui, self.speak)
                    self.speak(r or "Diagnosis finished.")
                except Exception as e:
                    print(f"[era] [DIAG] background diagnosis failed: {e}")
                    self.speak("The self-diagnosis hit an error — check the log.")

            self._spawn_background_task(_run_self_diagnosis(), "self_diagnosis")
            result = "Deep self-diagnosis started in the background — I'll report when it's done."

        elif tool_name == "self_improve":
            improve_args = dict(args)
            improve_action = str(improve_args.get("action") or "propose").lower().strip()

            if improve_action in ("propose", "list_backups", ""):
                # Read-only and fast enough to answer inline.
                r = await asyncio.to_thread(
                    self_improve, improve_args, None, self.ui, None, self.speak
                )
                result = r or "Done."
            else:
                # apply/rollback: LLM generation + verification can take a while —
                # run in the background exactly like self_diagnosis.
                async def _run_self_improve():
                    try:
                        r = await asyncio.to_thread(
                            self_improve, improve_args, None, self.ui, None, self.speak
                        )
                        self.speak(r or "Self-improvement finished.")
                    except Exception as e:
                        print(f"[era] [IMPROVE] background self-improve failed: {e}")
                        self.speak(
                            "The self-improvement attempt hit an error — my code was "
                            "not left modified. Check the log."
                        )

                self._spawn_background_task(_run_self_improve(), "self_improve")
                result = (
                    "Working on my own code in the background — I'll back it up, "
                    "verify it, and report the outcome."
                )

        elif tool_name == "agent_browser":
            r = await loop.run_in_executor(
                None, lambda: agent_browser(parameters=args, player=self.ui)
            )
            result = r or "Done."

        else:
            result = f"Unknown tool: {tool_name}"

        return result
