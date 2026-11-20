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
import unicodedata
from urllib.parse import urlsplit

import requests

from core.cancel_registry import raise_if_cancelled
from core.tool_result import ToolResult

_lock = threading.Lock()
_cached = None
_cached_at = 0.0
_cached_token = ""
_cooldown_until = 0.0
_URI = re.compile(r"spotify:(track|album|artist|playlist):([A-Za-z0-9]{22})$")
_ACTIONS = {
    "search",
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


def _title_key(value: str) -> str:
    return " ".join(
        re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", value).casefold()).split()
    )


def _request(token: str, method: str, path: str, **kwargs):
    global _cooldown_until
    raise_if_cancelled()
    response = requests.request(
        method,
        "https://api.spotify.com/v1" + path,
        headers={"Authorization": "Bearer " + token},
        timeout=(0.5, 1.5),
        allow_redirects=False,
        **kwargs,
    )
    if response.status_code == 429:
        try:
            delay = min(3600, max(1, int(response.headers.get("Retry-After", "5"))))
        except (TypeError, ValueError):
            delay = 5
        _cooldown_until = time.monotonic() + delay
        raise ValueError(f"Spotify rate limit; wait {delay} seconds")
    if response.status_code in {401, 403}:
        raise ValueError("Spotify OAuth authorization failed; reconnect with playback scopes")
    if not 200 <= response.status_code < 300:
        raise ValueError(f"Spotify API HTTP {response.status_code}; no command replayed")
    return response


def _verified_playback(token: str, uri: str | None) -> dict:
    """Read-only bounded polling after exactly one mutation, never a replay."""
    for attempt in range(4):
        response = _request(token, "GET", "/me/player")
        state = response.json() if response.status_code != 204 else {}
        item = state.get("item") or {}
        observed = item.get("uri")
        linked = (item.get("linked_from") or {}).get("uri")
        matches = not uri or (
            uri in {observed, linked}
            if uri.startswith("spotify:track:")
            else (state.get("context") or {}).get("uri") == uri
        )
        if state.get("is_playing") is True and matches:
            return ToolResult(
                success=True,
                message=f"Playing {item.get('name') or uri or 'Spotify'}; verified by Spotify API.",
                verified=True,
                verification_status="VERIFIED",
                verifier_name="spotify_api",
                data={"requested_uri": uri, "observed_uri": observed, "is_playing": True},
            )
        if attempt < 3:
            time.sleep(0.25)
    return {
        "success": False,
        "message": "Spotify playback could not be verified for the requested track; the command was not replayed.",
        "error_type": "PlaybackNotVerified",
        "data": {"requested_uri": uri, "observed_uri": observed},
    }


def _search_and_play(token: str, params: dict) -> dict:
    query = str(params.get("value") or params.get("query") or "").strip()
    if not query:
        raise ValueError("A track title is required")
    title, _, artist = query.partition(" by ")
    response = _request(token, "GET", "/search", params={"q": query, "type": "track", "limit": 10})
    tracks = (response.json().get("tracks") or {}).get("items") or []
    matches = [
        t
        for t in tracks
        if _title_key(t.get("name", "")) == _title_key(title)
        and (
            not artist
            or any(
                _title_key(a.get("name", "")) == _title_key(artist) for a in t.get("artists", [])
            )
        )
        and t.get("is_playable") is not False
    ]
    # Duplicate album releases of the same recording are equivalent; distinct
    # recordings/artists require an explicit choice rather than the first hit.
    recordings = {(t.get("external_ids", {}).get("isrc") or t.get("uri")) for t in matches}
    if len(recordings) != 1 or not matches:
        return {
            "success": False,
            "error_type": "TrackSelectionRequired",
            "message": f"Spotify did not find one unambiguous exact title for '{query}'. Specify the artist or an exact track URI.",
            "data": {
                "candidates": [
                    {
                        "title": t.get("name"),
                        "uri": t.get("uri"),
                        "artists": [a.get("name") for a in t.get("artists", [])],
                    }
                    for t in tracks[:5]
                ]
            },
        }
    uri = identity(matches[0]["uri"])
    _request(token, "PUT", "/me/player/play", json={"uris": [uri]})
    return _verified_playback(token, uri)


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
        return "Spotify command failed: queue is busy. Please retry after the current command."
    try:
        raise_if_cancelled()
        now = time.monotonic()
        if now < _cooldown_until:
            return "Spotify command failed: rate limit is active; please wait before sending another command."
        if action == "search":
            _cached, _cached_at = None, 0.0
            return _search_and_play(token, params)
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
            return f"Spotify command failed: rate limit: retry after {delay} seconds."
        if response.status_code in {401, 403}:
            return "Spotify API authorization failed. Reconnect OAuth with playback scopes and a Premium account."
        if not 200 <= response.status_code < 300:
            return f"Spotify command failed: API rejected the command (HTTP {response.status_code}); it was not replayed."
        if method == "GET":
            state = response.json() if response.status_code != 204 else {}
            _cached, _cached_at, _cached_token = copy.deepcopy(state), time.monotonic(), token
            return json.dumps({"cached": False, "state": state})
        if action in {"play", "resume", "play_uri"}:
            return _verified_playback(token, uri)
        return f"Spotify accepted {action.replace('_', ' ')}."
    except (ValueError, TypeError) as exc:
        return "Spotify command failed: " + str(exc)
    except requests.RequestException:
        return "Spotify API connection failed; command outcome is unknown. It was not replayed."
    finally:
        _lock.release()
