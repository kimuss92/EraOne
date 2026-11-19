"""Tool dispatch + tool result handling + session config for EraLive (from main.py).

The tool router (_execute_tool_raw), result verification/confirmation, live-session
config builder, and memory-save authorization. Operates on self.* state.
"""

import asyncio
import json
import re
import threading
import time
import traceback
from datetime import UTC, datetime

from google.genai import types

from actions.agent_browser_use import agent_browser
from actions.browser_control import browser_control
from actions.code_helper import code_helper
from actions.computer_control import computer_control
from actions.computer_settings import computer_settings
from actions.design_studio import design_studio
from actions.desktop import desktop_control
from actions.dev_agent import dev_agent
from actions.era_workbench import era_workbench
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
from actions.self_heal import self_heal_tool
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
from app.hud.events import hud
from app.system_prompt import (  # noqa: F401  (re-export: preserves public + internal names)
    _load_system_prompt,
)
from app.text_intents import _sanitize_spoken, _say_exactly
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
from core.cancel_registry import CURRENT_TOKEN, REGISTRY, CancelledTask, offload, run_with_grace
from core.confirmation import ConfirmationManager
from core.confirmation_preview import (
    ANNOUNCED_KEY,
    CONFIRM_META,
    PREVIEW_KEY,
    build_confirmation_preview,
    carried_from,
    strip_carried,
)
from core.era_state import EraState
from core.logger import log_crash, log_event, log_frames, main_logger
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
from core.pending_confirmation import (
    BUSY_CONFIRMATION_MESSAGE,
    NOT_USER_APPROVED_MESSAGE,
    PendingConfirmationBusy,
)
from core.process_awareness import format_running_apps
from core.safety_guard import classify_action
from core.self_healing_router import divert_era_reboot, record_tool_result, route_tool_call
from core.tool_recovery import TurnRecovery
from core.tool_result import ToolResult
from core.tts_fish import is_configured as is_fish_configured
from core.verification import verify_tool_result
from core.voice_mode import (
    MODE_FISH_AUDIO,
    MODE_GEMINI_AUDIO,
    MODE_KOKORO_AUDIO,
    MODE_LABELS,
    VOICE_CFG,
    gemini_voice_name,
    input_language_codes,
)
from core.window_awareness import format_context as format_window_context
from memory.memory_audit import audit_event
from memory.memory_manager import (
    format_memory_for_prompt_ranked,
    load_memory,
    update_memory,
)
from memory.transient_filter import is_transient_memory_key

# Model-facing acknowledgement returned the instant a crew `ask` is handed to a
# background task (see _start_background_crew_ask). A crew turn can run for
# minutes; awaiting it inline blocks the receive loop from draining the socket,
# which trips Gemini's keepalive (1011) and kills the session mid-call -- and the
# result then has nowhere to return, so the HUD shows the real outcome while Era
# stays silent (the exact "panels disagree / never reports back" bug). The real
# outcome is delivered when the crew finishes, through _deliver_crew_outcome ->
# _deliver_or_queue_cm_report, which survives a reconnect. This text tells the
# model to say it is on it WITHOUT inventing an answer the crew has not given.
_CREW_BG_ACK = (
    "The crew is now working on this in the background. Tell the user, in your own "
    "words, that you have put the crew on it and will report back the moment they "
    "answer. Do NOT claim the crew has answered yet, do NOT invent their findings, "
    "and do NOT call the crew tool again for this request."
)

# Model-facing text for a task the owner cancelled WHILE it was running (the X
# on the HUD row, "cancel that", or a correction). Most tool bodies sit in
# run_in_executor and cannot be interrupted, so the work may well have finished
# -- but its result is unwanted and must not be reported or retried. The owner
# has already had their feedback (the row went terminal, or Era said so), which
# is why this asks for no apology: narrating a cancel at length recreates
# exactly the noise the owner cancelled to stop.
_CANCEL_DISCARD = (
    "Not completed: the user cancelled this task while it was running. Whatever it "
    "had already started may be half-done. Do NOT retry it, do NOT report a result "
    "for it, and do NOT apologise at length -- the user already knows it stopped. "
    "If they gave a replacement instruction, that instruction is what matters now."
)

# How long a slow tool may hold the receive loop before it is detached and the
# model answered. Keyed just above each tool's MEASURED median
# (logs/task_trace-*.jsonl, 16 days, 2500+ calls), because these are bimodal:
# spotify_control's median is 976 ms but its p90 is 10.8 s. A name-keyed
# "always background" would ack over ~2000 pause/next/volume calls that already
# return in under a second; a grace discriminates by what actually happened.
_BG_GRACE_S: dict[str, float] = {
    "era_workbench": 1.5,  # Office/REA/network work must not block voice reception.
    "open_app": 1.5,  # Window read-back/catalog discovery must not stall voice reception.
    "system_performance": 1.5,  # PC Manager UIA can stall for minutes.
    "media_coordinator": 1.5,  # Browser/CDP recovery can exceed 45 seconds.
    "self_improve": 0.5,  # median 28ms  / p90 4.6s  / max 68s
    "code_helper": 1.0,  # median 3ms   / p90 61.6s / max 76s  (worst ratio)
    "file_controller": 1.2,  # median 24ms  / p90 588ms / max 68s
    "spotify_control": 1.5,  # median 976ms / p90 10.8s / max 57s
    "netflix_control": 1.5,  # median 831ms / p90 13.7s / max 47s
    "gmail_control": 1.5,  # median 920ms / p90 7.6s  / max 61s
    "web_search": 2.0,  # median 10.1s -- essentially always detaches
    "file_processor": 2.0,  # median 5.8s  / p90 22.0s
    "screen_process": 2.5,  # median 6.3s  / p90 30.7s
}

_WEB_API_GRACE_S = 8.0

# Model-facing text for a tool that outlived its grace. It must not let the
# model invent an outcome: the real one is spoken later by _deliver_tool_outcome
# through the reconnect-surviving path, exactly as the crew lane does.
_BG_ACK_TEMPLATE = (
    "Started: {label}. It is STILL RUNNING and has not finished. Tell the user "
    "briefly that you are on it. Do NOT claim a result, do NOT guess what it "
    "found, and do NOT call this tool again for this request -- the real "
    "outcome is reported to the user automatically the moment it completes."
)

_PROTOCOL_ONE_COMMAND = re.compile(r"\blaunch\s+protocol\s+(?:one|1)\b", re.IGNORECASE)
# After a cycle stages fixes, the user's own "yes"/"confirm" applies them all —
# accepted within this window of hearing the summary (see _SCHED_APPROVE_COMMAND).
_PROTOCOL_ONE_CONFIRM_WINDOW_S = 180.0
# "Launch Protocol Zero" — same rule for the prompt-sync agent, which writes prompt.txt.
_PROTOCOL_ZERO_COMMAND = re.compile(r"\blaunch\s+protocol\s+(?:zero|0)\b", re.IGNORECASE)

# voice_lock: turning the owner-voice check OFF opens Era to every voice, so the
# model's tool choice is never enough — the user must have SAID it (same grounding
# as Protocol One). Only the owner's verified audio reaches the model while the
# lock is on, so a stranger cannot say it; text Era reads from a web page or an
# email is not a user utterance, so injected content cannot either. English + French.
_VOICE_LOCK_TARGET = (
    r"(?:voice\s+(?:recognition|verification|lock|id|identification|authentication|security|check)"
    r"|speaker\s+(?:recognition|verification)"
    r"|reconnaissance\s+vocale|v[eé]rification\s+vocale|verrou\s+vocal)"
)
_VOICE_LOCK_OFF_COMMAND = re.compile(
    r"\b(?:disable|deactivate|turn\s+off|switch\s+off|shut\s+off|stop|pause|remove"
    r"|d[eé]sactiv\w*|arr[eê]t\w*|coup\w*|enl[eè]v\w*)\b(?:\W+\w+){0,3}?\W+"
    + _VOICE_LOCK_TARGET
    + r"|"
    + _VOICE_LOCK_TARGET
    + r"\W+(?:\w+\W+){0,2}?(?:off|disabled|d[eé]sactiv\w*)\b",
    re.IGNORECASE,
)
_VOICE_LOCK_ON_COMMAND = re.compile(
    r"\b(?:enable|activate|reactivate|turn\s+on|switch\s+on|start|resume|restore"
    r"|r?activ\w*|r[eé]activ\w*|remet\w*|rallum\w*)\b(?:\W+\w+){0,3}?\W+"
    + _VOICE_LOCK_TARGET
    + r"|"
    + _VOICE_LOCK_TARGET
    + r"\W+(?:\w+\W+){0,2}?(?:on|enabled|back\s+on|activ\w*)\b",
    re.IGNORECASE,
)
_VOICE_LOCK_WINDOW_S = 60.0

# Session-state tools the MODEL can call on its own initiative. Gemini Live hears
# ambient audio (a recitation, a film) and has shut Era down, flipped standby and
# muted the mic with no user input at all (task_trace user_text "" or a TV phrase).
# Like voice_lock, the model's tool choice is never enough: the user must have SAID
# or TYPED something that plausibly asks for it. Deliberately generous (EN/FR/AR) —
# it exists to refuse "nothing was said" and "something unrelated was said", not
# to second-guess phrasing. Typed, HUD and Telegram inputs pass through the same
# utterance record, so they ground it too.
_STATE_TOOL_ORDERS: dict[str, re.Pattern] = {
    "shutdown": re.compile(
        r"shut\s*(?:down|off|yourself)|shutdown|power\s*(?:off|down)|turn\s+(?:yourself\s+)?off"
        r"|\b(?:close|quit|exit|terminate|goodbye|good\s*bye|bye|good\s*night|see\s+you|reboot"
        r"|restart|re-?launch|that'?s\s+(?:all|it)|i'?m\s+done|ciao"
        r"|ferme\w*|[eé]teins\w*|arr[eê]te\w*|quitte\w*|au\s+revoir|red[eé]marre\w*)\b"
        r"|أغلق|اغلق|اطفئ|أطفئ|وداع|مع السلامة|إلى اللقاء|الى اللقاء|اقفل|أقفل",
        re.IGNORECASE,
    ),
    # PF-045: only the owner's standby words ("standby", "take a break") and the
    # silence orders this tool turns into inactivity. The old ~40 loose words
    # ("thank you", "done", "merci") grounded a standby call on "mes amis" (live
    # 2026-11-18 23:28). The enter_standby branch then checks the WHOLE utterance.
    "standby": re.compile(
        r"stand\s*-?\s*by|\btake\s+a\s+break\b|\b(?:silent|quiet)\b|silencieu",
        re.IGNORECASE,
    ),
    "microphone": re.compile(
        r"\b(?:mic|microphone|mute|unmute|listen\w*|hear\w*|deaf|quiet|silent|activate"
        r"|deactivate|turn\s+(?:on|off)|switch\s+(?:on|off)|micro|[eé]cout\w*|coupe\w*|active\w*"
        r"|r[eé]active\w*)\b|ميكروفون|ميك|اسمع|استمع|اسكت|فعل|فعّل",
        re.IGNORECASE,
    ),
}
_STATE_TOOL_KIND = {
    "shutdown_era": "shutdown",
    "close_era": "shutdown",
    "exit_era": "shutdown",
    "enter_standby": "standby",
    "set_microphone": "microphone",
}
_STATE_TOOL_WINDOW_S = 60.0

# PF-028: starting music needs the owner's words too. Live 2026-11-15 23:06:
# "Open Spotify" made the model add spotify_control play, music the owner never
# asked for (opening Spotify must not start playback, decided 2026-09-25). The
# app's name alone ("Open Spotify") is never an order to play.
_MEDIA_START_ORDER = re.compile(
    r"\b(?:play\w*|music|song|songs|track|tracks|resume|unpause|continue|put\s+on|listen\w*"
    r"|playlist|album|mix|beats?|tune|tunes|radio|jou\w*|mets|musique|chanson\w*|reprend\w*"
    r"|[eé]cout\w*|lance\s+(?:la|une|de\s+la)\s+musique)\b"
    r"|شغل|شغّل|موسيقى|موسيقا|اغنية|أغنية|اغاني|أغاني",
    re.IGNORECASE,
)
_MEDIA_START_ACTIONS = frozenset({"play", "resume", "spotify_play", "spotify_resume"})


def _ungrounded_media_start(era, tool_name: str, args: dict | None) -> str | None:
    """A refusal when the model starts playback nobody asked for, else None."""
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    if action not in _MEDIA_START_ACTIONS:
        return None
    if tool_name == "media_coordinator":
        target = str(args.get("target") or args.get("app") or "").strip().lower()
        if target not in {"", "spotify", "music"}:
            return None
    elif tool_name != "spotify_control":
        return None
    utterances = _user_utterances(era, _STATE_TOOL_WINDOW_S)
    if any(_MEDIA_START_ORDER.search(t or "") for _, t in utterances):
        return None
    heard = _current_user_text(era)
    log_event(
        main_logger, "tool.grounding.refused", "WARNING", tool=tool_name, heard=(heard or "")[:80]
    )
    print(
        f"[era] [GROUNDING] refused {tool_name} {action}: nobody asked for music (heard={heard!r})"
    )
    return (
        "Not done: the user did not ask for music, so playback was NOT started. "
        "Do not start it. Answer only what the user actually asked."
    )


# Granting a per-category HIGH-risk waiver (manage_preference) needs the user's own
# words: a "don't ask / without confirmation" phrase AND the category named.
_RISK_WAIVE_COMMAND = re.compile(
    r"\b(?:stop|quit|no\s+need\s+to|without|skip|don'?t\s+need\s+to|never\s+again)\b"
    r"(?:\W+\w+){0,4}?\W+(?:ask\w*|confirm\w*|permission|approval)"
    r"|\b(?:don'?t|do\s+not)\s+(?:need\s+to\s+)?(?:ask|confirm)\w*"
    r"|\bwithout\s+(?:asking|confirmation|permission)"
    r"|\bne\s+(?:me\s+)?demande\w*\s+plus"
    r"|\bsans\s+(?:me\s+)?(?:demander|confirmation|permission)"
    r"|\barr[eê]te\w*\s+de\s+(?:me\s+)?demander",
    re.IGNORECASE,
)
_RISK_WAIVE_WINDOW_S = 120.0
_RISK_CATEGORY_WORDS = {
    "shutdown": ("shut", "power off", "turn off", "teind", "arrêt", "arret"),
    "restart": ("restart", "reboot", "redémarr", "redemarr"),
    "close": ("close", "closing", "ferme"),
}

# Scheduled tasks (core/task_scheduler.py). Running a task parked for approval and
# waiving a task's confirmation are both decided from the USER's recorded words —
# the same grounding as Protocol One — so neither the model's own tool choice nor
# text read from a page or an email can approve or un-gate a risky scheduled run.
_SCHED_APPROVE_COMMAND = re.compile(
    r"\b(?:yes|yeah|yep|approved?|go\s+ahead|run\s+it|do\s+it|proceed|confirm(?:ed)?"
    r"|(?<!make\s)sure|absolutely|affirmative|allow(?:ed)?|accept(?:ed)?|please\s+do"
    r"|go\s+for\s+it|oui|ouais|vas-y|allez-y|d'accord|c'est\s+bon|approuv\w*|confirme[rz]?"
    r"|okay|ok)\b",
    re.IGNORECASE,
)
_SCHED_APPROVE_WINDOW_S = 60.0
_SCHED_WAIVE_WINDOW_S = 120.0
# A waiver said NAMING a task ("run the movie search without asking"). "wave" is how
# speech-to-text usually spells a spoken "waive"; it only counts next to
# confirmation/approval/it, so "wave goodbye" is not a waiver.
_SCHED_WAIVE_COMMAND = re.compile(
    r"\b(?:waive[ds]?|wave[ds]?\s+(?:the\s+|my\s+)?(?:confirmation|approval|it)"
    r"|without\s+(?:asking|confirmation|confirming|approval|my\s+approval)"
    r"|(?:don'?t|do\s+not|never)\s+ask|no\s+(?:confirmation|approval)"
    r"|no\s+need\s+to\s+ask|stop\s+asking"
    r"|skip\s+(?:the\s+)?(?:confirmation|approval)|sans\s+(?:confirmation|demander)"
    r"|ne\s+(?:me\s+)?demande\s+(?:pas|plus)"
    r"|(?:yes|approve[d]?|confirm(?:ed)?|allow|run\s+it|ok(?:ay)?|oui)\W+(?:always|toujours)"
    r"|always\s+(?:approve|allow|run\s+it|say\s+yes|yes|ok)"
    r"|(?:approve|allow|run)\s+(?:it\s+)?(?:permanently|forever|for\s+good|from\s+now\s+on))\b"
    r"|^\W*(?:always|toujours)\W*$",
    re.IGNORECASE,
)
# "No, I said approve" is a correction, not a refusal — the leading "no" answers
# Era's "I need your approval", it does not negate the approval that follows.
_SCHED_CORRECTION_PREFACE = re.compile(
    r"\b(?:no|nope|non)\W+(?:i\s+(?:already\s+)?(?:said|told\s+you)|j'ai\s+dit)\b",
    re.IGNORECASE,
)
# Scheduling a Protocol run is the user's command only in the same launch/execute
# wording the live gate uses, for THAT Protocol — merely mentioning it is not enough.
_SCHED_PROTOCOL_COMMANDS = {
    "protocol_one": re.compile(
        r"\b(?:launch|run|start|lance\w*)\b(?:\W+\w+){0,4}?\W+protocol[e]?\s+(?:one|1|un)\b",
        re.IGNORECASE,
    ),
    "protocol_zero": re.compile(
        r"\b(?:launch|run|start|lance\w*)\b(?:\W+\w+){0,4}?\W+protocol[e]?\s+(?:zero|0)\b",
        re.IGNORECASE,
    ),
}
# A phrase preceded (within three words) by a negation is not assent: "never run clean
# temp without asking", "don't do it", "je ne suis pas d'accord". Errs toward refusing.
_NEGATION_BEFORE = re.compile(
    r"\b(?:not|never|don'?t|do\s+not|doesn'?t|no|ne|pas|jamais|non)\W+(?:\w+\W+){0,3}$",
    re.IGNORECASE,
)
# Any refusal or deferral word anywhere disqualifies an utterance as an APPROVAL
# (fail closed): "can I approve it later?" is not a yes.
_SCHED_REFUSAL = re.compile(
    r"\b(?:no|nope|not|don'?t|do\s+not|never|skip|cancel|stop|wait|hold|non|pas|jamais"
    r"|annule\w*|attends|later|tomorrow|maybe|perhaps|hang\s+on|plus\s+tard|demain"
    r"|peut-[eê]tre)\b",
    re.IGNORECASE,
)
_SCHED_QUESTION = re.compile(
    r"\?|^\W*(?:can|could|what|why|how|when|should|shall|is|are|was|does|did|will|would"
    r"|est-ce|pourquoi|comment|quand)\b",
    re.IGNORECASE,
)
# A BARE reply (no task named) answers only the task Era announced last, so it must
# be nothing but a reply: every word from this list. "okay thanks", "yeah, what time
# is it", "you never ask before doing things" are not replies to the announcement.
_SCHED_BARE_WORDS = frozenset(
    "yes yeah yep sure ok okay approve approved confirm confirmed go ahead run it do "
    "proceed absolutely affirmative please for oui ouais vas-y allez-y d'accord c'est "
    "bon approuve approuvé confirme i said already told you no nope non j'ai dit "
    "era sir just now then fine alright all right".split()
)
# The WHOLE utterance as a short "yes, and don't ask again" — never a phrase inside a
# longer sentence ("stop asking me questions and play music" is a complaint).
_SCHED_BARE_WAIVE = re.compile(
    r"^(?:(?:era|sir)\W+)?(?:(?:yes|yeah|ok|okay|sure|oui|approved?|confirm(?:ed)?)\W+)?"
    r"(?:and\s+)?(?:always(?:\s+(?:approve|allow|run\s+it|say\s+yes|yes))?|toujours"
    r"|(?:don'?t|do\s+not|never)\s+ask(?:\s+me)?\s+(?:again|anymore)|no\s+need\s+to\s+ask"
    r"|stop\s+asking(?:\s+me)?|(?:waived?|wave)(?:\s+(?:it|the\s+confirmation|confirmation))?"
    r"|(?:run|approve)\s+it\s+(?:always|permanently|forever|from\s+now\s+on)"
    r"|ne\s+(?:me\s+)?demande\s+plus)(?:\W+(?:please|era|sir|thanks))?\W*$",
    re.IGNORECASE,
)
# Creating a task "without asking" waives it only when the same words are about
# scheduling — "summarise this page without asking me questions" is not.
_SCHED_SCHEDULING_WORDS = re.compile(
    r"\b(?:every|each|daily|weekly|nightly|hourly|weekday\w*|schedul\w*|remind\w*"
    r"|at\s+\d|tous\s+les|chaque|programm\w*)\b",
    re.IGNORECASE,
)
_NAME_STOPWORDS = frozenset({"the", "and", "for", "task", "every", "daily", "with", "my", "run"})


