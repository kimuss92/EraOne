import os

# Force Qt to use standard scaling pass-throughs before the window hooks launch
os.environ["QT_SCALE_FACTOR"] = "1"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_LOGGING_RULES"] = "qt.qpa.window=false"

import asyncio
import base64
import json
import re
import sys
import threading

# --- Console encoding: prevent mojibake in PowerShell ---
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
import time
import traceback
from typing import Any

active_era_instance = None
from pathlib import Path

# Block 0-byte junk files from stray writes landing in the project root, and log
# a stack trace of whatever attempts one so its origin can be fixed at source.
try:
    from core.root_file_guard import install as _install_root_file_guard

    _install_root_file_guard()
except Exception:
    pass

# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────
from app.cm_intents import (  # noqa: F401  (re-export: preserves public + internal names)
    _ADS_INTENT,
    _APPROVE_INTENT,
    _CAMPAIGN_INTENT,
    _PUBLISH_INTENT,
    _REVISE_INTENT,
    _RUSH_INTENT,
    _infer_cm_task_type,
)
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

# A typed order to go to sleep. Needed because the safety gate classifies anything
# this short as SHORT_UNCLEAR and answers "Looks like a short message, what should I
# do with it?" instead of acting — so "take a break" sent from Telegram never reached
# enter_standby(). Matched BEFORE the gate, on the whole message only (fullmatch-ish
# anchors): "go offline" as a standalone order sleeps her, but the same words buried
# in "tell me why the server went offline" must not.
#
# Standby-logic overhaul (2026-09-16): the accepted set is EXACTLY the four
# sanctioned triggers — "standby", "take a break", "go offline",
# "enter stand by mode" — and nothing else. The older, broader set (French
# veille/dors/bonne nuit, goodnight, go to sleep, ciao, bare "sleep") was
# deliberately removed so conversational noise and phonetic near-misses can no
# longer sleep her. The whole-message anchors are what enforce "no partial
# matches"; "standby"/"stand by" are the same word spelled two ways.
_STANDBY_INTENT = re.compile(
    r"^\W*(?:era\s*[,;:]?\s*)?"
    r"(?:enter\s+stand\s*by\s+mode|stand\s*by|take\s+a\s+break|go\s+offline)"
    r"\W*$",
    re.IGNORECASE,
)


from app.era_constants import (  # noqa: F401
    _AWAITING_REPLY_WINDOW_S,
    _CM_HANDOFF_WINDOW_S,
    _PENDING_ASK_ALLOWED_TOOLS,
    _RESUME_ACK,
)
from app.text_intents import (  # noqa: F401  (re-export: preserves public + internal names)
    _CTRL_RE,
    _PATH_ACTION_VERBS,
    _RAW_UNC_PATH_RE,
    _RAW_WINDOWS_PATH_RE,
    _THEME_INTENT,
    _build_paste_clarify_instruction,
    _clean_transcript,
    _extract_raw_path,
    _is_bare_path_paste,
    _model_intent,
    _say_exactly,
    _theme_intent_mode,
    _unwrap_say_exactly,
    _voice_switch_intent,
    build_resume_note,
    is_drop_request,
    is_resume_request,
)


def _wake_intent_matches(text: str) -> bool:
    """True when a typed message is a wake order.

    Built from the LIVE wake-word config, not a hard-coded list: the wake word is
    user-changeable at runtime (`set_wake_word`, config/wake_word.json), and a typed
    wake must track a spoken one or Telegram silently stops being able to wake her.
    Word-boundary anchored — the wake word inside a longer word must not fire.
    """
    words = [WAKE_CFG.word, *WAKE_CFG.aliases]
    pattern = "|".join(re.escape(w) for w in words if w)
    if not pattern:
        return False
    return bool(re.search(rf"\b(?:{pattern})\b", text, re.IGNORECASE))


# --- Suppress noisy pywinauto STA COM warning ---
import warnings

from google import genai

from core.llm.quota_failover import install as _install_quota_failover

# Gemini quota -> spare key -> Claude, for every tool's generate_content call.
_install_quota_failover()

from app.hud.events import hud
from app.notify import (  # noqa: F401  (re-export: preserves public + internal names)
    _tg_notify,
)
from core.conversation_log import ConversationLog
from core.era_state import EraState
from core.input_safety_gate import CLARIFICATION_PHRASE, InputSafetyGate
from core.live_turn_state import LiveTurnState, TurnSnapshot
from core.logger import log_crash, log_event, main_logger
from core.pending_confirmation import get_shared_store
from core.session_manager import SessionManager
from core.speaker_recognition import get_verifier
from core.turn_memory import TurnMemory

# ──────────────────────────────────────────────────────────────
from core.utils import BASE_DIR, get_api_key, get_api_keys, log
from core.vad import build_barge_in_detector, get_barge_in_fade_ms, get_resume_config
from core.voice_gate import WakeAudioTap, WakeVerifier

warnings.filterwarnings("ignore", message="Revert to STA COM threading mode", category=UserWarning)
# ------------------------------------------------

from actions.file_controller import file_controller

# NOTE: the wake-word match regex is no longer a hard-coded constant here — it
# lives on WAKE_CFG (see WakeWordConfig below) so Era can change its own wake
# word at runtime via the set_wake_word tool without a code edit or restart.


# ----- NEW IMPORTS FOR STANDBY / WAKE WORD -----
try:
    import vosk
except ImportError:
    vosk = None
# -----------------------------------------------


API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
from agent.executor import AgentExecutor
from agent.task_queue import get_queue
from app.audio_config import (  # noqa: F401  (re-export: preserves public + internal names)
    CHANNELS,
    CHUNK_SIZE,
    RECEIVE_SAMPLE_RATE,
    SEND_SAMPLE_RATE,
)
from app.wake import (  # noqa: F401  (re-export: preserves public + internal names)
    _DEFAULT_WAKE_DECOYS,
    VOSK_MODEL_PATH,
    WAKE_CFG,
    WAKE_WORD,
    WAKE_WORD_CONFIG_PATH,
    WakeWordConfig,
    init_vosk,
)

get_queue().set_executor_factory(lambda: AgentExecutor())


from app.era_live.audio import AudioIOMixin
from app.era_live.cascade import CascadeMixin
from app.era_live.cm_flow import CMFlowMixin
from app.era_live.dispatch import DispatchMixin
from app.era_live.local_fallback import LocalFallbackMixin
from app.era_live.mic_mute import MicMuteMixin
from app.era_live.model_route import ModelRouteMixin
from app.era_live.panic import PanicStopMixin
from app.era_live.reconnect_mixin import ReconnectMixin
from app.era_live.speech_out import SpeechOut
from app.era_live.tool_batch import ToolBatchMixin
from app.era_live.tool_dispatch import ToolDispatchMixin
from app.reconnect import (  # noqa: F401  (re-export: preserves public + internal names)
    grow_cooldown,
    reconnect_acknowledgment_text,
)
from app.system_prompt import (  # noqa: F401  (re-export: preserves public + internal names)
    _load_system_prompt,
)
from app.tool_declarations import TOOL_DECLARATIONS  # noqa: F401  (re-export)
from core.tts_fish import is_configured as _fish_configured
from core.voice_mode import MODE_FISH_AUDIO, VOICE_CFG


