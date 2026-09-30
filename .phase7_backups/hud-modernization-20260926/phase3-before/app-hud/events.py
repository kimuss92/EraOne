"""The HUD's single, truthful event stream and agent state machine.

Everything the localhost HUD animates comes from here, and everything here comes
from a real backend signal: a tool call entering or leaving ``_execute_tool``, a
Gemini transcription chunk, a TTS state flip, a connection-status change, the
RMS of the real microphone. Nothing invents activity. The only timers are
EXPIRY guards, so that a state can never stick — the HUD must never sit in
THINKING forever because a follow-up signal never came.

Wire format (JSON over the HUD WebSocket):

  {"type": "event", "event": AgentEvent}      discrete, sequenced, replayable
  {"type": "voice_level", "level", "active"}  high-rate real mic level

AgentEvent = {id, seq, ts, sessionId, type, state?, label?, message?,
              toolName?, toolCallId?, duration?, metadata?}

State priority (first match wins) — see ``_derive``:

  standby > speaking > transient > foreground tool > phase > offline > idle

Split across three modules to stay readable: tool events (bus_tools.py),
legacy-UI observation + read side (bus_legacy.py), constants (bus_const.py).

Degrade-never-raise: every public method swallows its own errors. The engine
calls these from the audio callback and from tool dispatch; a HUD bug must
never cost the user a turn.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

from app.hud.bus_const import (
    CONN_LABEL,
    LEVEL_MIN_INTERVAL_S,
    MAX_RESPONSE_CHARS,
    PHASE_GUARD_S,
    STATES,  # noqa: F401  (re-export)
    TOOL_GUARD_S,
    TRANSIENT_S,
    TURN_DONE_GUARD_S,
    VOICE_HOLD_S,
    VOICE_ON,
    VOICE_ONSET,
    classify_connection,
)
from app.hud.bus_legacy import LegacyObserverMixin, ReadSideMixin
from app.hud.bus_tools import ToolEventsMixin


class HudBus(ToolEventsMixin, LegacyObserverMixin, ReadSideMixin):
    """Thread-safe observer of the engine. One instance per process (``hud``)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._sink: Callable[[dict], None] | None = None
        self.session_id = uuid.uuid4().hex[:12]
        self._seq = itertools.count(1)
        self._started = time.time()

        self._events: deque[dict] = deque(maxlen=300)
        self._tools: OrderedDict[str, dict] = OrderedDict()
        self._cards: deque[dict] = deque(maxlen=40)
        self._sources: deque[dict] = deque(maxlen=40)
        self._conversation: deque[dict] = deque(maxlen=400)
        self._failures: deque[dict] = deque(maxlen=25)
        self._recent_user: deque[tuple[float, str]] = deque(maxlen=8)
        self._memory = {"retrieved_total": 0, "last_count": 0, "last_at": None, "entries": []}

        # facts
        self._standby = False
        self._speaking = False
        self._mic_active = True
        self._connection = {"kind": "connecting", "status": "Starting", "since": time.time()}
        self._phase: tuple[str, str, float] | None = None
        self._transient: tuple[str, str, float] | None = None
        self._state: tuple[str, str] = ("offline", "Starting")
        self._model: dict | None = None
        self._catalog: dict | None = None

        # current turn
        self._transcript = {"user": "", "userFinal": False, "response": "", "responding": False}
        self._turn_t0: float | None = None
        # First-audio anchor: outlives the text turn, because in transcript-synth
        # voice modes (Fish/Kokoro) the reply text completes BEFORE its audio plays.
        self._audio_t0: float | None = None
        self._first_partial_at: float | None = None
        self._first_audio_seen = False
        self._ttft_seen = False
        self._turn_interrupted = False
        self._suppress_tts_complete = False
        self._turn_done_at: float | None = None
        self._last_final = ""

        self._latency: dict[str, int | None] = {
            "ttftMs": None,
            "firstAudioMs": None,
            "responseMs": None,
            "transcriptionMs": None,
        }
        self._latency_hist: dict[str, deque[int]] = {k: deque(maxlen=10) for k in self._latency}
        self._tool_latency: dict[str, int] = {}

        self._voice = {"level": 0.0, "active": False, "above": 0, "last_above": 0.0, "emit": 0.0}
        self._timer: threading.Thread | None = None

    # ── plumbing ─────────────────────────────────────────────────────────

    def attach(self, sink: Callable[[dict], None] | None) -> None:
        """Route events to the WebSocket broadcaster (or detach with None)."""
        with self._lock:
            self._sink = sink

    def _send(self, msg: dict) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink(msg)
        except Exception:
            pass

    def _emit(self, etype: str, *, buffer: bool = True, **fields: Any) -> dict:
        ev: dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "seq": next(self._seq),
            "ts": time.time(),
            "sessionId": self.session_id,
            "type": etype,
        }
        for k, v in fields.items():
            if v is not None:
                ev[k] = v
        if buffer:
            self._events.append(ev)
        self._send({"type": "event", "event": ev})
        return ev

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._events)[-limit:]

    def _ensure_timer(self) -> None:
        if self._timer is not None and self._timer.is_alive():
            return
        self._timer = threading.Thread(target=self._timer_loop, name="HudStateTimer", daemon=True)
        self._timer.start()

    def _timer_loop(self) -> None:
        with self._cond:
            while True:
                try:
                    deadline = self._next_deadline()
                    now = time.monotonic()
                    if deadline is None:
                        self._cond.wait(30.0)
                    elif deadline > now:
                        self._cond.wait(min(30.0, deadline - now))
                    self._expire(time.monotonic())
                except Exception:
                    time.sleep(0.5)

    def _poke(self) -> None:
        """Recompute state now and let the timer re-read its deadlines."""
        self._update_state()
        self._ensure_timer()
        self._cond.notify_all()

    def _next_deadline(self) -> float | None:
        cands = []
        if self._transient:
            cands.append(self._transient[2])
        if self._phase:
            cands.append(self._phase[2])
        if self._turn_done_at is not None:
            cands.append(self._turn_done_at + TURN_DONE_GUARD_S)
        for run in self._tools.values():
            if run["status"] == "running":
                cands.append(run["_mono"] + TOOL_GUARD_S)
        return min(cands) if cands else None

    def _expire(self, now: float) -> None:
        if self._transient and self._transient[2] <= now:
            self._transient = None
        if self._phase and self._phase[2] <= now:
            self._phase = None
        if self._turn_done_at is not None and now - self._turn_done_at > TURN_DONE_GUARD_S:
            self._turn_done_at = None
        for cid, run in self._tools.items():
            if run["status"] == "running" and now - run["_mono"] > TOOL_GUARD_S:
                # We genuinely do not know how it ended; say exactly that.
                run["status"] = "unknown"
                run["summary"] = "No result was reported"
                self._emit(
                    "tool.failed",
                    toolName=run["tool"],
                    toolCallId=cid,
                    label=run["label"],
                    message=run["summary"],
                    metadata=self._public_run(run),
                )
        self._update_state()

    # ── state machine ────────────────────────────────────────────────────

    def _derive(self, now: float) -> tuple[str, str]:
        if self._standby:
            return "standby", "Standby"
        if self._speaking:
            return "speaking", "Speaking"
        t = self._transient
        if t and t[2] > now:
            return t[0], t[1]
        fg = [r for r in self._tools.values() if r["status"] == "running" and not r["background"]]
        if fg:
            r = fg[-1]
            return "tool_running", f"{r['label']} · {r['activity']}"
        p = self._phase
        if p and p[2] > now:
            return p[0], p[1]
        kind = self._connection["kind"]
        if kind not in ("connected", "local"):
            return "offline", CONN_LABEL.get(kind, "Offline")
        return "idle", ("Ready" if self._mic_active else "Mic off")

    def _update_state(self) -> None:
        new = self._derive(time.monotonic())
        if new != self._state:
            prev = self._state
            self._state = new
            self._emit(
                "agent.state.changed", state=new[0], label=new[1], metadata={"previous": prev[0]}
            )

    def _set_phase(self, name: str, label: str) -> None:
        self._phase = (name, label, time.monotonic() + PHASE_GUARD_S[name])

    def _set_transient(self, name: str, label: str) -> None:
        self._transient = (name, label, time.monotonic() + TRANSIENT_S[name])

    def _record_latency(self, key: str, ms: float) -> None:
        v = max(0, int(ms))
        self._latency[key] = v
        self._latency_hist[key].append(v)

    # ── connection / wake / errors ───────────────────────────────────────

    def connection(self, status: str) -> None:
        try:
            with self._lock:
                kind = classify_connection(status)
                if kind == self._connection["kind"] and status == self._connection["status"]:
                    return
                self._connection = {"kind": kind, "status": status, "since": time.time()}
                if kind in ("cooldown", "auth", "config"):
                    self._failures.append({"ts": time.time(), "where": "model", "message": status})
                self._emit("connection.status", state=kind, label=CONN_LABEL[kind], message=status)
                self._poke()
        except Exception:
            pass

    def wake_detected(self) -> None:
        try:
            with self._lock:
                self._set_transient("wake_detected", "Wake word")
                self._emit("voice.wake_detected", label="Wake word detected")
                self._poke()
        except Exception:
            pass

    def error(self, message: str, where: str = "engine") -> None:
        try:
            with self._lock:
                self._failures.append({"ts": time.time(), "where": where, "message": message})
                self._set_transient("error", message[:60] or "Error")
                self._emit("error", message=message, metadata={"where": where})
                self._poke()
        except Exception:
            pass

    # ── microphone ───────────────────────────────────────────────────────

    def voice_level(self, rms: float) -> None:
        """Real mic RMS (0..1). Called from the audio callback — must stay cheap."""
        try:
            now = time.monotonic()
            with self._lock:
                v = self._voice
                level = max(0.0, min(1.0, float(rms)))
                v["level"] = level
                if level >= VOICE_ON:
                    v["above"] += 1
                    v["last_above"] = now
                else:
                    v["above"] = 0
                active = v["above"] >= VOICE_ONSET or (
                    v["active"] and now - v["last_above"] < VOICE_HOLD_S
                )
                if active != v["active"]:
                    v["active"] = active
                    self._emit(
                        "voice.listening.started" if active else "voice.listening.stopped",
                        buffer=False,
                    )
                    if active and not self._speaking and not self._standby:
                        if self._phase is None or self._phase[0] == "listening":
                            self._set_phase("listening", "Listening")
                    self._poke()
                elif active and self._phase and self._phase[0] == "listening":
                    self._set_phase("listening", "Listening")
                if now - v["emit"] >= LEVEL_MIN_INTERVAL_S:
                    v["emit"] = now
                    self._send({"type": "voice_level", "level": round(level, 4), "active": active})
        except Exception:
            pass

    # ── the turn: transcript → request → response ───────────────────────

    def transcript_partial(self, text: str) -> None:
        try:
            with self._lock:
                if self._standby or not text:
                    return
                if self._first_partial_at is None:
                    self._first_partial_at = time.monotonic()
                self._transcript["user"] = text
                self._transcript["userFinal"] = False
                self._set_phase("transcribing", "Transcribing")
                self._emit("voice.transcript.partial", buffer=False, message=text)
                self._poke()
        except Exception:
            pass

    def user_speech_finished(self, text: str) -> None:
        """Gemini's `input_transcription.finished` — the user stopped talking."""
        try:
            with self._lock:
                now = time.monotonic()
                if self._first_partial_at is not None:
                    self._record_latency("transcriptionMs", (now - self._first_partial_at) * 1000)
                self._first_partial_at = None
                self._begin_turn(now)
                self._set_phase("thinking", "Thinking")
                self._final(text)
                self._poke()
        except Exception:
            pass

    def transcript_final(self, text: str) -> None:
        try:
            with self._lock:
                self._first_partial_at = None
                self._final(text)
                if self._phase and self._phase[0] == "transcribing":
                    self._phase = None
                self._poke()
        except Exception:
            pass

    def _final(self, text: str) -> None:
        text = (text or "").strip()
        if not text or text == self._last_final:
            return
        self._last_final = text
        self._transcript["user"] = text
        self._transcript["userFinal"] = True
        self._emit("voice.transcript.final", message=text)

    def _begin_turn(self, now: float) -> None:
        self._turn_t0 = now
        self._audio_t0 = now
        self._ttft_seen = False
        self._first_audio_seen = False
        self._turn_interrupted = False
        self._turn_done_at = None
        self._transcript["response"] = ""
        self._transcript["responding"] = False

    def routing_started(self) -> None:
        try:
            with self._lock:
                self._set_phase("routing", "Routing")
                self._emit("agent.routing.started", label="Routing request")
                self._poke()
        except Exception:
            pass

    def routing_finished(self, target: str, duration_ms: float | None = None) -> None:
        try:
            with self._lock:
                if self._phase and self._phase[0] == "routing":
                    self._phase = None
                self._emit(
                    "agent.routing.completed",
                    label=f"Routed to {target}",
                    duration=int(duration_ms) if duration_ms is not None else None,
                    metadata={"target": target},
                )
                self._poke()
        except Exception:
            pass

    def request_sent(self, source: str = "typed") -> None:
        """A request was handed to the live model; the answer is now pending."""
        try:
            with self._lock:
                self._begin_turn(time.monotonic())
                self._set_phase("thinking", "Thinking")
                self._emit("agent.request.sent", metadata={"source": source})
                self._poke()
        except Exception:
            pass

    def response_delta(self, chunk: str) -> None:
        try:
            with self._lock:
                if not chunk:
                    return
                now = time.monotonic()
                if not self._transcript["responding"]:
                    self._transcript["responding"] = True
                    self._emit("agent.response.started", buffer=False)
                if self._turn_t0 is not None and not self._ttft_seen:
                    self._ttft_seen = True
                    self._record_latency("ttftMs", (now - self._turn_t0) * 1000)
                resp = (self._transcript["response"] + " " + chunk).strip()
                self._transcript["response"] = resp[-MAX_RESPONSE_CHARS:]
                if not self._speaking:
                    self._set_phase("synthesizing", "Preparing response")
                self._emit("agent.response.delta", buffer=False, message=chunk)
                self._poke()
        except Exception:
            pass

    def response_completed(self, text: str) -> None:
        try:
            with self._lock:
                now = time.monotonic()
                interrupted = self._turn_interrupted
                self._turn_interrupted = False  # the next turn starts clean
                if self._turn_t0 is not None and text:
                    self._record_latency("responseMs", (now - self._turn_t0) * 1000)
                self._turn_t0 = None
                self._transcript["responding"] = False
                if self._phase and self._phase[0] in ("thinking", "synthesizing"):
                    self._phase = None
                if text and not interrupted:
                    if self._speaking:
                        self._turn_done_at = now
                    elif self._first_audio_seen:
                        self._set_transient("complete", "Complete")
                self._emit(
                    "agent.response.completed",
                    message=(text or "")[-MAX_RESPONSE_CHARS:],
                    metadata={"interrupted": interrupted},
                )
                self._poke()
        except Exception:
            pass

    def barge_in(self, source: str = "local") -> None:
        try:
            with self._lock:
                if self._turn_interrupted:
                    return
                self._turn_interrupted = True
                self._suppress_tts_complete = True
                self._turn_done_at = None
                self._set_transient("interrupted", "Interrupted")
                self._emit("tts.interrupted", label="Interrupted", metadata={"source": source})
                self._poke()
        except Exception:
            pass


hud = HudBus()
