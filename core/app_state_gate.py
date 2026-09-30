# core/app_state_gate.py
"""Pre-command system-state gate for Era One application control.

Before Era acts ON an application (pause, next, minimize, close, focus,
volume, skip …) this gate gathers real telemetry for the *target* — process
presence, a visible window through the Win32 API (never vision), and, where the
app exposes one, its native status command — and refuses to let a control
command run against an app that cannot support it. That is what stops Era
claiming "paused, sir" when Spotify was never open.

It is defense-in-depth, not a replacement for the handlers' own checks. Two
rules keep it from ever making things worse:

  * **Degrade-never-raise.** It never raises. A control command is skipped ONLY
    on positive evidence the target is absent; unknown or failed telemetry falls
    through to ALLOW, so a psutil hiccup can never block a legitimate command.
  * **Never gate a launch or a query.** LAUNCH intents (play/open/search)
    legitimately start a closed app, and QUERY intents (status/list) ARE the
    telemetry — gating either would defeat the point.

Direct app/audio-session and window checks outrank bare process-list presence
when deciding "is it really there and usable", per the requirement to prefer
direct checks over a name in the process list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import process_awareness
from core.utils import log

# ── intent classification ────────────────────────────────────────────────────
# Actions arrive already canonicalized (see core/action_registry). We normalize
# once more here so a raw "spotify_pause" and a bare "pause" both classify.


def _norm(s: str | None) -> str:
    return (s or "").lower().strip().replace("-", "_").replace(" ", "_")


# Starting/opening media is fine when the app is closed — the command IS what
# opens it. Never gate these.
_LAUNCH_ACTIONS = frozenset(
    {
        "play",
        "open",
        "launch",
        "start",
        "resume",
        "search",
        "grid_play",
        "play_music",
        "resume_music",
        "some_music",
        "start_music",
        "continue_music",
        "play_some_music",
        "liked_songs",
        "new_releases",
        "made_for_you",
        "nothing_playing",
        "spotify_play",
        "spotify_search",
        "youtube_play",
        "youtube_resume",
        "netflix_play",
    }
)

# These read state; they are the telemetry itself. Never gate them.
_QUERY_ACTIONS = frozenset(
    {
        "status",
        "is_playing",
        "verify",
        "check",
        "current_track",
        "list",
        "active",
        "active_window",
        "running_apps",
        "system_snapshot",
        "monitors",
    }
)

# Acting on an app that must already be there. These are gated.
_CONTROL_ACTIONS = frozenset(
    {
        "pause",
        "stop",
        "next",
        "previous",
        "prev",
        "skip",
        "back",
        "forward",
        "rewind",
        "skip_intro",
        "minimize",
        "maximize",
        "restore",
        "show",
        "focus",
        "close",
        "move",
        "resize",
        "like",
        "dislike",
        "like_track",
        "dislike_track",
        "volume_up",
        "volume_down",
        "spotify_pause",
        "spotify_next",
        "spotify_previous",
        "spotify_like",
        "youtube_pause",
        "netflix_pause",
        "netflix_forward",
        "netflix_rewind",
    }
)

# A concise verb for the "can't do X, it's closed" message.
_ACTION_VERB = {
    "pause": "pause",
    "spotify_pause": "pause",
    "youtube_pause": "pause",
    "netflix_pause": "pause",
    "stop": "stop",
    "next": "skip",
    "spotify_next": "skip",
    "skip": "skip",
    "forward": "skip forward in",
    "netflix_forward": "skip forward in",
    "previous": "go back a track in",
    "spotify_previous": "go back a track in",
    "prev": "go back a track in",
    "back": "go back in",
    "rewind": "rewind",
    "netflix_rewind": "rewind",
    "skip_intro": "skip the intro on",
    "minimize": "minimize",
    "maximize": "maximize",
    "restore": "restore",
    "show": "show",
    "focus": "focus",
    "close": "close",
    "move": "move",
    "resize": "resize",
    "like": "like the track in",
    "like_track": "like the track in",
    "spotify_like": "like the track in",
    "dislike": "change the track rating in",
    "dislike_track": "change the track rating in",
    "volume_up": "change the volume of",
    "volume_down": "change the volume of",
}

# Human-friendly display names for the target app.
_DISPLAY = {
    "spotify": "Spotify",
    "youtube": "YouTube",
    "netflix": "Netflix",
    "premiere": "Premiere Pro",
    "brave": "Brave",
    "chrome": "Chrome",
}

_BROWSERS = ("brave", "chrome", "edge", "firefox", "opera")


@dataclass
class GateDecision:
    """Outcome of the pre-command telemetry check.

    allow=True means the command may proceed. allow=False means the target
    cannot support it; `message` is the concise, honest line to hand back as the
    tool result (Era speaks it). `telemetry` is kept for logging/diagnosis.
    """

    allow: bool
    message: str = ""
    app: str = ""
    reason: str = ""
    telemetry: dict = field(default_factory=dict)


# ── win32 window enumeration (visible titles + classes; never vision) ────────
def _visible_windows() -> list[tuple[str, str]]:
    """(title_lower, class_name) for every visible top-level window. Empty on
    non-Windows or if win32 is unavailable — callers treat empty as 'unknown'."""
    out: list[tuple[str, str]] = []
    try:
        import win32gui
    except Exception:
        return out

    def _enum(hwnd, acc):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip().lower()
            cls = win32gui.GetClassName(hwnd) or ""
            if title or cls:
                acc.append((title, cls))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_enum, out)
    except Exception:
        pass
    return out


def _any_browser_running() -> bool:
    try:
        return any(process_awareness.is_running(b) for b in _BROWSERS)
    except Exception:
        return True  # unknown → assume present (fail-open)


# ── per-app telemetry ────────────────────────────────────────────────────────
def _spotify_telemetry() -> dict:
    """Prefer Spotify's own window handle and Windows audio-session state over a
    bare process-list hit — a name in the process list does not prove a usable,
    audible client."""
    running = False
    window = False
    audio_session = None
    try:
        running = process_awareness.is_running("spotify")
    except Exception:
        running = False
    try:
        from actions.spotify_control import (
            _get_spotify_audio_session_state,
            _spotify_window_exists,
        )

        window = _spotify_window_exists()
        audio_session = _get_spotify_audio_session_state()
    except Exception:
        pass
    present = bool(window) or (audio_session is not None) or running
    return {
        "process_running": running,
        "window_present": window,
        "audio_session_state": audio_session,
        "present": present,
    }


def _desktop_app_telemetry(app_key: str) -> dict:
    """Process + visible-window telemetry for a native desktop app (Spotify,
    Premiere, browsers, …) using the window_control registry for the exe/class."""
    try:
        from actions.window_control import MASTER_APP_REGISTRY
    except Exception:
        MASTER_APP_REGISTRY = {}

    spec = MASTER_APP_REGISTRY.get(app_key, {})
    exe = (spec.get("exe") or "").lower()
    win_class = spec.get("class") or ""

    running = False
    try:
        running = process_awareness.is_running(app_key)
    except Exception:
        running = False
    if not running and exe:
        try:
            for p in process_awareness.list_processes():
                if (p.name or "").lower() == exe:
                    running = True
                    break
        except Exception:
            pass

    window = False
    for title, cls in _visible_windows():
        if app_key and app_key in title:
            window = True
            break
        if win_class and cls == win_class:
            window = True
            break

    return {
        "process_running": running,
        "window_present": window,
        "present": bool(running or window),
    }


# ── target resolution ────────────────────────────────────────────────────────
def _resolve_target(tool: str, action: str, params: dict) -> str | None:
    """The app a tool+action acts on, or None when the tool is not app-scoped
    (or the target can't be pinned down — in which case we don't gate)."""
    if tool in ("spotify_control",):
        return "spotify"
    if tool == "computer_settings" and action.startswith("spotify"):
        return "spotify"
    if tool == "youtube_video":
        return "youtube"
    if tool == "netflix_control":
        return "netflix"
    if tool == "premiere_control":
        return "premiere"
    if tool in ("window_control", "desktop_control"):
        title = _norm(params.get("title") or params.get("app") or params.get("target") or "")
        if not title:
            return None
        try:
            from actions.window_control import MASTER_APP_REGISTRY
        except Exception:
            return None
        for key in MASTER_APP_REGISTRY:
            k = _norm(key)
            if k and (k in title or title in k):
                # Return the registry key so telemetry can use its exe/class.
                return key
        return None  # unknown title → let the handler resolve it honestly
    if tool == "browser_control" and action in ("close_tab", "next_tab", "prev_tab"):
        return "brave"
    return None


def _classify(action: str) -> str:
    if action in _LAUNCH_ACTIONS or action in _QUERY_ACTIONS:
        return "skip"  # never gate
    if action in _CONTROL_ACTIONS:
        return "control"
    return "skip"  # unknown action → don't gate


def _display(app: str) -> str:
    return _DISPLAY.get(app, app.replace("_", " ").title())


def _verb(action: str) -> str:
    return _ACTION_VERB.get(action, "control")


# ── public entrypoint ────────────────────────────────────────────────────────
def evaluate_app_command(tool: str, action: str | None, params: dict | None) -> GateDecision:
    """Decide whether an app-control command may run, from live telemetry.

    Returns allow=True for anything that is not a gated control command, and for
    any case where telemetry cannot positively prove the target is absent.
    Never raises: any failure inside becomes an ALLOW.
    """
    try:
        params = params or {}
        act = _norm(action)

        if _classify(act) != "control":
            return GateDecision(allow=True, reason="not_a_control_action")

        app = _resolve_target(tool, act, params)
        if not app:
            return GateDecision(allow=True, reason="no_app_target")

        # ── Browser-hosted media: a backgrounded tab has no top-level title, so
        # window/title telemetry can't prove a tab is absent. Only refuse when no
        # browser is running at all — then there is nothing that could be playing.
        if app in ("youtube", "netflix"):
            if _any_browser_running():
                return GateDecision(
                    allow=True, app=app, reason="browser_running", telemetry={"browser": True}
                )
            name = _display(app)
            return GateDecision(
                allow=False,
                app=app,
                reason="no_browser",
                telemetry={"browser": False},
                message=(
                    f"No browser is open, sir, so there's no {name} playing to "
                    f"{_verb(act)}. I've left it as it is."
                ),
            )

        # ── Spotify: its own process + audio session are directly checkable. ──
        if app == "spotify":
            tel = _spotify_telemetry()
            if tel["present"]:
                return GateDecision(allow=True, app=app, reason="present", telemetry=tel)
            return GateDecision(
                allow=False,
                app=app,
                reason="absent",
                telemetry=tel,
                message=(
                    f"Spotify isn't running, sir, so there's nothing to {_verb(act)}. "
                    "I've left it as it is."
                ),
            )

        # ── Any other native desktop app (Premiere, browsers, registry apps). ──
        tel = _desktop_app_telemetry(app)
        if tel["present"]:
            return GateDecision(allow=True, app=app, reason="present", telemetry=tel)
        name = _display(app)
        return GateDecision(
            allow=False,
            app=app,
            reason="absent",
            telemetry=tel,
            message=(f"{name} isn't open, sir, so I can't {_verb(act)} it. I've left it as it is."),
        )
    except Exception as exc:  # degrade-never-raise: uncertainty → allow
        try:
            log("AppStateGate", f"telemetry skipped ({type(exc).__name__}: {exc}); allowing")
        except Exception:
            pass
        return GateDecision(allow=True, reason="telemetry_error")