def _current_user_text(era) -> str:
    """The utterance in progress. Speech-to-text streams a voice turn in chunks and
    _last_user_text holds only the LATEST chunk ("…movie search"), so a mid-turn tool
    call must read the joined in-flight buffer — reading the chunk is why "waive
    confirmation for evening movie search" was never seen whole. Module-level: tests
    bind the gates onto SimpleNamespace stubs."""
    inflight = getattr(era, "_inflight_user_text", "") or ""
    return (inflight or getattr(era, "_last_user_text", "") or "").strip()


def _user_utterances(era, window_s: float, since_ts: float = 0.0) -> list[tuple[float, str]]:
    """The user's own utterances, oldest first, each stamped with when it was SAID.
    The in-progress voice turn is stamped with when the user STARTED it, never "now":
    a sentence begun before a task parked is not a reply to its announcement."""
    now = time.time()
    texts = list(getattr(era, "_recent_user_texts", []))
    inflight = (getattr(era, "_inflight_user_text", "") or "").strip()
    if inflight and (not texts or texts[-1][1] != inflight):
        texts.append((getattr(era, "_inflight_started_at", 0.0) or 0.0, inflight))
    return [(ts, t) for ts, t in texts if now - ts <= window_s and ts >= since_ts]


def _ungrounded_state_change(era, tool_name: str, relaunch: bool = False) -> str | None:
    """A refusal string when the model called a session-state tool (shutdown,
    standby, mic) without the user having asked for it, else None.

    "Asked" = an utterance in the last minute that matches the tool's order
    pattern. An empty record (the model acted on ambient audio or an internal
    turn) and an unrelated one (a recitation on the TV) both refuse. Phrased
    without "error"/"failed" so it is not logged as a tool failure. Module-level
    like _current_user_text: tests bind the gates onto SimpleNamespace stubs."""
    kind = _STATE_TOOL_KIND.get(tool_name)
    if kind is None:
        return None
    pattern = _STATE_TOOL_ORDERS[kind]
    utterances = _user_utterances(era, _STATE_TOOL_WINDOW_S)
    if any(pattern.search(t or "") for _, t in utterances):
        return None
    if kind == "shutdown":
        from core.era_relaunch import is_reboot_command

        is_order = getattr(era, "_is_reboot_order", is_reboot_command)
        if any(is_order(t or "") for _, t in utterances):
            return None
    heard = _current_user_text(era)
    log_event(
        main_logger, "tool.grounding.refused", "WARNING", tool=tool_name, heard=(heard or "")[:80]
    )
    print(f"[era] [GROUNDING] refused {tool_name}: no matching spoken order (heard={heard!r})")
    if relaunch and heard:
        # She often says "Restarting" before this returns, so silence here reads as a
        # restart that never comes. The user spoke: tell them it did not happen.
        return (
            f"Not done: Era did NOT restart. The words came through as {heard[:60]!r}, not "
            "'reboot Era One'. If you said you were restarting, correct that now in one short "
            "sentence: say you did not restart, and ask the user to repeat it or use the "
            "restart button on the HUD."
        )
    return (
        f"Not done: nobody asked for {tool_name.replace('_', ' ')} just now. "
        "That call came from background audio or an internal turn. "
        "Say nothing and carry on."
    )


def _not_a_standby_order(era) -> str | None:
    """PF-045: a refusal unless the owner's WHOLE utterance is "standby" or "take a
    break" — the one in progress, or the last one said within 15 s (the transcript
    can land just before the call). Live 2026-11-18 23:28 the model slept Era on
    "mes amis"; the loose grounding words let it through."""
    from core.attention import is_standby_order

    heard = [_current_user_text(era)] + [t for _, t in _user_utterances(era, 15.0)[-1:]]
    if any(is_standby_order(t) for t in heard):
        return None
    log_event(
        main_logger,
        "tool.grounding.refused",
        "WARNING",
        tool="enter_standby",
        heard=(heard[0] or "")[:80],
        reason="not a standby order",
    )
    print(f"[era] [GROUNDING] refused enter_standby: not a standby order ({heard[0]!r})")
    return (
        "Not done: standby happens only when the user says 'standby' or 'take a break'. "
        "Do not mention standby; carry on the conversation."
    )


def _sched_clean(text: str) -> str:
    return _SCHED_CORRECTION_PREFACE.sub(" ", (text or "").lower()).strip()


def _sched_refused(low: str) -> bool:
    """A refusal/deferral word or a question — outside any waiver phrase, where "don't
    ask again" / "no need to ask" MEAN yes."""
    return bool(_SCHED_REFUSAL.search(_SCHED_WAIVE_COMMAND.sub(" ", low))) or bool(
        _SCHED_QUESTION.search(low)
    )


_CM_STATUS_QUESTION = re.compile(
    r"^\W*(?:what(?:'s| is)\s+)?(?:the\s+)?(?:(?:cm|community[- ]manag\w*)\s+)?"
    r"(?:job\s+|jobs\s+|task\s+|tasks\s+)?(?:status|progress|running|what'?s\s+running)"
    r"(?:\s+of\s+(?:the\s+)?(?:cm|community[- ]manag\w*)?\s*(?:jobs?|tasks?)?)?\W*$",
    re.IGNORECASE,
)

_BRIDGE_QUESTION = re.compile(r"\b(?:spotify\s+)?bridge\b|spicetify", re.IGNORECASE)


def _note_bridge_is_off(era, tool_name: str, result):
    """Append the bridge's real state to a Spotify reading when the owner asked about
    the bridge. Live 2026-11-17 "is the Spotify bridge connected?" -> spotify_control
    status -> "Yes, sir, it is", three times, though the bridge is switched off: a
    context note lost to the fresh tool result, so the fact rides in the result."""
    try:
        if not tool_name.startswith("spotify"):
            return result
        asked = getattr(era, "_inflight_user_text", "") or getattr(era, "_last_user_text", "")
        if not _BRIDGE_QUESTION.search(asked or ""):
            return result
        from app.tool_declarations import _withheld

        if "spotify_bridge" not in _withheld:
            return result
        note = (
            " NOTE: the Spotify bridge (Spicetify era-bridge) is SWITCHED OFF and not"
            " connected; this reading came from the Spotify desktop app."
        )
        if isinstance(result, ToolResult):
            from dataclasses import replace

            return replace(result, message=(result.message or "") + note)
        return f"{result}{note}"
    except Exception:
        return result


_MEDIA_DEDUPE_S = 10.0


def _media_start_key(tool: str, params: dict) -> tuple[str, str] | None:
    """(platform, normalized query) for a call that STARTS named media, else None."""
    action = str(params.get("action") or "").lower()
    query = params.get("query") or params.get("value") or ""
    if tool == "spotify_control" and action in ("search", "play_uri"):
        platform = "Spotify"
    elif tool == "youtube_video" and action in ("play", "search"):
        platform = "YouTube"
    elif tool == "netflix_control" and action == "search" and params.get("_auto_play"):
        platform = "Netflix"
    else:
        return None
    q = re.sub(r"^(?:some|a\s+bit\s+of|any)\s+", "", str(query).strip().lower())
    q = re.sub(r"\s+", " ", q)
    return (platform, q) if q else None


def _forget_session_on_exit(era) -> None:
    """Drop the saved Gemini resumption handle on a plain shutdown (never raises)."""
    try:
        manager = getattr(era, "session_manager", None)
        if manager is not None:
            manager.clear_token()
            manager.save_to_disk()
            print("[era] [SHUTDOWN] session handle discarded (plain shutdown).")
    except Exception as e:
        print(f"[era] [SHUTDOWN] could not discard the session handle: {e}")


def _typed_cancel_reply(low: str) -> bool:
    """A typed reply that cancels a pending confirmation: a refusal WORD, never a bare
    question. Unlike _sched_refused, a question is NOT a cancel here — live 2026-11-17
    "what scheduled tasks do I have running?" was swallowed as a cancel and never
    answered. "Yes"-type approvals are checked by the caller first."""
    low = (low or "").lower()
    return bool(_SCHED_REFUSAL.search(_SCHED_WAIVE_COMMAND.sub(" ", low))) and not bool(
        _SCHED_APPROVE_COMMAND.search(low)
    )


def _sched_affirms(pattern: re.Pattern, low: str) -> bool:
    return any(not _NEGATION_BEFORE.search(low[: m.start()]) for m in pattern.finditer(low))


def _is_bare_waiver(text: str) -> bool:
    """Nothing but "always" / "yes, don't ask again" / "stop asking" / "waive it"."""
    return bool(_SCHED_BARE_WAIVE.match(_sched_clean(text)))


def _is_bare_approval(text: str) -> bool:
    """Nothing but a reply: "yes", "approved", "go ahead", "no, I said approve"."""
    if _is_bare_waiver(text):
        return True
    low = _sched_clean(text)
    words = re.findall(r"[a-zàâéèêîôûç'-]+", low)
    return (
        bool(words)
        and len(words) <= 8
        and all(w in _SCHED_BARE_WORDS for w in words)
        and not _sched_refused(low)
        and bool(_SCHED_APPROVE_COMMAND.search(low))
    )


# Whole replies that also approve a pending HIGH-risk action: the affirmative
# CONFIRMATION_REPLIES (core/input_safety_gate.py) _is_bare_approval does not
# cover. Exact match only — "go on, open the browser" is a new order.
_CONFIRM_EXTRA_REPLIES = frozenset(
    {
        "yup",
        "of course",
        "carry on",
        "go on",
        "do it now",
        "allez",
        "vas y",
        "bien sur",
        "bien sûr",
        "fais-le",
        "fais le",
    }
)


def _approves_pending(text: str) -> bool:
    """The WHOLE reply is a yes ("yes", "go ahead", "oui, vas-y", "of course").

    Stricter than _is_bare_approval on one point: a bare waiver with no yes in it
    ("always", "stop asking") is not an answer to "delete this, sir?"."""
    low = _sched_clean(text)
    if _is_bare_approval(text) and _SCHED_APPROVE_COMMAND.search(low):
        return True
    low = re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", low)).strip()
    low = re.sub(r"^(?:era|sir)\s+|\s+(?:era|sir|please)$", "", low)
    return low in _CONFIRM_EXTRA_REPLIES


def _user_approved_pending(era, pending) -> bool:
    """The USER said yes to `pending` after it was armed — never the model.

    Guards both ways a model can approve a HIGH-risk action it armed itself
    (confirm_pending_confirmation, or re-calling the tool with confirmed=true and
    the id it was shown), including when steered by injected tool output. Only
    the LATEST utterance counts — it is what this turn answers, so "yes ... no,
    don't" is a no — and it must have been begun at/after arming. A spoken "yes"
    is usually still in flight when the model calls confirm, so the in-progress
    utterance counts, stamped with when the user STARTED it. (Not via
    _user_utterances: it drops an in-flight "yes" equal to the last recorded one,
    and that older "yes" predates the arming.)"""
    if pending is None:
        return False
    from core.approval_execution import remote_approves

    if remote_approves(pending):
        return True
    recent = list(getattr(era, "_recent_user_texts", None) or [])
    said_at, text = recent[-1] if recent else (0.0, "")
    # Every complete input clears the in-flight buffer, so when one is present
    # it is always the newest thing the user said.
    inflight = (getattr(era, "_inflight_user_text", "") or "").strip()
    if inflight:
        said_at, text = (getattr(era, "_inflight_started_at", 0.0) or 0.0), inflight
    return bool(text) and said_at >= pending.created_at and _approves_pending(text)


def _name_tokens(name: str) -> tuple[str, ...]:
    words = [w for w in re.findall(r"[a-z0-9éèàù]+", name.lower()) if len(w) >= 3]
    tokens = tuple(w for w in words if w not in _NAME_STOPWORDS)
    return tokens or (name.lower().strip(),)


def _names_task(low: str, name: str) -> bool:
    """MOST of the task's name said as whole words ("the movie search" names
    evening_movie_search; "search for pizza" does not). A word may carry a short
    suffix ("cleanup" says "clean"), never a long one ("temperature" is not "temp")."""
    tokens = _name_tokens(name)
    words = re.findall(r"[a-z0-9éèàù]+", low)
    hits = sum(1 for t in tokens if any(w.startswith(t) and len(w) - len(t) <= 3 for w in words))
    return hits * 2 > len(tokens)


def _sched_named(text: str, pattern: re.Pattern, name: str) -> bool:
    """`pattern` said as assent (not negated, refused or asked), naming the task."""
    low = _sched_clean(text)
    return _sched_affirms(pattern, low) and not _sched_refused(low) and _names_task(low, name)


def _scheduled_by_voice(tool: str) -> bool:
    """True while running the ONE scheduled step whose EXACT action the user
    authorised by naming the Protocol. That stands in for the live command.

    Scoped to the step in flight, not to the task: a chain runs several tool calls
    under a single authorization, and "the user said launch Protocol One" must not
    answer for a Protocol Zero link sitting beside it. Both halves are checked —
    the grant still covers the whole chain's fingerprint (so editing any link
    revokes it), AND this step must itself be the gated action it was granted for.
    """
    from core.task_scheduler import (
        VOICE_GATED_ACTIONS,
        current_step,
        current_task,
        norm_action,
        voice_authorized_for,
    )

    task, step = current_task.get(), current_step.get()
    if not task or not step or step.get("tool") != tool:
        return False
    if VOICE_GATED_ACTIONS.get(tool) != norm_action(step.get("args")):
        return False  # this step is not the gated action the grant was given for
    return voice_authorized_for(task, tool)