class EraLive(
    ToolDispatchMixin,
    ToolBatchMixin,
    ModelRouteMixin,
    DispatchMixin,
    CMFlowMixin,
    ReconnectMixin,
    MicMuteMixin,
    PanicStopMixin,
    AudioIOMixin,
    LocalFallbackMixin,
    CascadeMixin,
):
    def __init__(self, ui: Any):
        self.ui = ui
        self.session = None
        self.audio_in_queue = None
        self.out_queue = None
        self._loop = None
        self._is_speaking = False
        self._speaking_lock = threading.Lock()
        self.ui.on_text_command = self._on_text_command
        self.ui.on_file_upload = self._on_file_upload
        self._turn_done_event: asyncio.Event | None = None

        # ----- VOICE LOCK STATE (persisted to memory/user_prefs.json) -----
        # On startup, read the saved preference and apply it to the verifier
        # so the user's setting survives restarts and reconnects.
        try:
            from core.user_prefs import get_voice_lock_enabled

            saved_lock = get_voice_lock_enabled()
            verifier = get_verifier()
            # load_settings() already applies the saved pref when the verifier is
            # built, so these normally agree; reconcile anyway so a future path
            # that constructs it from config alone still honours the preference.
            if verifier.enabled != saved_lock:
                verifier.set_enabled(saved_lock)
            # Logged unconditionally: the restore is otherwise invisible, and
            # "did my setting survive the relaunch?" must be answerable from the log.
            state = "ON" if verifier.enabled else "OFF"
            print(f"[VOICE-LOCK] Voice recognition {state} — restored from memory/user_prefs.json")
        except Exception as e:
            print(f"[VOICE-LOCK] Could not restore persisted state: {e}")
        # ---------------------------------------------------------------------

        # ----- SPEECH OUTPUT (Fish Audio primary) -----
        # Constructed unconditionally, started lazily: it costs nothing while
        # Era is on the default Gemini voice, and it must already exist the
        # moment a quota drop flips her to the local one. The supervisor keeps
        # the Voicebox backend warm for the same reason — a first-run model
        # download during an outage would defeat the whole design.
        self.speech_out = SpeechOut(self)

        # ----- BARGE-IN (Copilot-style real-time interruption) -----
        # While Era speaks, the mic loop feeds frames to this detector instead of
        # dropping them (half-duplex); a positive detection stops her voice at
        # once and drops to LISTENING. `_barge_in_requested` gates the receive
        # path so a Gemini turn that is still streaming server-side cannot refill
        # the audio queue after the user has cut in. See app/era_live/audio.py.
        self._barge_in = build_barge_in_detector()
        self._barge_in_requested = False
        # Duck length for a graceful stop on interruption (0 = hard cut).
        self._barge_in_fade_ms = get_barge_in_fade_ms()

        # ----- TTS PRIMARY/FALLBACK (Fish Audio -> Gemini) -----
        # In a transcript-synth mode (fish_audio) Gemini's own
        # audio is held here rather than discarded, so if the primary backend
        # fails mid-turn _tts_fallback can flush it and the reply still lands.
        # _tts_primary_mode remembers what to go BACK to once Fish recovers.
        self._cloud_audio_buffer: list[bytes] = []
        self._tts_primary_retry_at = 0.0
        self._tts_primary_mode = ""
        # Last time _play_audio asked for a reconnect to re-enumerate PortAudio so
        # a pinned voice device that connected AFTER startup becomes routable.
        # Rate-limits that request so a flapping/absent device can't loop it.
        self._voice_reroute_requested_at = 0.0

        # ----- CONTEXTUAL RESUMPTION (Phase 2) -----
        # When a barge-in cuts Era off, `turn_memory` stashes the unfinished
        # thought so she can pick it back up. `_live_turn_text` mirrors what she
        # is currently saying (updated in _receive_audio) so trigger_barge_in can
        # snapshot it. The autonomous "shall I finish about X?" offer is gated by
        # `_resume_offer_enabled` (config barge_in.resume_offer, default off);
        # explicit "continue"/"never mind" work regardless.
        _resume_cfg = get_resume_config()
        self.turn_memory = TurnMemory(ttl_s=_resume_cfg["ttl_s"])
        self._resume_offer_enabled = _resume_cfg["offer"]
        self._live_turn_text = ""

        # ----- LIVE TURN STATE (received / played / generated / completed) -----
        # `_live_turn_text` above is a transcript mirror and proves only that the
        # MODEL spoke. This tracks what the USER actually heard, plus the model's
        # own generation_complete / interrupted signals, which is what lets a
        # reconnect tell "the answer finished" from "the socket cut it off".
        # See core/live_turn_state.py for the signal semantics and their source.
        self.live_turn = LiveTurnState()
        # Snapshot taken at the instant the transport died; consumed once by the
        # recovery decision on the next successful connect.
        self._disconnect_snapshot: TurnSnapshot | None = None
        # Monotonic id for the live connection. Every event carries the id of the
        # connection it came from, so a late callback from a socket we have
        # already replaced cannot mutate the new one's state.
        self._conn_generation: int = 0
        self._conn_started_at: float = 0.0
        # Set when the user speaks after a drop: their input outranks any
        # pending automatic continuation.
        self._user_spoke_since_drop: bool = False
        # Set from the server's GoAway; the receive loop takes the renewal at the
        # first idle moment before it, or on the deadline itself.
        self._renewal_deadline: float | None = None
        self._tools_in_flight: int = 0

        # ----- CONVERSATION CONTINUITY ACROSS SESSIONS -----
        # `turn_memory` above holds ONE interrupted sentence, for a barge-in.
        # This holds the last few COMPLETED exchanges, for a session boundary —
        # a quota drop, an expired resumption handle, a relaunch. Loaded from
        # disk so a restart mid-conversation resumes the thread instead of
        # starting cold; injected by _build_config only when Gemini has no
        # resumption handle of its own (see core/conversation_log.py).
        self.conversation_log = ConversationLog()
        self.conversation_log.load()
        # The user line awaiting Era's reply, for pairing in the recap.
        self._recap_pending_user: str = ""

        # ----- FILE INDEX WARM-UP -----
        # Build/refresh the file index in the background at startup so
        # "open <file/folder>" resolves instantly instead of falling back to a
        # multi-second os.walk. Non-blocking (spawns a daemon thread).
        try:
            from actions.file_indexer import init_file_index

            init_file_index()
        except Exception as _idx_err:
            print(f"[era] [WARN] File index warm-up skipped: {_idx_err}")
        # ------------------------------

        # Set while a Telegram-originated order is in flight, so the reply Era speaks
        # aloud is also mirrored back to the chat. Off by default: when you're sitting
        # at the machine, every spoken aside would otherwise land in Telegram too.
        self._tg_reply_pending = False

        # Expiry of the window during which Era is waiting on an ANSWER, armed when
        # a reply of hers ends in a question (see _note_era_question). The typed
        # channel needs this and the spoken one does not: speech goes straight into
        # the live session, which remembers what it just asked, while text passes
        # through a safety gate that sees one message at a time. Without it that
        # gate reads "Yes" as a two-word fragment of unknown intent instead of the
        # answer to the question Era asked one second earlier.
        self._awaiting_reply_until: float = 0.0

        # ----- STANDBY / WAKE WORD ATTRIBUTES -----
        self._standby_active = False  # True when muted and waiting for wake word
        self._standby_entering = False  # True during the standby-notification speak window
        self._vosk_model = init_vosk()
        self._wake_recognizer: Any | None = None
        self._wake_queue: asyncio.Queue[bytes] | None = None
        self._wake_task: asyncio.Task | None = None
        self._wake_tap = WakeAudioTap()
        self._wake_verifier: WakeVerifier | None = None

        # Wake arming state
        self._wake_armed = False
        self._wake_ignore_until: float = 0.0
        self._last_wake_queue_missing_log_ts: float = 0.0
        self._last_wake_partial_text: str = ""
        self._last_wake_partial_count: int = 0
        # ------------------------------------------

        # ----- BROWSER MIC TOGGLE -----
        self._browser_mic_active = True  # browser mic ON by default
        # Throttle for repetitive audio-drop logs so a muted/off mic doesn't
        # spam the console once per audio chunk (~every 250ms). Keyed by reason.
        self._audio_drop_log_ts: dict[str, float] = {}
        # ------------------------------

        # ----- PENDING CONFIRMATION -----
        self._pending_store = get_shared_store()
        self._shutdown_requested = False
        # True once Era has asked "a job is running, confirm shutdown?" and is
        # waiting for the user's yes. Gates the `confirmed` flag so a stray
        # confirmed=true can never bypass the running-job check.
        self._shutdown_awaiting_confirm = False
        # ---------------------------------

        # ----- SESSION MANAGER AND SAFETY GATE -----
        self.session_manager = SessionManager()
        self.safety_gate = InputSafetyGate()
        # -------------------------------------------

        # ----- LAST USER TEXT (for route_tool_call context) -----
        self._last_user_text: str = ""
        # The current voice turn's transcript so far (reset at turn_complete).
        self._inflight_user_text: str = ""
        self._inflight_started_at: float = 0.0
        # Rolling window of recent complete user utterances (timestamp, text)
        # for the save_memory gate: the explicit ask and the save call are
        # often several tool-turns apart, so the gate must look further back
        # than the single last utterance. Bounded; see _note_user_utterance.
        self._recent_user_texts: list[tuple[float, str]] = []
        # A CM status report (community_manager result) that finished while there
        # was no live session to speak it into — speak() no-ops with no session,
        # so without this the result would just vanish. Held here and delivered
        # as a concise "what you missed" summary the moment the next reconnect
        # succeeds, instead of the run silently completing into an empty room.
        # See _deliver_or_queue_cm_report / _build_reconnect_acknowledgment.
        self._pending_cm_announcement: str | None = None
        # Set when a save_memory call is blocked: (timestamp, category, key,
        # value). A plain user affirmation within _MEMORY_CONFIRM_WINDOW_S
        # authorizes the retry Era makes after asking aloud.
        self._blocked_memory_save: tuple[float, str, str, str] | None = None
        # Timestamps of self-initiated (unprompted) memory writes, for the rate
        # cap in _autonomous_memory_save_allowed.
        self._auto_memory_saves: list[float] = []
        # --------------------------------------------------------

        # ----- HERMES / CM DISPATCH (Stage 3 — typed text only; voice is its own
        # later stage, see [[era-hermes-routing]] for why) -----
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_cm_ask: dict[str, Any] | None = None
        # Set to an expiry timestamp the moment a CM utterance is claimed; while it
        # is in the future the live Gemini model is muted for that turn so only the
        # headless CM lane acts. See _CM_HANDOFF_WINDOW_S and _cm_handoff_active().
        self._cm_handoff_until: float = 0.0
        # save_memory is otherwise blocked unless the user explicitly asked; a
        # deliberate CM-pipeline step may set this True around a controlled write
        # (and MUST reset it after). Never left True across turns. See the
        # save_memory guard for the two-condition rule.
        self._memory_write_authorized: bool = False
        # --------------------------------------------------------

        # ----- TOKEN LOG RATE-LIMIT -----
        self._last_token_log_ts = 0.0
        self._token_log_interval = 30.0  # seconds between token log lines
        # --------------------------------

        # ----- GOAWAY GRACEFUL TEARDOWN -----
        self._go_away_event = asyncio.Event()
        # -------------------------------------

        # ----- RECONNECT BACKOFF -----
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = 6
        # Consecutive disconnects that were plainly "no internet"; see run().
        self._offline_streak: int = 0
        self._has_completed_initial_boot: bool = False
        # Gemini's periodic Live-session rotation is expected, not a failure:
        # it is counted separately and never feeds the failure streak.
        self._rotation_count: int = 0
        self._rotation_reconnect: bool = False
        # Set when a resumption handle was refused, so the connect loop
        # retries immediately WITHOUT it instead of serving a backoff for
        # a fault that is already fixed. See _is_resumption_rejection.
        self._handle_rejected_retry: bool = False
        # After this many *non-quota* failures in a row, assume the saved
        # resumption handle may be stale and drop it so the next attempt starts
        # a clean session instead of wedging on a dead handle.
        self._stale_token_after: int = 2
        # -----------------------------

        # ----- QUOTA COOLDOWN -----
        # When Gemini reports quota exhaustion (1011), retrying fast only burns
        # more quota and never succeeds. Instead we wait a long, growing cooldown
        # and recover automatically once quota returns. Start at 15 min, double
        # each time it's still exhausted, cap at 2 hours, never give up.
        self._quota_cooldown_base: float = float(
            os.environ.get("ERA_QUOTA_COOLDOWN_SECONDS", "") or 900.0
        )
        self._quota_cooldown_max: float = 7200.0  # 2 hours
        self._quota_cooldown_current: float = self._quota_cooldown_base
        # Why the last session dropped: "rotation" | "quota" | "auth" | "transient".
        self._last_disconnect_kind: str = "transient"
        # Set once the model has refused a text-only session, so neither the
        # quota escape hatch nor a manual switch can put Era back into a voice
        # mode this model cannot connect in. Per-process, not persisted: it is a
        # fact about the current model, and a new model deserves a fresh try.
        self._text_modality_unsupported: bool = False

        # Human-readable connection status, mirrored to disk for the UI/tools.
        self._connection_status: str = "Offline"
        self._connection_status_path = Path(
            os.environ.get("ERA_CONNECTION_STATUS_FILE", "")
            or str(Path.home() / ".era" / "era_connection.json")
        )
        # --------------------------

        # ----- IDLE AUTO-STANDBY -----
        self._last_interaction_timestamp: float = time.time()
        self._idle_timeout_seconds: float = 300.0
        # -----------------------------

    # ── awaiting-reply window ────────────────────────────────────────────────

    def _note_era_question(self, reply: str) -> None:
        """Arm (or disarm) the awaiting-reply window from Era's own last reply.

        Called on every completed turn. A reply ending in a question mark means
        the next thing the user says is an ANSWER, so short input stops being
        "unclear" and becomes the thing Era is waiting for. A reply that asks
        nothing clears the window immediately — leaving it armed would let a
        stale question keep waving short fragments through to the model.
        """
        body = (reply or "").strip().rstrip("\"')]}*_ ")
        self._awaiting_reply_until = (
            time.time() + _AWAITING_REPLY_WINDOW_S if body.endswith("?") else 0.0
        )

    def _awaiting_reply_active(self) -> bool:
        """True only while Era's own last reply was a question and is still fresh.
        Self-expiring: no timer, the next read after the window sees it inactive."""
        return time.time() < self._awaiting_reply_until

    def _text_state_snapshot(self) -> dict:
        """Session state as the TYPED path sees it, with the two context flags the
        safety gate cannot work out on its own: whether a tool is awaiting a
        yes/no, and whether Era herself has a question outstanding."""
        snapshot = self.session_manager.get_state_snapshot()
        snapshot["pending_confirmation_active"] = self._pending_store.is_pending()
        snapshot["awaiting_reply"] = self._awaiting_reply_active()
        return snapshot

    def _tg_ack(self, message: str) -> None:
        """Answer a Telegram-originated order that ends WITHOUT a model turn.

        Every early return in _on_text_command is a path where no turn_complete
        will ever fire, so nothing would disarm _tg_reply_pending and nothing
        would reply. The sender then sees silence — indistinguishable, from
        another network, from Era being offline — and the next thing Era says to
        whoever is in the room gets mirrored to the chat instead. No-op when the
        order came from the desk.
        """
        if not self._tg_reply_pending:
            return
        self._tg_reply_pending = False
        _tg_notify(message)

    def _on_text_command(self, text: str):
        # HUD conversation record: the typed/Telegram order as the user sent it.
        # Sentinels ("__mute__" etc.) are skipped inside user_input. Never raises.
        hud.user_input(text, "telegram" if self._tg_reply_pending else "typed")

        # === "abort mission", TYPED — FIRST, above everything ===
        # Nothing may outrank it, not even the reboot order: an abort is the
        # cheaper escape from a runaway loop, and the user should not have to
        # restart Era to get out of one. Needs no session, no model and no
        # tool call, which is the whole point — see core/panic_stop.py.
        if self._is_abort_order(text):
            self._note_user_utterance(text)
            self.ui.write_log(f"You: {text}")
            self.trigger_abort("typed", text)
            return

        # === "reboot Era One", TYPED (desk or Telegram) ===
        # First, and needing no session: a reboot is for when Era is lagging or
        # half-broken, which is exactly when the model may not be reachable.
        if self._is_reboot_order(text):
            self._note_user_utterance(text)
            self.ui.write_log(f"You: {text}")
            self._tg_ack("Rebooting now, sir. Back in a few seconds.")
            self._shutdown_era({"relaunch": True, "_source": "typed"})
            return

        # === Voice switch, TYPED ===
        # This must be reachable without the model, because the situation it
        # exists for is exactly the one where the model is unreachable: during a
        # quota outage there is no session, and the mic dies with it, so a
        # SPOKEN "switch to your local voice" can never be heard. Intercepted
        # here, ahead of everything else, it works with Era fully offline.
        voice_target = _voice_switch_intent(text)
        if voice_target is not None:
            self._apply_voice_switch(voice_target)
            self._tg_ack("Voice switch applied, sir.")
            return

        # === Model routing override, TYPED ===
        # Placed immediately after the voice switch, and for the same reason:
        # it is a local preference that needs no session, so it must stay
        # reachable when the model is not. Deliberately AFTER the voice switch,
        # because a bare "use gemini" has always meant the cloud VOICE and must
        # keep meaning that — see _MODEL_INTENT's collision note.
        model_token = _model_intent(text)
        if model_token is not None:
            applied = self.apply_model_override(model_token)
            self.ui.write_log(f"SYS: {applied}")
            self._tg_ack(applied)
            return

        # === Contextual resume / drop, TYPED ===
        # Only when a thought is actually stashed, so a bare "continue" with
        # nothing pending falls through to normal command routing (it might mean
        # "continue the movie"). Mirrors the voice intercept in _receive_audio.
        if self.turn_memory.has_pending():
            if is_drop_request(text):
                self.turn_memory.clear()
                self.ui.write_log("SYS: Dropped the earlier thread, sir.")
                self._tg_ack("Dropped it, sir.")
                return
            if is_resume_request(text):
                turn = self.turn_memory.take()
                if turn is not None and self._loop and self.session:
                    self._note_user_utterance(text)
                    self._last_interaction_timestamp = time.time()
                    asyncio.run_coroutine_threadsafe(
                        self.session.send_realtime_input(
                            text=build_resume_note(turn.spoken, turn.remainder)
                        ),
                        self._loop,
                    )
                    return

        # === Claude dispatch (phase 10, feature-flagged, default OFF) ===
        # Only fires when the flag is on AND the user has explicitly pinned a
        # non-voice model. should_dispatch() owns every condition; this call
        # site just hands the work to the loop so the blocking subprocess never
        # runs on the UI thread.
        # Shadow: record what AUTOMATIC routing would have done with this.
        # Observe-only and cost-free; see core/llm/shadow.py for why it stores
        # the classification and never the utterance.
        self.observe_routing(text, handled_by="typed")

        dispatch_decision = self.should_dispatch(text)
        if dispatch_decision is not None:
            self._note_user_utterance(text)
            self._last_interaction_timestamp = time.time()
            self.publish_route(dispatch_decision)
            self.ui.write_log(f"You: {text}")
            self.start_dispatch(text, dispatch_decision)
            return

        # === F4 toggle: enter or exit standby ===
        if text.strip() == "__standby__":
            if not self._standby_active:
                self.enter_standby()
            return
        if text.strip() == "__unstandby__":
            if self._standby_active:
                self._exit_standby()
            return

        # === React mute/power button: maps to same standby toggle ===
        if text.strip() == "__mute__":
            if not self._standby_active:
                self.enter_standby()
            return
        if text.strip() == "__unmute__":
            if self._standby_active:
                self._exit_standby()
            return

        # === INTERCEPTION STANDBY : Permet au rÃ©seau (iPhone/Watch) de rÃ©veiller ERA ===
        if self._standby_active:
            clean_cmd = text.lower().strip()
            # A request to ENTER standby while already in standby must never
            # wake her. Era can already be asleep (idle timeout) without the
            # user realizing, and "era, enter standby mode" used to hit the
            # bare "era" substring below and answer "Back online, sir." — the
            # exact opposite of what was asked. Checked FIRST so a standby
            # request always wins over a wake-phrase match in the same text.
            if _STANDBY_INTENT.match(clean_cmd):
                self.ui.write_log("SYS: Already in standby — standby request ignored.")
                if self._tg_reply_pending:
                    self._tg_reply_pending = False
                    _tg_notify("Already in standby, sir.")
                return
            # Wake on the CONFIGURED wake word (currently "shazam"), not a hard-coded
            # list. The old pattern matched "era"/"active"/"wake up", so the wake word
            # the user actually set could not wake her from Telegram at all.
            if _wake_intent_matches(clean_cmd):
                print("[WAKE] Remote wake phrase detected via network shortcut!")
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._resume_from_standby("remote_wake", speak_ack=True), self._loop
                    )
                    asyncio.run_coroutine_threadsafe(
                        send_era_notification_async("ERA", _RESUME_ACK), self._loop
                    )
                # That notification is itself a Telegram message, so the wake is already
                # acknowledged in the chat; mirroring the spoken ack would double it.
                self._tg_reply_pending = False
                return
            else:
                self.ui.write_log(f"SYS: Ignored remote command '{text}' while in standby.")
                # No turn happens, so nothing would disarm the mirror or answer the
                # sender. From Telegram, a standby drop is indistinguishable from Era
                # being offline — say which it is.
                if self._tg_reply_pending:
                    self._tg_reply_pending = False
                    _tg_notify(f'I\'m in standby — send "{WAKE_CFG.word}" to wake me first.')
                return

        # "mute your mic" / "start listening" typed at the desk or from Telegram.
        # Before the session check: muting must work with Era offline, and a
        # typed "start listening" is the fallback when voice unmute can't hear.
        if self._handle_typed_mic_order(text):
            return

        if not self._loop or not self.session:
            # Gemini is down but the local fallback is serving: the local brain
            # answers typed and Telegram orders instead of "session isn't up".
            if self.local_submit_text(text):
                return
            if self._tg_reply_pending:
                self._tg_reply_pending = False
                _tg_notify("I'm not fully awake yet — my session isn't up. Try again shortly.")
            return

        # A typed "take a break" / "go offline" is a STANDBY ORDER, not conversation.
        # It must be handled here, before the safety gate: the gate sees a two-word
        # message, classifies it SHORT_UNCLEAR, and asks "what should I do with it?" —
        # so the order was answered instead of executed. Same path as the F4 sentinel.
        if _STANDBY_INTENT.match(text.strip()):
            self.ui.write_log(f"SYS: Standby ordered by text: '{text.strip()}'")
            # enter_standby() speaks its own confirmation, which the turn mirror then
            # relays to Telegram — so leave _tg_reply_pending armed here.
            self.enter_standby()
            return

        # A typed theme order ("go dark", "switch to light mode") is a UI toggle,
        # not conversation — handle it before the gate for the same reason as
        # standby: the gate would ask "what should I do with it?".
        theme_mode = _theme_intent_mode(text)
        if theme_mode:
            self._note_user_utterance(text)
            self._last_interaction_timestamp = time.time()
            set_theme = getattr(self.ui, "set_theme", None)
            if set_theme:
                set_theme(theme_mode)
                self.ui.write_log(f"SYS: Theme switched to {theme_mode} mode.")
                self._tg_ack(f"Switched to {theme_mode} mode, sir.")
            else:
                self._tg_ack("I can't reach the interface to change the theme, sir.")
            return

        # A pending "which brand?" reply must resolve BEFORE the generic safety
        # gate. A bare brand name like "Delta Cuisine" is a valid answer to an
        # outstanding CM ask, but the gate reads it as SHORT_UNCLEAR (<=2 words)
        # and bails at ASK_CLARIFICATION, so _consume_pending_cm_ask (downstream
        # in _route_text_through_hermes) never ran. Check it first: on a match,
        # re-dispatch the original order and skip the gate; on no match the ask
        # is consumed (one-shot) and we fall through to normal safety + Hermes.
        # See [[era-hermes-routing]].
        if self._pending_cm_ask:
            payload = self._consume_pending_cm_ask(text)
            if payload is not None:
                # _on_text_command runs off the event-loop thread, so hand the
                # (potentially long) CM job to the loop the same way the normal
                # path below does — asyncio.create_task would raise here.
                self._note_user_utterance(text)
                self._last_interaction_timestamp = time.time()
                asyncio.run_coroutine_threadsafe(
                    self._run_cm_flow(payload),
                    self._loop,
                )
                return
            # No match: the pending ask is now consumed (one-shot). Fall through
            # so this reply is treated as a fresh standalone input by the safety
            # gate + Hermes below.

        # Carries the two context flags the gate cannot derive on its own: an
        # action awaiting a yes/no, and a question of Era's still outstanding.
        # Either one makes a short "proceed" a REPLY rather than SHORT_UNCLEAR.
        state_snapshot = self._text_state_snapshot()

        # ── Pasted-data clarification (bare path / link) ──────────────────
        # A bare file path or web link with no verb is DATA, not a command, and
        # is never a CM/Hermes order — so short-circuit it here, before the gate,
        # and hand it to the live model wrapped in an "identify this and ask what
        # to do" instruction. Era then asks instead of silently ignoring it, and
        # voice + text stay complementary (the user answers by speaking or typing:
        # "open it", "save it"). Longer pasted blobs and short unclear snippets are
        # handled downstream so they can't steal a CM order that merely lacks a
        # recognized verb (see _route_text_through_hermes + the ASK handler below).
        paste_kind = self.safety_gate.classify_typed_input(text, state_snapshot)
        if paste_kind in ("PATH", "URL"):
            self._note_user_utterance(text)
            self._last_interaction_timestamp = time.time()
            wrapped = _build_paste_clarify_instruction(paste_kind, text)
            print(f"[PASTE] Classified typed input as {paste_kind}; asking user what to do.")
            if self._loop and self.session:
                asyncio.run_coroutine_threadsafe(
                    self.session.send_realtime_input(text=wrapped),
                    self._loop,
                )
            else:
                self._tg_ack("I can see that, sir, but my session is down — ask me again shortly.")
            return
        # ──────────────────────────────────────────────────────────────────

        # typed=True: the ASR-artefact rules (fillers, noise, low confidence,
        # repeat suppression) exist to defend against a MICROPHONE and have no
        # meaning for a keystroke. Applying them here is what made the typed
        # channel behave unlike the spoken one. See InputSafetyGate.validate_input.
        decision = self.safety_gate.validate_input(
            transcript=text,
            confidence=None,
            state_snapshot=state_snapshot,
            typed=True,
        )

        if decision.action == "BLOCK_SILENTLY":
            print(f"[SAFETY] Blocked text input: '{text}' — {decision.reason}")
            self.safety_gate.record_blocked(text)
            # Silent is right at the desk (the UI log says why) but wrong from
            # Telegram, where it reads as Era being dead. Say what happened.
            self._tg_ack(f"I couldn't act on that right now — {decision.reason.lower()}.")
            return

        if decision.action == "ASK_CLARIFICATION":
            print(f"[SAFETY] Clarification for text input: '{text}' — {decision.reason}")
            self.safety_gate.record_blocked(text)
            # For a typed snippet that's just unclear/vague (not misheard audio),
            # asking "Sorry, I didn't catch that" is wrong — Era read it fine, she
            # just doesn't know the intent. Hand the actual text to the model and
            # let her ask a grounded, useful question ("You typed 'X' — save it,
            # send it, look it up?"). Keeps typed + voice complementary: the user
            # answers by voice or text. The generic phrase stays only as a fallback
            # when the session isn't available to ask.
            useful_ask_codes = {"SHORT_UNCLEAR", "VAGUE_SHORT_INPUT", "URL_WITHOUT_COMMAND"}
            if decision.reason_code in useful_ask_codes and self._loop and self.session:
                self._note_user_utterance(text)
                self._last_interaction_timestamp = time.time()
                wrapped = _build_paste_clarify_instruction("UNCLEAR", text)
                asyncio.run_coroutine_threadsafe(
                    self.session.send_realtime_input(text=wrapped),
                    self._loop,
                )
                return
            self.ui.write_log(f"SAFETY: {CLARIFICATION_PHRASE}")
            self.speak(CLARIFICATION_PHRASE)
            # speak() only mirrors on the kokoro branch; on gemini_audio the
            # session answers and turn_complete disarms. Either way the Telegram
            # sender must not be left waiting on a turn that may never come.
            self._tg_ack(CLARIFICATION_PHRASE)
            return

        # Input accepted — update last user text for memory authorization
        self._note_user_utterance(text)
        self._last_interaction_timestamp = time.time()

        # Typed input is real user speech for feedback purposes. Only the voice
        # path used to run this, so a standing rule TYPED into the command bar
        # ("minimize Spotify after use") was recorded nowhere and never survived
        # the turn, let alone a relaunch. Same gate, same grounding guarantee:
        # this is the user's own text, never model- or page-supplied content.
        try:
            self._maybe_record_feedback(text)
        except Exception as _fb_err:
            print(f"[Memory] [WARN] feedback detection skipped: {_fb_err}")

        raw_path = _extract_raw_path(text)
        if raw_path and not _is_bare_path_paste(text, raw_path):
            # Explicit instruction attached to the path ("open D:\foo",
            # "delete D:\foo") — act immediately, same as before.
            loop = self._loop
            if loop and loop.is_running():
                result = file_controller(
                    parameters={"action": "open", "path": raw_path},
                    player=self.ui,
                )
                self.ui.write_log(f"PATH: {result}")
                # No model turn happens on this path, so this is the only place
                # the Telegram sender can be told whether the file opened.
                self._tg_ack(str(result))
                return
        # A bare pasted path (no verb) falls through to Era instead of being
        # silently opened — she needs to see it to ask what you want done
        # with it, and to be able to answer "did you get that path?" later.

        asyncio.run_coroutine_threadsafe(self._route_text_through_hermes(text), self._loop)

    _UPLOAD_DIR = BASE_DIR / "runtime" / "uploads"
    _MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB — generous for images/PDFs/docs

    def _on_file_upload(self, name: str, mime: str, data_b64: str):
        """Handle a file dropped/selected in the React UI.

        Browsers never expose a dropped file's real filesystem path (that's
        sandboxed), so previously the UI only sent Era the bare filename as
        text — leaving her to guess a path for file_processor, which is
        exactly how "File not found: ...Temp\\31.png" happened. This saves
        the actual bytes to a real path and tells Era about it as a normal
        conversation turn (same fix shape as the bare-path-paste bug) so she
        never has to guess, and can always answer "what happened to my
        upload?" — success or failure.
        """
        note: str
        try:
            raw = base64.b64decode(data_b64 or "", validate=False)
        except Exception as e:
            self.ui.write_log(f"WARN: Upload of '{name}' failed to decode: {e}")
            note = f"[User tried to upload a file named '{name}' but it failed to decode and was not saved.]"
        else:
            if not raw:
                note = f"[User tried to upload a file named '{name}' but no data arrived — nothing was saved.]"
            elif len(raw) > self._MAX_UPLOAD_BYTES:
                self.ui.write_log(
                    f"WARN: '{name}' is too large to upload ({len(raw) / (1024 * 1024):.1f} MB)"
                )
                note = (
                    f"[User tried to upload '{name}' ({len(raw) / (1024 * 1024):.1f} MB) "
                    f"but it exceeds the upload limit and was not saved.]"
                )
            else:
                safe_name = (
                    re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name or "upload").name).strip()
                    or "upload"
                )
                try:
                    self._UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    dest = self._UPLOAD_DIR / f"{int(time.time())}_{safe_name}"
                    if raw and len(raw) > 0:
                        dest.write_bytes(raw)
                    else:
                        # Prevent 0-byte truncated upload files
                        self.ui.write_log("WARN: Upload created 0-byte file; skipped.")
                except Exception as e:
                    self.ui.write_log(f"ERR: Could not save uploaded file '{name}': {e}")
                    note = f"[User tried to upload '{name}' but saving it failed: {e}]"
                else:
                    self.ui.current_file = str(dest)
                    self.ui.write_log(f"FILE: Uploaded {name} ({len(raw) / 1024:.0f} KB)")
                    note = (
                        f"[User uploaded a file: {name} ({mime or 'unknown type'}), "
                        f"saved at {dest}. Ask what they'd like done with it before "
                        f"calling file_processor — do not guess a different path.]"
                    )

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.session.send_realtime_input(text=note), self._loop
            )

    def _on_browser_mic_toggle(self, active: Any = None):
        # Prefer an ABSOLUTE state from the UI when it sends one ("active": bool).
        # A blind flip can drift out of phase with the UI (a dropped message or a
        # reconnect leaves backend and indicator disagreeing) — the exact failure
        # behind "status says Off but Era still hears me". When the UI tells us the
        # state it wants, we obey it instead of toggling blindly.
        #
        # The indicator shows the EFFECTIVE state (hard toggle AND not voice-muted,
        # see core/mic_mute.py), so a click toggles that: turning the mic ON from
        # the UI also lifts a voice mute ("stop listening"); turning it OFF is the
        # hard-off, which also silences the local voice-unmute listener.
        if active is None:
            target = not self.mic_effective_active()
        else:
            target = bool(active)
        self._browser_mic_active = target
        if target and self._mic_muted:
            self.unmute_microphone("mic button", speak_ack=False)
        # Echo the authoritative backend state back so every client's indicator
        # matches reality, not its own local guess.
        self._broadcast_mic_state()

    def _apply_voice_switch(self, target: str) -> None:
        """Apply a typed or spoken voice switch. Works with Era fully offline.

        Fish and Gemini share one AUDIO session shape, so the change is live on
        the next sentence — no reconnect, no session teardown.
        """
        resolved = VOICE_CFG._resolve_alias(target)
        if resolved is None:
            self.ui.write_log(f"SYS: Unknown voice '{target}'. Say 'fish' or 'gemini'.")
            return

        wants_fish = resolved == MODE_FISH_AUDIO
        if wants_fish and not _fish_configured():
            self.ui.write_log("SYS: Fish Audio has no API key — staying on the Gemini voice.")
            return

        already_active = (wants_fish and VOICE_CFG.is_fish()) or (
            not wants_fish and VOICE_CFG.is_gemini()
        )
        if already_active:
            # Era is already speaking in this voice — but she may be here only via
            # a transient fallback. An explicit request still (a) cancels any
            # pending auto-restore that would flip her back, and (b) commits this
            # as the standing preference when it isn't already, so it survives a
            # relaunch. This is the case that used to let "stay on Gemini" be
            # silently undone by the recovery timer.
            self._tts_primary_retry_at = 0.0
            self._tts_primary_mode = ""
            label = "Fish" if wants_fish else "Gemini"
            if VOICE_CFG.preferred != resolved:
                VOICE_CFG.set_mode(resolved)  # persist the deliberate choice
                self.ui.write_log(f"SYS: Staying on the {label} voice — saved as your preference.")
            else:
                self.ui.write_log(f"SYS: Already using the {label} voice.")
            return

        try:
            applied = VOICE_CFG.set_mode(resolved)
        except ValueError as e:
            self.ui.write_log(f"SYS: Voice switch failed — {e}")
            return

        # Drop audio queued in the OLD voice so a half-played sentence does not
        # finish in the other one.
        self._flush_audio_in_queue()
        # A deliberate switch overrides any pending fallback-recovery timer.
        self._tts_primary_retry_at = 0.0

        label = "Fish" if applied == MODE_FISH_AUDIO else "Gemini"
        print(f"[VOICE] Switch -> {applied}")
        self.ui.write_log(f"SYS: Voice switched to {label}.")
        self.speak(f"Switching to my {label} voice, sir.")

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            changed = self._is_speaking != value
            self._is_speaking = value
        # Arm/disarm the barge-in detector only on the actual transition, never
        # on the redundant set_speaking(True) that _play_audio issues per chunk —
        # re-arming each chunk would keep resetting the onset grace and the
        # detector could never accumulate enough voiced audio to fire.
        det = getattr(self, "_barge_in", None)
        if value:
            if changed:
                # A fresh spoken turn: clear any stale interrupt and re-arm.
                self._barge_in_requested = False
                if det is not None:
                    det.note_speaking(True)
            self.ui.set_state("SPEAKING")
            self.session_manager.transition_to(EraState.SPEAKING, reason="speaking_started")
        elif not self.ui.muted:
            if changed and det is not None:
                det.note_speaking(False)
            self.ui.set_state("LISTENING")
            self.session_manager.transition_to(EraState.LISTENING, reason="speaking_finished")

    def _current_client(self):
        """A genai client bound to the key currently in play.

        Rebuilt per connect attempt because rotating keys means a new client;
        the old code built one client up front, which is why a spare key could
        never actually be used.
        """
        keys = getattr(self, "_api_keys", None) or [get_api_key()]
        idx = getattr(self, "_api_key_index", 0) % len(keys)
        return genai.Client(api_key=keys[idx], http_options={"api_version": "v1beta"})

    def _rotate_api_key(self) -> bool:
        """Move to the next Gemini key. False if there is no other one to try.

        Quota is per-key, so this converts a 15-minute cooldown into an
        immediate retry. Returns False once every key has been tried since the
        last success, which is what stops rotation from becoming a hot loop
        around several equally-exhausted keys.
        """
        keys = getattr(self, "_api_keys", None) or []
        if len(keys) < 2:
            return False
        tried = getattr(self, "_api_keys_tried", 0) + 1
        if tried >= len(keys):
            self._api_keys_tried = 0
            return False
        self._api_keys_tried = tried
        self._api_key_index = (getattr(self, "_api_key_index", 0) + 1) % len(keys)
        print(f"[era] [AUTH] Rotating to Gemini key #{self._api_key_index + 1}/{len(keys)}.")
        return True

    def _install_loop_exception_handler(self, loop) -> None:
        """Route exceptions the event loop would otherwise swallow into era.log.

        sys.excepthook and threading.excepthook (see _install_crash_guards) do
        NOT cover asyncio: a task that dies without being awaited, or a callback
        scheduled with call_soon_threadsafe that raises, is reported by the loop
        itself and never reaches either hook. Those failures previously showed
        up only as "Task exception was never retrieved" on stderr — invisible in
        the log Era self-diagnoses from.

        Deliberately does NOT swallow anything: it logs and then delegates to
        the default handler, so behaviour is unchanged apart from being visible.
        """
        default = loop.get_exception_handler()

        def _handler(lp, context):
            exc = context.get("exception")
            try:
                if exc is not None:
                    log_crash(
                        main_logger,
                        "asyncio.loop",
                        type(exc),
                        exc,
                        exc.__traceback__,
                        event="engine.loop.uncaught",
                        level="ERROR",
                        message=str(context.get("message", ""))[:200],
                        future=str(context.get("future", ""))[:200],
                    )
                else:
                    log_event(
                        main_logger,
                        "engine.loop.uncaught",
                        "ERROR",
                        source="asyncio.loop",
                        message=str(context.get("message", ""))[:200],
                        # "Task was destroyed but it is pending!" carries the
                        # task (its coroutine name + creation site) only here;
                        # without it the 2026-09-27 instance was untraceable.
                        task=str(context.get("task") or context.get("future") or "")[:300],
                    )
            except Exception:
                pass  # a logging failure must never take down the loop
            if default is not None:
                default(lp, context)
            else:
                lp.default_exception_handler(context)

        try:
            loop.set_exception_handler(_handler)
        except Exception as e:
            print(f"[era] [WARN] could not install loop exception handler: {e}")

    def speak(self, text: str):
        """Make Era say something. Behaviour depends on the active voice mode.

        gemini_audio — text is sent INTO the live session as conversational
        input, and Gemini speaks its own reaction to it. It does not read the
        string back verbatim, which is why callers that need a literal utterance
        wrap it in _say_exactly().

        fish_audio — the text is synthesized by Fish and spoken LITERALLY.
        Crucially this path needs **no session at all**, so Era can still talk
        during a quota outage — the situation that today prints
        "[STANDBY] Network offline. Skipping cloud TTS." and leaves her silent.

        Called from non-loop threads (tool_dispatch, cm_flow, reconnect_mixin,
        actions/premiere_control), so both branches must stay non-blocking.
        """
        text = (text or "").strip()
        if not text:
            return

        # No session, local fallback serving: speak literally through Fish or
        # Piper. Neither branch below can (both end at the missing session).
        if self.session is None and self.local_speak(_unwrap_say_exactly(text)):
            self._tg_ack(_unwrap_say_exactly(text))
            return

        if VOICE_CFG.uses_speech_out():
            # This branch never touches the live session, so no turn_complete
            # will fire and the Telegram mirror in _receive_audio can never run.
            # An order given from Telegram while Era is on the local voice would
            # otherwise be answered ALOUD, in an empty room, and the sender would
            # see nothing at all. Mirror here instead — _tg_ack disarms, so the
            # reply lands exactly once whichever path speaks it.
            spoken = _unwrap_say_exactly(text)
            self._tg_ack(spoken)
            # On this branch speak() IS the final utterance — nothing downstream
            # produces a turn_complete — so this is where a question of Era's has
            # to arm the awaiting-reply window, exactly as _receive_audio does for
            # the cloud voice. Without it the window would silently depend on
            # which voice mode happened to be active.
            self._note_era_question(spoken)
            loop = self._loop
            if loop and loop.is_running():
                # Hop to the loop thread: the streamer's queue and worker task
                # are not thread-safe.
                loop.call_soon_threadsafe(self.speech_out.say, text)
            else:
                self.speech_out.say(text)
            return

        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(self.session.send_realtime_input(text=text), self._loop)

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} â€” {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    # ----- STANDBY / WAKE WORD METHODS -----

    def _wake_word_found(self, text: str) -> bool:
        """Check if text contains the current wake word (or an alias) using a
        word-boundary regex. Reads the live pattern from WAKE_CFG so a wake-word
        change via set_wake_word applies without restarting."""
        normalized = text.lower().strip()
        return bool(WAKE_CFG.pattern.search(normalized))

    def _new_wake_recognizer(self):
        """Build a grammar-constrained Vosk recognizer for the wake word.

        Constraining the decoder to WAKE_CFG.grammar is what makes wake detection
        reliable — free-form recognition mishears noise as "huh"/"era".
        """
        rec = vosk.KaldiRecognizer(self._vosk_model, 16000, WAKE_CFG.grammar)
        # Vosk word timings restart at 0 with every recognizer, so the audio tap
        # used for the wake-word voice check must restart with it.
        self._wake_tap.clear()
        # Emit per-word confidences on final results. Without this, Result() has
        # no confidence field and the final-path confidence gate can never pass —
        # which forces every wake through the noisier partial path and is the
        # root cause of false wakes. With SetWords we get a real score to gate on.
        try:
            rec.SetWords(True)
        except Exception:
            pass
        return rec

    def _reset_wake_recognizer(self) -> None:
        """Create a fresh Vosk recognizer to clear stale audio state."""
        if self._vosk_model:
            self._wake_recognizer = self._new_wake_recognizer()

    def _flush_wake_queue(self) -> int:
        """Drain all pending audio chunks from _wake_queue. Returns count removed."""
        count = 0
        if self._wake_queue is None:
            return 0
        while not self._wake_queue.empty():
            try:
                self._wake_queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        return count

    def _flush_audio_in_queue(self) -> int:
        """Drain all pending model-output audio chunks. Returns count removed.

        Prevents a stale/delayed spoken turn (e.g. a sign-off that finished
        streaming late) from playing back across the standby boundary, since
        _play_audio has no standby gate of its own.

        In kokoro_text mode the queue is only half the story: sentences waiting
        to be synthesized have not reached it yet, and would refill it moments
        after this drain. So the streamer is emptied here too — this is already
        the single choke point for "stop stale speech crossing a boundary", and
        local synthesis is just a new source of the same staleness.
        """
        count = 0
        try:
            count += self.speech_out.interrupt()
        except Exception:
            pass
        if self.audio_in_queue is None:
            return count
        while not self.audio_in_queue.empty():
            try:
                self.audio_in_queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        return count

    async def _run_wake_listener(self) -> None:
        """Dedicated wake word listener. Processes audio from _wake_queue and detects wake word."""
        print("[WAKE] Listener starting")
        try:
            while self._standby_active:
                if self._wake_queue is None or self._wake_recognizer is None:
                    print("[WAKE] Listener missing queue/recognizer; exiting")
                    break

                data = await self._wake_queue.get()

                # --- Pre-checks: skip processing if not ready ---
                if not self._wake_armed:
                    continue
                now = time.time()
                if now < self._wake_ignore_until:
                    continue
                if self._is_speaking:
                    continue

                # --- Process Vosk ---
                # Stay silent on ambient speech/noise — only log and act when
                # the actual wake word is detected.
                self._wake_tap.append(data)
                if self._wake_recognizer.AcceptWaveform(data):
                    result = json.loads(self._wake_recognizer.Result())
                    final_text = result.get("text", "").lower().strip()
                    # Confidence-gated match. A final result carries a real
                    # confidence score; below WAKE_CFG.min_confidence the
                    # decoder was unsure (often because it force-fitted a
                    # near-miss like "shadow" → "shazam"), so we reject it.
                    # Derive a real confidence from the per-word scores emitted by
                    # SetWords(True). Vosk has no top-level "confidence" field, so
                    # the old `result.get("confidence", 0.0)` always yielded 0.0 and
                    # silently killed the final gate. We average the word "conf"
                    # scores instead; only when NO scored words are present do we
                    # fall back to None (regex-only match), matching match_final's
                    # documented contract.
                    confidence: float | None = None
                    words = result.get("result") or []
                    confs = [float(w["conf"]) for w in words if isinstance(w, dict) and "conf" in w]
                    if confs:
                        confidence = sum(confs) / len(confs)
                    else:
                        raw_conf = result.get("confidence")
                        if raw_conf is not None:
                            try:
                                confidence = float(raw_conf)
                            except (TypeError, ValueError):
                                confidence = None
                    if final_text and WAKE_CFG.match_final(final_text, confidence):
                        # The right WORD is not enough: the wake phrase itself must
                        # be the enrolled owner's voice. Fail-closed — with no
                        # profile/model, voice can never wake Era (F4 still can).
                        # Said twice in a row, both takes are verified together.
                        if self._wake_verifier is None:
                            self._wake_verifier = WakeVerifier(get_verifier())
                        verdict = await asyncio.to_thread(
                            self._wake_verifier.check, self._wake_tap, result
                        )
                        if not verdict.accepted:
                            print(
                                f"[WAKE] Wake word from unverified voice ignored "
                                f"(text={final_text!r}, {verdict.reason})"
                            )
                            continue
                        print(
                            f"[WAKE] Wake word detected from final "
                            f"(text={final_text!r}, conf={confidence}, {verdict.reason})"
                        )
                        hud.wake_detected()
                        await self._resume_from_standby("wake_word_final", speak_ack=True)
                        break
                    elif final_text and WAKE_CFG.pattern.search(final_text):
                        # Regex matched but confidence was too low — Vosk
                        # force-fitted something else onto the wake word.
                        # Log it so rejections are visible for tuning.
                        print(
                            f"[WAKE] Rejected low-confidence candidate "
                            f"(text={final_text!r}, conf={confidence}, "
                            f"min={WAKE_CFG.min_confidence})"
                        )
                # NOTE: partial results are deliberately NOT a wake trigger.
                # Vosk partials are cumulative within an utterance — once the
                # grammar-constrained decoder force-fits noise onto the wake
                # word, the SAME partial text repeats on every subsequent
                # chunk until the utterance ends, so any "repeat N times"
                # gate is satisfied by a single mis-decode. That made the
                # partial path the source of every false wake. Finals carry
                # real per-word confidence (SetWords) and are the ONLY
                # trigger; the ~0.5s end-of-utterance latency is the cost of
                # never waking on the wrong word.
        except asyncio.CancelledError:
            print("[WAKE] Listener cancelled")
        except Exception as e:
            print(f"[WAKE] Listener error: {e}")
        finally:
            print("[WAKE] Listener stopped")

    def _stop_wake_word_listener(self):
        """Stop the wake word listener task safely."""
        task = self._wake_task
        self._wake_task = None
        self._wake_queue = None
        self._wake_recognizer = None

        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None

        if task and not task.done() and task is not current:
            task.cancel()

    def enter_standby(self, network_failure: bool = False):
        if self._standby_active:
            return

        # Stop any existing listener
        self._stop_wake_word_listener()

        # Mark "entering" — the receive loop checks this to avoid killing the
        # notification speech's turn (see _receive_audio). _standby_active is
        # NOT set yet so the TTS audio plays through the normal unguarded path.
        self._standby_entering = True

        self.ui.set_state("STANDBY")
        self.ui.write_log("SYS: Entering standby mode.")

        # Speak the notification NOW, while still in normal mode. The audio
        # arrives through the normal receive path with no standby gates.
        if not network_failure:
            print("[STANDBY] Speaking notification...")
            # speak() feeds the live session as if the USER had said this, so a bare
            # affirmation was treated as conversation and paraphrased — answering
            # "I'm online. What's the goal?" while actually going to sleep. Pin the
            # wording verbatim the way the CM brand-ask does.
            #
            # Standby-logic overhaul (2026-09-16): the spoken phrase is EXACTLY
            # "System offline." — no other phrasing is permitted, and the wake word
            # is NEVER spoken aloud (this line used to say "Say {wake word} to
            # resume", which both broke the wake-word constraint and used a
            # forbidden phrase). The user already knows the wake word; the Telegram
            # text notice (not spoken) still names it for a remote wake.
            self.speak(
                "[System note: Say aloud exactly this and nothing more, then stop: "
                '"System offline." '
                "Do not add any other words, do not ask a question, do not use any tool.]"
            )
        else:
            print("[STANDBY] Network offline. Skipping cloud TTS.")

        async def wait_and_mute():
            try:
                if not network_failure:
                    # Wait for the notification speech to actually start
                    for _ in range(50):
                        if self._is_speaking:
                            break
                        await asyncio.sleep(0.1)
                    # Wait for it to finish
                    while self._is_speaking or not self.audio_in_queue.empty():
                        await asyncio.sleep(0.1)
                    await asyncio.sleep(0.5)

                # --- NOW activate standby — after the notification has played ---
                self._standby_entering = False
                self._standby_active = True
                self.session_manager.wake_word_active = False
                self.session_manager.transition_to(EraState.STANDBY, reason="user_standby_request")

                # Initialize Vosk wake detection
                if self._vosk_model:
                    self._wake_recognizer = self._new_wake_recognizer()
                    self._wake_queue = asyncio.Queue()
                    print("[WAKE] Queue ready")
                else:
                    self.ui.write_log("WARN: Vosk model missing — cannot wake from standby.")

                # Arm state: disabled until recognizer is fresh
                self._wake_armed = False
                self._wake_ignore_until = time.time() + 9999
                self._last_wake_partial_text = ""
                self._last_wake_partial_count = 0

                # Start wake listener task
                if self._vosk_model and self._loop and self._loop.is_running():
                    try:
                        current_loop = asyncio.get_running_loop()
                        self._wake_task = current_loop.create_task(self._run_wake_listener())
                    except RuntimeError:
                        self._wake_task = asyncio.run_coroutine_threadsafe(
                            self._run_wake_listener(), self._loop
                        )

                # --- Post-speech: flush stale audio, reset recognizer, arm wake ---
                flushed = self._flush_wake_queue()
                self._flush_audio_in_queue()
                self._reset_wake_recognizer()
                self._last_wake_partial_text = ""
                self._last_wake_partial_count = 0
                self._wake_ignore_until = time.time() + 0.8
                self._wake_armed = True
                print(f"[WAKE] Armed (flushed {flushed} stale chunks)")
            except Exception as e:
                print(f"[STANDBY] Mute-wait error: {e}")
                # Ensure standby is activated even on error
                self._standby_entering = False
                self._standby_active = True
            finally:
                self.ui.set_mute(True)

        if self._loop and self._loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
                current_loop.create_task(wait_and_mute())
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(wait_and_mute(), self._loop)
        else:
            self._standby_entering = False
            self._standby_active = True
            self.session_manager.wake_word_active = False
            self.session_manager.transition_to(EraState.STANDBY, reason="user_standby_request")
            self.ui.set_mute(True)

    async def run(self):
        # Publish the loop handle IMMEDIATELY, not on the first successful
        # connect. Every cross-thread caller (speak, _on_text_command, the
        # Telegram bridge, the wake listener) routes work through self._loop,
        # and it used to be assigned only inside the connected block below — so
        # while the very first connect was failing, self._loop stayed None and
        # each of those callers silently took its "no loop" fallback path.
        # For speak() in kokoro_text that path calls SpeechOut.say() inline on
        # the calling thread, which reaches asyncio.create_task() and raises
        # "RuntimeError: no running event loop" (13 of them on 2026-07-29, while
        # a bricked kokoro_text config kept the connect failing).
        #
        # The loop is genuinely running from the moment this coroutine starts;
        # only Era's knowledge of it was late. get_running_loop() is the correct
        # call here — get_event_loop() is deprecated inside a coroutine.
        self._loop = asyncio.get_running_loop()

        # Turn otherwise-invisible failures into logged ones: an exception in a
        # bare create_task() or a callback scheduled via call_soon_threadsafe
        # never reaches the try/except around the connect block, so without this
        # it surfaces only as "Task exception was never retrieved" on stderr.
        self._install_loop_exception_handler(self._loop)

        # A voice mic-mute survives relaunch; re-arm it before the mic opens.
        self.restore_mic_mute()

        # Quota is per-KEY, so a spare key is a far better answer to a 1011 than
        # a cooldown. The client is rebuilt per attempt (see _current_client)
        # rather than once here, because swapping the key means a new client.
        self._api_keys = get_api_keys()
        self._api_key_index = 0
        if len(self._api_keys) > 1:
            print(f"[era] [AUTH] {len(self._api_keys)} Gemini keys available for failover.")

        # Restore a recent session token from disk so a restart can resume the
        # Gemini conversation instead of starting cold. A stale/expired token is
        # dropped inside load_from_disk, so this is always safe.
        if self.session_manager.load_from_disk():
            print("[era] [SESSION] Restored a recent session token from disk.")
        self.set_status("Reconnecting")

        while True:
            # NOTE: there is deliberately no "park until wake" guard here. The
            # mic (_listen_audio) lives inside the session TaskGroup and feeds
            # the Vosk wake listener, so a dead session during standby MUST be
            # restored in the background — otherwise the wake word can never be
            # heard and standby deadlocks. Pacing happens in the backoff below.
            #
            # A stop request IS honoured, though. This is the only guard that
            # works when the loop is between attempts rather than inside a
            # failing one, and it is what lets closing the window end the engine
            # instead of leaving it reconnecting inside a dying process.
            if self._shutdown_requested:
                print("[era] [EXIT] Shutdown requested — leaving the connect loop.")
                self.session = None
                return
            # ── Cascade voice mode: no Gemini Live session at all ──
            # mic -> local STT -> CodeCraft -> Fish/Piper (app/era_live/cascade.py).
            # Taken only when CodeCraft is configured; returns False otherwise,
            # and Gemini Live connects below exactly as before.
            if await self.serve_cascade():
                continue
            try:
                print("[era] [CONNECT] Connecting...")
                # Background reconnects during standby must not flash the UI
                # out of STANDBY.
                if not self._standby_active:
                    self.ui.set_state("THINKING")
                self._go_away_event.clear()
                # Whether THIS attempt is replaying a resumption handle. Read
                # before _build_config so the failure path can tell a rejected
                # handle (drop it, retry now) from a genuinely bad config
                # (retrying is futile) — they report the same INVALID_ARGUMENT.
                resumed_with_handle = self.session_manager.get_resumption_token() is not None
                config = self._build_config()
                # Bound to whichever key is currently in play, so a rotation
                # after a quota drop actually takes effect on the next attempt.
                client = self._current_client()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session = session
                    # self._loop is already bound at the top of run(); it is the
                    # same loop every iteration, so it is deliberately NOT
                    # reassigned here (the old get_event_loop() call is also
                    # deprecated inside a coroutine on 3.14).
                    # ── Connection generation ──
                    # Every connect gets a new id. Events carrying an older id
                    # belong to a socket we have already replaced, so a late
                    # callback cannot mutate this connection's state.
                    self._conn_generation += 1
                    self._conn_started_at = time.time()

                    # ── Audio continuity across the drop ──
                    # The player's queue is NOT blindly recreated. Audio already
                    # received is sound the user is owed: if the answer was cut
                    # off by the transport (not by the user) and frames are still
                    # unplayed, carrying them over is what turns an abrupt
                    # mid-word stop into a brief gap.
                    #
                    # It IS discarded when the user interrupted, when nothing is
                    # outstanding, or when the turn is stale — the original
                    # reason this was a fresh queue still stands: sentences left
                    # in the streamer would otherwise be spoken into the new
                    # session, answering a question from before the drop.
                    _snap = self._disconnect_snapshot
                    _carry = (
                        _snap is not None
                        and not _snap.interrupted
                        and _snap.playback_outstanding > 0
                        and self.audio_in_queue is not None
                        and not self._standby_active
                    )
                    if _carry:
                        carried = self.audio_in_queue.qsize()
                        log_event(
                            main_logger,
                            "engine.audio.carried_over",
                            "INFO",
                            frames=carried,
                            outstanding_bytes=_snap.playback_outstanding,
                            conn_generation=self._conn_generation,
                        )
                        print(f"[era] [AUDIO] Carrying {carried} buffered frame(s) across.")
                    else:
                        self.audio_in_queue = asyncio.Queue()
                        self.speech_out.interrupt()
                    self.out_queue = asyncio.Queue(maxsize=50)
                    self._turn_done_event = asyncio.Event()

                    print("[era] [OK] Connected.")
                    self._offline_streak = 0
                    self.note_cloud_restored()
                    # A successful connection means any prior outage is over —
                    # start the next failure streak from zero and reset the quota
                    # cooldown back to its base length. Captured BEFORE the reset
                    # below so the greeting branch can still tell what kind of gap
                    # (if any) the user just sat through.
                    recovered_from_kind = self._last_disconnect_kind
                    self._reconnect_attempts = 0
                    self._quota_cooldown_current = self._quota_cooldown_base
                    self._last_disconnect_kind = "transient"
                    # The key in play works, so the NEXT outage gets a full
                    # sweep of the spares again. Without this reset the counter
                    # would creep up across unrelated outages and stop rotating
                    # long before the keys were actually exhausted.
                    self._api_keys_tried = 0
                    self.set_status("Connected")
                    self.session_manager.save_to_disk()

                    # ── Session Manager: restore state after reconnect ──
                    self.session_manager.restore_after_reconnect()

                    # ── UX: first boot audible greeting; routine rotations silent;
                    # a genuine outage (quota cooldown / auth fix / network blip)
                    # gets a short acknowledgment naming the last topic instead of
                    # resuming as if nothing happened. ──
                    if self._standby_active:
                        self.ui.set_state("STANDBY")
                        print("[era] [SILENT] Reconnected in background (standby).")
                    elif not self._has_completed_initial_boot:
                        self._has_completed_initial_boot = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: era ready.")
                        self.speak("era is online and ready, sir.")
                        print("[era] [OK] Initial boot complete. Greeting spoken.")
                    else:
                        self.ui.set_state("LISTENING")
                        resume_note = self._build_reconnect_acknowledgment(recovered_from_kind)
                        if resume_note:
                            self.ui.write_log(f"SYS: {resume_note}")
                            # Wrapped: this text is spoken specifically BECAUSE a
                            # reconnect just happened, the exact situation that
                            # produced the 2026-07-21 bug where an unwrapped
                            # reconnect message got answered/acted on by Gemini
                            # instead of just spoken. See _say_exactly's docstring.
                            self.speak(_say_exactly(resume_note))
                            print("[era] [OK] Reconnected — delivered a CM status note.")
                        else:
                            print("[era] [SILENT] Reconnected seamlessly.")

                    # ── Audio-engine re-enumeration (the only safe moment) ──
                    # PortAudio freezes its device list at Pa_Initialize, so a
                    # device first connected after Era started is invisible to
                    # it even though Windows shows it live. Refreshing means
                    # Pa_Terminate, which invalidates EVERY open stream in the
                    # process — including the microphone below. Here the old
                    # TaskGroup has unwound and the new tasks have not started,
                    # so nothing is open. No-op unless a pinned device is
                    # connected and genuinely missing from the engine's list.
                    try:
                        from core.voice_output import refresh_between_sessions

                        refresh_between_sessions()
                    except Exception as _vo_err:
                        print(f"[era] [AUDIO] device refresh skipped: {_vo_err}")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._monitor_idle_state())

                    # ── Finish what the drop cut off ──
                    # The handle restored what the MODEL knows; this restores
                    # what the USER was in the middle of hearing. Deliberately
                    # after the tasks are queued: the first await starts
                    # _receive_audio, so the continuation is already being
                    # listened for when it arrives. A no-op unless the last
                    # disconnect actually caught Era mid-reply — see
                    # _resume_after_disconnect for every case it refuses.
                    await self._resume_after_disconnect()

            except Exception as e:
                # Stash the half-spoken reply BEFORE anything else looks at the
                # error: every branch below ends in a reconnect, and by then
                # _receive_audio has been torn down and _live_turn_text is the
                # only record that Era was mid-sentence at all.
                self._capture_turn_cut_by_drop()

                # One structured record per disconnect, before any classifier
                # runs. Connection AGE is the field that separates a scheduled
                # server renewal (long-lived, GoAway first) from a network or
                # proxy fault (short-lived, no warning) — the two look identical
                # in a bare error string and want opposite responses.
                _leaves = self._flatten_exc(e)
                log_event(
                    main_logger,
                    "engine.connection.closed",
                    "INFO",
                    conn_generation=self._conn_generation,
                    conn_age_s=round(time.time() - (self._conn_started_at or time.time()), 1),
                    had_go_away=self._go_away_event.is_set(),
                    planned_renewal=self._renewal_deadline is not None,
                    has_checkpoint=self.session_manager.get_resumption_token() is not None,
                    checkpoint_stale=self.session_manager.checkpoint_is_stale,
                    error_type=type(_leaves[0]).__name__ if _leaves else type(e).__name__,
                    # Close code / reason only — never the conversation.
                    detail=(str(_leaves[0])[:160] if _leaves else str(e)[:160]),
                )
                self._renewal_deadline = None

                # --- The process is going away; stop, do not reconnect ---
                # Checked FIRST: every classifier below assumes a live
                # interpreter, and once the futures flag is set no attempt can
                # ever succeed, so anything that retries here spins forever
                # inside a half-dead process. See _is_interpreter_shutdown for
                # the 177-error, 2.5-hour zombie this closes.
                if self._is_interpreter_shutdown(e):
                    print("[era] [EXIT] Interpreter is shutting down — stopping the connect loop.")
                    log_event(
                        main_logger,
                        "engine.shutdown.detected",
                        "INFO",
                        source="run_connect_loop",
                        reason="interpreter_shutdown",
                    )
                    self.session = None
                    return

                # --- A resumption handle the server will not take back ---
                # Expired, or belonging to a session it has since dropped.
                # Without this the same dead handle is replayed on every retry
                # and, because it reports INVALID_ARGUMENT, _classify_disconnect
                # calls it "config" and parks Era on a 15-minute re-check loop
                # that can never succeed. Drop it once and connect clean.
                if resumed_with_handle and self._is_resumption_rejection(e):
                    print("[era] [SESSION] Resumption handle rejected — starting a fresh session.")
                    self.ui.write_log("SYS: Could not restore the session — starting a fresh one.")
                    log_event(
                        main_logger,
                        "engine.session.handle_rejected",
                        "WARNING",
                        source="run_connect_loop",
                    )
                    self.session_manager.clear_token()
                    self.session_manager.save_to_disk()
                    self.session = None
                    self._last_disconnect_kind = "transient"
                    self._handle_rejected_retry = True
                    self.session_manager.prepare_reconnect()

                # --- Gemini rotates the Live session every ~2.5 min and closes
                # it with 1008 (policy violation) "The operation was aborted" —
                # both while awake and while idle in standby. This is routine:
                # we reconnect below with the resumption token and the
                # conversation continues seamlessly. Log it quietly at INFO,
                # WITHOUT a traceback, so it does not masquerade as a crash.
                # (It arrives wrapped in a TaskGroup ExceptionGroup, so the
                # close code must be read from the leaves — see
                # _is_expected_rotation.)
                elif self._is_expected_rotation(e):
                    self._rotation_count += 1
                    self._rotation_reconnect = True
                    where = "standby" if self._standby_active else "active"
                    print(f"[era] [ROTATE] Live session rotated by Gemini ({where}); reconnecting.")
                    log_event(
                        main_logger,
                        "engine.session.rotated",
                        "INFO",
                        source="run_connect_loop",
                        standby=self._standby_active,
                        rotation_count=self._rotation_count,
                    )
                    self.session = None
                    self._last_disconnect_kind = "rotation"
                    self.session_manager.prepare_reconnect()
                else:
                    kind = self._classify_disconnect(e)
                    self._last_disconnect_kind = kind
                    # Consecutive "no internet" failures (DNS, refused, aborted).
                    # Two in a row sends Era to local mode within seconds instead
                    # of after the full ~90 s backoff (see the branch below).
                    self._offline_streak = (
                        self._offline_streak + 1 if self.failure_is_offline(e) else 0
                    )
                    self.session = None
                    # Persist the token across the outage so a restart mid-outage
                    # can still resume.
                    self.session_manager.save_to_disk()
                    if kind == "quota":
                        # Quota exhaustion is now an expected, handled condition —
                        # not a crash. Log it quietly (no traceback) so it does not
                        # poison self-diagnosis, and let the cooldown below react.
                        print("[era] [QUOTA] Gemini quota exhausted — will cool down.")
                        log_event(
                            main_logger,
                            "engine.quota.exhausted",
                            "WARNING",
                            source="run_connect_loop",
                            standby=self._standby_active,
                        )
                    elif kind == "auth":
                        print("[era] [AUTH] Gemini rejected the API key.")
                        log_event(
                            main_logger,
                            "engine.auth.invalid",
                            "ERROR",
                            source="run_connect_loop",
                        )
                    elif self._is_network_blip(e):
                        # Wi-Fi/DNS dropped out. Recovery is unchanged (this is
                        # still a "transient" kind and reconnects on the normal
                        # backoff) — but it is the network failing, not Era, so
                        # log one quiet line instead of an ERROR + traceback.
                        # The leaf message is extracted because the raw
                        # ExceptionGroup only ever str()s as "unhandled errors
                        # in a TaskGroup (N sub-exceptions)".
                        leaves = self._flatten_exc(e)
                        detail = str(leaves[0]) if leaves else str(e)
                        print(f"[era] [NET] Connection dropped ({detail}) — reconnecting.")
                        log_event(
                            main_logger,
                            "engine.network.blip",
                            "WARNING",
                            source="run_connect_loop",
                            standby=self._standby_active,
                            error_type=type(leaves[0]).__name__ if leaves else type(e).__name__,
                            error=detail[:200],
                        )
                    else:
                        print(f"[era] [WARN] {e}")
                        traceback.print_exc()
                        log_crash(
                            main_logger,
                            "run_connect_loop",
                            type(e),
                            e,
                            e.__traceback__,
                            event="engine.reconnect_error",
                            level="ERROR",
                            # Surface the concrete leaf errors: an ExceptionGroup's
                            # own message names only the count, never the cause.
                            leaf_errors=[
                                f"{type(x).__name__}: {str(x)[:120]}"
                                for x in self._flatten_exc(e)[:5]
                            ],
                        )
                    self.session_manager.prepare_reconnect()

            self.set_speaking(False)
            if not self._standby_active:
                self.ui.set_state("THINKING")

            # ── Graceful GoAway rotation: expected, not a failure ──
            # Gemini rotates the Live session periodically and signals it with a
            # GoAway. Reconnect immediately using the resumption token so the
            # conversation continues seamlessly — no backoff, no failure count.
            # This must also fire during standby: the mic + Vosk wake listener
            # depend on a live session, so a background reconnect keeps wake alive.
            if self._go_away_event.is_set():
                self._reconnect_attempts = 0
                if self._standby_active:
                    print(
                        "[era] [RECONNECT] GoAway during standby — restoring mic/wake in background."
                    )
                else:
                    print("[era] [RECONNECT] Graceful GoAway rotation — reconnecting immediately.")
                continue

            # ── A dead resumption handle: reconnect at once, WITHOUT it ──
            # The config is already rebuilt from scratch every attempt and the
            # token is gone, so the next connect is a clean one. No backoff: the
            # fault was ours to drop, not the network's, and the mic (with it
            # the wake word) is dead until a session exists.
            if getattr(self, "_handle_rejected_retry", False):
                self._handle_rejected_retry = False
                self._reconnect_attempts = 0
                continue

            # ── Gemini's ~2.5min 1008 session rotation: same deal as a GoAway.
            # Not a failure, so it must not feed the failure streak or serve a
            # backoff delay — the mic (and with it the Vosk wake listener) is
            # dead until we hold a session again, so reconnect at once.
            if self._rotation_reconnect:
                self._rotation_reconnect = False
                self._reconnect_attempts = 0
                continue

            # ── Quota exhausted: long, growing cooldown, then auto-recover ──
            # Retrying fast only burns more quota and can never succeed, so wait
            # a long time (start 15 min, double each time, cap 2 h) and try again.
            # This does NOT count toward the failure streak, so it never falls
            # into the misleading "say era to retry" standby. When Google frees
            # the quota, the next attempt connects and everything (mic + wake)
            # comes back on its own — no manual step.
            if self._last_disconnect_kind == "quota":
                self._reconnect_attempts = 0

                # ── First escape hatch: a DIFFERENT KEY ──
                # Quota is billed per key, so a spare key beats every other
                # recovery: it restores the full cloud voice immediately instead
                # of degrading it, and — more importantly — it keeps the session
                # alive, which is what the wake word depends on. Tried before
                # the voice fallback and long before the cooldown, and it costs
                # nothing when no spare is configured (_rotate_api_key returns
                # False and we fall straight through).
                if self._rotate_api_key():
                    self.set_status("Quota exhausted — trying backup API key")
                    self.ui.write_log(
                        "SYS: Gemini quota exhausted on the current key. "
                        "Switching to the backup key and reconnecting now."
                    )
                    continue

                wait_s = self._quota_cooldown_current

                # ── Escape hatch: serve LOCALLY instead of waiting deaf ──
                # Mic, local STT, the Ollama brain (with Era's gated tools) and
                # Fish/Piper speech, for the length of the cooldown — capped at
                # routing.local_fallback.probe_gemini_every_s so Gemini is retried
                # often and Era returns to it as soon as quota is back. Returns
                # False when the local stack isn't ready; the old wait follows.
                if await self.serve_locally_during_outage(wait_s, "quota"):
                    self._quota_cooldown_current = grow_cooldown(
                        self._quota_cooldown_current, self._quota_cooldown_max
                    )
                    self.set_status("Reconnecting")
                    continue

                mins = wait_s / 60.0
                if not self._standby_active:
                    self.ui.set_state("STANDBY")
                self.set_status(f"Cooling down (quota) — retrying in ~{mins:.0f} min")
                # Be honest: the wake word can't be heard with no live session.
                self.ui.write_log(
                    "SYS: Gemini quota is exhausted. Era is waiting and will "
                    f"reconnect automatically in about {mins:.0f} minutes. While it "
                    "waits it CANNOT hear the wake word — no action needed, it "
                    "recovers on its own."
                )
                print(f"[era] [QUOTA] Cooling down for ~{mins:.0f} min before retry.")
                await self._interruptible_sleep(wait_s)
                # Grow the next cooldown (double, capped) in case quota is still out.
                self._quota_cooldown_current = grow_cooldown(
                    self._quota_cooldown_current, self._quota_cooldown_max
                )
                self.set_status("Reconnecting")
                continue

            # ── Config rejected by the model: retrying the SAME config is futile ──
            # The live-tested case (2026-07-29) is kokoro_text: it asks for
            # response_modalities=["TEXT"], and no general-conversation Live
            # model available today accepts that —
            # gemini-3.1-flash-live-preview answers 1007 "The requested
            # combination of response modalities (TEXT) is not supported".
            #
            # This is the one failure a *preference* can cause, and it is fatal:
            # the mode persists to config/voice.json, so without this branch Era
            # boots into a mode that can never connect and stays offline forever
            # across restarts. Falling back to the mode we know works keeps a
            # voice preference from ever costing the whole connection — the same
            # "degrade, never raise" rule the TTS layer follows.
            if self._last_disconnect_kind == "config":
                self._reconnect_attempts = 0

                # Some other rejected config: retrying identically cannot work,
                # so pace it instead of hot-looping, and say so plainly.
                self.set_status("Connection config rejected — retrying periodically")
                self.ui.write_log(
                    "SYS: Gemini rejected the session configuration. Era will keep "
                    "re-checking, but this needs a config fix, not a retry."
                )
                if await self.serve_locally_during_outage(self._quota_cooldown_base, "config"):
                    self.set_status("Reconnecting")
                    continue
                await self._interruptible_sleep(self._quota_cooldown_base)
                self.set_status("Reconnecting")
                continue

            # ── Invalid API key: retrying fast won't help. Wait long, keep checking
            # (in case the key gets fixed in config), never crash-loop. ──
            if self._last_disconnect_kind == "auth":
                self._reconnect_attempts = 0
                self.set_status("API key invalid — check config/api_keys.json")
                self.ui.write_log(
                    "SYS: Gemini rejected the API key. Fix gemini_api_key in "
                    "config/api_keys.json. Era will keep re-checking automatically."
                )
                if await self.serve_locally_during_outage(self._quota_cooldown_base, "auth"):
                    self.set_status("Reconnecting")
                    continue
                await self._interruptible_sleep(self._quota_cooldown_base)
                self.set_status("Reconnecting")
                continue

            # ── Backoff with jitter → standby at max ──
            if not self._standby_active:
                self._reconnect_attempts += 1
                self.set_status("Reconnecting")

                # After a couple of non-quota failures, assume the saved
                # resumption handle may be stale and drop it so the next attempt
                # opens a clean session instead of wedging on a dead handle.
                if (
                    self._reconnect_attempts >= self._stale_token_after
                    and self.session_manager.get_resumption_token() is not None
                ):
                    print("[era] [SESSION] Clearing possibly-stale resumption token.")
                    self.session_manager.clear_token()
                    self.session_manager.save_to_disk()

                # Network down: serve locally for a minute, then try Gemini again.
                # Taken after two consecutive "no internet" failures (~5 s), or
                # once the fast retries run out for any other reason. The first
                # version waited for all 6 retries (~90 s of backoff), so a
                # 30-second Wi-Fi outage never reached local mode (2026-09-23).
                offline = self._offline_streak >= 2
                if (offline or self._reconnect_attempts >= self._max_reconnect_attempts) and (
                    await self.serve_locally_during_outage(60.0, "network")
                ):
                    self.set_status("Reconnecting")
                    continue

                if self._reconnect_attempts >= self._max_reconnect_attempts:
                    print(
                        f"[era] [FATAL] {self._reconnect_attempts} consecutive failures. Entering standby."
                    )
                    self.set_status("Offline — retrying in background")
                    self.ui.write_log("SYS: Connection lost. Entering standby. Say 'era' to retry.")
                    self.enter_standby(network_failure=True)
                else:
                    delay = self._compute_reconnect_delay(self._reconnect_attempts - 1)
                    print(
                        f"[era] [RETRY] Reconnecting in {delay:.1f}s... "
                        f"(attempt {self._reconnect_attempts}/{self._max_reconnect_attempts})"
                    )
                    await asyncio.sleep(delay)
            else:
                # Standby: never give up — the Vosk wake listener only hears
                # audio while a live session TaskGroup owns the mic, so keep
                # restoring the session in the background at a gentle pace
                # (delay caps at ~60s via _compute_reconnect_delay).
                self._reconnect_attempts = min(
                    self._reconnect_attempts + 1, self._max_reconnect_attempts
                )
                delay = self._compute_reconnect_delay(self._reconnect_attempts - 1)
                print(f"[era] [RETRY] Standby background reconnect in {delay:.1f}s...")
                # Sleep in slices so a wake (F4/remote) or GoAway cuts the
                # wait short instead of stalling recovery for up to a minute.
                deadline = time.time() + delay
                while time.time() < deadline:
                    if not self._standby_active or self._go_away_event.is_set():
                        break
                    await asyncio.sleep(0.25)


