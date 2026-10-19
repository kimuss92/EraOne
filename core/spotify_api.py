"""Bounded exact-ID Spotify Web API control, opt-in through an OAuth access token.

No browser estimation, token discovery, mutation retries or desktop replay after
an ambiguous network outcome. Callers must obtain tokens through Spotify OAuth.
"""

import copy
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

import requests

from core.cancel_registry import raise_if_cancelled

_lock = threading.Lock()
_cached = None
_cached_at = 0.0
_cached_token = ""
_cooldown_until = 0.0
_URI = re.compile(r"spotify:(track|album|artist|playlist):([A-Za-z0-9]{22})$")
_ACTIONS = {
    "play",
    "resume",
    "pause",
    "next",
    "previous",
    "queue",
    "play_uri",
    "status",
    "playback_state",
}


def identity(value: str, kind: str = "track") -> str:
    if not isinstance(value, str):
        raise ValueError("An exact Spotify URI or ID is required")
    if value.startswith("https://"):
        url = urlsplit(value)
        match = re.fullmatch(
            r"/(?:intl-[a-zA-Z]{2}/)?(track|album|artist|playlist)/([A-Za-z0-9]{22})/?",
            url.path,
        )
        if url.netloc.lower() != "open.spotify.com" or match is None:
            raise ValueError("An exact Spotify URI or ID is required")
        value = f"spotify:{match[1]}:{match[2]}"
    if re.fullmatch(r"[A-Za-z0-9]{22}", value):
        value = f"spotify:{kind}:{value}"
    if not _URI.fullmatch(value):
        raise ValueError("An exact Spotify URI or ID is required")
    return value


def execute(action: str, params: dict) -> str | None:
    """None means not configured/applicable, so native controls may handle it."""
    global _cached, _cached_at, _cached_token, _cooldown_until
    token = os.environ.get("ERA_SPOTIFY_ACCESS_TOKEN", "").strip()
    if not token or action not in _ACTIONS:
        return None
    if not _lock.acquire(timeout=0.5):
        return "Spotify command queue is busy. Please retry after the current command."
    try:
        raise_if_cancelled()
        now = time.monotonic()
        if now < _cooldown_until:
            return "Spotify rate limit is active; please wait before sending another command."
        if action in {"status", "playback_state"}:
            if _cached_token == token and _cached is not None and now - _cached_at < 0.4:
                return json.dumps({"cached": True, "state": copy.deepcopy(_cached)})
            method, endpoint, body, query = "GET", "", None, {}
        else:
            uri = params.get("uri") or params.get("track_id") or params.get("context_uri")
            body, query = None, {}
            if action == "queue":
                uri = identity(uri)
                if not uri.startswith("spotify:track:"):
                    raise ValueError("Queue requires an exact track URI")
                method, endpoint, query = "POST", "/queue", {"uri": uri}
            elif action in {"play", "resume", "play_uri"}:
                method, endpoint = "PUT", "/play"
                if uri:
                    uri = identity(uri)
                    body = (
                        {"uris": [uri]}
                        if uri.startswith("spotify:track:")
                        else {"context_uri": uri}
                    )
                elif action == "play_uri":
                    raise ValueError("play_uri requires an exact Spotify URI")
            else:
                method, endpoint = (
                    ("PUT", "/pause") if action == "pause" else ("POST", "/" + action)
                )
            # A subsequent read must reflect Spotify, not our predicted state.
            _cached, _cached_at = None, 0.0
        response = requests.request(
            method,
            "https://api.spotify.com/v1/me/player" + endpoint,
            headers={"Authorization": "Bearer " + token},
            params=query,
            json=body,
            timeout=(0.5, 1.5),
            allow_redirects=False,
        )
        if response.status_code == 429:
            try:
                delay = min(3600, max(1, int(response.headers.get("Retry-After", "5"))))
            except ValueError:
                delay = 5
            _cooldown_until = time.monotonic() + delay
            return f"Spotify rate limit: retry after {delay} seconds."
        if response.status_code in {401, 403}:
            return "Spotify API authorization failed. Reconnect OAuth with playback scopes and a Premium account."
        if not 200 <= response.status_code < 300:
            return f"Spotify API rejected the command (HTTP {response.status_code}); it was not replayed."
        if method == "GET":
            state = response.json() if response.status_code != 204 else {}
            _cached, _cached_at, _cached_token = copy.deepcopy(state), time.monotonic(), token
            return json.dumps({"cached": False, "state": state})
        return f"Spotify accepted {action.replace('_', ' ')}."
    except (ValueError, TypeError) as exc:
        return "Spotify command failed: " + str(exc)
    except requests.RequestException:
        return "Spotify API connection failed; command outcome is unknown. It was not replayed."
    finally:
        _lock.release()