def finalize_tool_result(
    era,
    tool_name: str,
    parameters: dict,
    raw,
    *,
    risk_level: str = "LOW",
    requires_confirmation: bool = False,
    duration_ms: int | None = None,
) -> ToolResult:
    """Convert, verify, tag and log ONE executed tool call.

    Module-level, not a mixin method, because callers and tests bind the
    dispatcher's individual steps onto duck-typed objects: a method call would
    make "has the finalizer" a requirement of every test double. `era` is only
    used for the optional caught-exception hook.

    Until 2026-11-11 the `confirm_pending_confirmation` branch returned
    `_execute_tool_raw` directly, so the action that needed approval most — a
    delete, a send, an overwrite — was the one action whose postcondition was
    never checked. Both paths now end here.
    """
    if isinstance(raw, str) and ("Unknown tool:" in raw or "Unknown action:" in raw):
        return ToolResult.fail(
            message=raw,
            error_type="UnknownTool",
            tool_name=tool_name,
            action_name=parameters.get("action"),
            duration_ms=duration_ms,
        )

    result = ToolResult.from_any(
        raw,
        tool_name=tool_name,
        action_name=parameters.get("action"),
        duration_ms=duration_ms,
    )

    result.risk_level = risk_level
    result.requires_confirmation = requires_confirmation

    result = verify_tool_result(result, tool=tool_name, parameters=parameters)
    # Evidence-gated completion (core/completion_contract.py): what the runtime
    # can PROVE rides with the result, so neither the model nor the speech layer
    # has to infer success from the absence of an exception.
    try:
        from core.completion_contract import annotate

        result = annotate(result, tool_name, parameters.get("action"))
    except Exception as _completion_err:  # never change an outcome here
        print(f"[era] [COMPLETION] annotation skipped for {tool_name}: {_completion_err}")

    recovery = result.data.get("recovery") if isinstance(result.data, dict) else None
    if isinstance(recovery, dict) and recovery.get("strategy") == "same_read_once":
        recovery["verification"] = result.verification_status
        if not result.success and recovery.get("outcome") == "returned_success":
            recovery["outcome"] = "verification_failed"
        log_event(
            main_logger,
            "tool.recovery.verified",
            "INFO",
            tool=tool_name,
            action=parameters.get("action"),
            success=result.success,
            recovery=recovery,
        )

    if not result.success:
        print(
            f"[era] [ERROR] {tool_name} failed: {result.error_type} - {result.short_message(200)}"
        )
        log_event(
            main_logger,
            "tool.execution.failed",
            "ERROR",
            tool=tool_name,
            action=parameters.get("action"),
            error_type=result.error_type,
            # Without the message, self_diagnosis could only guess at causes
            # ("2x tool=file_controller") — and a cancelled confirmation looks
            # identical to a crash. The message is what tells them apart.
            error=result.short_message(200),
            duration_ms=result.duration_ms,
        )
        log_caught = getattr(era, "_log_caught_exception", None)
        if callable(log_caught):
            log_caught(tool_name, parameters, result)

    return result


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

    # ── Tool recovery: arm on failure, flush at turn boundary (board item 19) ──
    @property
    def _recovery(self) -> TurnRecovery:
        """Lazily create the turn's failure store.

        The store is safe to call at any time, in any order, any number of times.
        Nothing here may raise — it sits in the tool funnel and in the audio
        receive loop; instrumentation that can throw takes the voice with it.
        """
        # Tests pass the store as `self.tool_recovery`; production uses `_tool_recovery`.
        # An explicitly set None (e.g., `h.tool_recovery = None`) means "no store:
        # announce immediately" — the property returns None in that case.
        if hasattr(self, "tool_recovery"):
            return self.tool_recovery  # may be None explicitly
        store = getattr(self, "_tool_recovery", None)
        if store is None:
            store = self._tool_recovery = TurnRecovery()
        return store

    def _note_tool_failure(
        self, tool: str, args: dict | None, error_type: str, message: str
    ) -> None:
        """Arm a notice instead of speaking one.

        Deferring the announcement gives a workaround (retry, re-route, model
        trying another tool) room to cancel it before the user hears anything.
        """
        action = str((args or {}).get("action") or "")
        store = self._recovery
        if store is None:
            self.speak_error(tool, message)
        else:
            store.note_failure(tool, action, error_type, message)

    def _note_tool_success(self, tool: str, args: dict | None) -> None:
        """The workaround worked: drop every armed notice for this tool+action.

        Every one, not the last: a tool retried twice before succeeding armed
        two notices, and announcing either would report a failure the user has
        no reason to hear about.
        """
        action = str((args or {}).get("action") or "")
        store = self._recovery
        if store is not None:
            store.note_success(tool, action)

    def _flush_failure_notices(self) -> None:
        """Drain the turn. Speaks any unrecovered failures; stale ones are dropped.

        Called at BOTH turn boundaries — the cloud turn in AudioIOMixin._receive_audio
        and the local-brain turn in LocalFallbackMixin._local_turn. A missed flush
        must never surface a failure in a later, unrelated conversation.
        """
        store = self._recovery
        if store is None:
            return
        notices = store.take()
        for n in notices:
            try:
                self.speak_error(n.tool, n.message)
            except Exception:
                pass  # spoken or not, the store is drained

    def _realtime_input_config(self) -> types.RealtimeInputConfig | None:
        """Turn-endpointing config for the Live session (Phase 3 turn-taking).

        Returns None when disabled, which omits the field and keeps Gemini's own
        defaults. The 'low'/'high' strings from get_endpointing_config map to the
        genai enums here so core/vad.py needs no google-genai import.
        """
        from core.vad import get_endpointing_config

        ep = get_endpointing_config()
        if not ep["enabled"]:
            return None
        start = (
            types.StartSensitivity.START_SENSITIVITY_HIGH
            if ep["start_sensitivity"] == "high"
            else types.StartSensitivity.START_SENSITIVITY_LOW
        )
        end = (
            types.EndSensitivity.END_SENSITIVITY_LOW
            if ep["end_sensitivity"] == "low"
            else types.EndSensitivity.END_SENSITIVITY_HIGH
        )
        return types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=start,
                end_of_speech_sensitivity=end,
                prefix_padding_ms=ep["prefix_ms"],
                silence_duration_ms=ep["silence_ms"],
            )
        )

    def _build_config(self) -> types.LiveConnectConfig:

        memory = load_memory()
        mem_str = format_memory_for_prompt_ranked(memory, top_k=15)
        try:
            from memory.memory_context import prompt_context

            mem_str += prompt_context()
        except Exception as exc:
            log_event(
                main_logger,
                "memory.context.retrieve_failed",
                "WARNING",
                error_type=type(exc).__name__,
            )
        sys_prompt = _load_system_prompt()

        now = datetime.now()
        local_now = now.astimezone()
        _off = local_now.strftime("%z")
        time_str = (
            local_now.strftime("%A, %B %d, %Y - %I:%M %p")
            + f" LOCAL time (UTC{_off[:3]}:{_off[3:]}) at session start; the owner lives in this"
            " time zone, so never answer in UTC. For the time NOW call computer_control clock."
        )

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

        # A gated-off tool is invisible to the model, which then invents its state:
        # live 2026-11-17 "is the Spotify bridge connected?" -> "Yes, sir, it is",
        # while the bridge was switched off. Name what is off so she can say so.
        try:
            from app.tool_declarations import _withheld as _off_tools
        except Exception:
            _off_tools = set()
        _off_labels = {
            "spotify_bridge": (
                'The Spotify bridge (the Spicetify "era-bridge") is SWITCHED OFF and not '
                'connected. If asked "is the Spotify bridge connected?", answer no, it is off; '
                "Spotify itself still works through the desktop app."
            ),
        }
        off_note = (
            "[SWITCHED-OFF FEATURES]\n"
            + "".join(
                "- "
                + _off_labels.get(name, f"{name} is switched off; if asked, say it is off.")
                + "\n"
                for name in sorted(_off_tools)
            )
            + "\n"
            if _off_tools
            else ""
        )

        # Live 2026-11-17 "what's your wake word?" was answered "Era" (no tool) while
        # the configured word was "shazam". State it.
        try:
            from app.wake import WAKE_CFG as _wake

            wake_note = (
                f'[WAKE WORD]\nYour standby wake word is "{_wake.word}"'
                + (f" (aliases: {', '.join(_wake.aliases)})" if _wake.aliases else "")
                + '. "Era" is your name; it opens the listening window but does not wake standby.\n\n'
            )
        except Exception:
            wake_note = ""

        os_vision_ctx = (
            off_note + wake_note + f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n\n"
            f"[REAL-TIME SYSTEM EYE (OS AWARENESS)]\n"
            f'User Foreground Focus: "{foreground_window}"\n'
            f"All Open Applications & Active Web Tabs:\n"
            f"{chr(10).join(running_applications) if running_applications else '- None'}\n\n"
            f"[LIVE MEDIA TELEMETRY STATES]\n"
            f"- Spotify Application Open/Running: {spotify_active}\n"
            f"- YouTube Browser Content Present: {youtube_active}\n"
            f"- Window presence/title does not verify playback; use fresh OS observations or a live status tool.\n\n"
            f"CONTEXTUAL RULES (media routing itself is in the main prompt: generic music controls go to spotify_control, never YouTube/Netflix, and never ask 'Spotify or YouTube?'):\n"
            f"1. If the user says 'close this app' or 'minimize this', extract \"{foreground_window}\" as the target parameter for window_control.\n"
            f"2. NEVER use screen_process (vision) to verify media state. Vision costs 12+ seconds and is unnecessary:\n"
            f"   - To check if Spotify is playing, a playlist/song actually started, or the window state: call spotify_control with action='status'. It reads the real Windows audio session instantly.\n"
            f"   - Fresh post-command live_app_state observations outrank command receipts and this older context. Unknown telemetry is unverified; never infer playback from a window title.\n"
            f"3. Window operations on Spotify ('minimize spotify', 'maximize spotify', 'focus spotify') target the DESKTOP APP window. Use spotify_control action='minimize' for minimize, or window_control with title='Spotify' for other window actions. NEVER open the Spotify web player (open.spotify.com) for these commands — the desktop app is already running.\n"
            f"4. Trust window_control's returned message: it verifies the real window state after acting. Do not follow a successful window action with another tool call to 'check' it.\n\n"
        )
        try:
            from core.system_awareness import format_media_observations

            awareness_ctx = (
                "\n[PHASE-1 AWARENESS LAYER]\n"
                + format_window_context()
                + "\n\nRunning process snapshot:\n"
                + format_running_apps(limit=35)
                + format_media_observations()
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
            "- stale audio from reconnect\n"
            "- song lyrics, singing, music, or TV / video / film dialogue playing nearby\n"
            "- speech clearly meant for someone else (a phone call, another person)\n"
            "Treat intelligible speech as media only when there is evidence it is playback\n"
            "or clearly meant for someone else. Low volume, headset distortion, a short\n"
            "command, or a pause alone is not evidence of background media. A request\n"
            "addressed to Era deserves a response or clarification, even when quiet.\n"
            "While awake, do not require the user to say your name on every request\n"
            "or after a period of inactivity. Ordinary intelligible requests deserve\n"
            "an answer; ask briefly if unclear instead of silently ignoring them.\n"
            "Never answer actual media with 'Sorry, I did not catch that' — that phrase is\n"
            "only for the user's own unclear request (TIER 3).\n\n"
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
            '"Era One online." Only after reconnection, never before a restart.\n'
            "For a request to reboot or restart yourself, call shutdown_era with relaunch=true immediately. Never merely promise to restart.\n"
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
        try:
            from core.task_lessons import format_lessons_for_prompt

            lessons_str = format_lessons_for_prompt()
            if lessons_str:
                parts.append(lessons_str)
        except Exception as _lessons_err:
            print(f"[era] [LESSONS] prompt injection skipped: {_lessons_err}")

        # ── What her last tool calls REALLY did ──
        # The session is rebuilt every couple of minutes; without this she
        # restarts blind to whether "open Instagram" ever worked, and answers
        # "did you do it?" from a hopeful reading of the last reply.
        try:
            from core.task_trace import recent_tool_calls, render_recent_calls

            _recent_calls = recent_tool_calls(5, max_age_s=900)
            if _recent_calls:
                parts.append(
                    "[RECENT TOOL OUTCOMES - the runtime's record, newest last]\n"
                    "Only CONFIRMED proves an effect. FAILED / NOT CONFIRMED means it did not "
                    "demonstrably happen: say so, never claim it. 'Era actually ran' means the "
                    "router swapped the tool. For anything older or unclear, call "
                    "protocol_one(action='recent').\n" + render_recent_calls(_recent_calls)
                )
        except Exception as _recent_err:  # noqa: BLE001
            print(f"[era] [OUTCOMES] context skipped: {_recent_err}")

        parts.append(ANTI_HALLUCINATION_INSTR)

        # ── Live audio-routing state (Star Glasses) ──
        # Detected fresh each connect so the volume/routing policy in prompt.txt
        # is applied against the ACTUAL hardware, not a hard-coded assumption.
        try:
            from core.audio_devices import routing_summary

            parts.append(routing_summary())
        except Exception as _ar_err:
            print(f"[era] [AUDIO] routing summary skipped: {_ar_err}")

        parts.append(sys_prompt)

        # ── Session resumption token (if available) ──
        token = self.session_manager.get_resumption_token()
        resumption_config = types.SessionResumptionConfig()
        if token:
            resumption_config.handle = token

        # ── Unlimited session length ──
        # A Live session without this dies when its context window fills, and
        # Era fills one FAST: the system prompt below is the tool declarations
        # plus long-term memory plus prefs plus the vault plus the recap, and
        # then every second of audio in both directions is spent on top of it.
        # That is what the "Gemini rotates us every ~2.5 minutes" 1008 actually
        # is — not a policy rotation on a timer, a context window running out —
        # and every one of those is a reconnect the user hears as Era going
        # quiet mid-sentence.
        #
        # Sliding-window compression makes the server drop the oldest turns
        # instead of closing the socket, so one conversation can run for hours
        # on a single session. It is the cheapest fix available for the drops
        # themselves: the resumption handle repairs a session that died, this
        # stops it dying.
        compression_config = types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        )

        # ── Conversation continuity (only when Gemini can't do it itself) ──
        # With a handle, the server still holds the conversation and replaying
        # it here would duplicate context and invite Era to repeat herself.
        # WITHOUT one — a cold start, an expired handle (15 min), a rejected
        # one after a quota drop — the session used to begin knowing only the
        # system prompt and long-term memory, with the last fifteen minutes of
        # conversation simply gone. That gap is what "she loses track of what I
        # told her earlier" actually was, most of the time.
        if not token:
            try:
                recap = self.conversation_log.recap_block()
                if recap:
                    parts.append(recap)
                    print("[era] [CONTEXT] carried the recent conversation into a fresh session")
            except Exception as _recap_err:
                print(f"[era] [CONTEXT] recap skipped: {_recap_err}")

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
        realtime_input_config = self._realtime_input_config()

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
            # Language hints (core/voice_mode.input_language_codes): without
            # them Gemini transcribed English as Japanese on weak Bluetooth audio.
            input_audio_transcription=(
                types.AudioTranscriptionConfig(language_codes=input_language_codes())
                if input_language_codes()
                else {}
            ),
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=resumption_config,
            context_window_compression=compression_config,
            realtime_input_config=realtime_input_config,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    # config/voice.json "gemini_voice" (default Algenib); the 30
                    # choices are core/voice_mode.GEMINI_VOICES. Applies per connect.
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=gemini_voice_name())
                )
            ),
        )

    def _trace_cancelled_call(self, name: str, fc, result) -> None:
        """Record an abandoned task in logs/task_trace-*.jsonl. Never raises.

        core/task_trace.py only ever wrote on completion, so before this a
        cancelled task left no trace at all -- and "how much did the cancel
        actually stop?" is the first question after one. The requested args are
        kept so a mishearing ("hard track" for "next track") is visible in the
        log that explains why the owner cancelled.
        """
        try:
            from core.task_trace import trace

            routed = getattr(self, "_trace_routed", None) or (name, {})
            trace(
                "tool",
                app="era",
                user_text=getattr(self, "_inflight_user_text", "")
                or getattr(self, "_last_user_text", ""),
                requested=name,
                requested_args=fc_args_to_dict(fc),
                tool=routed[0],
                args=routed[1],
                safety=getattr(self, "_trace_safety", None),
                ok=False,
                error_type="Cancelled",
                result=(result.short_message(300) if result is not None else "cancelled"),
                duration_ms=getattr(result, "duration_ms", None),
                verification=getattr(result, "verification_status", None),
            )
        except Exception:
            pass

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name or "unknown"
        from app.codemode_runtime import owns_reply

        if owns_reply() and name != "code_mode":
            return build_function_response(
                fc,
                ToolResult.fail(
                    "CodeMode owns this conversation; the parallel model tool did not run.",
                    error_type="ReplyOwned",
                ),
            )
        print(f"[era] [TOOL] {name}")
        log_event(main_logger, "tool.call.received", "INFO", tool=name, params=fc_args_to_dict(fc))
        # HUD Live Activity: the real call, as it enters and leaves this gate.
        hud_args = fc_args_to_dict(fc)
        import uuid

        claim_id = f"live-{uuid.uuid4().hex}"
        token, existing = REGISTRY.claim_unique(claim_id, name, hud_args)
        if existing is not None:
            log_event(main_logger, "tool.duplicate.in_flight", "INFO", tool=name, cid=existing.cid)
            return build_function_response(
                fc,
                ToolResult.ok(
                    message=f"This {name} task is already running in Live Activity. No duplicate was started.",
                    data={"alreadyRunning": True, "liveTaskId": existing.cid},
                ),
                types.FunctionResponse,
            )
        try:
            hud_call = hud.tool_started(claim_id, name, hud_args)
        except BaseException:
            REGISTRY.finish(claim_id)
            raise
        # One cancel token per task, keyed by the HUD row's id -- the only task
        # id that already reaches the browser, so the X on that row can address
        # it. The ContextVar carries it down the coroutine chain; crossing into
        # a worker thread needs cancel_registry.offload(), because
        # run_in_executor does not copy contextvars.
        if hud_call != claim_id:
            REGISTRY.rekey_claim(token, hud_call)
        token_reset = CURRENT_TOKEN.set(token)
        self.ui.set_state("THINKING")
        self.session_manager.transition_to(
            EraState.PROCESSING, reason="tool_call", metadata={"tool": name}
        )

        # ── Crew 'ask': never awaited on the receive loop ───────────────────
        # A crew turn runs for seconds to minutes. Awaiting it here keeps the
        # `async for ... in session.receive()` loop suspended, so Gemini's
        # keepalive ping goes unanswered and the session is torn down (1011)
        # mid-call -- after which the result reaches the HUD (fed straight from
        # the ToolResult) but not the model (its channel is gone), so Era never
        # reports the outcome. Hand the turn to a background task, like the CM
        # lane does for its long jobs, and answer the model NOW; the real
        # outcome is spoken when the crew finishes, through a path that survives
        # a reconnect (see _start_background_crew_ask / _deliver_crew_outcome).
        if self._crew_ask_should_background(name, hud_args):
            self._start_background_crew_ask(fc, hud_call, hud_args)
            if not self.ui.muted and not self._standby_active:
                self.ui.set_state("LISTENING")
            # The token stays REGISTERED so the owner can still cancel the crew
            # ask from its HUD row; the worker retires it in its own finally.
            # Only the ContextVar is released, because this coroutine is done.
            CURRENT_TOKEN.reset(token_reset)
            return build_function_response(
                fc, ToolResult.ok(message=_CREW_BG_ACK), types.FunctionResponse
            )

        # In-flight count: a planned GoAway renewal must never close the socket
        # while an action is mid-execution, because the tool result would have
        # nowhere to go and the model would be left believing it never ran.
        # Counted rather than a bool — tool batches can overlap.
        self._tools_in_flight = getattr(self, "_tools_in_flight", 0) + 1
        result = None
        detached = False
        try:
            # ── Slow tools: answer within the grace, then detach ────────────
            # Awaiting a slow tool here keeps `async for ... session.receive()`
            # suspended, so nothing the owner says is READ BACK until it
            # returns. That is exactly why a mid-task correction was
            # impossible: her words reached Gemini, but the reply could not be
            # processed and the abort check on the partial transcript could not
            # fire. Detaching the slow ones keeps the socket draining, which is
            # what lets "no, I meant the next track" land while the wrong
            # search is still running. Fast calls never reach the deadline, so
            # nothing about them changes.
            # getattr: detaching is an enhancement layered on execution, so a
            # partial Era (the stubs several tests build) must fall back to
            # running inline rather than fail. None here == "stay inline".
            _grace_of = getattr(self, "_background_grace", None)
            grace = _grace_of(name, hud_args) if _grace_of is not None else None
            if grace is None:
                result = await self._execute_tool_result(fc)
            else:
                finished, result, task = await run_with_grace(self._execute_tool_result(fc), grace)
                if not finished:
                    detached = True
                    # Declared so the log can match a call that has no inline
                    # outcome: its real result lands later, in the trace.
                    log_event(
                        main_logger, "tool.execution.detached", "INFO", tool=name, grace_s=grace
                    )
                    REGISTRY.attach_task(hud_call, task)
                    self._spawn_background_deliver(fc, hud_call, hud_args, task)
        finally:
            # Released even when detached: this counter only protects the tool
            # RESPONSE from a GoAway closing the socket under it, and the ack
            # below is that response. Holding it for a 60 s detached job would
            # block session renewal for a minute.
            self._tools_in_flight = max(0, getattr(self, "_tools_in_flight", 1) - 1)
            CURRENT_TOKEN.reset(token_reset)
            if not detached:
                REGISTRY.finish(hud_call)  # the worker retires a detached one
                if result is None:  # raised/cancelled: never leave the row "running"
                    hud.tool_finished(
                        hud_call,
                        name,
                        hud_args,
                        ToolResult(success=False, error="No result (raised or cancelled)"),
                    )

        if detached:
            if not self.ui.muted and not self._standby_active:
                self.ui.set_state("LISTENING")
            return build_function_response(
                fc,
                ToolResult.ok(message=_BG_ACK_TEMPLATE.format(label=name)),
                types.FunctionResponse,
            )

        # ── Soft cancel: the owner abandoned this while it ran ──────────────
        # Most tool bodies sit in run_in_executor, a thread Python cannot
        # interrupt, so the work finished anyway. The result is still unwanted.
        # Discard it rather than act on it: no success/failure fed to the
        # recovery layer, nothing taught to intent memory, no HUD row to close
        # (tool_cancelled already made it terminal), and a model-facing note
        # that says plainly what happened so Era neither reports a result the
        # owner cancelled nor retries it.
        if REGISTRY.is_cancelled(hud_call):
            print(f"[era] [CANCEL] Discarding the result of {name}: cancelled mid-flight.")
            # The batch loop knows only the provider's call id, which is not
            # always the HUD row id this token is keyed by. Alias it so
            # tool_batch can reopen the ledger claim for a re-ask.
            REGISTRY.note_cancelled_id(getattr(fc, "id", "") or "")
            self._trace_cancelled_call(name, fc, result)
            self._trace_routed = self._trace_safety = None
            return build_function_response(
                fc,
                ToolResult.fail(
                    message=_CANCEL_DISCARD,
                    error=_CANCEL_DISCARD,
                    error_type="Cancelled",
                    tool_name=name,
                ),
                types.FunctionResponse,
            )

        if not result.success and result.error:
            error_type = result.error_type or ""
            if error_type not in (
                "BlockedAction",
                "ConfirmationRequired",
                "UnknownTool",
                "ActionError",
                "ConfirmationFailed",
            ):
                self._note_tool_failure(name, fc_args_to_dict(fc), error_type, result.error)
        elif result.success:
            self._note_tool_success(name, fc_args_to_dict(fc))

        # Every result passes here (the Spotify branches return before the generic
        # refresh), so this is where the bridge's real state is attached.
        result = _note_bridge_is_off(self, name, result)
        print(f"[era] [OUT] {name} -> {result.short_message(80)}")
        # Task trace (core/task_trace.py, 25/09/2026): the whole procedure of
        # this call on one line -- words, requested vs routed tool, safety,
        # outcome. Read with scripts/task_log.py. Never raises.
        try:
            from core.task_trace import trace

            routed = getattr(self, "_trace_routed", None) or (name, {})
            trace(
                "tool",
                app="era",
                user_text=getattr(self, "_inflight_user_text", "")
                or getattr(self, "_last_user_text", ""),
                requested=name,
                requested_args=fc_args_to_dict(fc),
                tool=routed[0],
                args=routed[1],
                safety=getattr(self, "_trace_safety", None),
                ok=bool(result.success),
                error_type=result.error_type,
                result=result.short_message(300),
                duration_ms=result.duration_ms,
                verification=result.verification_status,
            )
        except Exception:
            pass
        _routed = getattr(self, "_trace_routed", None)
        hud.tool_finished(
            hud_call, name, hud_args, result, routed_to=(_routed[0] if _routed else None)
        )
        self._learn_routed = _routed
        self._trace_routed = self._trace_safety = None
        # Telemetry that was declared but never fed: tool_ms/tool_count in
        # `turn.latency` read 0 on every turn because nothing called note_tool.
        try:
            from app.era_live.audio import _latency_mark

            _latency_mark(self, "note_tool", result.duration_ms or 0)
        except Exception:  # noqa: BLE001 - instrumentation never breaks a tool
            pass
        if result.success:
            try:
                from core.completion_contract import completion_tag

                _verdict = (completion_tag(result) or {}).get("verdict")
            except Exception:  # noqa: BLE001
                _verdict = None
            log_event(
                main_logger,
                "tool.execution.completed",
                "INFO",
                tool=name,
                duration_ms=result.duration_ms,
                verification_status=result.verification_status,
                verdict=_verdict,
                # The tool that really ran, when the router swapped it. Without
                # this the log shows computer_control "completed" for a
                # netflix_control status.
                ran=(_routed[0] if _routed and _routed[0] != name else None),
            )

        try:
            record_tool_result(name, fc_args_to_dict(fc), result=result.message)
        except Exception as memory_error:
            print(f"[era] [WARN] intent memory record failed: {memory_error}")

        if not self.ui.muted and not self._standby_active:
            self.ui.set_state("LISTENING")

        # Every tool: say each thing once, never twice (app/era_live/speak_once.py).
        # After the HUD and intent memory took the real result — model-facing only.
        from app.era_live.speak_once import speak_once

        result = speak_once(result, name, fc_args_to_dict(fc), getattr(self, "_live_turn_text", ""))
        # Through the class: partial test stand-ins borrow this method without the mixin.
        result = ToolDispatchMixin._attach_learned_fix(self, result, name, fc)
        result = ToolDispatchMixin._attach_route_notice(self, result, name, fc)
        return build_function_response(fc, result, types.FunctionResponse)

    def _attach_route_notice(self, result, name: str, fc):
        """Tell the model when the router ran a DIFFERENT tool than it asked for.

        The reply is returned under the REQUESTED name, so a `computer_control
        press` answered with "Netflix state: unknown" read as a keypress that
        worked (live 2026-11-15). Model-facing only; it never changes success,
        the verdict or an error - those stay exactly as the tool left them.
        """
        try:
            from dataclasses import replace

            routed = getattr(self, "_learn_routed", None)
            if not routed or not routed[0] or routed[0] == name:
                return result
            asked = fc_args_to_dict(fc)
            ran_args = routed[1] if isinstance(routed[1], dict) else {}

            def label(tool, args):
                action = args.get("action") if isinstance(args, dict) else None
                return f"{tool}({action})" if action else str(tool)

            note = (
                f"ROUTING NOTE: you asked for {label(name, asked)} but Era executed "
                f"{label(routed[0], ran_args)} instead. The result below belongs to "
                f"{routed[0]}; it does not prove the requested action happened."
            )
            data = dict(result.data) if isinstance(result.data, dict) else {"result": result.data}
            data["routed"] = {"asked": label(name, asked), "ran": label(routed[0], ran_args)}
            log_event(
                main_logger,
                "tool.routed",
                "INFO",
                asked=data["routed"]["asked"],
                ran=data["routed"]["ran"],
                success=bool(result.success),
            )
            return replace(
                result, message=(note + chr(10) + (result.message or "")).strip(), data=data
            )
        except Exception:  # noqa: BLE001
            return result

    def _attach_learned_fix(self, result, name: str, fc):
        """A failed call that Era has failed (and recovered from) before gets the
        known fix attached, so she retries the right way in this same turn
        (core/task_lessons.py). Model-facing only; never raises."""
        try:
            from dataclasses import replace

            from core.task_lessons import diagnostic_for_failure, hint_for_failure
            from core.tool_result import BENIGN_ERROR_TYPES

            if result.success or result.error_type in BENIGN_ERROR_TYPES:
                return result
            # The routed tool, not the requested name: lessons are keyed by what ran.
            routed = getattr(self, "_learn_routed", None) or (name, fc_args_to_dict(fc))
            hint = hint_for_failure(
                getattr(self, "_inflight_user_text", "") or getattr(self, "_last_user_text", ""),
                routed[0] or name,
                routed[1] if isinstance(routed[1], dict) else fc_args_to_dict(fc),
                result.error_type,
            )
            diagnostic = diagnostic_for_failure(routed[0] or name, routed[1], result.error_type)
            hint = "\n".join(part for part in (hint, diagnostic) if part)
            if not hint:
                return result
            return replace(
                result,
                message=(result.message + chr(10) + hint).strip(),
                error=((result.error or "") + chr(10) + hint).strip(),
            )
        except Exception:  # noqa: BLE001
            return result

    # ── Crew: run off the receive loop, report the real outcome ─────────────
    def _background_grace(self, name: str, args: dict) -> float | None:
        """Seconds this tool may hold the receive loop, or None to stay inline.

        New live tools also get a bounded grace; omission cannot stall the ear.
        None for every case where an ack is the wrong answer -- a later step,
        another lane or the batch itself needs the REAL result, not a promise.
        """
        grace = _BG_GRACE_S.get(name, 1.5)
        if name == "era_workbench" and (args or {}).get("capability") == "web_api":
            # A search or call takes 1-3 s and the model needs the answer to reply. Detached,
            # the answer can only be spoken as one sentence, so it would be lost.
            grace = _WEB_API_GRACE_S
        from app.era_live.tool_batch import HALTING_TOOLS

        if name in HALTING_TOOLS:
            return None  # tool_batch decides what runs next from the real result
        try:
            from core.task_scheduler import current_task

            if current_task.get() is not None:
                return None  # a scheduled chain: a later step consumes this
        except Exception:  # noqa: BLE001 — no scheduler context means the live path
            pass
        try:
            from app.era_live.autopilot import LOCAL_EXECUTION

            if LOCAL_EXECUTION.get():
                return None  # autopilot awaits with wait_for(...,30) and reads .message
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pending_cm_ask_active() or self._cm_handoff_active():
                return None  # the CM holds must not be skipped by an ack
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "ui", None) is not None and self.ui.muted:
            return None  # nobody to deliver the later outcome to
        if getattr(self, "_standby_active", False):
            return None
        return grace

    def _spawn_background_deliver(self, fc, hud_call: str, hud_args: dict, task) -> None:
        """Finish a detached tool: close its HUD row and report the real outcome.

        The same shape as _start_background_crew_ask, generalised. The HUD row
        stays *running* until the true result lands, so the panel never shows a
        premature success, and the trace line keeps the inline path's fields.
        """
        name = fc.name or "unknown"
        user_text = getattr(self, "_inflight_user_text", "") or getattr(self, "_last_user_text", "")
        # Consume the router's crumbs now, or they leak onto the next call.
        routed = getattr(self, "_trace_routed", None) or (name, dict(hud_args))
        safety = getattr(self, "_trace_safety", None)
        self._trace_routed = self._trace_safety = None

        async def _worker_body() -> None:
            try:
                result = await task
            except asyncio.CancelledError:
                # Explicit: CancelledError does not inherit from Exception, and
                # a cancel is an ordinary outcome here, not a crash to log.
                result = ToolResult.fail(
                    message="Cancelled.", error="Cancelled.", error_type="Cancelled"
                )
            except Exception as exc:  # noqa: BLE001 — a crash is still an outcome
                result = ToolResult.fail(
                    message=f"{name} failed: {type(exc).__name__}",
                    error=f"{name} failed: {type(exc).__name__}",
                    error_type="BotError",
                )
            if not isinstance(result, ToolResult):
                result = ToolResult.from_any(result)

            if REGISTRY.is_cancelled(hud_call):
                print(f"[era] [CANCEL] Discarding {name}: cancelled while detached.")
                self._trace_cancelled_call(name, fc, result)
                return
            try:
                hud.tool_finished(hud_call, name, hud_args, result, routed_to=routed[0])
            except Exception as e:  # noqa: BLE001 — the HUD never breaks the report
                print(f"[era] [BG] HUD finish skipped: {e}")
            try:
                from core.task_trace import trace

                trace(
                    "tool",
                    app="era",
                    user_text=user_text,
                    requested=name,
                    requested_args=hud_args,
                    tool=routed[0],
                    args=routed[1],
                    safety=safety,
                    ok=bool(result.success),
                    error_type=result.error_type,
                    result=result.short_message(300),
                    duration_ms=result.duration_ms,
                    verification=result.verification_status,
                )
            except Exception:  # noqa: BLE001
                pass
            if result.success:
                # The inline path logs this; a detached call never did, so 8 of
                # 14 calls in the 2026-11-15 session had no outcome event at all.
                log_event(
                    main_logger,
                    "tool.execution.completed",
                    "INFO",
                    tool=name,
                    duration_ms=result.duration_ms,
                    verification_status=result.verification_status,
                    ran=(routed[0] if routed and routed[0] != name else None),
                    detached=True,
                )
            if name == "web_search":
                await self._report_search_outcome(hud_call, hud_args, result)
            else:
                self._deliver_tool_outcome(name, result)

        async def _worker() -> None:
            try:
                await _worker_body()
            finally:
                # Keep a search cancellable while its report awaits the floor.
                REGISTRY.finish(hud_call)

        self._spawn_background_task(_worker(), f"bg-{name}")

    async def _report_search_outcome(self, cid: str, args: dict, result: ToolResult) -> None:
        """Serialize search reports; never inject one into the user's turn."""
        from app.era_live.search_report import search_report_prompt
        from core.model_reply import send_model_text

        lock = getattr(self, "_search_report_lock", None)
        if lock is None:
            lock = self._search_report_lock = asyncio.Lock()
        deadline = time.monotonic() + 120
        if getattr(self, "_standby_active", False):
            wake = getattr(self, "_request_completion_wake", None)
            if callable(wake):
                wake()
        async with lock:
            while not REGISTRY.is_cancelled(cid) and not getattr(
                self, "_shutdown_requested", False
            ):
                tracker = getattr(self, "_model_reply_ownership", None)
                busy = (
                    getattr(self, "_is_speaking", False)
                    or bool(getattr(self, "_inflight_user_text", ""))
                    # An empty tool-call turn deliberately retains ownership;
                    # it is awaiting THIS result, not still talking.
                    or bool(tracker and tracker.active and not tracker.tools_seen)
                    or getattr(self, "_tools_in_flight", 0) > 0
                    or getattr(self, "_standby_active", False)
                )
                if getattr(self, "session", None) is not None and not busy:
                    prompt = search_report_prompt(
                        str(args.get("query") or ""),
                        result.message or result.error or "",
                        bool(result.success),
                    )
                    try:
                        if tracker is not None:
                            # This new text starts the report turn. A retained
                            # empty-tool flag must not let the NEXT report overlap.
                            tracker.tools_seen = False
                        await send_model_text(self, prompt, internal=True)
                        log_event(
                            main_logger,
                            "search.report.submitted",
                            "INFO",
                            cid=cid,
                            success=result.success,
                        )
                        return
                    except Exception as exc:
                        log_event(
                            main_logger,
                            "search.report.retry",
                            "WARNING",
                            cid=cid,
                            error_type=type(exc).__name__,
                        )
                if time.monotonic() >= deadline:
                    self.ui.write_log(
                        "SYS: Search report deferred: conversation busy or disconnected. Results remain in the HUD."
                    )
                    log_event(
                        main_logger,
                        "search.report.deferred",
                        "WARNING",
                        cid=cid,
                        reason="busy_or_disconnected",
                        success=result.success,
                    )
                    return
                await asyncio.sleep(0.25)
            log_event(main_logger, "search.report.cancelled", "INFO", cid=cid)

    def _deliver_tool_outcome(self, name: str, result: ToolResult) -> None:
        """Speak a detached tool's real outcome -- success OR failure.

        Through the reconnect-surviving path, so a result is never swallowed
        because the session rotated while the work ran, and the HUD and Era can
        never disagree about what happened.
        """
        # One plain sentence, never the tool's internals read aloud
        # (app/era_live/speak_once.spoken_outcome). The HUD keeps the full text.
        from app.era_live.speak_once import spoken_outcome

        spoken = spoken_outcome(name, result)
        try:
            self._deliver_or_queue_cm_report(spoken, verbatim=True)
        except Exception as e:  # noqa: BLE001
            print(f"[era] [BG] could not deliver the {name} outcome: {e}")

    def _crew_ask_should_background(self, name: str, args: dict) -> bool:
        """True for a crew `ask` that should run as a background task.

        Only `ask` is slow enough to matter -- `status`/`list` return in
        milliseconds and stay inline. A crew call inside a SCHEDULED chain is
        left inline too: a later step may feed the crew's answer, and an
        immediate "working on it" ack would break that hand-off.
        """
        if name != "crew":
            return False
        if str((args or {}).get("action") or "ask").strip().lower() != "ask":
            return False
        try:
            from core.task_scheduler import current_task

            if current_task.get() is not None:
                return False
        except Exception:  # noqa: BLE001 — no scheduler context means the live path
            pass
        # While a brand-ask is open, _execute_tool_raw holds every non-whitelisted
        # tool so Era just asks and waits. Backgrounding would skip that hold, so
        # fall through to the inline path (which returns the hold message) instead.
        try:
            if self._pending_cm_ask_active():
                return False
        except Exception:  # noqa: BLE001 — no CM-ask state means nothing to hold for
            pass
        return True

    def _start_background_crew_ask(self, fc, hud_call, hud_args: dict) -> None:
        """Run a crew `ask` off the receive loop and report its real outcome.

        The HUD row opened by `_execute_tool` stays *running* until this worker
        finishes it with the TRUE result, so the panel shows running -> real
        outcome and never a premature "success". The task-trace line is written
        here too (same shape and fields as the inline path), and the spoken
        outcome goes through `_deliver_or_queue_cm_report`, which speaks now if a
        session is live and otherwise delivers on the next reconnect -- so a
        failure is reported even when the session rotated mid-call.

        Created as a plain background task (not adopted into the session's
        TaskGroup), exactly like the CM lane's long jobs, so a rotation that
        tears the session down cannot cancel the crew turn or lose its result.
        """
        from crew.runtime import run_tool

        user_text = getattr(self, "_inflight_user_text", "") or getattr(self, "_last_user_text", "")
        # Consume the router's trace crumbs now: the foreground call returns the
        # ack and never reaches the code that would read or clear them, so the
        # worker owns them and they must not leak onto the next tool call.
        routed = getattr(self, "_trace_routed", None) or ("crew", dict(hud_args))
        safety = getattr(self, "_trace_safety", None)
        self._trace_routed = self._trace_safety = None

        async def _worker() -> None:
            try:
                result = await offload(lambda: run_tool(dict(hud_args), user_text=user_text))
            except Exception as exc:  # noqa: BLE001 — a crew crash is still an outcome to report
                result = ToolResult.fail(
                    message=f"The crew failed: {type(exc).__name__}",
                    error=f"The crew failed: {type(exc).__name__}",
                    error_type="BotError",
                )
            finally:
                # _execute_tool handed the token over still registered so the
                # owner could cancel the crew from its HUD row; retire it here,
                # or a stale entry would keep catching the next "cancel that".
                REGISTRY.finish(hud_call)
            if not isinstance(result, ToolResult):
                result = ToolResult.from_any(result)

            # The owner cancelled while the crew was thinking: the HUD row is
            # already terminal and the answer is unwanted. Say nothing.
            if REGISTRY.is_cancelled(hud_call):
                print("[era] [CANCEL] Discarding the crew's answer: cancelled mid-flight.")
                return

            # A staged replace that needs the user's yes: put the plan in front
            # of the user over a channel the model cannot edit, and let the
            # existing confirmation flow take over (the store was armed
            # in-process by crew/runtime). No spoken "outcome" in this case --
            # the announced plan already asks the question.
            confirmation = result.error_type == "ConfirmationRequired" and isinstance(
                result.data.get(PREVIEW_KEY), str
            )
            if confirmation:
                announced = False
                try:
                    announced = bool(self._announce_pending_plan(result.data[PREVIEW_KEY]))
                except Exception as e:  # noqa: BLE001
                    print(f"[era] [CREW] could not announce the crew plan: {e}")
                result.data[ANNOUNCED_KEY] = announced
                # If we couldn't announce (no session, standby, or announcement failed),
                # queue the preview for delivery on reconnect/resume.
                if not announced:
                    self._deliver_or_queue_cm_report(result.data[PREVIEW_KEY], verbatim=True)

            try:
                hud.tool_finished(hud_call, "crew", hud_args, result, routed_to="crew")
            except Exception as e:  # noqa: BLE001 — the HUD must never break the report
                print(f"[era] [CREW] HUD finish skipped: {e}")

            try:
                from core.task_trace import trace

                trace(
                    "tool",
                    app="era",
                    user_text=user_text,
                    requested="crew",
                    requested_args=hud_args,
                    tool=routed[0],
                    args=routed[1],
                    safety=safety,
                    ok=bool(result.success),
                    error_type=result.error_type,
                    result=result.short_message(300),
                    duration_ms=result.duration_ms,
                    verification=result.verification_status,
                )
            except Exception:  # noqa: BLE001
                pass

            if not confirmation:
                self._deliver_crew_outcome(result)

        self._spawn_background_task(_worker(), "crew-ask")

    def _deliver_crew_outcome(self, result: ToolResult) -> None:
        """Speak the crew's real outcome -- success OR failure -- through the
        reconnect-surviving path, so the HUD and Era can never disagree and a
        failure is never swallowed when the live session rotated mid-call."""
        if result.success:
            answer = result.short_message(800).strip() or "Done."
            spoken = f"The crew's finished, sir. {answer}"
        else:
            reason = result.short_message(400).strip() or (result.error_type or "no reason given")
            spoken = f"The crew couldn't finish that, sir. {reason}"
        try:
            self._deliver_or_queue_cm_report(spoken, verbatim=True)
        except Exception as e:  # noqa: BLE001
            print(f"[era] [CREW] could not deliver the crew outcome: {e}")

    def _handle_set_voice(self, args: dict) -> str:
        """Switch Era's voice between Fish Audio, Kokoro (local GPU) and Gemini.

        Never needs a reconnect: both modes run an AUDIO session, so the change
        is live on the very next sentence, mid-conversation.
        """
        requested = str(args.get("voice") or "").strip()
        resolved = VOICE_CFG._resolve_alias(requested)
        if resolved is None:
            return (
                f"I don't know a voice called '{requested}'. Ask the user whether they "
                "mean my Fish, Kokoro or Gemini voice."
            )

        wants_fish = resolved == MODE_FISH_AUDIO

        if wants_fish and not is_fish_configured():
            return (
                "I can't switch to my Fish voice — no Fish Audio API key is configured. "
                "Tell the user that, and that I'm staying on my Gemini voice."
            )
        if resolved == MODE_KOKORO_AUDIO:
            from core.tts_kokoro import KOKORO

            if not KOKORO.ready:
                ok, why = KOKORO.available()
                if not ok:
                    return f"My Kokoro voice is not installed ({why}). Tell the user."
                self._kokoro_ready_for_switch("kokoro")  # switches by itself once loaded
                return (
                    "My Kokoro voice is loading (about ten seconds); I'll switch to it "
                    "automatically as soon as it is ready. Tell the user, briefly."
                )

        label = MODE_LABELS[resolved]
        if (
            resolved == MODE_GEMINI_AUDIO
            and getattr(self, "request_gemini_reconnect", lambda: False)()
        ):
            VOICE_CFG.set_mode(resolved)
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""
            self.ui.write_log("SYS: Gemini is not connected; retrying the cloud voice.")
            return (
                "Gemini is not connected. A reconnection was requested; do not "
                "say it is already running or that the switch succeeded."
            )
        if VOICE_CFG.mode == resolved:
            # Already speaking in this voice, but possibly only via a transient
            # fallback. Cancel any pending auto-restore and, when this isn't yet
            # the saved preference, commit it so a relaunch keeps it.
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""
            if VOICE_CFG.preferred != resolved:
                VOICE_CFG.set_mode(resolved)
                self.ui.write_log(f"SYS: Voice preference saved: {label}.")
                return (
                    f"I'm already speaking in my {label} voice; I've saved it as my "
                    "preferred voice so it sticks after a restart. Tell the user, and "
                    "do nothing else."
                )
            return f"I'm already using my {label} voice. Tell the user, and do nothing else."

        try:
            applied = VOICE_CFG.set_mode(resolved)
        except ValueError as e:
            return f"I couldn't switch my voice: {e}. Ask the user which voice they meant."

        # Drop audio queued in the OLD voice, so a half-played sentence does not
        # finish in the other voice after the switch.
        self._flush_audio_in_queue()
        # A manual switch back to Fish clears any fallback countdown, otherwise
        # the recovery timer could yank the voice back on its own schedule.
        self._tts_primary_retry_at = 0.0

        label = MODE_LABELS.get(applied, applied)
        print(f"[VOICE] Switching to {applied}")
        self.ui.write_log(f"SYS: Voice switched to {label}.")
        return (
            f"Voice switched to my {label} voice, effective immediately. "
            "Tell the user it is done. Say nothing else."
        )

    def _ensure_gmail_notifier(self, set_notifier) -> None:
        """Give the Gmail watcher a way to reach the voice channel, once.

        Two guarantees live here, and both matter:

        1. **It never talks over anything.** `_can_notify` refuses while Era is
           muted, reconnecting, speaking, or in standby without a wake word. A
           refused alert is not lost — it stays queued for
           gmail_control(action='notifications').

        2. **An email subject can never become an instruction.** In
           gemini_audio mode speak() feeds its argument into the LIVE SESSION
           as conversational input, and that session holds Era's whole tool
           surface. A subject line is text a stranger chose. So the alert is
           sanitized with the same delimiter-neutralising helper used for
           other-model output, then wrapped in _say_exactly so Gemini reads it
           aloud instead of reasoning about it — the exact failure the
           2026-07-21 "Skid track" incident was.
        """
        if getattr(self, "_gmail_notifier_installed", False):
            return

        def _can_notify() -> bool:
            try:
                snap = self.session_manager.get_state_snapshot()
            except Exception:
                return False  # fail closed: unknown state means stay quiet
            if snap.get("is_muted") or snap.get("is_reconnecting") or snap.get("is_speaking"):
                return False
            if snap.get("standby_active") and not snap.get("wake_word_active"):
                return False
            return True

        def _notify(summary: str, alert: dict) -> None:
            try:
                from core.gmail_priority import detect_injection

                sender = (alert.get("from_address") or "someone").strip()
                # A subject carrying assistant-directed text is never repeated,
                # not even quoted: the user gets the sender and a warning, and
                # can open it deliberately.
                if detect_injection(summary):
                    line = (
                        f"Sir, a high-priority email from {sender}. Its subject line "
                        "contains text aimed at an assistant rather than at you, so "
                        "I am not repeating it. Say 'check my important emails' to "
                        "look at it yourself."
                    )
                else:
                    line = f"Sir, a high-priority email. {summary}"
                self.speak(_say_exactly(_sanitize_spoken(line, limit=300)))
            except Exception as e:
                print(f"[era] [GMAIL] alert delivery failed: {type(e).__name__}: {e}")

        try:
            set_notifier(notify=_notify, can_notify=_can_notify)
            self._gmail_notifier_installed = True
        except Exception as e:
            print(f"[era] [GMAIL] could not install the alert notifier: {e}")

    def _note_user_utterance(self, text: str, said_at: float | None = None) -> None:
        """Record a complete user utterance for tool routing AND the
        save_memory authorization window. Every site that accepts a full
        user input must go through here, not assign _last_user_text raw.

        said_at: when the user STARTED saying it (voice); approval gates compare it
        with when a task parked. Any complete input also ends an in-progress voice
        fragment, so a stale partial can never outrank what was said after it."""
        self._last_user_text = text
        # A new, genuine user input releases the analysis-only fallback latch.
        self._autopilot_analysis_only = False
        self._inflight_user_text = ""
        if text and text.strip():
            # Held for the conversation recap until Era actually replies (see
            # the turn_complete handler in app/era_live/audio.py). This funnel
            # is the only place that sees TYPED input, which never produces an
            # input transcription of its own.
            self._recap_pending_user = text
            self._recent_user_texts.append((said_at or time.time(), text))
            if len(self._recent_user_texts) > 10:
                del self._recent_user_texts[:-10]
            try:
                from memory.memory_context import capture

                owner = getattr(self, "_input_envelope", None)
                capture(
                    text,
                    getattr(owner, "source", "voice"),
                    getattr(owner, "id", "") if getattr(owner, "text", None) == text else "",
                )
            except Exception as exc:
                log_event(
                    main_logger,
                    "memory.context.capture_failed",
                    "WARNING",
                    error_type=type(exc).__name__,
                )

    def _user_said(self, pattern: re.Pattern, window_s: float = 120) -> bool:
        """True only if the user themself said `pattern` in the last two minutes.
        Same grounding idea as the save_memory gate: the user's recorded
        utterances are the authority, never the model's tool choice."""
        now = time.time()
        texts = [(now, _current_user_text(self))] + list(reversed(self._recent_user_texts))
        for ts, txt in texts:
            if now - ts > window_s:
                break
            if pattern.search(txt or ""):
                return True
        return False

    def _is_reboot_order(self, text: str) -> bool:
        """The user's whole utterance is "reboot Era One" (wake word allowed)."""
        from core.era_relaunch import is_reboot_command

        try:
            from app.wake import WAKE_CFG

            wake = [WAKE_CFG.word, *WAKE_CFG.aliases]
        except Exception:
            wake = []
        return is_reboot_command(text, wake)

    def _arm_relaunch(self, reason: str, source: str, **extra) -> bool:
        """Order the watchdog to bring Era straight back, then let her exit.

        The one copy of this sequence. Two callers need it — "reboot Era One"
        and a verified self-edit that asked to go live — and two copies of a
        restart sequence is how they drift until one of them forgets the
        supervisor check below.

        THE CHECK THAT MATTERS: if nothing supervises this process (Era was
        started by hand), exiting is not a reload, it is an outage. In that
        case a detached supervisor is started first, and it waits for this
        process to die before relaunching. If that ALSO fails, the caller is
        told, so nobody exits believing they will be brought back.
        """
        import os as _os

        from core import era_relaunch

        supervised = era_relaunch.is_supervised()
        flag_written = era_relaunch.request_relaunch(reason=reason, source=source, **extra)
        recoverable = flag_written and (supervised or era_relaunch.spawn_supervisor(_os.getpid()))
        if not recoverable:
            print("[era] [RELAUNCH] WARNING: no supervisor and could not start one.")
        elif reason == "user said 'reboot Era One'":
            # This marker is separate from the supervisor's consumed relaunch
            # flag and is spent only when the new engine reaches ready.
            era_relaunch.arm_ready_ack(source, muted=bool(getattr(self.ui, "muted", False)))
        log_event(
            main_logger,
            "era.relaunch.requested",
            "WARNING",
            supervised=supervised,
            recoverable=recoverable,
            reason=reason,
            source=source,
        )
        self._shutdown_requested = bool(recoverable)
        self._shutdown_awaiting_confirm = False
        return bool(recoverable)

    def _exit_after_speaking(self, *, close_hud: bool) -> None:
        """Leave the process on purpose, once Era has finished her sentence.

        THE ONE COPY of the exit. `_shutdown_requested = True` is only a
        REQUEST: the connect loop reads it between session attempts
        (main.py:1556), so while a Gemini Live session is up — normally minutes
        — nothing acts on it. That is not a clean exit, it is a wait.

        Observed 2026-09-29 15:45: a verified self-edit armed the relaunch and
        then stayed up, because this sequence lived inside `_shutdown_era` as a
        local function and the reload path had no way to reach it. The
        supervisor's 25-second relaunch grace expired and it killed the process
        tree — `exit_code 1, forced: true` in logs/era_lifecycle.jsonl, with
        whatever was in flight truncated. Era came back, so the user saw
        nothing; the audit log is the only place the difference shows.

        Why a thread and not an await: the caller may be a tool handler running
        inside the event loop, and os._exit must not run before Era has said
        what she is doing. The waits are all bounded — a lagging or silent Era
        must never hold an exit open past the supervisor's grace, so the total
        is capped at ~21s against a 25s budget.

        `close_hud` is the only difference between the two callers: a shutdown
        takes the HUD window with it, a relaunch leaves it open because it
        reconnects to the new backend by itself.
        """

        def _leave():
            import os

            deadline = time.time() + 20

            def _talking() -> bool:
                # PF-045: the exit line is spoken by the voice the owner SELECTED
                # (main.speak while _shutdown_requested). _exit_speech_pending covers
                # the gap before its first audio (synthesis / Gemini latency); once
                # sound is actually playing (_is_speaking, or _local_speaking for the
                # local fallback) the wait is the playback itself.
                if self._is_speaking or getattr(self, "_local_speaking", False):
                    self._exit_speech_pending = False
                    return True
                return bool(getattr(self, "_exit_speech_pending", False))

            # She may not have started talking yet.
            for _ in range(30):
                if _talking():
                    break
                time.sleep(0.1)
            # Wait for speech to finish AND stay finished. The reload path
            # queues two utterances (the self-edit summary, then "restarting
            # now"), and the gap between them must not be mistaken for silence
            # — exiting in it truncates the sentence that explains the restart.
            quiet = 0
            while quiet < 15 and time.time() < deadline:
                quiet = 0 if _talking() else quiet + 1
                time.sleep(0.1)
            if close_hud:
                try:
                    from app.hud.browser import close_hud_windows

                    close_hud_windows()
                except Exception as e:
                    print(f"[era] [SHUTDOWN] could not close the HUD window: {e}")
                # A plain shutdown ends the conversation. Live 2026-11-17 the next
                # boot resumed the saved Gemini session — whose last turn was the
                # shutdown — and the model called shutdown_era again 2 s after the
                # greeting. A relaunch (close_hud=False) keeps it (PF-026).
                _forget_session_on_exit(self)
            try:
                from core import era_relaunch

                # PF-024: no DLL teardown that can hang past the supervisor's grace.
                era_relaunch.hard_exit(0)
            finally:
                os._exit(0)

        threading.Thread(target=_leave, daemon=True).start()

    def _self_improve_refusal(self, args: dict) -> str:
        """The honest answer when a self-edit cannot resolve a target, or "".

        WHY THIS EXISTS (board item 25, observed live 2026-09-29 15:46 and 15:50)
        `self_improve` apply/rollback is backgrounded, so the dispatcher answers
        the model immediately with "Working on my own code in the background —
        I'll back it up, verify it, and report the outcome." Twice that day the
        request could not be resolved to a file at all: `core/safety_guard.py`
        had ALREADY classified it `LOW: Self-improve target ambiguous — action
        will only report, not write`, and `actions/self_improve.py` returned the
        refusal and wrote nothing. Proof it no-opped:
        `runtime/self_improve/backups/` gained exactly one directory that day,
        for the 15:44 call. Era still said she was working on it.

        Nothing was broken and nothing was unsafe. She was simply **wrong about
        herself**, which is the failure Phase 0 exists to remove — acceptance
        point 3. A promise made before the check is a promise that can be false.

        So the dispatcher now resolves FIRST and only promises what it can keep.
        `resolve_request` is the same function the safety guard consults, and its
        own message already names the ambiguity and asks for the specific file —
        exactly what the user needs to answer — so it is passed through rather
        than replaced with a generic apology.

        Returns "" when the work can genuinely start; the caller then
        backgrounds it exactly as before. Deliberately fail-OPEN: if
        `resolve_request` itself cannot be consulted, the answer is "start the
        work", because the pipeline's own backup/verify/rollback is the real
        protection and this method is only about what Era SAYS.
        """
        action = str(args.get("action") or "propose").lower().strip()
        if action not in ("apply", "rollback"):
            return ""
        try:
            from actions.self_improve import resolve_request

            _target, status, message = resolve_request(
                str(args.get("file_path") or "").strip(),
                str(args.get("description") or "").strip(),
            )
        except Exception as e:
            print(f"[era] [IMPROVE] could not pre-resolve the target ({e}); proceeding.")
            return ""
        if status == "ok":
            return ""
        self.ui.write_log(f"SYS: self-improve not started — target {status}.")
        # The model is told not to dress this up as progress: the tool result is
        # the only thing standing between an unresolved target and a false claim.
        return (
            f"NOT started — I have not touched any of my code, and nothing is running "
            f"in the background. {message} Tell the user exactly this: you could not "
            f'tell which file to change. First call self_improve action="locate" or autonomous_development inspect, then ask which file they '
            f"mean. Do NOT say you are working on it."
        )

    def _reload_after_self_edit(self, outcome: dict) -> None:
        """Restart so a just-verified self-edit goes live — or explain why not.

        This is the last step of the voice-to-code loop: the user says "change
        X on the HUD", Era edits her own source, verifies it, and the change is
        actually VISIBLE without a second command.

        It refuses in three cases, and says so rather than restarting quietly:

          a shutdown or reboot is already in flight
              two exit paths racing is worse than a late reload.
          a community-management job is running
              the same protection "reboot Era One" has. A CM job is minutes of
              real work against live brand accounts; a self-edit is never
              urgent enough to interrupt one.
          nothing can bring her back
              _arm_relaunch reports this. Exiting here would be an outage
              disguised as a reload, so she stays up and asks to be restarted.

        `reload_safe` was already required upstream: only a clean "pass" from
        the independent verifier reaches this method at all.
        """
        rel = outcome.get("rel") or "my code"
        if self._shutdown_requested:
            self.speak("A restart is already under way, sir — the change will be live after it.")
            return

        try:
            import era_dispatch

            job = era_dispatch.current_job()
        except Exception as e:
            print(f"[era] [IMPROVE] job check unavailable ({e}); assuming nothing running.")
            job = None
        if job:
            brand = job.get("brand", "a brand")
            ttype = job.get("task_type", "") or "job"
            self.ui.write_log(f"SYS: self-edit reload deferred; {brand}/{ttype} job running.")
            self.speak(
                f"I've verified the change to {rel}, but a {brand} {ttype} job is running "
                "and a restart would interrupt it. Say 'reboot Era One' when it's finished."
            )
            return

        self.ui.write_log(f"SYS: self-edit verified ({rel}); relaunching to load it.")
        if not self._arm_relaunch(reason=f"self_improve applied {rel}", source="self_improve"):
            self.speak(
                f"The change to {rel} is verified, but nothing is watching me right now, "
                "so if I exited I would not come back. Restart me when you're ready."
            )
            self._shutdown_requested = False
            return
        self.speak("Restarting now to load the change, sir. Back in a few seconds.")
        # Arming the flag is not leaving. Without this the supervisor's grace
        # expires and the "reload" becomes a force-kill (see _exit_after_speaking).
        self._exit_after_speaking(close_hud=False)

    def _shutdown_era(self, args: dict) -> str:
        """shutdown_era, and its relaunch=True form ("reboot Era One").

        Plain shutdown writes era.stop so the supervisor leaves Era down. A
        relaunch writes era.relaunch instead, so the supervisor brings her back
        at once. Both exit the same way, after Era finishes speaking.
        """
        from core import era_relaunch

        relaunch = bool(args.get("relaunch", False))
        # "confirm reboot" after the running-job question: the model's follow-up
        # call may drop relaunch=true, and that must not turn a reboot into a stop.
        if args.get("confirmed") and self._shutdown_awaiting_confirm:
            relaunch = relaunch or getattr(self, "_shutdown_pending_relaunch", False)
        verb = "Relaunch" if relaunch else "Shutdown"
        if self._shutdown_requested:
            return f"era {verb.lower()} is already in progress."

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
            # do NOT write era.stop / era.relaunch.
            self._shutdown_awaiting_confirm = True
            self._shutdown_pending_relaunch = relaunch
            brand = job.get("brand", "a brand")
            ttype = job.get("task_type", "") or "job"
            order = (job.get("order") or "").strip()
            order_clip = (order[:120] + "...") if len(order) > 120 else order
            self.ui.write_log(
                f"SYS: {verb} requested but {brand}/{ttype} job running; asking to confirm."
            )
            what = "Rebooting" if relaunch else "Shutting down"
            say = "confirm reboot" if relaunch else "confirm shutdown"
            self.speak(
                f"Heads up, sir - a {brand} {ttype} is running right now"
                + (f': "{order_clip}".' if order_clip else ".")
                + f" {what} will interrupt it. Say '{say}' to go ahead,"
                " or 'cancel' to keep it running."
            )
            return f"{verb} paused: a job is running, awaiting the user's confirmation."

        # -- Proceeding: nothing running, OR the user confirmed. --
        self._shutdown_requested = True
        cancel_autopilot = getattr(self, "cancel_autopilot", None)
        if cancel_autopilot is not None:
            cancel_autopilot()
        self._shutdown_awaiting_confirm = False

        if relaunch:
            if not self._arm_relaunch(
                reason="user said 'reboot Era One'",
                source=str(args.get("_source", "voice")),
                interrupted_job=(f"{job.get('brand')}/{job.get('task_type')}" if job else None),
            ):
                self._shutdown_requested = False
                self.speak("I could not schedule my restart. I am still running.")
                return "Restart failed; Era is still running."
            self.ui.write_log("SYS: Reboot ordered; relaunching Era One.")
            if not getattr(self.ui, "muted", False):
                self.speak("I'm restarting now, sir.")
        else:
            # Tell the watchdog this exit is INTENTIONAL, BEFORE exiting, so it does
            # not relaunch Era. Same path era_supervisor.py watches: ~/.era/era.stop.
            try:
                stop_flag = era_relaunch.stop_flag()
                stop_flag.parent.mkdir(parents=True, exist_ok=True)
                stop_flag.write_text(
                    f"Intentional shutdown by user at {datetime.now().isoformat(timespec='seconds')}.\n"
                    "The supervisor will NOT relaunch Era while this file exists.\n"
                    "Resume: double-click the ERA ONE shortcut (it clears this flag),\n"
                    "or run  register_era_supervisor.ps1 -Action wake\n",
                    encoding="utf-8",
                )
                print(f"[era] [SHUTDOWN] wrote stop flag {stop_flag} (watchdog will not relaunch).")
            except Exception as e:
                print(
                    f"[era] [SHUTDOWN] WARNING: could not write era.stop ({e}); watchdog MAY relaunch Era."
                )

            # Stop persisting fresh resumption handles from the goodbye turn on.
            self._discard_session_on_exit = True
            _forget_session_on_exit(self)
            self.ui.write_log("SYS: Shutdown confirmed; era.stop written.")
            self.speak("Goodbye, sir. When you want me back, double-click ERA ONE on your desktop.")

        self._exit_after_speaking(close_hud=not relaunch)
        return "Relaunching..." if relaunch else "Shutting down..."

    def _handle_voice_lock(self, args: dict) -> str:
        """Turn the owner-only voice check on/off in real time (voice_lock tool)."""
        from core.speaker_recognition import get_verifier

        verifier = get_verifier()
        want = args.get("enabled")
        if isinstance(want, str):
            want = {"true": True, "false": False}.get(want.strip().lower())
        if want is None:
            state = "ON" if verifier.enabled else "OFF"
            return f"Voice recognition is currently {state}. Tell the user in one short sentence."
        if want == verifier.enabled:
            state = "on" if want else "off"
            return f"Voice recognition is already {state}. Tell the user briefly."
        if not want:
            if not self._user_said(_VOICE_LOCK_OFF_COMMAND, window_s=_VOICE_LOCK_WINDOW_S):
                print("[VOICE-LOCK] Disable refused: not grounded in the user's own words.")
                return (
                    "Not done: voice recognition can only be turned off when the user "
                    "explicitly says so (e.g. 'turn off voice recognition'). Do not retry; "
                    "tell the user that in one sentence."
                )
            verifier.set_enabled(False)
            gate = getattr(self, "_voice_gate", None)
            if gate is not None:
                gate.reset()
            self._voice_lock_log("Voice recognition OFF — Era now answers any voice.")
            return (
                "Voice recognition is now OFF and stays off across restarts until the user "
                "turns it back on: Era responds to anyone who speaks. "
                "Tell the user that in one sentence."
            )
        if not self._user_said(_VOICE_LOCK_ON_COMMAND, window_s=_VOICE_LOCK_WINDOW_S):
            return (
                "Not done: turn voice recognition on only when the user asks for it. Do not retry."
            )
        ok, why = verifier.set_enabled(True)
        if not ok:
            return (
                f"Could not turn voice recognition on: {why}. It stays off. The user must "
                "run tools/enroll_speaker.py first. Tell them that in one sentence."
            )
        gate = getattr(self, "_voice_gate", None)
        if gate is not None:
            gate.reset()
        self._voice_lock_log("Voice recognition ON — only your voice is accepted.")
        return (
            "Voice recognition is now ON: only the owner's voice is accepted. "
            "Tell the user that in one sentence."
        )

    def _voice_lock_log(self, msg: str) -> None:
        print(f"[VOICE-LOCK] {msg}")
        try:
            self.ui.write_log(f"SYS: {msg}")
        except Exception:
            pass
        try:
            log_event(main_logger, "voice.lock", message=msg)
        except Exception:
            pass

    def _protocol_one_commanded(self) -> bool:
        return self._user_said(_PROTOCOL_ONE_COMMAND) or _scheduled_by_voice("protocol_one")

    def _protocol_one_confirm_commanded(self) -> bool:
        """Apply the fixes Protocol One staged last cycle on the user's single
        "yes"/"confirm" — the whole point of the one-word approval. Grounded the
        same way as the launch: the user must have SAID an affirmation (not the
        model deciding, not text on a page), AND a fresh batch must actually be
        waiting, so a stray "yes" with nothing staged does nothing. The fixes
        still run through the backup/verify/rollback pipeline and never touch a
        safety-critical file, so a broad affirmation here is safe."""
        from actions.protocol_one import has_pending_fixes

        if not has_pending_fixes():
            return False
        return self._user_said(_SCHED_APPROVE_COMMAND, window_s=_PROTOCOL_ONE_CONFIRM_WINDOW_S)

    def _protocol_zero_commanded(self) -> bool:
        return self._user_said(_PROTOCOL_ZERO_COMMAND) or _scheduled_by_voice("protocol_zero")

    # ── scheduled tasks: authority from the user's own words ─────────────────
    def _user_words_match(
        self, pattern: re.Pattern, *, window_s: float, require_tokens: tuple[str, ...] = ()
    ) -> bool:
        """Stricter than _user_said, for gates that act unattended later: real
        timestamps only (see _user_utterances), negation-aware, and — with
        require_tokens — one of those words in the SAME utterance."""
        for _ts, t in _user_utterances(self, window_s):
            low = (t or "").lower()
            if require_tokens and not any(tok in low for tok in require_tokens):
                continue
            if _sched_affirms(pattern, low):
                return True
        return False

    def _scheduled_task_authority(self):
        """Approve / waive / Protocol authority for scheduled_task, from the user's OWN
        words only. Two shapes, and nothing looser:
          BARE  — the user's LATEST utterance, begun after the task parked, is nothing
                  but a reply ("yes", "approved", "always", "don't ask again"). Counts
                  only for the task Era announced last (`focused`, decided by the store).
          NAMED — any utterance in the window says approve/waive as assent (not
                  negated, refused, deferred or asked) AND names most of the task.
        """
        from actions.scheduled_task import Authority

        def _latest(since: float) -> str | None:
            said = _user_utterances(self, _SCHED_APPROVE_WINDOW_S, since)
            return said[-1][1] if said else None

        def _named(pattern: re.Pattern, name: str, window_s: float, since: float) -> bool:
            return any(
                _sched_named(t, pattern, name) for _ts, t in _user_utterances(self, window_s, since)
            )

        def approves(task: dict, focused: bool) -> bool:
            since = datetime.fromisoformat(task["pending"]["since"]).timestamp()
            # A bare reply answers the QUESTION: it must be begun after the user was asked.
            latest = _latest(max(since, task["pending"].get("announced_at") or 0.0))
            if focused and latest and _is_bare_approval(latest):
                return True
            return _named(_SCHED_APPROVE_COMMAND, task["name"], _SCHED_APPROVE_WINDOW_S, since)

        def waives(name: str, focused: bool = False, since: float = 0.0) -> bool:
            latest = _latest(since)
            if focused and latest and _is_bare_waiver(latest):
                return True
            return _named(_SCHED_WAIVE_COMMAND, name, _SCHED_WAIVE_WINDOW_S, since)

        def waives_new(name: str) -> bool:
            """While CREATING a task: "every night at 9 … without asking" — the waiver
            and the scheduling in the same, latest utterance; or the task named."""
            latest = _latest(0.0)
            if not latest:
                return False
            low = _sched_clean(latest)
            said_it = _sched_affirms(_SCHED_WAIVE_COMMAND, low) and not _sched_refused(low)
            if said_it and _SCHED_SCHEDULING_WORDS.search(low):
                return True
            return _named(_SCHED_WAIVE_COMMAND, name, _SCHED_WAIVE_WINDOW_S, 0.0)

        def names_protocol(tool: str) -> bool:
            pattern = _SCHED_PROTOCOL_COMMANDS.get(tool)
            return bool(pattern) and self._user_words_match(pattern, window_s=120.0)

        return Authority(
            approves=approves, waives=waives, waives_new=waives_new, names_protocol=names_protocol
        )

    # ── scheduled tasks: execution + reporting hooks ─────────────────────────
    def install_task_scheduler_hooks(self) -> None:
        """Point core.task_scheduler at THIS engine. main.py calls it for every
        EraLive it builds, including the crash-rebuild, so a task never runs
        against a dead instance."""
        from core import task_scheduler

        task_scheduler.set_executor(self._run_scheduled_tool_blocking)
        task_scheduler.set_reporter(self._report_scheduled_task)

    def _run_scheduled_tool_blocking(self, tool: str, args: dict, task: dict) -> dict:
        """Called from a scheduler worker thread — never from the event loop."""
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return {"success": False, "message": "Era's event loop is not running."}
        future = asyncio.run_coroutine_threadsafe(self._run_scheduled_tool(tool, args, task), loop)
        return future.result(timeout=600)

    async def _run_scheduled_tool(self, tool: str, args: dict, task: dict) -> dict:
        """Execute one scheduled tool call. The scheduler has ALREADY applied the
        safety gate (classify_action + approval/waiver), exactly as the confirmed
        path of confirm_pending_confirmation calls _execute_tool_raw directly."""
        from core.panic_stop import panic
        from core.task_scheduler import current_step, current_task

        # A scheduled run reaches _execute_tool_raw directly, so tool_batch's
        # abort gate never sees it. Without this check, "abort mission" would
        # silence the model's loop and then let the 8pm weather report fire
        # into the silence — which is exactly the "any current task" the order
        # is meant to cover. The task is not cancelled, only skipped: the latch
        # lasts seconds, and a recurring task's next run is the right retry.
        if panic.active():
            log_event(
                main_logger,
                "task.scheduled.aborted",
                "WARNING",
                tool=tool,
                task=task.get("id"),
            )
            print(f"[TASKS] skipping '{task.get('name')}': user aborted.")
            return {"success": False, "message": "Skipped: the user aborted. Say nothing."}

        token = current_task.set(task)
        step_token = current_step.set({"tool": tool, "args": dict(args)})
        print(f"[TASKS] running '{task.get('name')}': {tool}")
        log_event(main_logger, "task.scheduled.run", "INFO", tool=tool, task=task.get("id"))
        try:
            raw = await self._execute_tool_raw(tool, dict(args))
            result = ToolResult.from_any(raw, tool_name=tool, action_name=args.get("action"))
            result = verify_tool_result(result, tool=tool, parameters=args)
            return {"success": result.success, "message": result.short_message(500)}
        finally:
            current_step.reset(step_token)
            current_task.reset(token)

    def _report_scheduled_task(self, text: str, urgent: bool) -> bool:
        """Deliver a scheduled-task report: Telegram always, voice only when Era is
        free (same rule as Gmail alerts), the UI log otherwise. speak_dispatched
        sanitises and says it verbatim — a run result can quote a web page, and it
        must never be read by the live session as an instruction.

        Returns whether it was SPOKEN: only then is a bare "yes" an answer to it
        (core/task_scheduler.mark_announced)."""
        from app.away_calls import automatic_report

        automatic_report(self, text, "scheduled")
        try:
            from app.notify import _tg_notify

            _tg_notify(text)
        except Exception:
            pass
        try:
            snap = self.session_manager.get_state_snapshot()
            busy = any(snap.get(k) for k in ("is_muted", "is_speaking", "is_reconnecting"))
            asleep = snap.get("standby_active") and not snap.get("wake_word_active")
        except Exception:
            busy, asleep = True, False
        if busy or asleep:
            try:
                self.ui.write_log(f"SYS: {text}")
            except Exception:
                pass
            if asleep:
                from core.task_scheduler import current_report

                report = current_report.get() or {}
                deliver = getattr(self, "_deliver_or_queue_cm_report", None)
                if deliver is not None and report.get("status") in {
                    "ok",
                    "failed",
                    "partial",
                    "blocked",
                    "missed",
                }:
                    deliver(text, verbatim=True)
            return False
        self.speak_dispatched(text)
        return True

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
        from core.tool_execution_lane import LANE, ToolLaneBusy, resource_tool

        try:
            with LANE.enter(resource_tool(fc.name, fc_args_to_dict(fc))):
                return await ToolDispatchMixin._execute_tool_result_owned(self, fc)
        except ToolLaneBusy:
            return ToolResult.fail(
                "Machine automation is running for a Telegram call. Please try again when it finishes.",
                error_type="ExecutionBusy",
                tool_name=fc.name,
            )

    async def _execute_tool_result_owned(self, fc) -> ToolResult:
        """Runs the call under its own id, so an exception a tool catches can
        only ever be attributed to the call it happened in — never to another
        tool, a background thread, or whatever a web page's text points at."""
        from core import handled_exceptions
        from core.approval_execution import APPROVED_CONFIRMATION, publish_approved_result

        try:
            handled_exceptions.install_context_executor(asyncio.get_running_loop())
        except Exception as e:
            print(f"[era] [SELF-HEAL] context executor not installed: {e}")
        token = handled_exceptions.CURRENT_CALL.set(handled_exceptions.new_call_id())
        approval_token = APPROVED_CONFIRMATION.set(None)
        result = None
        try:
            # Through the class, not self: the confirmation tests bind this method
            # onto a stub object that has no _execute_tool_result_inner of its own.
            result = await ToolDispatchMixin._execute_tool_result_inner(self, fc)
            return result
        finally:
            handled_exceptions.CURRENT_CALL.reset(token)
            cid = APPROVED_CONFIRMATION.get()
            APPROVED_CONFIRMATION.reset(approval_token)
            if cid is not None:
                if not isinstance(result, ToolResult):
                    # Cancellation after atomic consumption must resolve a parked
                    # job too; otherwise it waits forever for the missing outcome.
                    result = ToolResult.fail(
                        "Approved action was interrupted; completion could not be verified.",
                        error_type="ApprovalExecutionInterrupted",
                    )
                await asyncio.to_thread(publish_approved_result, cid, result)

    async def _execute_tool_result_inner(self, fc) -> ToolResult:
        tool_name = fc.name
        from app.era_live.autopilot import LOCAL_EXECUTION

        if getattr(self, "_autopilot_analysis_only", False) and not LOCAL_EXECUTION.get():
            return ToolResult.fail(
                message="The coding fallback is analysis-only; tool execution is disabled for this turn.",
                error_type="BlockedAction",
                tool_name=tool_name,
            )
        parameters = fc_args_to_dict(fc)
        original_name, original_params = tool_name, dict(parameters)
        from core.approval_execution import REMOTE_APPROVAL, remote_approves

        remote = REMOTE_APPROVAL.get()
        if remote is not None and (
            tool_name != remote.tool
            or {k: v for k, v in parameters.items() if k not in CONFIRM_META}
            != {k: v for k, v in remote.parameters.items() if k not in CONFIRM_META}
            or parameters.get("confirmation_id") != remote.confirmation_id
            or parameters.get("confirmed") is not True
        ):
            return ToolResult.fail(
                message="Remote approval does not match this action.",
                error_type="ConfirmationFailed",
                tool_name=tool_name,
            )

        try:
            user_text = getattr(self, "_last_user_text", "") or ""
            # The whole utterance so far, not just the last transcript chunk: the
            # model often calls its tool before the turn completes, and "reboot" /
            # "Era One" can arrive in different chunks.
            inflight = getattr(self, "_inflight_user_text", "") or user_text
            if remote is None:
                tool_name, parameters = divert_era_reboot(tool_name, parameters, inflight)
                tool_name, parameters = route_tool_call(tool_name, parameters, inflight)
                from core.personal_media import divert_personal_song, resolve_song

                tool_name, parameters = divert_personal_song(tool_name, parameters, inflight)
                tool_name, parameters = resolve_song(tool_name, parameters, inflight)
            if (tool_name, parameters) != (original_name, original_params):
                print(
                    f"[era] [ROUTE] routed {original_name} {original_params} -> {tool_name} {parameters}"
                )
        except Exception as route_error:
            print(f"[era] [WARN] routing layer skipped: {route_error}")

        print(f"[era] [ARGS] {tool_name}  {parameters}")
        self._trace_routed = (tool_name, dict(parameters))  # for the task trace

        # Live 2026-11-17: the model sent media_coordinator AND the platform tool for
        # one "play X" (1-2 s apart, separate turns). After routing both were the same
        # start; the second hit the still-loading player and was reported as a FAILED
        # playback although the first one was playing. Start it once.
        media_key = _media_start_key(tool_name, parameters)
        if media_key is not None:
            last = getattr(self, "_recent_media_start", None)
            now_ts = time.monotonic()
            asked = (getattr(self, "_last_user_text", "") or "").strip().lower()
            if (
                last is not None
                and last[0] == media_key
                and now_ts - last[1] < _MEDIA_DEDUPE_S
                and last[2] == asked  # a new utterance ("try again") always runs
            ):
                print(f"[era] [DUP] {tool_name} {media_key[1]!r} already started; not repeated.")
                log_event(main_logger, "tool.media.duplicate", "INFO", tool=tool_name)
                return ToolResult.ok(
                    f"Already started '{media_key[1]}' on {media_key[0]} a moment ago; not"
                    " repeated. Report the first call's result only.",
                    tool_name=tool_name,
                    action_name=parameters.get("action"),
                    data={"executed": False, "duplicate_media_start": True},
                )
            self._recent_media_start = (media_key, now_ts, asked)

        from core.file_arguments import file_argument_error

        argument_error = file_argument_error(tool_name, parameters)
        if argument_error:
            log_event(main_logger, "tool.arguments.invalid", "WARNING", tool=tool_name)
            return ToolResult.fail(
                argument_error,
                error_type="InvalidArguments",
                tool_name=tool_name,
                action_name=parameters.get("action"),
                data={"executed": False},
            )

        start = time.perf_counter()

        def duration():
            return int((time.perf_counter() - start) * 1000)

        # ── JIT vision capture for mutation actions ──
        try:
            from core.vision_context import jit_vision_capture

            _jit_action = parameters.get("action", "")
            if isinstance(_jit_action, str):
                await jit_vision_capture(tool_name, _jit_action.lower().strip(), parameters)
        except Exception:
            pass

        # Per-category HIGH-risk waivers cover the user's own direct call only: a
        # call the router rewrote (e.g. shutdown_era -> computer_settings restart on a
        # mis-heard sentence) keeps its confirmation, since the waiver once caught it.
        # A caller (i.e. the model) must not be able to supply its own
        # "already approved" plan, so any carried key is dropped before this
        # call is classified, stored or run. Only a preview builder may set one.
        parameters = strip_carried(parameters)

        safety = classify_action(
            tool_name, parameters, honor_risk_waivers=(tool_name == original_name)
        )
        risk_level = safety["risk_level"]
        requires_confirmation = safety["requires_confirmation"]
        reason = safety["reason"]
        self._trace_safety = f"{risk_level}: {reason}"  # for the task trace
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

        if requires_confirmation or remote is not None:
            print(f"[era] [WARN] confirmation required ({risk_level})")
            confirmed = bool(parameters.get("confirmed", False))
            confirmation_id = parameters.get("confirmation_id", "")

            if confirmed and confirmation_id:
                # The model is shown the id, so presenting it proves nothing:
                # the same user-approval check as confirm_pending_confirmation.
                pending = self._pending_store.peek(confirmation_id)
                if pending is not None and not _user_approved_pending(self, pending):
                    print(
                        f"[era] [SAFETY] confirmed re-call refused: no user approval "
                        f"since {pending.tool} id={pending.confirmation_id} was armed"
                    )
                    log_event(
                        main_logger,
                        "tool.confirmation.unapproved",
                        "WARNING",
                        tool=pending.tool,
                        confirmation_id=pending.confirmation_id,
                    )
                    return ToolResult.fail(
                        message=NOT_USER_APPROVED_MESSAGE,
                        error=NOT_USER_APPROVED_MESSAGE,
                        error_type="BlockedAction",
                        tool_name=tool_name,
                        action_name=parameters.get("action"),
                        duration_ms=duration(),
                    )
                if remote is not None and not remote_approves(pending):
                    return ToolResult.fail(
                        message="This approval is no longer valid.",
                        error_type="ConfirmationFailed",
                        tool_name=tool_name,
                    )
                pending = self._pending_store.confirm(
                    confirmation_id,
                    **({"expected_created_at": remote.created_at} if remote is not None else {}),
                )
                if pending is None:
                    return ToolResult.fail(
                        message="Invalid or expired confirmation.",
                        error="Confirmation ID not found or has expired.",
                        error_type="ConfirmationFailed",
                        tool_name=tool_name,
                        action_name=parameters.get("action"),
                        duration_ms=duration(),
                    )
                from core.approval_execution import APPROVED_CONFIRMATION

                APPROVED_CONFIRMATION.set(pending.confirmation_id)
                stored_params = {
                    k: v for k, v in pending.parameters.items() if k not in CONFIRM_META
                }
                call_params = {k: v for k, v in parameters.items() if k not in CONFIRM_META}
                # The approved plan comes from the STORE, never from the
                # caller's echo: it is what the user was actually shown.
                parameters = {**parameters, **carried_from(pending.parameters)}
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
                # Resolve what this call will actually DO before asking about
                # it, and store that resolution so approval covers the real
                # effects. Planning can call a model, so it runs off-loop.
                preview_text, carried = await asyncio.get_running_loop().run_in_executor(
                    None, build_confirmation_preview, tool_name, dict(parameters)
                )
                try:
                    cid = self._pending_store.store(
                        tool=tool_name,
                        parameters={**parameters, **carried},
                        risk_level=risk_level,
                        reason=reason,
                    )
                    # Remember which task owns the single confirmation slot, so
                    # cancelling that task releases the question too. Otherwise
                    # the slot stays occupied (the store refuses the next one as
                    # busy) and a later "yes" meant for something else runs this
                    # abandoned action. See app/era_live/cancel.py.
                    _token = CURRENT_TOKEN.get()
                    if _token is not None:
                        _token.confirmation_id = cid
                except PendingConfirmationBusy as busy:
                    # Never displace the question the user is mid-answer to.
                    print(f"[era] [BUSY] refused: {busy}")
                    return ToolResult.fail(
                        message=BUSY_CONFIRMATION_MESSAGE,
                        error=str(busy),
                        error_type="ConfirmationBusy",
                        tool_name=tool_name,
                        action_name=parameters.get("action"),
                        duration_ms=duration(),
                    )
                announced = bool(preview_text) and self._announce_pending_plan(preview_text)
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
                    data={
                        **dict(parameters),
                        PREVIEW_KEY: preview_text,
                        ANNOUNCED_KEY: announced,
                    },
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
            from core.task_recovery import execute_read_recovery

            raw = await execute_read_recovery(
                tool_name,
                parameters,
                lambda retry_parameters: self._execute_tool_raw(tool_name, retry_parameters, fc),
            )
            self._last_tool_name = tool_name
        except CancelledTask as e:
            # Cooperative cancellation is a user decision, not an engine fault.
            return ToolResult.fail(
                message=str(e),
                error=str(e),
                error_type="Cancelled",
                tool_name=tool_name,
                action_name=parameters.get("action"),
                duration_ms=duration(),
            )
        except Exception as e:
            dur = duration()
            traceback.print_exc()
            # Persist the stack, not just the message: core/self_heal acts only
            # on faults Python itself located, and stdout never reaches era.log.
            log_crash(
                main_logger,
                f"tool:{tool_name}",
                type(e),
                e,
                e.__traceback__,
                event="tool.execution.exception",
                level="ERROR",
                tool=tool_name,
                action=parameters.get("action"),
            )
            return ToolResult.fail(
                message=str(e),
                error=str(e),
                error_type=type(e).__name__,
                tool_name=tool_name,
                action_name=parameters.get("action"),
                duration_ms=dur,
            )

        verify_tool, verify_parameters = tool_name, parameters
        if tool_name == "confirm_pending_confirmation":
            confirmed = getattr(self, "_confirmed_action", None)
            if isinstance(confirmed, tuple) and len(confirmed) == 2:
                verify_tool, verify_parameters = confirmed
                self._confirmed_action = None

        return finalize_tool_result(
            self,
            verify_tool,
            verify_parameters,
            raw,
            risk_level=risk_level,
            requires_confirmation=requires_confirmation,
            duration_ms=duration(),
        )

    def _log_caught_exception(self, tool_name: str, parameters: dict, result) -> None:
        """A tool that caught its own exception returned a string, so no stack
        reached era.log and core/self_heal could not see the defect. If the
        exception behind THIS failure was recorded by core/handled_exceptions,
        log its frames now. A held gate is the system working — never logged."""
        try:
            from core import handled_exceptions
            from core.tool_result import BENIGN_ERROR_TYPES

            if not handled_exceptions.installed() or result.error_type in BENIGN_ERROR_TYPES:
                return
            module = {
                "agent_browser": "agent_browser_use",
                "screen_process": "screen_processor",
                "vault_note": "vault_writer",
            }.get(tool_name, tool_name)
            call = handled_exceptions.CURRENT_CALL.get()
            caught = handled_exceptions.match(
                call, result.short_message(4000), module_hint=f"actions/{module}."
            )
            if caught is None:
                return
            log_frames(
                main_logger,
                f"tool:{tool_name}",
                caught["type"],
                caught["msg"],
                caught["frames"],
                event="tool.execution.caught_exception",
                tool=tool_name,
                action=parameters.get("action"),
                correlation=caught.get("correlation", ""),
                call_id=call,
            )
        except Exception as e:  # diagnostics must never change a tool's outcome
            print(f"[era] [SELF-HEAL] caught-exception capture failed: {e}")

    def _announce_pending_plan(self, preview_text: str) -> bool:
        """Put the resolved plan in front of the user WITHOUT going through the model.

        Until this existed, the preview reached the user only as
        `suggested_confirmation_question` in the function response — a hint to
        the very model the threat model treats as adversarial. It could say
        "shall I tidy your desktop?" while the store held a plan that moves
        files, and the fix's central claim (the user approved what runs) did
        not hold.

        The UI log is written here directly, so the real steps exist somewhere
        the model cannot edit or suppress. The spoken copy goes through
        `_say_exactly`, the same wrapper `speak_dispatched` uses, so it is read
        out verbatim instead of summarised. Returns whether the user was told.
        """
        told = False
        try:
            for line in preview_text.splitlines():
                if line.strip():
                    self.ui.write_log(f"CONFIRM: {line.strip()}")
            told = True
        except Exception as e:
            print(f"[era] [WARN] could not log the plan for confirmation: {e}")

        from app.era_live.autopilot import LOCAL_EXECUTION

        speak = None if LOCAL_EXECUTION.get() else getattr(self, "speak", None)
        if speak:
            # One line for speech: the log keeps the readable layout.
            spoken = _sanitize_spoken(
                " ... ".join(ln.strip() for ln in preview_text.splitlines() if ln.strip())
            )
            if spoken:
                try:
                    speak(_say_exactly(spoken))
                    told = True
                except Exception as e:
                    print(f"[era] [WARN] could not speak the plan for confirmation: {e}")
        return told

    async def _execute_tool_raw(self, tool_name: str, args: dict, fc=None) -> str:
        if tool_name == "code_mode":
            from app.codemode_runtime import codemode_allowed, route
            from core.codemode import ENTRY, EXIT, get_codemode

            action = args.get("action", "status")
            if not codemode_allowed():
                # PF-050: switched off by the owner; say so instead of "accepted".
                return ToolResult.fail(
                    "CodeMode is turned off. For code work, hand it to the crew: "
                    "call the crew tool with bot='cody'.",
                    error_type="FeatureDisabled",
                )
            if action == "status":
                return ToolResult.ok(
                    message="Current CodeMode state", data=get_codemode().snapshot()
                )
            owner = getattr(self, "_input_envelope", None)
            pattern = ENTRY if action == "enter" else EXIT if action == "exit" else None
            if owner is None or pattern is None or not pattern.fullmatch(owner.text.strip()):
                return ToolResult.fail(
                    "CodeMode requires the user's explicit mode command.",
                    error_type="UserIntentRequired",
                )
            route(self, owner.text, owner)
            return ToolResult.ok(
                message="CodeMode command accepted", data=get_codemode().snapshot()
            )

        if tool_name == "telegram_call":
            from app.telegram_call_runtime import handle_tool

            return await handle_tool(self, args)

        # ── Final safety guard before any tool execution ──
        # These holds stop the MODEL acting while muted/asleep or while another lane
        # owns the request. A scheduled task was authorised by the user in advance
        # and already passed the scheduler's own safety gate, so it runs through.
        from core.task_scheduler import current_task

        scheduled_run = current_task.get() is not None
        snapshot = self.session_manager.get_state_snapshot()
        if not scheduled_run and snapshot.get("is_muted", False):
            return "Action blocked: era is muted."
        if not scheduled_run and snapshot.get("is_reconnecting", False):
            return "Action blocked: era is reconnecting."
        if (
            not scheduled_run
            and snapshot.get("standby_active", False)
            and not snapshot.get("wake_word_active", False)
        ):
            return "Action blocked: era is in standby."
        # While a brand-ask is open, Era should just ask and wait — not guess or
        # act. Allow-list: block EVERY tool except the minimal escape-hatch set so
        # a future tool is protected by default. Lifts automatically the moment
        # _pending_cm_ask_active() goes False (reply, non-match, or 45s timeout).
        if (
            not scheduled_run
            and tool_name not in _PENDING_ASK_ALLOWED_TOOLS
            and self._pending_cm_ask_active()
        ):
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
        if (
            not scheduled_run
            and tool_name not in _PENDING_ASK_ALLOWED_TOOLS
            and self._cm_handoff_active()
        ):
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

        # Session-state tools need a matching order from the user, never just the
        # model's say-so (ambient audio shut Era down and flipped standby/mic).
        if not scheduled_run:
            _ungrounded = _ungrounded_state_change(
                self, tool_name, relaunch=bool((args or {}).get("relaunch"))
            )
            if _ungrounded:
                return _ungrounded
            _unasked_music = _ungrounded_media_start(self, tool_name, args)
            if _unasked_music:
                return _unasked_music

        # ── System-state awareness gate (pre-command telemetry) ──────────────
        # Before acting ON an application (pause/next/minimize/close/focus/…),
        # gather real telemetry for the target — process presence, a visible
        # window via the Win32 API (never vision), and, where available, the
        # app's native status — and refuse a control command the app cannot
        # support instead of running it on an assumption and claiming success.
        # It is fail-open: it only skips on positive evidence the target is
        # absent, and never gates a launch (play/open) or a query (status).
        try:
            from core.app_state_gate import evaluate_app_command

            _gate = evaluate_app_command(tool_name, args.get("action"), args)
            if not _gate.allow:
                print(
                    f"[era] [STATE-GATE] skip {tool_name}/{args.get('action')} "
                    f"app={_gate.app} reason={_gate.reason} telemetry={_gate.telemetry}"
                )
                log_event(
                    main_logger,
                    "tool.state_gate.skipped",
                    "INFO",
                    tool=tool_name,
                    action=args.get("action"),
                    app=_gate.app,
                    reason=_gate.reason,
                )
                # Honest, non-error result: the app cannot support the command,
                # so report the real state instead of pretending it ran. Phrased
                # without "error"/"failed" so it is not logged as a failure.
                return _gate.message
        except Exception as _gate_err:
            # Never let the awareness layer block a legitimate command.
            print(f"[era] [STATE-GATE] skipped ({type(_gate_err).__name__}: {_gate_err})")

        if tool_name == "enter_standby":
            from core.attention import is_silence_order

            if is_silence_order(_current_user_text(self)):
                # PF-039: "stay silent" means inactivity mode, never the real
                # STANDBY (standby_enabled may be on: wake word only).
                self._enter_inactivity("voice")
                return "Inactivity mode: say nothing at all."
            _no = None if scheduled_run else _not_a_standby_order(self)  # PF-045
            if _no:
                return _no
            self.enter_standby()
            from core.session import standby_enabled

            if not standby_enabled():
                # One-mode session (2026-11-11): there is no teardown and no
                # code-spoken notice, so the model owns the single line.
                return "Session closed. Say exactly 'System offline.' once, then stop."
            # Do NOT ask the model to speak here — core/prompt.txt already
            # tells it to stay silent around this tool, and the code plays
            # its own "Standby mode active" line in wait_and_mute(). Telling
            # the model to also say a sign-off raced two spoken utterances;
            # the second one would sometimes only finish streaming after
            # standby had already muted, and leak out on the next wake.
            return "Standby mode activated. Say nothing — the system plays its own offline notice."

        if tool_name == "set_microphone":
            return self._handle_set_microphone(args)

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

        if tool_name == "voice_lock":
            return self._handle_voice_lock(args)

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
            # Live 2026-11-17 "what's the status of the CM jobs?" reached here as
            # order="status" and was treated as a NEW job needing a brand — one
            # "Delta Cuisine" reply away from launching a job named "status". A
            # status question is a read: answer it from the status file, start nothing.
            if _CM_STATUS_QUESTION.match(order):
                import era_dispatch

                job = era_dispatch.current_job()
                if not job:
                    return "No community-management job is running right now. Nothing was started."
                return (
                    f"Running now: {job.get('brand', 'a brand')} {job.get('task_type') or 'job'}"
                    f" (started {job.get('started', 'recently')}): "
                    f"{(job.get('order') or '').strip()[:120]}. Nothing new was started."
                )
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
                "package",
                "edit",
            ):
                task_type = _infer_cm_task_type(order)
            elif task_type != "ads":
                # The tool description tells the model to pass 'weekly' for any
                # plan/produce/create order, so "prepare the package" would arrive as
                # weekly and run the generating, Magnific-using brief. The two
                # content-workflow lanes are specific enough that the inferred lane
                # wins over a generic one the model picked.
                inferred = _infer_cm_task_type(order)
                if inferred in ("package", "edit"):
                    task_type = inferred
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
                # A bare "ok" was read back to the owner as "ok" (live 2026-11-17).
                return f"Saved to memory: {key.replace('_', ' ')} = {masked}."
            return "Nothing saved: a memory needs both a key and a value."

        if tool_name == "retrieve_memory_context":
            query = args.get("query", "")
            if not query:
                return "No query provided."
            # Reinforced retrieval: bumps access_count on the entries returned,
            # so memories the user actually asks about rank higher over time.
            from memory.memory_manager import retrieve_context_reinforced

            result = retrieve_context_reinforced(query, top_k=3)
            if not result:
                return "No relevant memories found."
            # Live path only (the agent lane keeps bare lines): Gemini read the
            # whole result aloud as its reply. Frame it as private input.
            return (
                "[Private notes for you, not for speech. Use them to act now; "
                "never read, list or summarise these lines aloud.]\n" + result
            )

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
            pending = self._pending_store.peek()
            if pending is None:
                return "This confirmation is no longer valid."
            # The user's own "yes", said after this was armed, is the only
            # approval — never the model's call, however it was steered. Left
            # pending on refusal so the user's real answer can still confirm it.
            if not _user_approved_pending(self, pending):
                print(
                    f"[era] [SAFETY] confirm refused: no user approval since "
                    f"{pending.tool} id={pending.confirmation_id} was armed"
                )
                log_event(
                    main_logger,
                    "tool.confirmation.unapproved",
                    "WARNING",
                    tool=pending.tool,
                    confirmation_id=pending.confirmation_id,
                )
                return ToolResult.fail(
                    message=NOT_USER_APPROVED_MESSAGE,
                    error=NOT_USER_APPROVED_MESSAGE,
                    error_type="BlockedAction",
                    tool_name=tool_name,
                )
            # Consume exactly the action that was checked, never a newer one
            # stored in between.
            pending = self._pending_store.confirm(pending.confirmation_id)
            if pending is None:
                return "This confirmation is no longer valid."
            from core.approval_execution import APPROVED_CONFIRMATION

            APPROVED_CONFIRMATION.set(pending.confirmation_id)
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
            raw = await self._execute_tool_raw(pending.tool, stored_params, fc)
            # A confirm call RUNS a different tool than its own name. The outer
            # dispatcher (and every duck-typed caller of this handler) only sees
            # "confirm_pending_confirmation", so record what actually ran and
            # with which parameters. `_execute_tool_result_inner` uses this to
            # verify the real action; without it, verify_action_result looks up
            # a verifier for the confirm handler, finds none, and the approved
            # delete/send/overwrite is the one action never checked.
            self._confirmed_action = (pending.tool, dict(stored_params))
            return raw

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
            r = await offload(lambda: open_app(parameters=args, response=None, player=self.ui))
            result = r or f"Opened {args.get('app_name')}."

        elif tool_name == "capability_check":
            # Acceptance point 3. The answer is COMPUTED here, from the same
            # declarations the live model was handed (gates applied), so "what
            # can you do?" cannot be answered from the prompt or from the
            # model's impression of itself. Pure reads of two files — fast
            # enough to answer inline, and never backgrounded, because a
            # backgrounded answer is what let item 25 promise work it never did.
            from core.capability_index import check as _capability_check

            result = await asyncio.to_thread(_capability_check, str(args.get("claim") or ""))

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

        elif tool_name == "audio_control":
            from actions.audio_control import audio_control

            r = await loop.run_in_executor(
                None, lambda: audio_control(parameters=args, response=None, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "system_performance":
            r = await offload(
                lambda: system_performance(parameters=args, response=None, player=self.ui)
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
            from agent.task_queue import DuplicateTaskError, get_queue
            from agent.types import TaskPriority
            from app.away_calls import automatic_report

            priority_map = {
                "low": TaskPriority.LOW,
                "normal": TaskPriority.NORMAL,
                "high": TaskPriority.HIGH,
            }
            priority = priority_map.get(args.get("priority", "normal").lower(), TaskPriority.NORMAL)
            task_speak = getattr(self, "speak_dispatched", None) or self.speak
            try:
                task_id = get_queue().submit(
                    goal=args.get("goal", ""),
                    priority=priority,
                    speak=task_speak,
                    on_complete=lambda task_id, outcome: automatic_report(
                        self, outcome.message, "background", f"{task_id}:{outcome.success}"
                    ),
                )
                result = f"Task queued (ID: {task_id})."
            except DuplicateTaskError as exc:
                result = str(exc)

        elif tool_name == "web_search":
            r = await offload(lambda: web_search_action(parameters=args, player=self.ui))
            result = r or "Done."

        elif tool_name == "era_workbench":
            result = await offload(lambda: era_workbench(args))

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

            r = await offload(lambda: spotify_control(parameters=args, player=self.ui))
            result = r or "Done."

        elif tool_name == "spotify_bridge":
            # Keyless Spotify control via the Spicetify era-bridge (127.0.0.1:8766).
            # DORMANT by default: gated behind config spotify_bridge.enabled. While
            # off, Spotify intents belong to the window-automation path, so this
            # never touches the network (no cold-start, no relay). The tool is also
            # withheld from the model's declarations while disabled.
            from core import spotify_bridge

            if not spotify_bridge.is_enabled():
                result = (
                    "The keyless Spotify bridge is off; use the standard Spotify controls, sir."
                )
            else:
                result = await spotify_bridge.handle_action(args) or "Done."

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
            r = await offload(lambda: media_coordinator(parameters=args, player=self.ui))
            result = r or "Done."

        elif tool_name == "window_control":
            from app.era_live.autopilot import LOCAL_EXECUTION

            window_speak = None if LOCAL_EXECUTION.get() else self.speak
            r = await loop.run_in_executor(
                None, lambda: window_control(parameters=args, player=self.ui, speak=window_speak)
            )
            result = r or "Done."

        elif tool_name in ("shutdown_era", "close_era", "exit_era"):
            result = self._shutdown_era(args)

        elif tool_name == "vault_note":
            # Era's own write path into the vault. Deliberately NOT routed through
            # save_memory's grounding gate: that gate exists because save_memory
            # can be provoked into persisting model- or page-supplied text, while
            # this writes a titled markdown note the user can open, read and undo
            # in Obsidian. The refusals that matter here live in vault_writer —
            # an unrecognized brand, and Era's own regenerated notes.
            from actions.vault_writer import vault_note

            result = vault_note(args, player=self.ui)

        elif tool_name == "vault_bridge":
            # The safe door into the vault (core/vault_bridge): locked, atomic,
            # read-back-verified writes; `replace` runs only a plan that was staged
            # and approved by the USER through the confirmation store, and the
            # preview builder + safety gate keep every other path out.
            from core import vault_bridge

            result = await loop.run_in_executor(None, lambda: vault_bridge.run(dict(args)))

        elif tool_name == "crew":
            # Laura and the specialists (crew/). One "working on it" if the call is
            # still running after 2.5 s — per call, never per bot turn or handoff.
            from core.approval_execution import APPROVED_CONFIRMATION
            from crew.runtime import await_with_ack, run_tool

            # Read here, on the loop: run_in_executor does not carry ContextVars into
            # the worker thread, so a verify_candidate approval would be invisible.
            approved = APPROVED_CONFIRMATION.get()
            said = getattr(self, "_inflight_user_text", "") or getattr(self, "_last_user_text", "")
            pending = loop.run_in_executor(
                None,
                lambda: run_tool(dict(args), approved_confirmation=approved, user_text=said),
            )
            if str(args.get("action") or "ask").lower() == "ask":
                # _say_exactly: in gemini_audio mode speak() feeds the live session, which
                # would otherwise improvise its own reaction instead of saying this.
                result = await await_with_ack(
                    pending, lambda: self.speak(_say_exactly("Working on it, sir."))
                )
            else:
                result = await pending
            # A replace a crew bot armed has not yet been put in front of the user over a
            # channel the model cannot edit (the native path announces; this path armed
            # the store itself). Do it here, so what is approved is what was read aloud.
            if (
                isinstance(result, ToolResult)
                and result.error_type == "ConfirmationRequired"
                and isinstance(result.data.get(PREVIEW_KEY), str)
            ):
                result.data[ANNOUNCED_KEY] = bool(
                    self._announce_pending_plan(result.data[PREVIEW_KEY])
                )

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
                set_risk_confirmation_waiver,
            )
            from core.user_prefs.risk_waiver import normalize_category
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
            elif pref_action in ("waive_high_risk_confirmation", "require_high_risk_confirmation"):
                grant = pref_action == "waive_high_risk_confirmation"
                pref_category = (args.get("category") or "").strip()
                if grant and not self._user_words_match(
                    _RISK_WAIVE_COMMAND,
                    window_s=_RISK_WAIVE_WINDOW_S,
                    require_tokens=_RISK_CATEGORY_WORDS.get(
                        normalize_category(pref_category), ("\x00",)
                    ),
                ):
                    # The confirmation gate alone is model-driven (the model can call
                    # confirm_pending_confirmation itself); a standing waiver needs the
                    # user's OWN words naming the category, like voice_lock off.
                    print("[PREFS] risk waiver refused: not grounded in the user's words.")
                    result = (
                        "Not done: a high-risk waiver is only granted when the user says it "
                        "themself, naming the action (e.g. 'stop asking before you shut "
                        "down the computer'). Do not retry; tell the user in one sentence."
                    )
                else:
                    result = set_risk_confirmation_waiver(pref_category, grant)
                _audit_pref(
                    operation="preference",
                    category="risk_waiver_grant" if grant else "risk_waiver_revoke",
                    key=pref_category or "unknown",
                    new_value_summary=result[:120],
                    source_tool="manage_preference",
                    reason="user_granted_risk_waiver" if grant else "user_revoked_risk_waiver",
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
            elif pref_action in ("set_session_window", "set_session"):
                from core.session import set_window_from_text

                seconds = args.get("seconds")
                if seconds in (None, ""):
                    # The phrase itself, when the model passed words instead of
                    # the converted number ("2 minutes").
                    seconds = args.get("rule") or args.get("value") or ""
                result = set_window_from_text(seconds)
            elif pref_action in ("get_session_window", "session_status"):
                from core.session import describe

                result = describe()
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
            # run_self_diagnosis skips the deep analysis on deep=false; say which ran
            # (live 2026-11-17 a quick check was announced as "Deep").
            depth = "Quick" if diag_args.get("deep") is False else "Deep"
            result = (
                f"{depth} self-diagnosis started in the background — I'll report when it's done."
            )

        elif tool_name == "deep_think":
            # Owner 2026-11-18 (PF-044): only on request or for genuinely deep
            # questions. Local Qwen (then Gemma) reasons WITHOUT tools; the full
            # answer is delivered through the reconnect-surviving report path, not
            # the one-sentence background outcome, so it is not cut short.
            from core.deep_think import think as _deep_think

            question = str(args.get("question") or "").strip()
            context = str(args.get("context") or "")
            if not question:
                result = "deep_think needs the question to think about; nothing started."
            else:

                async def _run_deep_think():
                    try:
                        ok, text, _route = await asyncio.to_thread(_deep_think, question, context)
                    except Exception as e:  # noqa: BLE001 - think() never raises; belt and braces
                        ok, text = False, type(e).__name__
                    try:
                        self.ui.write_log(f"DEEP THINK: {text[:2000]}")
                    except Exception:
                        pass
                    spoken = (
                        text if ok else f"I couldn't finish thinking that through, sir — {text}."
                    )
                    self._deliver_or_queue_cm_report(spoken, verbatim=True)

                self._spawn_background_task(_run_deep_think(), "deep_think")
                result = (
                    "Deep thinking started on the local model. It is STILL RUNNING. Tell the "
                    "user in one short phrase that you are thinking it over. Do NOT answer the "
                    "question yourself, do NOT guess, and do NOT call deep_think again — the "
                    "full answer is spoken automatically when it is ready."
                )

        elif tool_name == "protocol_one":
            from actions.protocol_one import (
                confirm_pending_fixes,
                launch_protocol_one,
                protocol_status,
                read_recent,
                read_report,
            )

            p1_args = dict(args)
            p1_action = str(p1_args.get("action") or "status").lower().strip()
            if p1_action == "recent":
                # Read-only: what Era's last tool calls REALLY did (asked vs ran,
                # confirmed or not), straight from the task trace. The answer to
                # "did you actually do that?" - never the model's own recollection.
                result = {
                    "success": True,
                    "message": await asyncio.to_thread(read_recent, p1_args.get("limit")),
                }
            elif p1_action == "report":
                # Read-only: hand back the real report file (or one section), with
                # its age and whether the code changed since. Explicit success, for
                # the same reason as status: the text is made of "failed"/"error".
                section = str(p1_args.get("section") or "").strip() or None
                result = {
                    "success": True,
                    "message": await asyncio.to_thread(read_report, section),
                }
            elif p1_action == "confirm":
                # The one-word approval: apply the fixes a prior cycle staged.
                # Same word-grounding as launch — the user must have SAID "yes"
                # and a fresh batch must be waiting — so a stray affirmation, or
                # "yes" read off a page, never edits Era's code.
                if not self._protocol_one_confirm_commanded():
                    result = {
                        "success": True,
                        "message": (
                            "There are no fixes waiting for your confirmation right now. "
                            "Say 'Launch Protocol One' to run a fresh cycle first. Tell the "
                            "user that in one sentence."
                        ),
                    }
                else:

                    async def _confirm_protocol_one():
                        try:
                            r = await asyncio.to_thread(confirm_pending_fixes, self.ui, self.speak)
                            self.speak(r or "Confirmed fixes applied.")
                        except Exception as e:
                            print(f"[era] [PROTOCOL1] confirm failed: {e}")
                            self.speak("Applying the confirmed fixes hit an error — check the log.")

                    self._spawn_background_task(_confirm_protocol_one(), "protocol_one_confirm")
                    result = "Applying the staged fixes now — I'll brief you when they're in."
            elif p1_action != "launch":
                # Explicit success: ToolResult.from_any judges plain strings by
                # their words, and a health briefing is MADE of "failed"/"error"
                # (it reports them). As a string it was logged as a failed call.
                result = {"success": True, "message": await asyncio.to_thread(protocol_status)}
            elif not self._protocol_one_commanded():
                # The launch edits Era's own code. Only the user's own words may
                # start it — not the model deciding it would be helpful, and not
                # text on a web page or in an email that says "launch protocol one".
                result = (
                    "Protocol One was NOT launched: it runs only when the user says "
                    "'Launch Protocol One'. Tell the user that in one sentence."
                )
            else:

                async def _run_protocol_one():
                    try:
                        r = await asyncio.to_thread(
                            launch_protocol_one, p1_args, self.ui, self.speak
                        )
                        self.speak(r or "Protocol One finished.")
                    except Exception as e:
                        print(f"[era] [PROTOCOL1] cycle failed: {e}")
                        self.speak("Protocol One hit an error — check the log.")

                self._spawn_background_task(_run_protocol_one(), "protocol_one")
                result = (
                    "Protocol One launched in the background — diagnosis first, then "
                    "verified fixes. I'll brief you when the cycle completes."
                )

        elif tool_name == "protocol_zero":
            from actions.protocol_zero import execute_protocol_zero, protocol_zero_status

            p0_args = dict(args)
            if str(p0_args.get("action") or "status").lower().strip() != "execute":
                result = {  # explicit success — see the protocol_one status branch
                    "success": True,
                    "message": await asyncio.to_thread(protocol_zero_status),
                }
            elif not self._protocol_zero_commanded():
                # Execute rewrites part of Era's own system prompt; only the user's
                # own words may start it (VS Code opening is the other trigger,
                # and that one fires from the spies, never from the model).
                result = (
                    "Protocol Zero was NOT executed: it runs only when the user says "
                    "'Launch Protocol Zero'. Tell the user that in one sentence."
                )
            else:

                async def _run_protocol_zero():
                    try:
                        r = await asyncio.to_thread(
                            execute_protocol_zero, p0_args, self.ui, self.speak, "voice"
                        )
                        self.speak(r or "Protocol Zero finished.")
                    except Exception as e:
                        print(f"[era] [PROTOCOL0] sync failed: {e}")
                        self.speak("Protocol Zero hit an error — check the log.")

                self._spawn_background_task(_run_protocol_zero(), "protocol_zero")
                result = (
                    "Protocol Zero executing in the background — auditing my capabilities "
                    "against my prompt. I'll brief you on anything it changes."
                )

        elif tool_name == "visual_engine":
            from core import visual_engine

            try:
                raw_command = args.get("command", "{}")
                command = json.loads(raw_command) if isinstance(raw_command, str) else raw_command
                if not isinstance(command, dict):
                    raise ValueError("Visual command must be a JSON object")
                result = await asyncio.to_thread(
                    visual_engine.snapshot
                    if command.get("action") == "snapshot"
                    else lambda: visual_engine.apply(command)
                )
                if "success" not in result:
                    result = {"success": True, "message": "HUD command applied.", "data": result}
            except ValueError as exc:
                # apply()/snapshot() reject an invalid HUD command by design —
                # edited state is never trusted as markup (and json.loads raises
                # ValueError on malformed JSON). A bad command the model sent is
                # an EXPECTED tool failure, not a crash. Report it gracefully so
                # it logs as tool.execution.failed and never reaches the generic
                # handler that calls log_crash(tool.execution.exception) — which
                # feeds core/self_heal, making it loop trying to "repair" the
                # safety-critical validator behind the guard that blocks it.
                # This mirrors how actions/scheduled_task catches TaskError.
                result = {
                    "success": False,
                    "message": f"Visual command rejected: {exc}",
                    "error_type": "VisualCommandRejected",
                }

        elif tool_name == "autonomous_development":
            from core import development, feature_engine

            try:
                action = args.get("action", "status")
                if action == "propose":
                    paths = json.loads(args.get("file_paths", "[]"))
                    if not isinstance(paths, list) or any(
                        not isinstance(path, str) for path in paths
                    ):
                        raise ValueError("file_paths must be a JSON array of strings")
                    job = await asyncio.to_thread(
                        development.propose,
                        args.get("request", ""),
                        paths,
                        reload=args.get("reload", True),
                    )
                    result = f"Development {job['id']} started in staging. Review verified changes in the HUD; final approval is required."
                elif action == "inspect":
                    result = json.dumps(
                        await asyncio.to_thread(
                            development.inspect_request, args.get("request", "")
                        )
                    )
                elif action in ("retry", "reject"):
                    job_id = str(args.get("job_id", "") or "").strip()
                    if job_id.lower() in ("", "latest", "last", "newest", "current"):
                        # The model says "the latest job"; ids are 16 hex chars.
                        jobs = await asyncio.to_thread(development.list_jobs)
                        job_id = jobs[0]["id"] if jobs else ""
                    act = development.retry if action == "retry" else development.reject
                    result = json.dumps(await asyncio.to_thread(act, job_id))
                elif action == "status":
                    await asyncio.to_thread(feature_engine.restore)
                    result = json.dumps(
                        {
                            "jobs": await asyncio.to_thread(development.list_jobs),
                            "features": feature_engine.catalog(),
                        }
                    )
                else:
                    raise ValueError(
                        "Only inspect/propose/status/retry/reject are allowed; final approval belongs to the user in the HUD"
                    )
            except (ValueError, FileNotFoundError) as exc:
                result = {
                    "success": False,
                    "message": f"Development request rejected: {exc}. Use status to list valid job IDs.",
                    "error_type": "ValidationError",
                }

        elif tool_name == "run_feature":
            from core import feature_engine

            arguments = json.loads(args.get("arguments", "{}"))
            if not isinstance(arguments, dict):
                raise ValueError("Feature arguments must be an object")
            await asyncio.to_thread(feature_engine.restore)
            result = json.dumps(await feature_engine.run(args.get("feature_id", ""), arguments))

        elif tool_name == "self_improve":
            from app.era_live.autopilot import LOCAL_EXECUTION

            improve_args = dict(args)
            improve_action = str(improve_args.get("action") or "propose").lower().strip()

            if improve_action in ("propose", "list_backups", "find", "locate", ""):
                # Read-only and fast enough to answer inline.
                r = await asyncio.to_thread(
                    self_improve, improve_args, None, self.ui, None, self.speak
                )
                result = r or "Done."
            elif improve_action == "apply":
                from core import development

                target = improve_args.get("file_path")
                job = await asyncio.to_thread(
                    development.propose,
                    improve_args.get("description", ""),
                    [target] if target else [],
                    reload=improve_args.get("reload", True),
                )
                result = f"Candidate {job['id']} is being generated and verified in staging. Final approval is required in the HUD."
            elif refusal := await asyncio.to_thread(self._self_improve_refusal, improve_args):
                # She cannot write, so she must not say she is working. See
                # _self_improve_refusal — this is the whole of item 25.
                result = refusal
            else:
                # apply/rollback: LLM generation + verification can take a while —
                # run in the background exactly like self_diagnosis.
                async def _run_self_improve():
                    try:
                        outcome: dict = {}
                        r = await asyncio.to_thread(
                            self_improve,
                            improve_args,
                            None,
                            self.ui,
                            None,
                            None if LOCAL_EXECUTION.get() else self.speak,
                            outcome,
                        )
                        report = self._autopilot_reply if LOCAL_EXECUTION.get() else self.speak
                        report(r or "Self-improvement finished.")
                        if outcome.get("reload_requested"):
                            self._reload_after_self_edit(outcome)
                    except Exception as e:
                        print(f"[era] [IMPROVE] background self-improve failed: {e}")
                        report = self._autopilot_reply if LOCAL_EXECUTION.get() else self.speak
                        report(
                            "The self-improvement attempt hit an error — my code was "
                            "not left modified. Check the log."
                        )

                self._spawn_background_task(_run_self_improve(), "self_improve")
                result = (
                    "Working on my own code in the background — I'll back it up, "
                    "verify it, and report the outcome."
                )

        elif tool_name == "self_heal":
            heal_args = dict(args)
            if str(heal_args.get("action") or "status").lower().strip() != "heal":
                result = await asyncio.to_thread(self_heal_tool, heal_args, self.ui, self.speak)
            else:
                # Generation + verification take a while — same pattern as self_improve.
                async def _run_self_heal():
                    try:
                        r = await asyncio.to_thread(self_heal_tool, heal_args, self.ui, self.speak)
                        self.speak(r.get("message") or "Self-heal finished.")
                    except Exception as e:
                        print(f"[era] [SELF-HEAL] background heal failed: {e}")
                        self.speak("The self-heal attempt hit an error — my code was not changed.")

                self._spawn_background_task(_run_self_heal(), "self_heal")
                result = (
                    "Looking for the fault in my log and working on a fix in the "
                    "background — I'll report what I find."
                )

        elif tool_name == "agent_browser":
            r = await loop.run_in_executor(
                None, lambda: agent_browser(parameters=args, player=self.ui)
            )
            result = r or "Done."

        elif tool_name == "scheduled_task":
            from actions.scheduled_task import scheduled_task
            from core.cancel_intent import is_cancel_all_live_order, is_spoken_cancel_task_order

            utterance = getattr(self, "_inflight_user_text", "") or getattr(
                self, "_last_user_text", ""
            )
            if args.get("action") not in {"list", "history", "status", "get", "unread"} and (
                is_cancel_all_live_order(utterance) or is_spoken_cancel_task_order(utterance)
            ):
                return ToolResult.fail(
                    "That command cancels Live Activity work, not scheduled jobs. Schedules were not changed.",
                    error_type="BlockedAction",
                )

            st_args = dict(args)
            # Authority comes from what the USER said, never from the arguments.
            st_authority = self._scheduled_task_authority()
            # Executor thread: approve/run_now may run a tool, and the scheduler
            # hands that back to this loop via run_coroutine_threadsafe.
            result = await loop.run_in_executor(None, lambda: scheduled_task(st_args, st_authority))

        elif tool_name == "gmail_control":
            # Imported here, not at module load: Era must start normally on a
            # machine where the Google client libraries were never installed.
            from actions.gmail_control import gmail_control, set_notifier

            self._ensure_gmail_notifier(set_notifier)
            gmail_args = dict(args)
            gmail_action = str(gmail_args.get("action") or "").lower().strip()

            if gmail_action in ("authorize", "connect", "auth", "login"):
                # The consent flow blocks on a browser round-trip for up to
                # three minutes. Run it in the background so the microphone
                # stays live while the user is signing in to Google.
                async def _run_gmail_authorize():
                    try:
                        r = await asyncio.to_thread(gmail_control, gmail_args, None, self.ui)
                        self.speak(r or "The Gmail authorization finished.")
                    except Exception as e:
                        print(f"[era] [GMAIL] authorization failed: {e}")
                        self.speak("The Gmail authorization did not complete.")

                self._spawn_background_task(_run_gmail_authorize(), "gmail_authorize")
                result = (
                    "I've opened Google's consent page in your browser. Approve the "
                    "access there and I'll confirm when it's done. Say nothing else "
                    "about it until I report back."
                )
            else:
                r = await loop.run_in_executor(
                    None, lambda: gmail_control(parameters=gmail_args, player=self.ui)
                )
                result = r or "Done."

        else:
            result = f"Unknown tool: {tool_name}"

        from core.live_app_state import refresh_after_command

        return await refresh_after_command(tool_name, args, result)