import hmac
import logging

from flask import Flask, request

# Initialize a micro-webserver for your local network
watch_bridge = Flask(__name__)

# Suppress annoying terminal logs for every ping
log_control = logging.getLogger("werkzeug")
log_control.setLevel(logging.ERROR)

# Global variable to reference your active era instance
active_era_instance = None


def _remote_bridge_authorized(req) -> bool:
    """This endpoint hands its payload straight to Era, who acts on the real machine.
    It listens on 0.0.0.0, so ANY device on the Wi-Fi (a guest phone, a compromised
    IoT box) could otherwise drive Era. Require a shared secret.

    Set ERA_REMOTE_TOKEN in the environment and send it as the X-Era-Token header (or
    a "token" field in the JSON body). If the env var is unset the bridge REFUSES
    every request: failing closed is the only safe default for a remote-control
    endpoint — an unset secret must not silently mean "no security"."""
    expected = os.environ.get("ERA_REMOTE_TOKEN", "").strip()
    if not expected:
        return False
    supplied = (req.headers.get("X-Era-Token") or "").strip()
    if not supplied:
        supplied = str((req.get_json(silent=True) or {}).get("token", "")).strip()
    # Constant-time compare: a naive == leaks the secret one byte at a time to an
    # attacker who can time the response.
    return hmac.compare_digest(supplied, expected)


