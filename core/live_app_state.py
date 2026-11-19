"""Read-only, post-command evidence. Never replay a command to verify it."""

from __future__ import annotations

import asyncio
import time
from pathlib import PureWindowsPath

from core import process_awareness
from core.tool_result import ToolResult

_READS = {
    "status",
    "current_track",
    "current_track_data",
    "playback_state",
    "progress",
    "get_volume_exact",
    "list",
    "active",
    "monitors",
}


def command_target(tool: str, params: dict) -> str | None:
    action = str(params.get("action") or "").lower()
    if action in _READS:
        return None
    if tool == "spotify_control" or (tool == "computer_settings" and action.startswith("spotify_")):
        return "spotify"
    if tool in {"youtube_video", "netflix_control", "premiere_control"}:
        return tool.split("_")[0]
    if tool == "media_coordinator":
        target = str(params.get("target") or "spotify").lower()
        return target if target in {"spotify", "youtube", "netflix", "all"} else None
    if tool == "open_app":
        target = str(params.get("app_name") or "").strip()
        if "://" in target or not target:
            return None
        return target
    if tool in {"window_control", "desktop_control"}:
        return params.get("app") or params.get("title") or params.get("target") or None
    return None


def _identity(app: str) -> tuple[str, set[str]]:
    from actions.open_app import _normalize_app_token
    from actions.window_control import MASTER_APP_REGISTRY, _names_app

    key = _normalize_app_token(app)
    if key in {"youtube", "netflix"}:
        return key, {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe"}
    if key not in MASTER_APP_REGISTRY:
        key = next((k for k in MASTER_APP_REGISTRY if _names_app(key, k)), key)
    spec = MASTER_APP_REGISTRY.get(key, {})
    exe = spec.get("exe") or PureWindowsPath(app).name
    if not exe.lower().endswith(".exe"):
        exe += ".exe"
    names = {exe.casefold()}
    if key == "calculator":
        names.update({"calculator.exe", "calculatorapp.exe"})
        # Shared Store host presence alone cannot prove Calculator is running.
        names.discard("applicationframehost.exe")
    return key, names


def _processes(names: set[str]) -> list[int]:
    if not process_awareness._PSUTIL:
        raise RuntimeError("process telemetry unavailable")
    return [
        p.pid for p in process_awareness.list_processes(refresh=True) if p.name.casefold() in names
    ]


def _windows(names: set[str]) -> list[dict]:
    import psutil
    import win32gui
    import win32process

    rows = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            name = psutil.Process(pid).name().casefold()
        except psutil.NoSuchProcess:
            return True
        if name in names:
            rows.append(
                {
                    "hwnd": int(hwnd),
                    "pid": pid,
                    "state": "minimized" if win32gui.IsIconic(hwnd) else "visible",
                }
            )
        return True

    win32gui.EnumWindows(collect, None)
    return rows


async def _native_spotify_playback() -> str:
    # Native binding when installed. It has no CPython 3.14 wheel, so in Era
    # the warm GSMTC host reads it instead (PF-027); unknown is never paused.
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
    except ImportError:
        return _host_spotify_playback()

    manager = await asyncio.wait_for(Manager.request_async(), timeout=0.5)
    statuses = []
    for session in manager.get_sessions():
        app = session.source_app_user_model_id.lower()
        if app == "spotify.exe" or (
            app.startswith("spotifyab.spotifymusic_") and app.endswith("!spotify")
        ):
            statuses.append(session.get_playback_info().playback_status)
    if Status.PLAYING in statuses:
        return "playing"
    if statuses and all(s == Status.PAUSED for s in statuses):
        return "paused"
    if statuses and all(s in (Status.STOPPED, Status.CLOSED) for s in statuses):
        return "stopped"
    return "unknown"


def _host_spotify_playback(timeout: float = 1.0) -> str:
    """Spotify's GSMTC playback through Era's warm PowerShell host.

    PF-027: `_native_spotify_playback` needs the `winrt` binding, which has no
    CPython 3.14 wheel, so in Era it ALWAYS raised and playback was always
    "unknown" (measured 2026-11-15: host 0.37 s cold, 3-8 ms warm, "paused"
    while the native read said unknown). Read-only; never sends a command.
    """
    from actions.spotify_gsmtc_host import request

    data = request({"op": "playback_state"}, timeout=timeout)
    if not isinstance(data, dict) or data.get("ok") is not True:
        return "unknown"
    if data.get("is_playing"):
        return "playing"
    if data.get("is_paused"):
        return "paused"
    if data.get("is_stopped"):
        return "stopped"
    return "unknown"


def _spotify_media() -> tuple[str, str]:
    from core.audio_endpoints import _ensure_com
    from core.media_routing import media_sessions

    _ensure_com()
    audio, playback = "unknown", "unknown"
    try:
        sessions = [s for s in media_sessions() if s.stem == "spotify"]
        if sessions:
            audio = "active" if any(s.active for s in sessions) else "inactive"
    except Exception:
        pass
    try:
        playback = asyncio.run(_native_spotify_playback())
    except Exception:
        pass
    if audio == "active":
        # A live native pause and active audio disagree; do not invent certainty.
        playback = "conflicting" if playback in {"paused", "stopped"} else "playing"
    return audio, playback


async def _read_browser_pages(session, source: str) -> list[str]:
    from core.browser_media import READ_MEDIA, media_source

    context = session._context
    if context is None:
        return ["unknown"]
    statuses = []
    for page in list(context.pages):
        if page.is_closed() or media_source(page.url) != source:
            continue
        url = page.url
        try:
            data = await asyncio.wait_for(page.evaluate(READ_MEDIA), timeout=0.4)
            if page.url != url or data.get("url") != url:
                statuses.append("unknown")
            else:
                statuses.append(data.get("status", "unknown"))
        except Exception:
            statuses.append("unknown")
    return statuses


def _browser_playback(source: str) -> str:
    # Read only already-connected contexts. Session.run() can recover/relaunch
    # drivers; submit directly so verification never opens or focuses anything.
    from actions.browser_control import _registry

    with _registry._lock:
        sessions = list(_registry._sessions.values())
    statuses = []
    for session in sessions:
        loop = session._loop
        if loop is None or not loop.is_running():
            statuses.append("unknown")
            continue
        pending = asyncio.run_coroutine_threadsafe(_read_browser_pages(session, source), loop)
        try:
            statuses.extend(pending.result(timeout=0.6))
        except Exception:
            pending.cancel()
            statuses.append("unknown")
    if "playing" in statuses:
        return "playing"
    if statuses and all(s in {"paused", "ended"} for s in statuses):
        return "paused"
    return "unknown"


def collect_live_state(app: str) -> dict:
    """No cache reads; independent failed probes retain their unknown state."""
    state = {
        "app": app,
        "observed_at": time.time(),
        "source": "live_os",
        "process_running": None,
        "pids": [],
        "windows": None,
        "audio_session": "unknown",
        "playback": "unknown",
        "errors": [],
    }
    try:
        key, names = _identity(app)
    except Exception:
        state["errors"].append("identity_unavailable")
        return state
    state["app"] = key
    try:
        state["pids"] = _processes(names)
        state["process_running"] = bool(state["pids"])
    except Exception:
        state["errors"].append("process_unavailable")
    try:
        state["windows"] = _windows(names)
    except Exception:
        state["errors"].append("windows_unavailable")
    if key == "spotify":
        try:
            state["audio_session"], state["playback"] = _spotify_media()
            if state["playback"] == "unknown":
                state["errors"].append("playback_unreadable")  # named, never silent
        except Exception:
            state["errors"].append("media_unavailable")
    elif key in {"youtube", "netflix"}:
        state["process_scope"] = "browser"
        try:
            state["playback"] = _browser_playback(key)
        except Exception:
            state["errors"].append("media_unavailable")
    return state


def format_live_state(state: dict) -> str:
    running = {True: "running", False: "not observed", None: "unknown"}[state["process_running"]]
    windows = state["windows"]
    window = (
        "unknown"
        if windows is None
        else (", ".join(sorted({w["state"] for w in windows})) if windows else "not observed")
    )
    scope = "browser " if state.get("process_scope") == "browser" else ""
    text = f"{state['app']}: {scope}process {running}; {scope}window {window}."
    if state["app"] == "spotify":
        text += f" Playback {state['playback']}; audio session {state['audio_session']}."
    elif state["app"] in {"youtube", "netflix"}:
        text += f" Playback {state['playback']}."
    return text


def _plain_action(params: dict) -> str:
    """The action without its platform prefix ("youtube_pause" -> "pause").

    PF-047: the spoken verdict used to strip only "spotify_", so a failed
    YouTube/Netflix command was said aloud as "I sent youtube_pause to YouTube".
    """
    action = str(params.get("action") or "").lower()
    for prefix in ("spotify_", "youtube_", "netflix_"):
        action = action.removeprefix(prefix)
    return action


def _expected(tool: str, params: dict) -> tuple[str, object] | None:
    action = _plain_action(params)
    if tool == "open_app":
        mode = params.get("mode", "open")
        if mode == "close":
            return "process_running", False  # PF-027: a close is checked, like any action
        if mode != "open":
            return None
        return "window", True
    if tool == "spotify_control" and action in PLAYLIST_LABELS:
        # Liked Songs / Release Radar / Made For You start playback: the fresh
        # media session must show Spotify playing, exactly like "play".
        return "playback", "playing"
    if action in {"play", "resume", "pause", "stop"}:
        return "playback", {
            "play": "playing",
            "resume": "playing",
            "pause": "paused",
            "stop": "stopped",
        }[action]
    if action == "minimize":
        return "window", "minimized"
    if action == "close":
        return "process_running", False
    return None


# Spotify actions that start a named collection, and how Era names it aloud.
PLAYLIST_LABELS = {
    "liked_songs": "your Liked Songs",
    "new_releases": "your Release Radar",
    "made_for_you": "your Made For You mix",
}

# Handlers that read the real result back themselves (window state, monitor,
# foreground) before returning. Their evidence survives the generic probe.
_NATIVE_EVIDENCE = frozenset({"window_native", "browser_window"})


def _matches(state: dict, expected: tuple[str, object] | None) -> bool:
    if expected is None:
        return False
    field, value = expected
    if field == "playback":
        return state["playback"] == value
    if field == "process_running":
        return state["process_running"] == value
    if value is True:
        # "Open X" means the owner can SEE it: a window that is only minimized
        # (e.g. hidden again by a post-action rule) is not an open app.
        return any(w.get("state") != "minimized" for w in state["windows"] or [])
    return bool(state["windows"]) and all(w["state"] == value for w in state["windows"])


_DISPLAY_NAMES = {"spotify": "Spotify", "youtube": "YouTube", "netflix": "Netflix"}


def _display(app: str) -> str:
    app = str(app or "").strip()
    return _DISPLAY_NAMES.get(app.lower(), app[:1].upper() + app[1:] if app else "The app")


def status_sentence(state: dict) -> str:
    """What the app is doing now, in words, from the fresh observation only."""
    app = _display(state.get("app", ""))
    running, windows = state.get("process_running"), state.get("windows")
    if running is None:
        return f"I couldn't check {app} right now."
    if not running:
        return f"I don't see {app} running."  # a title may match no process name
    parts = [f"{app} is running"]
    if windows:
        shown = "minimized" if all(w.get("state") == "minimized" for w in windows) else "on screen"
        parts.append(f"its window is {shown}")
    elif windows is not None:
        parts.append("with no window")
    playback = state.get("playback")
    if playback and playback != "unknown":
        parts.append(f"and it is {playback}")
    return ", ".join(parts) + "."


def outcome_sentence(
    tool: str, params: dict, state: dict, expected: tuple[str, object] | None, verified: bool
) -> str | None:
    """One plain sentence: what was asked, and whether it really happened.

    Built only from the fresh observation, never from the command's receipt, so
    Era can say "done" only when she saw it and says exactly what she saw when
    she did not. A status read (nothing asked of the app) gets a plain report.
    """
    app = _display(state.get("app", ""))
    if expected is None:
        return status_sentence(state)
    field, value = expected
    action = _plain_action(params) or "the command"
    running, windows = state.get("process_running"), state.get("windows")
    if field == "window" and value is True:
        if verified:
            return f"{app} is open on screen."
        if running is None or windows is None:
            return (
                f"I asked {app} to open, but I couldn't check its window, so it is not confirmed."
            )
        if running and windows:
            return f"{app} is running but its window is minimized, so it is not open on screen."
        if running:
            return f"{app} is running but no window has appeared, so it is not confirmed open."
        return f"{app} did not open: no {app} process is running."
    if field == "playback":
        seen = state.get("playback") or "unknown"
        label = PLAYLIST_LABELS.get(action)
        if label:
            if verified:
                return f"{app} is playing {label}."
            if seen == "unknown":
                return f"I asked {app} to play {label}, but I couldn't read its playback, so it is not confirmed."
            return f"I asked {app} to play {label}, but it is {seen}, so it did not start."
        if verified:
            return f"{app} is {value}."
        if seen == "unknown":
            return f"I sent {action} to {app}, but I couldn't read its playback, so it is not confirmed."
        return f"I sent {action} to {app}, but it is {seen}, so it did not take effect."
    if field == "window" and value == "minimized":
        return f"{app} is minimized." if verified else f"{app} is not minimized yet."
    if field == "process_running" and value is False:
        if verified:
            return f"{app} is closed."
        if running is None:
            return (
                f"I asked {app} to close, but I couldn't check its process, so it is not confirmed."
            )
        return f"{app} is still running, so it did not close."
    return None


async def refresh_after_command(tool: str, params: dict, raw):
    """Attach authoritative observations after the handler returns, with bounded reads."""
    from core.cancel_registry import cancelled

    if cancelled():
        return raw
    # Provider readbacks bind success to the requested media identity. A generic
    # OS probe cannot identify that track and must not replace stronger evidence.
    if (
        isinstance(raw, ToolResult)
        and raw.verifier_name in {"spotify_api", "spotify_native", "youtube_dom"}
        and ((raw.success and raw.verified) or "previous_media_observation" in raw.data)
    ):
        return raw
    action = str(params.get("action") or "").removeprefix("spotify_")
    if tool == "spotify_control" or (
        tool == "computer_settings" and str(params.get("action", "")).startswith("spotify_")
    ):
        from core import media_observation

        query = params.get("value") or params.get("query")
        if action == "play_uri" and not query:
            from core.personal_media import saved_song
            from core.spotify_api import identity

            saved = saved_song()
            try:
                uri = identity(params.get("uri") or params.get("track_id"))
            except ValueError:
                uri = None
            if uri and uri == saved.get("uri"):
                query = saved.get("value")
        if (
            action in {"search", "play_uri", "play", "resume"}
            and isinstance(query, str)
            and query.strip()
        ):
            original = ToolResult.from_any(raw)
            # Gates and authorization failures are never overridden by telemetry.
            if original.error_type in {
                "ConfirmationRequired",
                "ConfirmationFailed",
                "BlockedAction",
                "UserCancelled",
            }:
                return original
            observation = await media_observation.settle_spotify(query)
            if observation.get("matched"):
                return ToolResult(
                    success=True,
                    verified=True,
                    verification_status="VERIFIED",
                    verifier_name="spotify_native",
                    message=f"Playing '{observation['title']}' by {observation['artist']}; verified from fresh Windows media state.",
                    data={"media_observation": observation, "requested_track": query},
                )
            return ToolResult(
                success=False,
                verification_status="NOT_VERIFIED",
                error_type="PlaybackNotVerified",
                message=f"Playback of '{query}' is not yet verified. The latest Windows observation has not confirmed the requested track; no repeat play command was sent.",
                data={
                    "media_observation": observation,
                    "requested_track": query,
                    "command_result": original.message,
                },
            )
        if action in {"status", "current_track", "current_track_data", "playback_state"}:
            observation = await asyncio.to_thread(media_observation.spotify)
            return ToolResult(
                success=observation["available"],
                message=(
                    f"Spotify {observation['playback']}: '{observation['title']}' by {observation['artist']}."
                    if observation["available"]
                    else "Fresh Spotify telemetry is unavailable; previous track status is not current evidence."
                ),
                data={"media_observation": observation},
            )
    target = command_target(tool, params)
    if not target:
        return raw
    if target == "all":
        results = await asyncio.gather(
            *[
                refresh_after_command(
                    tool, {**params, "target": source}, ToolResult.from_any(raw).to_dict()
                )
                for source in ("spotify", "youtube", "netflix")
            ]
        )
        result = ToolResult.from_any(raw, tool_name=tool, action_name=params.get("action"))
        result.data["command_result"] = result.message
        result.data["live_app_states"] = {
            r.data["live_app_state"]["app"]: r.data["live_app_state"] for r in results
        }
        result.message = "\n".join(
            format_live_state(s) for s in result.data["live_app_states"].values()
        )
        result.verified = all(r.verified for r in results)
        result.verifier_name = "live_app_state"
        result.verification_status = (
            "VERIFIED" if result.verified else ("NOT_VERIFIED" if result.success else "FAILED")
        )
        result.verification_message = "Fresh post-command observations; each source is independent."
        if not result.success:
            result.message = result.data["command_result"] + "\n" + result.message
        return result
    result = ToolResult.from_any(raw, tool_name=tool, action_name=params.get("action"))
    # The handler already read the real result back (window_control: monitor,
    # foreground, minimized/maximized). That proof stands; the probe below only
    # adds context. A title may match no process (PF-027), so the probe alone
    # could never confirm "move ERA ONE to screen one".
    native = bool(result.success and result.verified and result.verifier_name in _NATIVE_EVIDENCE)
    native_proof = result.verification_message if native else ""
    expected = _expected(tool, params)
    # PF-027: Spotify's media session reports a new state 2-3 s late (see
    # spotify_control), so a playback check spreads its 3 reads over 3.5 s.
    # Reads only: polling longer can never repeat or change an action.
    playback_check = expected is not None and expected[0] == "playback"
    interval = 1.0 if playback_check else 0.2
    deadline = time.monotonic() + (3.5 if playback_check else 2.0)
    state = None
    for attempt in range(3):
        if cancelled():
            return raw
        try:
            state = await asyncio.wait_for(
                asyncio.to_thread(collect_live_state, target),
                timeout=max(0.01, deadline - time.monotonic()),
            )
        except Exception:
            if state is not None:
                # PF-047: a later read timed out. Keep what the earlier read SAW
                # (it did not match, or the loop would have stopped) instead of
                # replacing it with "unknown"; the failed read stays named.
                state = {**state, "errors": [*state.get("errors", []), "probe_unavailable"]}
                break
            state = {
                "app": target,
                "observed_at": time.time(),
                "source": "live_os",
                "process_running": None,
                "pids": [],
                "windows": None,
                "audio_session": "unknown",
                "playback": "unknown",
                "errors": ["probe_unavailable"],
            }
            break
        if _matches(state, expected) or expected is None or not result.success:
            break
        if attempt < 2 and time.monotonic() + interval < deadline:
            await asyncio.sleep(interval)
        else:
            break
    result.data["command_result"] = result.message
    result.data["live_app_state"] = state
    probe_verified = result.success and _matches(state, expected)
    verified = probe_verified or native
    # The verdict leads; the raw observation follows for the model. Era used to
    # speak the telemetry itself ("spotify: process running; window minimized").
    sentence = outcome_sentence(tool, params, state, expected, verified) if result.success else None
    result.message = format_live_state(state)
    if native and not probe_verified:
        # The handler's own read-back is the verdict; the probe is context only,
        # and only when it found the app (a title may match no process, PF-027).
        command = str(result.data["command_result"] or "").strip()
        if (state or {}).get("process_running"):
            result.message = f"{command} Now: {status_sentence(state)} Observed: {result.message}"
        else:
            result.message = command
    elif sentence and expected is None:
        # Nothing was asked of the app's state (window move, focus...): the tool's
        # own result stays first, the observation is context, never a verdict.
        command = str(result.data["command_result"] or "").strip()
        result.message = f"{command} Now: {sentence} Observed: {result.message}".strip()
    elif sentence:
        result.data["outcome_sentence"] = sentence
        result.message = f"{sentence} Observed: {result.message}"
    if not result.success:
        result.message = result.data["command_result"] + "\n" + result.message
    result.verified = verified
    result.verifier_name = "live_app_state"
    result.verification_status = (
        "VERIFIED" if result.verified else ("NOT_VERIFIED" if result.success else "FAILED")
    )
    result.verification_message = native_proof or (
        "Fresh post-command observations override command receipts and older context; unknown is unverified."
    )
    return result
