import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.era_state import EraState

MAX_STATE_HISTORY = 50
MAX_MESSAGE_BUFFER = 200

# Default on-disk location for the persisted session (so a restart can resume the
# Gemini conversation instead of starting cold). Overridable via ERA_SESSION_FILE.
DEFAULT_SESSION_FILE = Path(
    os.getenv("ERA_SESSION_FILE", str(Path.home() / ".era" / "session_state.json"))
)

# A Gemini Live resumption handle is only valid for a limited window. If the
# saved token is older than this when we reload it, treat it as expired and start
# a fresh session rather than trying (and failing) to resume with a stale handle.
DEFAULT_MAX_TOKEN_AGE_SECONDS = 900.0  # 15 minutes — conservative, safe default


@dataclass
class StateTransition:
    previous: EraState | None
    new: EraState
    timestamp: float
    reason: str
    metadata: dict | None = None


@dataclass
class BufferedMessage:
    index: int
    data: Any
    mime_type: str
    timestamp: float


class SessionManager:
    def __init__(self):
        self.current_resumption_token: str | None = None
        self.is_resumed_session: bool = False
        # Wall-clock time the current token was captured, used to expire a stale
        # handle on reload (see load_from_disk / DEFAULT_MAX_TOKEN_AGE_SECONDS).
        self._token_saved_at: float | None = None
        # Set when an update arrived carrying no usable handle: the handle we
        # hold is still valid, but the live state is newer than it.
        self._checkpoint_stale: bool = False

        self.current_state: EraState = EraState.OFFLINE
        self.previous_state: EraState | None = None
        self.last_non_reconnecting_state: EraState | None = None

        self.is_muted: bool = False
        self.wake_word_active: bool = False
        self.interruption_allowed: bool = False
        # Hands-free verbal interruption: "era Wait" triggers immediately.
        self.wait_trigger_word = "wait"
        self.wait_trigger_phrase = "era wait"
        # Turn-taking hysteresis: don't flip interruption on brief VAD gaps.
        self.turn_silence_start: float | None = None
        self.turn_complete_hysteresis_s: float = 1.4
        self.interruption_locked_until: float = 0.0

        self.last_wake_word_timestamp: float | None = None
        self.last_reconnect_timestamp: float | None = None
        self.last_go_away_timestamp: float | None = None
        self.go_away_time_left: float | None = None

        self._state_history: list[StateTransition] = []

        self._message_buffer: list[BufferedMessage] = []
        self._next_message_index: int = 0
        self._last_consumed_index: int = -1

    # ── Session token management ──────────────────────────────────

    def extract_resumption_token(self, response: Any) -> str | None:
        """Safely extract a session resumption token from a LiveServerMessage."""
        try:
            update = response.session_resumption_update
            if update is None:
                return None
            if update.resumable and update.new_handle:
                return update.new_handle
        except AttributeError:
            pass
        return None

    def extract_consumed_index(self, response: Any) -> int | None:
        """Extract last_consumed_client_message_index if available."""
        try:
            update = response.session_resumption_update
            if update is None:
                return None
            if update.last_consumed_client_message_index is not None:
                return int(update.last_consumed_client_message_index)
        except AttributeError:
            pass
        return None

    def extract_go_away_time(self, response: Any) -> str | None:
        """Extract time_left from a GoAway signal."""
        try:
            ga = response.go_away
            if ga is not None:
                return ga.time_left
        except AttributeError:
            pass
        return None

    def capture_token(self, response: Any) -> bool:
        """Capture a session resumption token from a server response.
        Returns True only when the token actually changed.

        AN UPDATE WITHOUT A USABLE HANDLE MUST NOT ERASE THE LAST GOOD ONE.
        The server sends `sessionResumptionUpdate` with `resumable=False` while a
        turn is mid-flight (per https://ai.google.dev/api/live). Treating that as
        "the handle is gone" would throw away the only checkpoint we hold, at
        precisely the moment a drop is most likely — mid-generation. So a
        no-handle update is recorded as CHECKPOINT STALENESS instead: the handle
        stays, and `checkpoint_is_stale` tells callers the live state has moved
        past it. GoAway renewal reads that to decide whether this is a good
        moment to reconnect.
        """
        token = self.extract_resumption_token(response)
        if token is not None:
            self._checkpoint_stale = False
            if token != self.current_resumption_token:
                self.current_resumption_token = token
                self._token_saved_at = time.time()
                return True
            return False

        # No usable handle in this update. If it was an update at all (rather
        # than an unrelated message), the state has moved past our checkpoint.
        if self._is_resumption_update(response) and self.current_resumption_token:
            self._checkpoint_stale = True

        idx = self.extract_consumed_index(response)
        if idx is not None:
            self._last_consumed_index = idx
            self._prune_buffer()
        return False

    @staticmethod
    def _is_resumption_update(response: Any) -> bool:
        """True if this message carried a session_resumption_update at all."""
        try:
            return response.session_resumption_update is not None
        except AttributeError:
            return False

    @property
    def checkpoint_is_stale(self) -> bool:
        """The conversation has advanced past the handle we hold.

        Not a failure — resuming from it still works, it just replays from an
        older point. It is the signal that a planned reconnect should prefer to
        wait for a fresh checkpoint if the deadline allows.
        """
        return self._checkpoint_stale

    def has_fresh_checkpoint(self) -> bool:
        return self.current_resumption_token is not None and not self._checkpoint_stale

    def handle_go_away(self, response: Any) -> bool:
        """Handle GoAway signal. Returns True if GoAway was received.

        Also stores the time_left hint (seconds) so callers can schedule
        a graceful reconnect before the server force-drops the connection.
        """
        time_left = self.extract_go_away_time(response)
        if time_left is not None:
            self.last_go_away_timestamp = time.time()
            self.go_away_time_left = time_left
            return True
        return False

    def get_resumption_token(self) -> str | None:
        return self.current_resumption_token

    def clear_token(self):
        self.current_resumption_token = None
        self._token_saved_at = None
        self._checkpoint_stale = False

    # ── Disk persistence (survive a full restart) ─────────────────

    def save_to_disk(self, path: Path | str | None = None) -> bool:
        """Persist the resumption token + light state so a restart can resume.

        Atomic write (temp file + replace) so a crash mid-write cannot corrupt
        the file. Never raises — persistence is best-effort and must not take
        down the connection loop. Returns True on success.
        """
        target = Path(path) if path is not None else DEFAULT_SESSION_FILE
        payload = {
            "version": 1,
            "resumption_token": self.current_resumption_token,
            "token_saved_at": self._token_saved_at,
            "is_resumed_session": self.is_resumed_session,
            "current_state": getattr(self.current_state, "value", None),
            "saved_at": time.time(),
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, target)
            return True
        except OSError:
            return False

    def load_from_disk(
        self,
        path: Path | str | None = None,
        max_token_age_seconds: float = DEFAULT_MAX_TOKEN_AGE_SECONDS,
    ) -> bool:
        """Load a previously saved session token if it is still fresh.

        A token older than ``max_token_age_seconds`` is treated as expired and
        dropped, so we never try to resume with a stale handle (which would fail
        and could wedge the reconnect loop). Never raises. Returns True only when
        a usable token was restored.
        """
        source = Path(path) if path is not None else DEFAULT_SESSION_FILE
        try:
            if not source.exists():
                return False
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False

        token = data.get("resumption_token")
        saved_at = data.get("token_saved_at")
        if not token or saved_at is None:
            return False
        try:
            age = time.time() - float(saved_at)
        except (TypeError, ValueError):
            return False
        if age < 0 or age > max_token_age_seconds:
            # Stale or clock-skewed — start fresh instead of resuming.
            return False

        self.current_resumption_token = token
        self._token_saved_at = float(saved_at)
        return True

    # ── State management ──────────────────────────────────────────

    def transition_to(
        self,
        new_state: EraState,
        reason: str = "",
        metadata: dict | None = None,
    ):
        self.previous_state = self.current_state
        if new_state == EraState.RECONNECTING:
            self.last_non_reconnecting_state = self.current_state
        self.current_state = new_state

        # Interruption only when assistant is speaking AND turn has fully completed
        # (hysteresis prevents premature interruption on intra-utterance pauses).
        speaking_done = (
            new_state == EraState.SPEAKING
            and time.time() >= self.interruption_locked_until
            and (
                self.turn_silence_start is None
                or (time.time() - self.turn_silence_start) > self.turn_complete_hysteresis_s
            )
        )
        self.interruption_allowed = speaking_done

        transition = StateTransition(
            previous=self.previous_state,
            new=new_state,
            timestamp=time.time(),
            reason=reason,
            metadata=metadata,
        )
        self._state_history.append(transition)
        if len(self._state_history) > MAX_STATE_HISTORY:
            self._state_history = self._state_history[-MAX_STATE_HISTORY:]

    def handle_verbal_interrupt(self, text: str) -> bool:
        """Hands-free: 'era Wait' stops TTS and returns to listening immediately."""
        t = (text or "").lower()
        triggered = self.wait_trigger_phrase in t or (self.wait_trigger_word in t and "era" in t)
        if triggered and self.current_state == EraState.SPEAKING:
            self.interruption_allowed = True
            self.transition_to(EraState.LISTENING, reason="verbal_interrupt: era Wait")
        return triggered

    def get_state_snapshot(self) -> dict:
        return {
            "current_state": self.current_state,
            "previous_state": self.previous_state,
            "is_muted": self.is_muted,
            "wake_word_active": self.wake_word_active,
            "standby_active": self.current_state == EraState.STANDBY,
            "is_reconnecting": self.current_state == EraState.RECONNECTING,
            "is_speaking": self.current_state == EraState.SPEAKING,
            "interruption_allowed": self.interruption_allowed,
            "last_wake_word_timestamp": self.last_wake_word_timestamp,
            "last_reconnect_timestamp": self.last_reconnect_timestamp,
        }

    def get_state_history(self) -> list[StateTransition]:
        return list(self._state_history)

    # ── Greeting policy ───────────────────────────────────────────

    def should_greet_on_reconnect(self) -> bool:
        if not self.is_resumed_session:
            return False
        pre_reconnect = self.last_non_reconnecting_state or self.previous_state
        if pre_reconnect in (
            EraState.PROCESSING,
            EraState.SPEAKING,
            EraState.LISTENING,
        ):
            return True
        return False

    def get_greeting(self) -> str | None:
        if not self.is_resumed_session:
            return None
        if self.should_greet_on_reconnect():
            return "Back online, sir."
        return None

    # ── Message buffering ─────────────────────────────────────────

    def register_outgoing_message(self, data: Any, mime_type: str) -> int:
        idx = self._next_message_index
        self._next_message_index += 1
        self._message_buffer.append(
            BufferedMessage(
                index=idx,
                data=data,
                mime_type=mime_type,
                timestamp=time.time(),
            )
        )
        if len(self._message_buffer) > MAX_MESSAGE_BUFFER:
            self._message_buffer = self._message_buffer[-MAX_MESSAGE_BUFFER:]
        return idx

    def _prune_buffer(self):
        self._message_buffer = [
            m for m in self._message_buffer if m.index > self._last_consumed_index
        ]

    def get_unacknowledged_messages(self) -> list[BufferedMessage]:
        return [m for m in self._message_buffer if m.index > self._last_consumed_index]

    def clear_message_buffer(self):
        self._message_buffer.clear()
        self._next_message_index = 0
        self._last_consumed_index = -1

    # ── Reconnect helpers ─────────────────────────────────────────

    def prepare_reconnect(self):
        self.transition_to(EraState.RECONNECTING, reason="connection_dropped")

    def restore_after_reconnect(self):
        self.last_reconnect_timestamp = time.time()
        if self.last_non_reconnecting_state is not None:
            restore_to = self.last_non_reconnecting_state
            if restore_to == EraState.SPEAKING:
                restore_to = EraState.LISTENING
            self.transition_to(restore_to, reason="reconnect_restored")
        else:
            self.transition_to(EraState.ONLINE_IDLE, reason="reconnect_fallback")
        self.is_resumed_session = True

    # ── Debug logging ─────────────────────────────────────────────

    def get_debug_info(self) -> dict:
        return {
            "has_token": self.current_resumption_token is not None,
            "is_resumed": self.is_resumed_session,
            "current_state": self.current_state.value,
            "previous_state": self.previous_state.value if self.previous_state else None,
            "is_muted": self.is_muted,
            "wake_word_active": self.wake_word_active,
            "buffer_size": len(self._message_buffer),
            "last_consumed_index": self._last_consumed_index,
            "history_length": len(self._state_history),
        }