@watch_bridge.route("/remote_voice", methods=["POST"])
def remote_voice_input():
    global active_era_instance
    if not _remote_bridge_authorized(request):
        log_event(main_logger, "remote_voice.denied", "WARNING", reason="bad_or_missing_token")
        print("[BRIDGE] denied /remote_voice: missing or bad X-Era-Token")
        return {"status": "denied"}, 403

    payload = request.get_json(silent=True) or {}
    text_command = payload.get("command", "").strip()

    log_event(
        main_logger,
        "remote_voice.received",
        "INFO",
        has_command=bool(text_command),
        era_online=active_era_instance is not None,
    )

    if text_command and active_era_instance:
        log_event(main_logger, "remote_voice.command", "INFO", command=text_command)

        # === FIX: Safe notification for standard incoming remote tasks ===
        if active_era_instance._loop and active_era_instance._loop.is_running():
            # Check if it's not a wake-up command to prevent duplicate notifications
            if not active_era_instance._standby_active:
                asyncio.run_coroutine_threadsafe(
                    send_era_notification_async("Remote Task Executed", f"Command: {text_command}"),
                    active_era_instance._loop,
                )

        active_era_instance._on_text_command(text_command)
        return {"status": "success", "executed": text_command}, 200

    return {"status": "ignored", "reason": "Missing text or engine offline"}, 400


