# core/action_registry.py
"""Unified action registry for Era One routing.

The registry is intentionally lazy: handlers are imported only when called.
This avoids circular imports and keeps startup fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.utils import log


@dataclass(frozen=True)
class ActionSpec:
    tool: str
    action: str | None = None
    target: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""
    risk_level: str | None = None
    requires_confirmation: bool | None = None
    timeout_seconds: int | None = None


ACTION_SPECS: dict[str, ActionSpec] = {
    # system volume
    "volume_set": ActionSpec(
        "computer_settings", "volume_set", aliases=("volume", "set_volume", "absolute_volume")
    ),
    "volume_up": ActionSpec("computer_settings", "volume_up", aliases=("louder", "sound_up")),
    "volume_down": ActionSpec(
        "computer_settings", "volume_down", aliases=("quieter", "sound_down")
    ),
    "get_volume": ActionSpec(
        "computer_settings",
        "get_volume",
        aliases=(
            "current_volume",
            "volume_status",
            "check_volume",
            "volume_level",
            "what_is_the_volume",
        ),
    ),
    "mute": ActionSpec("computer_settings", "mute", aliases=("volume_mute",)),
    # unmute / toggle_mute are distinct from mute: collapsing them into "mute" is how
    # "unmute" ended up muting. Each keeps its own action all the way to the handler.
    "unmute": ActionSpec("computer_settings", "unmute"),
    "toggle_mute": ActionSpec("computer_settings", "toggle_mute"),
    # Star Glasses volume — a DISTINCT endpoint from the system mixer. Only these
    # explicit tokens touch the glasses; generic volume_up/down stay on system.
    "glass_volume_up": ActionSpec(
        "computer_settings", "glass_volume_up", aliases=("glasses_volume_up", "glasses_louder")
    ),
    "glass_volume_down": ActionSpec(
        "computer_settings", "glass_volume_down", aliases=("glasses_volume_down", "glasses_quieter")
    ),
    "glass_volume_set": ActionSpec(
        "computer_settings",
        "glass_volume_set",
        aliases=("glass_volume", "glasses_volume", "set_glass_volume"),
    ),
    # performance profiles (MSI Center) + PC Manager boost
    "msi_scenario": ActionSpec(
        "system_performance",
        "msi_scenario",
        aliases=("usage_scenario", "performance_mode", "user_scenario", "set_scenario"),
    ),
    "msi_scenario_status": ActionSpec(
        "system_performance",
        "msi_scenario_status",
        aliases=("scenario_status", "current_scenario", "performance_status", "which_scenario"),
    ),
    "pc_boost": ActionSpec(
        "system_performance", "pc_boost", aliases=("boost_pc", "pc_manager_boost", "smart_boost")
    ),
    "hotkey": ActionSpec("computer_control", "hotkey", aliases=("key_combo", "shortcut", "keys")),
    "press": ActionSpec("computer_control", "press", aliases=("press_key", "key_press")),
    "show_desktop": ActionSpec(
        "computer_settings", "show_desktop", aliases=("minimize_all", "desktop")
    ),
    "fullscreen": ActionSpec("computer_settings", "full_screen", aliases=("full_screen", "f11")),
    # spotify
    # Routed through computer_settings because that handler coordinates global media state.
    # actions/spotify_control.py remains the lower-level Windows/Spotify helper.
    "spotify_play": ActionSpec(
        "computer_settings", "spotify_play", "spotify", aliases=("play_music", "resume_music")
    ),
    "spotify_pause": ActionSpec(
        "computer_settings", "spotify_pause", "spotify", aliases=("pause_music", "stop_music")
    ),
    "spotify_next": ActionSpec(
        "computer_settings",
        "spotify_next",
        "spotify",
        aliases=("next_track", "next_song", "skip_track"),
    ),
    "spotify_previous": ActionSpec(
        "computer_settings",
        "spotify_previous",
        "spotify",
        aliases=("previous_track", "previous_song", "prev_track"),
    ),
    "spotify_search": ActionSpec("computer_settings", "spotify_search", "spotify"),
    "spotify_like": ActionSpec("computer_settings", "spotify_like", "spotify"),
    # Exact control (window-automation lane): volume via pycaw, track data via WinRT
    # GSMTC. Handled in actions/spotify_control.py -> actions/spotify_exact.py. NOT
    # the dormant keyless bridge. Routed through the spotify_control tool.
    "spotify_get_volume_exact": ActionSpec(
        "spotify_control",
        "get_volume_exact",
        "spotify",
        description="Report Spotify's current exact volume and mute state.",
    ),
    "spotify_set_volume_exact": ActionSpec(
        "spotify_control",
        "set_volume_exact",
        "spotify",
        description=(
            "Set Spotify's own volume to an exact percentage (0-100). Use for "
            "'set spotify volume to 42', 'spotify volume 30 percent'."
        ),
    ),
    "spotify_volume_delta": ActionSpec(
        "spotify_control",
        "volume_delta",
        "spotify",
        description=(
            "Nudge Spotify's volume up or down by a signed amount. Use for "
            "'louder', 'quieter', 'spotify a bit louder'."
        ),
    ),
    "spotify_mute_exact": ActionSpec(
        "spotify_control",
        "mute_exact",
        "spotify",
        description="Mute Spotify only, leaving other apps audible.",
    ),
    "spotify_unmute_exact": ActionSpec(
        "spotify_control", "unmute_exact", "spotify", description="Unmute Spotify."
    ),
    "spotify_toggle_mute_exact": ActionSpec(
        "spotify_control", "toggle_mute_exact", "spotify", description="Toggle Spotify mute."
    ),
    "spotify_current_track": ActionSpec(
        "spotify_control",
        "current_track_data",
        "spotify",
        description=(
            "Report the currently playing Spotify track as title, artist, album, "
            "position, duration. Use for 'what's playing', 'what song is this', "
            "'what track'."
        ),
    ),
    "spotify_playback_state": ActionSpec(
        "spotify_control",
        "playback_state",
        "spotify",
        description="Report whether Spotify is playing, paused, or stopped.",
    ),
    "spotify_progress": ActionSpec(
        "spotify_control",
        "progress",
        "spotify",
        description="Report Spotify's playback position and track duration.",
    ),
    "spotify_seek_exact": ActionSpec(
        "spotify_control",
        "seek_exact",
        "spotify",
        description=(
            "Seek Spotify to an exact timestamp in seconds. Use for 'jump to 1:20', "
            "'skip to the chorus', 'go to 90 seconds'."
        ),
    ),
    "spotify_forward": ActionSpec(
        "spotify_control",
        "seek_forward",
        "spotify",
        description=(
            "Seek Spotify playback FORWARD within the current song by N seconds "
            "(default 15) — not a track skip. Use for 'skip ahead 30 seconds', "
            "'fast forward', 'jump forward a bit'."
        ),
    ),
    "spotify_rewind": ActionSpec(
        "spotify_control",
        "seek_rewind",
        "spotify",
        description=(
            "Seek Spotify playback BACKWARD within the current song by N seconds "
            "(default 15) — not previous track. Use for 'go back 20 seconds', "
            "'rewind', 'replay that part'."
        ),
    ),
    # youtube
    "youtube_play": ActionSpec(
        "youtube_video", "youtube_play", "youtube", aliases=("play_youtube",)
    ),
    "youtube_pause": ActionSpec(
        "youtube_video", "youtube_pause", "youtube", aliases=("pause_youtube",)
    ),
    "youtube_resume": ActionSpec(
        "youtube_video", "youtube_resume", "youtube", aliases=("resume_youtube",)
    ),
    "youtube_volume_up": ActionSpec("youtube_video", "volume_up", "youtube"),
    "youtube_volume_down": ActionSpec("youtube_video", "volume_down", "youtube"),
    # netflix
    "netflix_play": ActionSpec(
        "netflix_control",
        "netflix_play",
        "netflix",
        aliases=("netflix_resume", "netflix_pause_toggle"),
    ),
    "netflix_pause": ActionSpec(
        "netflix_control", "netflix_pause", "netflix", aliases=("pause_netflix",)
    ),
    "netflix_forward": ActionSpec("netflix_control", "forward", "netflix"),
    "netflix_rewind": ActionSpec("netflix_control", "rewind", "netflix"),
    "netflix_skip_intro": ActionSpec("netflix_control", "skip_intro", "netflix"),
    # windows/browser
    "window_active": ActionSpec("window_control", "active"),
    "window_list": ActionSpec("window_control", "list"),
    "close_window": ActionSpec("window_control", "close"),
    "focus_window": ActionSpec("window_control", "focus"),
    "browser_new_tab": ActionSpec("browser_control", "new_tab"),
    "browser_close_tab": ActionSpec("browser_control", "close_tab"),
    "browser_next_tab": ActionSpec("browser_control", "next_tab"),
    "browser_prev_tab": ActionSpec("browser_control", "prev_tab"),
    # awareness actions
    "running_apps": ActionSpec(
        "computer_control", "running_apps", aliases=("processes", "what_is_running")
    ),
    "system_snapshot": ActionSpec(
        "computer_control", "system_snapshot", aliases=("pc_status", "computer_status")
    ),
    "active_window": ActionSpec("computer_control", "active_window"),
    # agent-browser
    "agent_browser_task": ActionSpec(
        "agent_browser",
        description="Advanced multi-step browser automation for research, form filling, data extraction",
    ),
}

_ALIAS_TO_KEY: dict[str, str] = {}
for key, spec in ACTION_SPECS.items():
    _ALIAS_TO_KEY[key] = key
    if spec.action and spec.action not in _ALIAS_TO_KEY:
        _ALIAS_TO_KEY[spec.action] = key
    for alias in spec.aliases:
        if alias not in _ALIAS_TO_KEY:
            _ALIAS_TO_KEY[alias] = key


def normalize_action(action: str | None) -> str:
    raw = (action or "").lower().strip().replace("-", "_").replace(" ", "_")
    return _ALIAS_TO_KEY.get(raw, raw)


def get_spec(action: str | None) -> ActionSpec | None:
    key = normalize_action(action)
    return ACTION_SPECS.get(key)


def canonicalize_tool_call(tool: str, parameters: dict | None) -> tuple[str, dict]:
    params = dict(parameters or {})
    action = params.get("action")

    # Fix legacy exact-volume naming drift.
    if tool == "computer_settings" and str(action).lower().strip() == "volume":
        if params.get("value") is not None:
            params["action"] = "volume_set"
        else:
            params["action"] = "volume_up"

    # ── YouTube volume guard: never rewrite youtube_video volume actions ──
    if tool == "youtube_video" and str(action).lower().strip() in ("volume_up", "volume_down"):
        params.setdefault("amount", 10)
        return tool, params

    # ── Netflix fullscreen guard: never rewrite netflix_control fullscreen ──
    # The global "fullscreen" spec routes to computer_settings (Windows-level full_screen),
    # but Netflix fullscreen must stay on netflix_control so it can use CDP trusted clicks.
    if tool == "netflix_control" and str(action).lower().strip() == "fullscreen":
        log("Canonicalize", f"netflix_control fullscreen guard fired (tool={tool} action={action})")
        return tool, params

    # ── Netflix guard: never rewrite a netflix_control call ──
    # Same trap as the Gmail guard below, and it bit for real: this registry
    # maps an action name to a tool WITHOUT looking at the tool the caller
    # asked for, and "volume_up"/"volume_down"/"set_volume" belong to
    # computer_settings. So netflix_control(action="volume_up") was rewritten
    # into a SYSTEM volume change — the user asked to turn Netflix up and the
    # whole machine got louder. The narrow fullscreen guard above predates this
    # and is now redundant, but is left in place as documentation of the same
    # class of bug. An explicit netflix_control call is always a Netflix
    # operation; returning early also immunises every action added later.
    if tool == "netflix_control":
        return tool, params

    # ── Gmail guard: never rewrite a gmail_control call ──
    # This registry maps an action name to a tool WITHOUT looking at the tool
    # the caller asked for, and Gmail's vocabulary is deliberately plain:
    # list, read, search, send, reply. "list" already belongs to window_control,
    # so gmail_control(action="list") was being canonicalized into a window
    # listing — Era answering "what's in my inbox" with a list of open windows.
    # An explicit gmail_control call is always a mail operation; returning early
    # also immunises it against any future action-name collision.
    if tool == "gmail_control":
        return tool, params

    # ── Protocol One guard: "launch" / "status" are generic action words ──
    # (see the Gmail guard above for how a collision silently reroutes a call).
    if tool in ("protocol_one", "protocol_zero"):
        return tool, params

    # ── Audio-control guard: device_* / list_devices / set_default are its own
    # namespace; never let the flat action map reroute an explicit audio_control call.
    if tool == "audio_control":
        return tool, params

    # ── Claude-bridge guard: "ask" / "reply" / "status" are generic action words.
    if tool == "claude_bridge":
        return tool, params

    # ── Spotify-bridge guard: the keyless Spicetify lane owns generic words like
    # "play", "pause", "next", "search", "like", "status" — every one of which the
    # flat action map already binds elsewhere (computer_settings/window_control).
    # An explicit spotify_bridge call is always a bridge op; returning early keeps
    # it off the window-automation spotify_* rows and immunises future actions.
    if tool == "spotify_bridge":
        return tool, params

    # ── Spotify-control guard: an explicit spotify_control call is always a
    # Spotify op. Without this, spotify_control(action="volume_up") was rewritten
    # into computer_settings volume_up (the SYSTEM mixer), and it also protects the
    # exact-control tokens (progress, playback_state, seek_exact, …) from the flat
    # action map. Same class as the gmail/netflix guards above.
    if tool == "spotify_control":
        return tool, params

    # ── Scheduled-task guard: "cancel", "list", "update", "run_now" are generic,
    # and game_updater already owns schedule/cancel_schedule. Same class of bug.
    if tool == "scheduled_task":
        return tool, params

    spec = get_spec(params.get("action"))
    if spec:
        tool = spec.tool
        if spec.action:
            params["action"] = spec.action
    return tool, params


def tool_names() -> list[str]:
    return sorted({spec.tool for spec in ACTION_SPECS.values()})


def get_action_metadata(tool: str, parameters: dict | None = None) -> dict:
    params = parameters or {}
    action = params.get("action")
    spec = get_spec(action)
    if spec:
        return {
            "tool": spec.tool,
            "action": spec.action,
            "risk_level": spec.risk_level,
            "requires_confirmation": spec.requires_confirmation,
            "timeout_seconds": spec.timeout_seconds,
        }
    return {
        "tool": tool,
        "action": action,
        "risk_level": None,
        "requires_confirmation": None,
        "timeout_seconds": None,
    }


def format_registry_for_prompt() -> str:
    rows = []
    for key, spec in sorted(ACTION_SPECS.items()):
        rows.append(
            f"- {key}: tool={spec.tool}, action={spec.action or '-'}, target={spec.target or '-'}"
        )
    return "\n".join(rows)
