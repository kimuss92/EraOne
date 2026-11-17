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


_MOOD_QUERY = re.compile(
    r"^(?:some|any|something|anything|a\s+(?:bit|little)\s+of)\b"
    r"|\b(?:music|songs|tracks|playlist|mix|vibes|beats)$",
    re.I,
)


def is_mood_request(query: str) -> bool:
    """A mood or genre search ("chill music", "night music", "chill rap mix").

    Same vocabulary as explicit_track's PF-021 test. A query naming an artist
    ("X by Y") is a track request and never a mood.
    """
    q = re.sub(r"[.!?,\s]+$", "", (query or "").strip())
    if not q or re.search(r"\sby\s", q, re.I):
        return False
    return bool(_MOOD_QUERY.search(q))


def explicit_track(utterance: str) -> str | None:
    """Preserve an explicitly spoken title, including a correction, before memory.

    Conservative whole requests only: questions, negations, generic playback and
    multi-action sentences remain the model's responsibility.
    """
    text = (utterance or "").strip().replace("’", "'")
    correction = re.search(r"\bI (?:ask(?:ed)? for|meant|said)\s+(.+?)[.!?]*$", text, re.I)
    if correction:
        title = correction[1]
    else:
        text = re.sub(
            r"^(?:(?:hey )?era(?: one)?[, ]+|please\s+|(?:can|could|would) you\s+)+",
            "",
            text,
            flags=re.I,
        )
        match = re.fullmatch(r"(?:play|put on)\s+(.+?)[.!?]*", text, re.I)
        if not match:
            return None
        title = match[1]
    title = re.sub(r"\s+(?:on|using)\s+(?:youtube|spotify)\s*$", "", title, flags=re.I)
    title = re.sub(
        r"^(?:(?:the|my)\s+)?(?:song|track)\s+(?:called\s+|named\s+)?", "", title, flags=re.I
    )
    # "play ME X" / "play FOR me X": the indirect object is not part of the title.
    title = re.sub(r"^(?:for\s+)?(?:me|us)\s+", "", title, flags=re.I)
    title = title.strip(" \"'.,!?")
    if not title or re.search(r"\b(?:and then|then|instead of|don't|do not)\b", title, re.I):
        return None
    # A MOOD or GENRE request is not a title ("some chill music", "chill rap mix").
    # Grounding it replaced the model's own, better query with the raw words (live,
    # 2026-11-14: "Play me some chill music. chill rap music" searched for the
    # literal "me some chill music. chill rap music" and was never verified).
    # Likewise a restatement across sentences is a correction in progress, not a title.
    if re.search(r"[.!?]\s+\S", title) or re.search(
        r"^(?:some|any|something|anything|a\s+(?:bit|little)\s+of)\b"
        r"|\b(?:music|songs|tracks|playlist|mix|vibes|beats)$",
        title,
        re.I,
    ):
        return None
    if favorite_song_key(title) or re.fullmatch(
        r"(?:my )?(?:favou?rite|saved|liked|some|any)?\s*(?:song|track|songs|tracks|music|playlist|it|that|this|something|spotify|youtube)",
        title,
        re.I,
    ):
        return None
    return title


def ground_explicit_track(tool: str, args: dict, utterance: str) -> tuple[str, dict]:
    playable = {
        "spotify_control": {"search", "play", "resume", "play_music", "play_uri"},
        "computer_settings": {"spotify_search", "spotify_play"},
        "youtube_video": {"play", "youtube_play"},
    }
    if args.get("action") not in playable.get(tool, set()):
        return tool, args
    title = explicit_track(utterance)
    if not title:
        return tool, args
    if tool == "youtube_video":
        return tool, {
            "action": "play",
            "query": title,
            **({"browser": args["browser"]} if args.get("browser") else {}),
        }
    return "spotify_control", {"action": "search", "value": title}


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


def saved_song() -> dict:
    """Read the owner's saved title and canonical identity together."""
    from memory.memory_manager import load_memory

    preferences = load_memory().get("preferences", {})
    entry = preferences.get("favorite_song", {})
    value = entry.get("value") if isinstance(entry, dict) else None
    uri = entry.get("spotify_uri") if isinstance(entry, dict) else None
    if not uri:
        saved = preferences.get("favorite_song_uri", preferences.get("favorite_song_id", {}))
        uri = saved.get("value") if isinstance(saved, dict) else None
    resolved = {}
    if isinstance(value, str) and value.strip():
        resolved["value"] = value.strip()
    if isinstance(uri, str):
        from core.spotify_api import identity

        try:
            canonical = identity(uri)
        except ValueError:
            pass
        else:
            if canonical.startswith("spotify:track:"):
                resolved["uri"] = canonical
    return resolved


def resolve_song(tool: str, args: dict, utterance: str) -> tuple[str, dict]:
    grounded = ground_explicit_track(tool, args, utterance)
    if grounded != (tool, args):
        return grounded
    actions = {
        "spotify_control": {"play", "search", "play_music", "resume", "play_uri"},
        "computer_settings": {"spotify_play", "spotify_search"},
    }
    if (
        args.get("action") not in actions.get(tool, set())
        or favorite_song_key(utterance) != "favorite_song"
    ):
        return tool, args
    saved = saved_song()
    value = saved.get("value")
    if saved.get("uri"):
        # Explicit stored identity wins over model-supplied IDs and title search.
        return "spotify_control", {"action": "play_uri", **saved}
    if not isinstance(value, str) or not value.strip():
        return tool, args
    return "spotify_control", {**args, "action": "search", "value": value.strip()}