def launch_network_bridge():
    # The bridge now fails closed. Say so plainly at startup, otherwise a watch/phone
    # shortcut that used to work just starts returning 403 with no visible reason.
    if not os.environ.get("ERA_REMOTE_TOKEN", "").strip():
        print(
            "[BRIDGE] ERA_REMOTE_TOKEN is not set — /remote_voice will DENY every "
            "request. Set it (and send it as the X-Era-Token header) to re-enable "
            "the LAN remote, or use the Telegram bridge instead."
        )

    # Suppress Flask startup banner (Serving Flask app, Debug mode)
    try:
        import flask.cli

        flask.cli.show_server_banner = lambda *a, **kw: None
    except (ImportError, AttributeError):
        pass
    # Binds to port 5005 on your local Wi-Fi network
    watch_bridge.run(host="0.0.0.0", port=5005, debug=False, use_reloader=False)


import re

import aiohttp


async def send_era_notification_async(title: str, message: str):
    """
    Envoie une notification push gratuite via Telegram de maniÃ¨re asynchrone.
    """
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not telegram_token or not chat_id:
        print(
            "[era PUSH] Telegram notification skipped: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured."
        )
        return

    # Helper format definition to escape reserved markdown characters safely
    def escape_markdown(text: str) -> str:
        escape_chars = r"_*[]()~`>#+-=|{}.!"
        return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

    # Clean the message text strings
    safe_title = escape_markdown(title)
    safe_message = escape_markdown(message)
    texte_final = f"*{safe_title}*\n\n{safe_message}"

    # Correct endpoint format containing /bot followed by the token
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": texte_final,
        "parse_mode": "MarkdownV2",
    }

    try:
        # Create an async client session with a timeout wrapper
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    print("[era PUSH] Notification envoyÃ©e avec succÃ¨s via Telegram.")
                else:
                    response_text = await response.text()
                    print(f"[era PUSH] Erreur Telegram Code : {response.status}")
                    print(f"[era PUSH] DÃ©tails : {response_text}")

    except TimeoutError:
        print("[era PUSH] Erreur : Le dÃ©lai d'attente (timeout) a expirÃ©.")
    except Exception as e:
        print(f"[era PUSH] Erreur rÃ©seau lors de l'envoi : {e}")


