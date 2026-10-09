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
from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG
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


def _sched_clean(text: str) -> str:
    return _SCHED_CORRECTION_PREFACE.sub(" ", (text or "").lower()).strip()


def _sched_refused(low: str) -> bool:
    """A refusal/deferral word or a question — outside any waiver phrase, where "don't
    ask again" / "no need to ask" MEAN yes."""
    return bool(_SCHED_REFUSAL.search(_SCHED_WAIVE_COMMAND.sub(" ", low))) or bool(
        _SCHED_QUESTION.search(low)
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
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=resumption_config,
            context_window_compression=compression_config,
            realtime_input_config=realtime_input_config,
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
        # HUD Live Activity: the real call, as it enters and leaves this gate.
        hud_args = fc_args_to_dict(fc)
        hud_call = hud.tool_started(getattr(fc, "id", None), name, hud_args)
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
            return build_function_response(
                fc, ToolResult.ok(message=_CREW_BG_ACK), types.FunctionResponse
            )

        # In-flight count: a planned GoAway renewal must never close the socket
        # while an action is mid-execution, because the tool result would have
        # nowhere to go and the model would be left believing it never ran.
        # Counted rather than a bool — tool batches can overlap.
        self._tools_in_flight = getattr(self, "_tools_in_flight", 0) + 1
        result = None
        try:
            result = await self._execute_tool_result(fc)
        finally:
            self._tools_in_flight = max(0, getattr(self, "_tools_in_flight", 1) - 1)
            if result is None:  # raised or was cancelled: never leave the row "running"
                hud.tool_finished(
                    hud_call,
                    name,
                    hud_args,
                    ToolResult(success=False, error="No result (raised or cancelled)"),
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
        self._trace_routed = self._trace_safety = None
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

    # ── Crew: run off the receive loop, report the real outcome ─────────────
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
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(None, lambda: run_tool(dict(hud_args)))
            except Exception as exc:  # noqa: BLE001 — a crew crash is still an outcome to report
                result = ToolResult.fail(
                    message=f"The crew failed: {type(exc).__name__}",
                    error=f"The crew failed: {type(exc).__name__}",
                    error_type="BotError",
                )
            if not isinstance(result, ToolResult):
                result = ToolResult.from_any(result)

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
        """Switch Era's voice between Fish Audio (primary) and Gemini (fallback).

        Never needs a reconnect: both modes run an AUDIO session, so the change
        is live on the very next sentence, mid-conversation.
        """
        requested = str(args.get("voice") or "").strip()
        resolved = VOICE_CFG._resolve_alias(requested)
        if resolved is None:
            return (
                f"I don't know a voice called '{requested}'. Ask the user whether they "
                "mean my Fish voice or my Gemini voice."
            )

        wants_fish = resolved == MODE_FISH_AUDIO

        if wants_fish and not is_fish_configured():
            return (
                "I can't switch to my Fish voice — no Fish Audio API key is configured. "
                "Tell the user that, and that I'm staying on my Gemini voice."
            )

        already_active = (wants_fish and VOICE_CFG.is_fish()) or (
            not wants_fish and VOICE_CFG.is_gemini()
        )
        if already_active:
            # Already speaking in this voice, but possibly only via a transient
            # fallback. Cancel any pending auto-restore and, when this isn't yet
            # the saved preference, commit it so a relaunch keeps it.
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""
            label = "Fish" if wants_fish else "Gemini"
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

        label = "Fish" if applied == MODE_FISH_AUDIO else "Gemini"
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
            if len(self._recent_user_texts) > 8:
                del self._recent_user_texts[:-8]

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
        era_relaunch.request_relaunch(reason=reason, source=source, **extra)
        recoverable = supervised or era_relaunch.spawn_supervisor(_os.getpid())
        if not recoverable:
            print("[era] [RELAUNCH] WARNING: no supervisor and could not start one.")
        log_event(
            main_logger,
            "era.relaunch.requested",
            "WARNING",
            supervised=supervised,
            recoverable=recoverable,
            reason=reason,
            source=source,
        )
        self._shutdown_requested = True
        self._shutdown_awaiting_confirm = False
        return recoverable

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
            # She may not have started talking yet.
            for _ in range(30):
                if self._is_speaking:
                    break
                time.sleep(0.1)
            # Wait for speech to finish AND stay finished. The reload path
            # queues two utterances (the self-edit summary, then "restarting
            # now"), and the gap between them must not be mistaken for silence
            # — exiting in it truncates the sentence that explains the restart.
            quiet = 0
            while quiet < 15 and time.time() < deadline:
                quiet = 0 if self._is_speaking else quiet + 1
                time.sleep(0.1)
            if close_hud:
                try:
                    from app.hud.browser import close_hud_windows

                    close_hud_windows()
                except Exception as e:
                    print(f"[era] [SHUTDOWN] could not close the HUD window: {e}")
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
            self._arm_relaunch(
                reason="user said 'reboot Era One'",
                source=str(args.get("_source", "voice")),
                interrupted_job=(f"{job.get('brand')}/{job.get('task_type')}" if job else None),
            )
            self.ui.write_log("SYS: Reboot ordered; relaunching Era One.")
            self.speak("Rebooting now, sir. Back in a few seconds.")
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
        """Runs the call under its own id, so an exception a tool catches can
        only ever be attributed to the call it happened in — never to another
        tool, a background thread, or whatever a web page's text points at."""
        from core import handled_exceptions

        try:
            handled_exceptions.install_context_executor(asyncio.get_running_loop())
        except Exception as e:
            print(f"[era] [SELF-HEAL] context executor not installed: {e}")
        token = handled_exceptions.CURRENT_CALL.set(handled_exceptions.new_call_id())
        try:
            # Through the class, not self: the confirmation tests bind this method
            # onto a stub object that has no _execute_tool_result_inner of its own.
            return await ToolDispatchMixin._execute_tool_result_inner(self, fc)
        finally:
            handled_exceptions.CURRENT_CALL.reset(token)

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

        try:
            user_text = getattr(self, "_last_user_text", "") or ""
            # The whole utterance so far, not just the last transcript chunk: the
            # model often calls its tool before the turn completes, and "reboot" /
            # "Era One" can arrive in different chunks.
            inflight = getattr(self, "_inflight_user_text", "") or user_text
            tool_name, parameters = divert_era_reboot(tool_name, parameters, inflight)
            tool_name, parameters = route_tool_call(tool_name, parameters, user_text)
            if (tool_name, parameters) != (original_name, original_params):
                print(
                    f"[era] [ROUTE] routed {original_name} {original_params} -> {tool_name} {parameters}"
                )
        except Exception as route_error:
            print(f"[era] [WARN] routing layer skipped: {route_error}")

        print(f"[era] [ARGS] {tool_name}  {parameters}")
        self._trace_routed = (tool_name, dict(parameters))  # for the task trace

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

        if requires_confirmation:
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
            raw = await self._execute_tool_raw(tool_name, parameters, fc)
            self._last_tool_name = tool_name
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
                action=parameters.get("action"),
                error_type=result.error_type,
                # Without the message, self_diagnosis could only guess at causes
                # ("2x tool=file_controller") — and a cancelled confirmation looks
                # identical to a crash. The message is what tells them apart.
                error=result.short_message(200),
                duration_ms=result.duration_ms,
            )
            self._log_caught_exception(tool_name, parameters, result)

        return result

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
            self.enter_standby()
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
            task_speak = getattr(self, "speak_dispatched", None) or self.speak
            task_id = get_queue().submit(
                goal=args.get("goal", ""), priority=priority, speak=task_speak
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
            r = await loop.run_in_executor(
                None, lambda: media_coordinator(parameters=args, player=self.ui)
            )
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
            from crew.runtime import await_with_ack, run_tool

            pending = loop.run_in_executor(None, lambda: run_tool(dict(args)))
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

        elif tool_name == "protocol_one":
            from actions.protocol_one import (
                confirm_pending_fixes,
                launch_protocol_one,
                protocol_status,
            )

            p1_args = dict(args)
            p1_action = str(p1_args.get("action") or "status").lower().strip()
            if p1_action == "confirm":
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
                command = json.loads(args.get("command", "{}"))
                result = json.dumps(
                    await asyncio.to_thread(
                        visual_engine.snapshot
                        if command.get("action") == "snapshot"
                        else lambda: visual_engine.apply(command)
                    )
                )
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

            action = args.get("action", "status")
            if action == "propose":
                paths = json.loads(args.get("file_paths", "[]"))
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
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
                    await asyncio.to_thread(development.inspect_request, args.get("request", ""))
                )
            elif action == "retry":
                result = json.dumps(
                    await asyncio.to_thread(development.retry, args.get("job_id", ""))
                )
            elif action == "reject":
                result = json.dumps(
                    await asyncio.to_thread(development.reject, args.get("job_id", ""))
                )
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

        return result
