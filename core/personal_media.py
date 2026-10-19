"""Resolve personal song references from saved memory before media execution."""

from __future__ import annotations

import re
import unicodedata

_MY_SONG = re.compile(r"\bmy\s+(?:(?:favou?rite|saved)\s+)?(?:song|track)\b", re.I)


def favorite_song_key(query: str) -> str | None:
    """Explicit own-song references must not retrieve a child's preference."""
    if re.search(r"\b(?:my\s+)?son(?:'s)?\s+(?:favou?rite\s+)?(?:song|track)\b", query or "", re.I):
        return None if _MY_SONG.search(query or "") else "son_favorite_song"
    if re.search(r"\b(?:son|daughter|child|his|her|their)\b", query or "", re.I):
        return None
    if (
        _MY_SONG.search(query or "")
        or re.search(r"\b(?:play|queue)\s+favou?rite\s+(?:song|track)\b", query or "", re.I)
        or re.fullmatch(r"\s*favou?rite\s+(?:song|track)\s*[?!.]?\s*", query or "", re.I)
    ):
        return "favorite_song"
    return None


def search_identity(tool: str, args: dict) -> tuple[str, dict]:
    """Equivalent Spotify search spellings share one in-flight owner."""
    if (tool, args.get("action")) not in {
        ("spotify_control", "search"),
        ("computer_settings", "spotify_search"),
    }:
        return tool, args
    query = next(
        (args.get(key) for key in ("value", "query", "text", "command") if args.get(key)), ""
    )
    if not isinstance(query, str) or not query.strip():
        return tool, args
    query = " ".join(unicodedata.normalize("NFKC", query).casefold().split())
    return "spotify_control", {"action": "search", "value": query}


_PLAY_VERB = re.compile(r"\b(?:play|put\s+on|queue|start)\b", re.I)


def divert_personal_song(tool: str, args: dict, utterance: str) -> tuple[str, dict]:
    """ "Play my track" is a command, so a lookup is the wrong first move.

    Asked to play the owner's song, the model reached for retrieve_memory_context
    (or the Liked Songs shortcut) and then read the result aloud instead of
    playing anything. Redirect those detours to a plain Spotify play, which
    resolve_song() then pins to the saved track. Questions ("what is my favourite
    song") and "my liked songs" do not match and are left alone.
    """
    if not _PLAY_VERB.search(utterance or "") or favorite_song_key(utterance) != "favorite_song":
        return tool, args
    if tool == "retrieve_memory_context" or (
        tool == "spotify_control" and args.get("action") == "liked_songs"
    ):
        return "spotify_control", {"action": "play"}
    return tool, args


def resolve_song(tool: str, args: dict, utterance: str) -> tuple[str, dict]:
    actions = {
        "spotify_control": {"play", "search", "play_music", "resume"},
        "computer_settings": {"spotify_play", "spotify_search"},
    }
    if (
        args.get("action") not in actions.get(tool, set())
        or favorite_song_key(utterance) != "favorite_song"
    ):
        return tool, args
    from memory.memory_manager import load_memory

    preferences = load_memory().get("preferences", {})
    entry = preferences.get("favorite_song", {})
    value = entry.get("value") if isinstance(entry, dict) else None
    uri = entry.get("spotify_uri") if isinstance(entry, dict) else None
    if not uri:
        saved = preferences.get("favorite_song_uri", preferences.get("favorite_song_id", {}))
        uri = saved.get("value") if isinstance(saved, dict) else None
    if isinstance(uri, str):
        from core.spotify_api import identity

        try:
            uri = identity(uri)
        except ValueError:
            uri = None
        if uri and uri.startswith("spotify:track:"):
            # Explicit stored identity wins over title search; never invent an ID.
            resolved = {"action": "play_uri", "uri": uri}
            if isinstance(value, str) and value.strip():
                resolved["value"] = value.strip()
            return "spotify_control", resolved
    if not isinstance(value, str) or not value.strip():
        return tool, args
    return "spotify_control", {**args, "action": "search", "value": value.strip()}