# # Example of how to execute this inside your async event loop
# if __name__ == "__main__":
#     async def main():
#         await send_era_notification_async(
#             "era Async Test: Success!",
#             "Status: Online. Non-blocking network call complete."
#         )
#     asyncio.run(main())


# ── WebSocket Bridge ──────────────────────────────────────────────────────────


from app.websocket_server import WebSocketServer  # noqa: F401  (re-export)


def _install_crash_guards():
    """Log uncaught exceptions (main + worker threads) instead of letting them
    vanish silently. Keeps Era observable when something slips past a handler."""

    def _log_uncaught(exc_type, exc_value, exc_tb, *, thread_name: str = "main"):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            log("UNCAUGHT", f"{exc_type.__name__}: {exc_value}")
        except Exception:
            pass
        # Persist the full stack to era.log so crashes survive the console.
        log_crash(
            main_logger,
            f"excepthook:{thread_name}",
            exc_type,
            exc_value,
            exc_tb,
            event="engine.uncaught",
        )
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_uncaught
    try:
        threading.excepthook = lambda a: _log_uncaught(
            a.exc_type,
            a.exc_value,
            a.exc_traceback,
            thread_name=getattr(a.thread, "name", "unknown"),
        )
    except Exception:
        pass

    # A silent return to the shell (Tk window closed, mainloop ends, or any
    # normal interpreter shutdown) leaves no traceback. Record an explicit
    # exit marker so "engine crashed" can be told apart from "process exited":
    #   - engine.uncaught present  -> crashed with an exception
    #   - only process.exit present -> clean/silent exit (e.g. window closed)
    #   - neither present           -> hard kill / native crash / os._exit
    import atexit

    def _log_exit():
        log_event(main_logger, "process.exit", "WARNING", reason="interpreter_shutdown")

    atexit.register(_log_exit)


