"""Constants and pure helpers shared by the HUD event bus modules."""

from __future__ import annotations

STATES = (
    "idle",
    "wake_detected",
    "listening",
    "transcribing",
    "routing",
    "thinking",
    "tool_running",
    "synthesizing",
    "speaking",
    "interrupted",
    "complete",
    "error",
    "offline",
    "standby",
    "awaiting_approval",
)

# Transient display states: shown for this long, then the state settles.
TRANSIENT_S = {"wake_detected": 1.2, "interrupted": 0.9, "complete": 1.4, "error": 3.5}
# Pending phases: the guard since the last signal that fed them.
PHASE_GUARD_S = {
    "listening": 1.3,
    "transcribing": 6.0,
    "routing": 20.0,
    "thinking": 25.0,
    "synthesizing": 12.0,
}
TOOL_GUARD_S = 240.0
# While an approval is shown, re-read the PendingConfirmationStore this often so a
# confirmation resolved (or expired) on any path never leaves the HUD asking.
APPROVAL_RECHECK_S = 2.0
TURN_DONE_GUARD_S = 4.0

# Normalized RMS of the real mic above which it is hearing sound worth calling
# voice activity (~ -35 dBFS). Needs VOICE_ONSET consecutive samples to open and
# holds VOICE_HOLD_S after the last one, so a click does not flicker the state.
VOICE_ON = 0.018
VOICE_ONSET = 2
VOICE_HOLD_S = 0.9
LEVEL_MIN_INTERVAL_S = 0.075

CONN_LABEL = {
    "connected": "Connected",
    "local": "Local mode",
    "connecting": "Connecting",
    "reconnecting": "Reconnecting",
    "cooldown": "Quota cooldown",
    "offline": "Model offline",
    "auth": "API key invalid",
    "config": "Config rejected",
}

MAX_TOOLS = 60
MAX_RESPONSE_CHARS = 4000


def classify_connection(status: str) -> str:
    """Map ReconnectMixin.set_status strings onto a small, stable set of kinds."""
    s = (status or "").strip().lower()
    if s.startswith("connected"):
        return "connected"
    if s.startswith("local mode") or s.startswith("cascade"):
        return "local"
    if "cooling down" in s or "quota" in s:
        return "cooldown"
    if "api key invalid" in s:
        return "auth"
    if "config rejected" in s:
        return "config"
    if s.startswith("reconnecting"):
        return "reconnecting"
    if s.startswith("offline"):
        return "offline"
    return "connecting"


def log_role(text: str) -> tuple[str, str, str]:
    """(role, text, level) for a legacy write_log line — same prefixes useEra used."""
    t = text or ""
    low = t.lower()
    if low.startswith("you:"):
        return "user", t[4:].strip(), "info"
    for p in ("era one:", "era:"):
        if low.startswith(p):
            return "agent", t[len(p) :].strip(), "info"
    for p, lvl in (("err:", "error"), ("warn:", "warning"), ("safety:", "warning")):
        if low.startswith(p):
            return "system", t, lvl
    return "system", t, "info"
