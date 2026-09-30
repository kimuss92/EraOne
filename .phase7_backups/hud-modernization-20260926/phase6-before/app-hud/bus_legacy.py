"""Legacy-UI observation and the read side of the HUD bus.

The old UI API (set_state / write_log / set_mute / browser_mic / model) keeps
working unchanged; every message it broadcasts is ALSO observed here, so the
state machine is fed by the very calls the engine already makes.

Mixed into HudBus (app/hud/events.py).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.hud.bus_const import CONN_LABEL, log_role


class LegacyObserverMixin:
    def observe_legacy(self, msg: dict) -> None:
        try:
            mtype = msg.get("type")
            if mtype == "amplitude":
                return  # 23 Hz; the HUD reads it directly, nothing to track
            with self._lock:
                if mtype == "state":
                    self._on_legacy_state(str(msg.get("state", "")))
                elif mtype == "mute":
                    self._standby = bool(msg.get("muted"))
                    if self._standby:
                        self._phase = None
                    self._poke()
                elif mtype == "browser_mic":
                    self._mic_active = bool(msg.get("active"))
                    self._poke()
                elif mtype == "log":
                    self._on_legacy_log(str(msg.get("text", "")))
                elif mtype == "model":
                    self._model = {k: v for k, v in msg.items() if k != "type"}
                elif mtype == "model_catalog":
                    self._catalog = {k: v for k, v in msg.items() if k != "type"}
        except Exception:
            pass

    def _on_legacy_state(self, s: str) -> None:
        now = time.monotonic()
        if s == "SPEAKING":
            if not self._speaking:
                self._speaking = True
                if self._phase and self._phase[0] in ("synthesizing", "thinking", "listening"):
                    self._phase = None
                if self._audio_t0 is not None and not self._first_audio_seen:
                    self._record_latency("firstAudioMs", (now - self._audio_t0) * 1000)
                self._audio_t0 = None
                self._first_audio_seen = True
                self._emit("tts.started", label="Speaking")
        elif s in ("LISTENING", "THINKING", "PROCESSING"):
            if self._speaking:
                self._speaking = False
                if not self._suppress_tts_complete:
                    self._emit("tts.completed", label="Finished speaking")
                    if self._turn_done_at is not None:
                        self._set_transient("complete", "Complete")
                self._suppress_tts_complete = False
                self._turn_done_at = None
            if s in ("THINKING", "PROCESSING") and not self._standby:
                self._set_phase("thinking", "Thinking")
        elif s == "STANDBY":
            self._standby = True
            self._phase = None
        elif s == "MUTED":
            self._standby = True
        self._poke()

    def _on_legacy_log(self, text: str) -> None:
        role, body, level = log_role(text)
        if not body:
            return
        if role == "user":
            now = time.monotonic()
            for ts, t in self._recent_user:
                if t == body and now - ts < 5.0:
                    return  # already recorded when it entered _on_text_command
        self._add_conversation(role, body, level=level)

    def _add_conversation(self, role: str, text: str, **extra: Any) -> None:
        entry = {"id": uuid.uuid4().hex[:10], "role": role, "text": text, "ts": time.time()}
        entry.update({k: v for k, v in extra.items() if v})
        self._conversation.append(entry)
        self._emit("conversation.entry", buffer=False, metadata={"entry": entry})

    def user_input(self, text: str, source: str = "typed") -> None:
        """A typed/Telegram order entering _on_text_command."""
        try:
            with self._lock:
                text = (text or "").strip()
                if not text or text.startswith("__"):
                    return
                self._recent_user.append((time.monotonic(), text))
                self._add_conversation("user", text, source=source)
        except Exception:
            pass


class ReadSideMixin:
    def state(self) -> dict:
        with self._lock:
            return {"state": self._state[0], "label": self._state[1]}

    def connection_info(self) -> dict:
        with self._lock:
            return dict(self._connection, label=CONN_LABEL[self._connection["kind"]])

    def snapshot(self, conversation_limit: int = 200) -> dict:
        with self._lock:
            return {
                "sessionId": self.session_id,
                "startedAt": self._started,
                "state": self._state[0],
                "label": self._state[1],
                "standby": self._standby,
                "speaking": self._speaking,
                "micActive": self._mic_active,
                "voiceActive": self._voice["active"],
                "connection": self.connection_info(),
                "tools": [self._public_run(r) for r in self._tools.values()],
                "cards": list(self._cards),
                "sources": list(self._sources),
                "conversation": list(self._conversation)[-conversation_limit:],
                "transcript": dict(self._transcript),
                "latency": self.latency(),
                "memory": dict(self._memory),
                "model": self._model,
                "catalog": self._catalog,
                "approval": self.approval_public(),
                "lastSeq": next(self._seq),
            }

    def conversation(self, limit: int = 400) -> list[dict]:
        with self._lock:
            return list(self._conversation)[-limit:]

    def activity(self) -> dict:
        with self._lock:
            return {
                "tools": [self._public_run(r) for r in self._tools.values()],
                "cards": list(self._cards),
                "sources": list(self._sources),
            }

    def latency(self) -> dict:
        with self._lock:
            out: dict[str, Any] = dict(self._latency)
            for k, hist in self._latency_hist.items():
                out[k.replace("Ms", "AvgMs")] = int(sum(hist) / len(hist)) if hist else None
            out["tools"] = dict(self._tool_latency)
            return out

    def failures(self) -> list[dict]:
        with self._lock:
            return list(self._failures)

    def memory_stats(self) -> dict:
        with self._lock:
            return dict(self._memory)

    def model(self) -> dict | None:
        with self._lock:
            return dict(self._model) if self._model else None