def _force_exit_after_ui_close(era_instance, grace_seconds: float = 5.0) -> None:
    """Guarantee the process actually dies once the window is closed.

    Closing the UI is supposed to end Era. What it really does is return from
    ui.root.mainloop(), after which the main thread runs threading._shutdown().
    That calls concurrent.futures' atexit hook, which sets a global "no new
    futures" flag and then JOINS every executor worker thread -- APScheduler's
    pool, the voice gate's pool, the event loop's default getaddrinfo pool.
    Those workers are NOT daemon threads, so one blocked task hangs the join
    forever. The interpreter never reaches Py_Finalize, which is the only thing
    that kills daemon threads, so Era's engine thread keeps running inside a
    process that can no longer do anything.

    Observed on 2026-09-21: the UI closed at 12:02, the process lived until
    14:31 emitting 177 ERROR reconnect failures, and a SECOND Era was launched
    on top of it at 12:12 -- two instances contending for the mic, the audio
    endpoint and the browser session. The 2026-09-20 17:58 shutdown logged
    process.exit 47ms after the mainloop returned; the hung ones logged no
    process.exit at all, which is exactly how to tell them apart in era.log.

    So: ask for a clean stop, give the normal path a short grace period, then
    leave by the one door that cannot be blocked. os._exit skips atexit hooks,
    so process.exit is logged here rather than relying on the hook.
    """
    import os as _os
    import threading as _threading

    if era_instance is not None:
        # Tells the connect loop and the standby sleeps to stop retrying.
        try:
            era_instance._shutdown_requested = True
        except Exception:
            pass

    def _bail():
        log_event(
            main_logger,
            "process.exit",
            "WARNING",
            reason="forced_after_ui_close",
            grace_seconds=grace_seconds,
            lingering_threads=[t.name for t in _threading.enumerate() if t.is_alive()][:12],
        )
        for handler in list(main_logger.handlers):
            try:
                handler.flush()
            except Exception:
                pass
        _os._exit(0)

    # Daemon timer: this guard must never itself become a reason not to exit.
    timer = _threading.Timer(grace_seconds, _bail)
    timer.daemon = True
    timer.start()


def _ui_mode() -> str:
    """ "browser" (default) is the localhost HUD. "qt" is the legacy desktop
    window, kept selectable (ERAONE_UI=qt) until the browser HUD has proven parity."""
    return "qt" if os.environ.get("ERAONE_UI", "").strip().lower() == "qt" else "browser"


def _hud_port() -> int:
    try:
        port = int(os.environ.get("ERAONE_PORT", "") or 8765)
    except ValueError:
        port = 8765
    return port if 1024 <= port <= 65535 else 8765


def _open_existing_hud() -> None:
    """A second backend was started: show the running one's HUD, start nothing."""
    try:
        from app.hud import browser, instance, settings

        info = instance.read_backend_info() or {}
        port = int(info.get("port") or _hud_port())
        browser.open_hud(
            f"http://127.0.0.1:{port}/", prefer_app=settings.load()["app_mode"], hud_clients=1
        )
    except Exception as e:
        print(f"[HUD] could not open the running HUD: {e}")


def _maybe_auto_open_hud(ws_server) -> None:
    """Open the HUD when the backend started on its own (logon autostart, or
    `python main.py`). Never after a crash-restart or a relaunch — an open HUD
    reconnects by itself — and never when the launcher is the one opening it.
    Waits a few seconds first: a HUD tab that is already open reconnects within
    ~3 s, and a second window on top of it would be noise."""
    from app.hud import browser, settings

    if os.environ.get("ERA_NO_AUTO_HUD") == "1":
        return
    if os.environ.get("ERA_LAUNCH_REASON", "") in ("restart", "relaunch"):
        return
    prefs = settings.load()
    if not prefs["open_hud_on_start"] or not ws_server.bound_port:
        return

    def _run():
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if ws_server.client_count() > 0:
                return
            time.sleep(0.25)
        how = browser.open_hud(
            f"http://127.0.0.1:{ws_server.bound_port}/", prefer_app=prefs["app_mode"]
        )
        print(f"[HUD] opened the HUD ({how}).")

    threading.Thread(target=_run, name="HudAutoOpen", daemon=True).start()


def main():
    from app.hud import instance as hud_instance

    ui_mode = _ui_mode()
    # Console output -> logs/eraone.log (+ eraone-error.log). With no visible
    # console (normal launch) print() would otherwise go nowhere.
    if os.environ.get("ERA_STDIO_LOG", "1") != "0":
        try:
            from core.stdio_log import install as _install_stdio_log

            _install_stdio_log()
        except Exception as e:
            print(f"[era] [WARN] console log tee not installed: {e}")
    _install_crash_guards()

    # ── Single instance ───────────────────────────────────────────────────
    # A named mutex the kernel releases however this process dies — so there
    # is never a stale lock. A second backend must not start: two Eras fight
    # over the mic, the speakers and the Live session (2026-09-21 incident).
    backend_lock = hud_instance.NamedMutex(hud_instance.backend_mutex_name())
    if not backend_lock.acquire():
        print("[era] Another ERA ONE backend is already running; opening its HUD instead.")
        log_event(main_logger, "process.duplicate_refused", "WARNING")
        _open_existing_hud()
        sys.exit(hud_instance.EXIT_ALREADY_RUNNING)

    # ── Localhost HUD server: page + REST + WebSocket on one port ─────────
    ws_server = WebSocketServer(port=_hud_port(), port_fallback=(10 if ui_mode == "browser" else 0))
    ws_server.meta = {"live_model": LIVE_MODEL, "ui_mode": ui_mode}
    ws_server.engine_provider = lambda: active_era_instance
    ws_server.start()
    ws_server.ready.wait(15)
    if ws_server.bound_port:
        hud_instance.write_backend_info(port=ws_server.bound_port, ui_mode=ui_mode)
        import atexit

        atexit.register(hud_instance.clear_backend_info, os.getpid())
        print(f"[HUD] ERA ONE HUD at http://127.0.0.1:{ws_server.bound_port}/ ({ui_mode} mode)")
    else:
        reason = ws_server.bind_error or "the HUD server did not start in time"
        print(f"[HUD] [ERROR] {reason} — voice keeps working, the HUD is unavailable.")
        hud_instance.write_startup_error("hud_server", reason)
        hud.error(f"HUD server unavailable: {reason}", where="hud")

    if ui_mode == "qt":
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            "--unsafely-treat-insecure-origin-as-secure=file://"
        )
        from ui import EraUI

        query = f"host=qt&token={ws_server.session_token}&port={ws_server.bound_port or 8765}"
        ui = EraUI("face.png", hud_query=query)
    else:
        from app.hud.ui_base import HeadlessUI

        ui = HeadlessUI()
    ui.set_ws_broadcast(ws_server.broadcast)
    hud.attach(ws_server.broadcast)

    # ── HUD actions: resolve the engine at CALL time (crash-rebuild safe) ──
    def _hud_interrupt() -> None:
        era = active_era_instance
        loop = getattr(era, "_loop", None)
        if era is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(era.trigger_barge_in, "hud")

    def _hud_shutdown(relaunch: bool = False) -> None:
        era = active_era_instance
        if era is None:
            # Engine never came up: nothing to finish, just leave cleanly.
            if not relaunch:
                try:
                    from core import era_relaunch

                    flag = era_relaunch.stop_flag()
                    flag.parent.mkdir(parents=True, exist_ok=True)
                    flag.write_text("Shutdown from the HUD.\n", encoding="utf-8")
                except Exception:
                    pass
                try:
                    from app.hud.browser import close_hud_windows

                    close_hud_windows()
                except Exception:
                    pass
            hud_instance.clear_backend_info(os.getpid())
            os._exit(0)
        try:
            result = era._shutdown_era({"relaunch": relaunch, "_source": "hud"})
            ui.write_log(f"SYS: {result}")
            if getattr(era, "_shutdown_requested", False):
                hud_instance.clear_backend_info(os.getpid())
        except Exception as e:
            print(f"[era] [WARN] HUD shutdown failed: {e}")

    ws_server._on_interrupt = _hud_interrupt
    ws_server._on_shutdown = _hud_shutdown
    ws_server._on_restart = lambda: _hud_shutdown(relaunch=True)
    if ui_mode == "browser":
        _maybe_auto_open_hud(ws_server)

    # ── Model switcher bridges ────────────────────────────────────────────
    # Both look the engine up at CALL time. The WebSocket server outlives every
    # engine instance (it is started before the first one and survives a crash
    # rebuild), so anything that captured `era` would keep answering from the
    # dead one — the same trap the mic-state provider's noqa B023 comment marks.
    # Neither raises: they run on socket threads, where an exception stops every
    # other message that socket would have carried.

    def _select_model(alias: str) -> None:
        era = active_era_instance
        if era is None:
            return
        try:
            era.select_model(alias)
        except Exception as e:
            print(f"[era] [WARN] model switch failed: {e}")

    def _publish_model_state() -> None:
        era = active_era_instance
        if era is None:
            return
        try:
            era.publish_model_state()
        except Exception as e:
            print(f"[era] [WARN] model state publish failed: {e}")

    def runner():
        global active_era_instance
        ui.wait_for_api_key()
        era = EraLive(ui)
        active_era_instance = era

        # Link WebSocket command handler
        ws_server._on_command = ui.on_text_command
        ws_server._on_window_control = ui.window_control
        ws_server._on_mic_toggle = era._on_browser_mic_toggle
        ws_server._mic_state_provider = lambda: era.mic_effective_active()
        ws_server._on_file_upload = ui.on_file_upload
        # Model switcher: a click sends an alias, and a connecting window asks
        # for the state it cannot know. Both read `active_era_instance` rather
        # than closing over `era`, so a crash-rebuilt engine is the one that
        # answers — binding this instance would leave the switcher driving a
        # dead object after a rebuild.
        ws_server._on_model_select = _select_model
        ws_server._on_client_connect = _publish_model_state

        # Publish once now, for a UI that was already open before this engine
        # finished starting (relaunch, or a crash rebuild) and so will never see
        # a connect event.
        _publish_model_state()

        # Démarre le serveur Flask sur un thread séparé
        threading.Thread(target=launch_network_bridge, daemon=True).start()

        # Two-way Telegram chat. Unlike the Flask bridge above (LAN-only), this uses
        # OUTBOUND long-polling, so it works from any network with no open port —
        # you can order Era, review her drafts, and approve them from anywhere.
        # Dormant unless TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set.
        def _start_telegram():
            try:
                import era_telegram

                def _on_telegram_command(text: str):
                    # Arm the mirror BEFORE dispatching: _on_text_command hands the
                    # order to the event loop and returns immediately, so the reply
                    # can arrive before we'd get another chance to set this.
                    era._tg_reply_pending = True
                    try:
                        era._on_text_command(text)
                    except Exception:
                        # The poller reports the failure itself; just don't leave the
                        # flag armed or the next local turn gets mirrored to the chat.
                        era._tg_reply_pending = False
                        raise

                if era_telegram.start(_on_telegram_command):
                    print("[TG] two-way Telegram bridge started.")
            except Exception as e:
                print(f"[TG] Telegram bridge failed to start: {e}")

        _start_telegram()

        # Start the CM scheduler so each brand's ENABLED weekly-plan / auto-publish
        # crons in brands.yaml actually fire (through the same era_dispatch lock as
        # on-demand orders — nothing ever collides). This was previously never
        # started, so scheduled runs silently never happened. Never blocks startup.
        def _start_cm_scheduler():
            try:
                import era_scheduler

                registered = era_scheduler.start_scheduler()
                print(
                    f"[SCHED] CM scheduler started; {len(registered)} enabled job(s): {registered}"
                )
            except Exception as e:
                print(f"[SCHED] CM scheduler failed to start: {e}")

        threading.Thread(target=_start_cm_scheduler, daemon=True, name="CMScheduler").start()

        # The user's own scheduled tasks (core/task_scheduler.py) — separate from the
        # CM scheduler above, whose reload() removes every job it holds. Hooks point
        # the scheduler at this engine; the crash-rebuild below re-points them.
        def _start_task_scheduler():
            try:
                from core import task_scheduler

                era.install_task_scheduler_hooks()
                active = task_scheduler.start()
                print(f"[TASKS] task scheduler started; {active} active task(s).")
            except Exception as e:
                print(f"[TASKS] task scheduler failed to start: {e}")

        threading.Thread(target=_start_task_scheduler, daemon=True, name="TaskScheduler").start()

        # Protocol One's spy network: stat()-level watchers on one daemon thread
        # that keep the hive's issue map current (core/protocol_spies.py). Its
        # first tick waits 2 minutes so it never competes with startup. A new
        # CRITICAL finding is written to the UI log only — never spoken — so a
        # background scan can never interrupt the user. ERA_SPIES=0 disables it.
        def _spy_alert(item: dict) -> None:
            try:
                ui.write_log(
                    f"SYS: Protocol spies — CRITICAL: {item['title']} ({item.get('location', '')})"
                )
            except Exception:
                pass

        # Protocol Zero, when VS Code opening on this project triggered it. Always
        # logged; SPOKEN only when it actually changed the prompt and Era is free
        # (not muted, speaking, reconnecting or in standby) — opening your editor
        # must never cut across a conversation just to say "nothing changed".
        # speak_dispatched sanitises and says it verbatim, never as an instruction.
        def _zero_report(summary: str) -> None:
            try:
                ui.write_log(f"SYS: {summary}")
                live = active_era_instance
                if live is None or "Nothing changed" in summary:
                    return
                snap = live.session_manager.get_state_snapshot()
                busy = ("is_muted", "is_speaking", "is_reconnecting", "standby_active")
                if not any(snap.get(k) for k in busy):
                    live.speak_dispatched(summary)
            except Exception as e:
                print(f"[SPIES] Protocol Zero briefing not delivered: {e}")

        try:
            from core import protocol_spies

            protocol_spies.start_network(on_critical=_spy_alert, on_zero_report=_zero_report)
        except Exception as e:
            print(f"[SPIES] failed to start: {e}")

        # Autonomous self-healing (core/self_heal.py): code-located faults in
        # era.log get a scoped, verified fix; probation rolls it back if the
        # fault returns. Notices go to the UI log only — a background repair
        # never interrupts a conversation. ERA_SELF_HEAL=off disables it.
        def _heal_notice(text: str) -> None:
            try:
                ui.write_log(f"SYS: Self-heal — {text}")
            except Exception:
                pass

        try:
            from core import self_heal

            self_heal.start(notify=_heal_notice)
        except Exception as e:
            print(f"[SELF-HEAL] failed to start: {e}")

        # Preload the local Hermes classifier (Ollama) in the background so the
        # first typed CM request doesn't pay the model cold-load latency. No-op
        # if Ollama isn't the configured provider; never blocks startup.
        def _warm_hermes():
            try:
                import hermes_router

                hermes_router.warm_up()
            except Exception as e:
                print(f"[HERMES] warm-up thread failed: {e}")

        threading.Thread(target=_warm_hermes, daemon=True, name="HermesWarmup").start()

        # Same idea for the local vision model: cold load is ~13.7s vs ~2.2s warm,
        # so preload it rather than making the first "look at my screen" pay for it.
        # No-op unless Ollama is the leading vision provider; never blocks startup.
        def _warm_vision():
            try:
                from core.vision_config import (
                    resolve_ollama_base_url,
                    resolve_ollama_keep_alive,
                    resolve_ollama_model,
                    resolve_vision_provider_order,
                )
                from core.vision_ollama import warm_up_ollama_vision

                if "ollama" not in resolve_vision_provider_order():
                    return
                ok = warm_up_ollama_vision(
                    model=resolve_ollama_model(),
                    base_url=resolve_ollama_base_url(),
                    keep_alive=resolve_ollama_keep_alive(),
                )
                print(f"[Vision] warm-up complete (local model ready: {ok})")
            except Exception as e:
                print(f"[Vision] warm-up skipped: {e}")

        threading.Thread(target=_warm_vision, daemon=True, name="VisionWarmup").start()

        # RAM watchdog: samples memory every 5 minutes and fires PC Boost above
        # 70%, unattended. Started here rather than inside the engine so a
        # crash-rebuild of EraLive doesn't spawn a second polling thread.
        try:
            from core.ram_watchdog import start_ram_watchdog

            start_ram_watchdog()
        except Exception as e:
            print(f"[RamWatchdog] failed to start: {e}")

        # Supervisor: era.run() is meant to loop forever. If it ever escapes
        # with an unexpected error we restart the engine (self-healing) rather
        # than let Era die silently while the window stays open.
        backoff = 3.0
        while True:
            try:
                asyncio.run(era.run())
                break  # clean return == intentional shutdown
            except KeyboardInterrupt:
                print("\n[EXIT] Shutting down...")
                break
            except Exception as e:
                try:
                    log("SUPERVISOR", f"Era engine crashed: {e!r} — self-healing in {backoff:.0f}s")
                    ui.write_log("SYS: Era hit a snag — self-healing, back in a moment.")
                    ui.set_state("THINKING")
                    hud.error("Engine restarting after an error", where="engine")
                except Exception:
                    pass
                log_crash(
                    main_logger,
                    "supervisor",
                    type(e),
                    e,
                    e.__traceback__,
                    event="engine.crash",
                    backoff_s=round(backoff, 1),
                )
                traceback.print_exc()
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)  # avoid a hot crash loop
                # Rebuild a fresh engine to clear any corrupted in-memory state.
                try:
                    era = EraLive(ui)
                    active_era_instance = era
                    try:
                        era.install_task_scheduler_hooks()
                    except Exception as hook_err:
                        print(f"[TASKS] could not re-point scheduler hooks: {hook_err}")
                    ws_server._on_command = ui.on_text_command
                    ws_server._on_window_control = ui.window_control
                    ws_server._on_mic_toggle = era._on_browser_mic_toggle
                    # noqa B023: reading the loop variable late is the POINT here.
                    # After a crash-rebuild the provider must report the CURRENT
                    # engine's mic state; binding `era` as a default would pin it
                    # to the dead instance and freeze the browser mic indicator.
                    ws_server._mic_state_provider = lambda: era.mic_effective_active()  # noqa: B023
                    ws_server._on_file_upload = ui.on_file_upload
                    # The switcher bridges resolve the engine at call time, so
                    # they need no rebinding — but the model pin did NOT survive
                    # the crash, and an indicator still showing it would be
                    # claiming a route that no longer exists.
                    _publish_model_state()
                except Exception as rebuild_err:
                    try:
                        log("SUPERVISOR", f"Engine rebuild failed: {rebuild_err!r}")
                    except Exception:
                        pass

    threading.Thread(target=runner, daemon=True).start()

    # Heartbeat for era_supervisor.py's hang check: beats from inside the engine's
    # event loop, so a frozen loop goes quiet and the supervisor restarts Era.
    try:
        from core.era_relaunch import start_heartbeat

        start_heartbeat(lambda: getattr(active_era_instance, "_loop", None))
    except Exception as e:
        print(f"[era] [WARN] heartbeat not started: {e}")

    log_event(main_logger, "process.start", "INFO")
    try:
        ui.root.mainloop()
    finally:
        # The engine runs in a daemon thread; when this returns the interpreter
        # tears down and that thread dies WITHOUT raising. Logging here is what
        # turns a mysterious "silent exit back to the shell" into a diagnosable
        # event (paired with process.exit from the atexit hook).
        log_event(main_logger, "ui.mainloop.returned", "WARNING", reason="tk_mainloop_exited")
        _force_exit_after_ui_close(active_era_instance)


if __name__ == "__main__":
    main()
