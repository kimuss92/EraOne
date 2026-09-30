import difflib
import re

_STOP_WORDS: frozenset = frozenset(
    {
        "play",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "for",
        "to",
        "with",
        "by",
        "is",
        "at",
        "its",
        "it's",
    }
)

_GENERIC_KEYWORDS: frozenset = frozenset(
    {
        "music",
        "songs",
        "song",
        "video",
        "videos",
        "highlights",
        "mix",
        "compilation",
        "comp",
        "tutorial",
        "tutorials",
        "news",
        "podcast",
        "podcasts",
        "relaxing",
        "funny",
        "cooking",
        "cook",
        "live",
        "stream",
        "streaming",
        "remix",
        "cover",
        "instrumental",
        "lofi",
        "chill",
        "calm",
        "ambient",
        "workout",
        "study",
        "sleep",
        "meditation",
        "rain",
        "nature",
        "asmr",
        "gaming",
        "gameplay",
        "walkthrough",
        "reaction",
        "review",
        "unboxing",
        "vlog",
        "daily",
        "fitness",
        "recipe",
        "diy",
        "howto",
        "how to",
        "karaoke",
        "lyrics",
        "ost",
        "soundtrack",
        "recitation",
        "cats",
        "animals",
        "pets",
        "scenic",
        "beach",
        "ocean",
        "waves",
        "fireplace",
        "jazz",
        "piano",
        "guitar",
        "classical",
        "orchestra",
        "symphony",
        "deep",
        "house",
        "edm",
        "electronic",
        "dance",
        "trance",
        "techno",
        "ibiza",
        "summer",
        "spring",
        "winter",
        "best",
        "top",
        "greatest",
        "latest",
        "new",
        "popular",
        "trending",
        "viral",
        "hit",
        "hits",
        "2024",
        "2025",
        "2026",
        "2027",
        "2028",
        "2029",
        "2030",
    }
)


def normalize_title(text: str) -> str:
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def token_overlap_score(query: str, title: str) -> float:
    q_tokens = set(normalize_title(query).split())
    t_tokens = set(normalize_title(title).split())
    if not q_tokens or not t_tokens:
        return 0.0
    intersection = q_tokens & t_tokens
    union = q_tokens | t_tokens
    return len(intersection) / len(union)


def is_youtube_watch_url(url: str) -> bool:
    if not url:
        return False
    return bool(re.search(r"youtube\.com/watch\?v=", url))


def is_youtube_rejected_url(url: str) -> bool:
    if not url:
        return True
    u = url.lower()
    if "/shorts/" in u:
        return True
    if "/playlist?" in u or "/playlists/" in u:
        return True
    if "/channel/" in u:
        return True
    if "/c/" in u or "/user/" in u:
        return True
    return False


def _is_likely_proper_noun(query: str) -> bool:
    """True if query is a proper-noun-like short phrase (e.g. 'Top Boy').

    Checks for title-case pattern in the original query (not lowered).
    """
    if not query or not query.strip():
        return False
    q = query.strip()
    words = q.split()
    if len(words) > 4:
        return False
    titlecased = sum(1 for w in words if w and w[0].isupper())
    return titlecased >= 1 and titlecased >= len(words) * 0.5


def is_generic_media_query(query: str, platform: str = "youtube") -> bool:
    if not query or not query.strip():
        return True
    q = query.strip()
    stripped = q.lower()
    if '"' in stripped or "\u201c" in stripped or "\u201d" in stripped:
        return False
    # Proper-noun heuristic: short title-cased phrases are NOT generic
    if _is_likely_proper_noun(q):
        return False
    words = stripped.split()
    if not words:
        return True
    if words[0] == "play":
        words = words[1:]
    if not words:
        return True
    if len(words) <= 2:
        if any(w in _GENERIC_KEYWORDS for w in words):
            return True
    generic_count = sum(1 for w in words if w in _GENERIC_KEYWORDS)
    if generic_count >= len(words) * 0.4:
        return True
    return False


_OFFICIAL_KEYWORDS = frozenset(
    {
        "official music video",
        "official video",
        "official audio",
        "music video",
        "official lyric video",
        "official lyrics",
    }
)


def _fuzzy_score(query: str, title: str) -> float:
    """SequenceMatcher fuzzy ratio between normalized query and title.

    Only used as fallback when token overlap is poor (≤4 tokens,
    low overlap). Capped at 0.85 so it never overrides exact/substring matches.
    """
    q = normalize_title(query)
    t = normalize_title(title)
    if not q or not t:
        return 0.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    return min(ratio, 0.85)


def _official_boost(title: str) -> float:
    tl = title.lower()
    for kw in _OFFICIAL_KEYWORDS:
        if kw in tl:
            return 0.10
    return 0.0


def _artist_song_boost(query: str, title: str) -> float:
    """Boost when query matches the song portion of an 'Artist — Song' format title."""
    if " — " not in title:
        return 0.0
    q = normalize_title(query)
    if not q:
        return 0.0
    parts = title.split(" — ")
    if len(parts) < 2:
        return 0.0
    right = normalize_title(parts[1])
    if q in right or right in q:
        return 0.10
    return 0.0


def score_media_candidate(
    query: str,
    candidate_title: str,
    candidate_url: str | None = None,
    platform: str | None = None,
) -> float:
    if not query or not candidate_title:
        return 0.0
    q_norm = normalize_title(query)
    t_norm = normalize_title(candidate_title)
    if q_norm == t_norm:
        return 1.0

    # Compute boosts before substring checks so official/artist-song boosts apply
    official = _official_boost(candidate_title)
    artist_song = _artist_song_boost(query, candidate_title)
    boost = official + artist_song

    if q_norm in t_norm:
        return min(0.9 + boost, 1.0)
    if t_norm in q_norm:
        return min(0.85 + boost, 1.0)
    score = token_overlap_score(q_norm, t_norm)
    # Fuzzy fallback for short / low-overlap queries
    q_tokens = q_norm.split()
    if len(q_tokens) <= 4 and score < 0.5:
        fuzzy = _fuzzy_score(q_norm, t_norm)
        score = max(score, fuzzy)
    if score >= 0.5:
        return min(0.65 + (score - 0.5) * 0.4 + boost, 1.0)
    capped = score * 1.3 + boost
    return min(capped, 0.50)


_FEAT_PATTERN = re.compile(
    r"\(feat[.\s].*?\)|\(ft[.\s].*?\)|\(official.*?\)|\(lyrics?\)|\(audio\)|\(video\)|\(music\s*video\)"
    r"|\(hd\)|\(4k\)|\(1080p\)|\(60fps\)|\(prod\..*?\)",
    re.IGNORECASE,
)


def _normalize_base_title(title: str) -> str:
    """Remove parentheticals, feat. references, and separators to find base title."""
    if not title:
        return ""
    t = title
    # Remove parenthetical content with feat./official/lyrics etc.
    t = _FEAT_PATTERN.sub("", t)
    # Remove // separators (keep the left side, usually the main title)
    t = t.split("//")[0]
    # Remove | separators (keep the left side)
    t = t.split("|")[0]
    # Strip leading/trailing whitespace and dashes
    t = t.strip(" -–—|/")
    # Normalize whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t.lower()


def _cluster_candidates_by_base_title(
    scored: list[tuple[dict, float, str, str]],
) -> dict[str, list[tuple[dict, float, str, str]]]:
    """Group scored candidates by their normalized base title."""
    clusters: dict[str, list] = {}
    for item in scored:
        candidate, score, title, url = item
        base = _normalize_base_title(title)
        if not base:
            base = normalize_title(title)
        clusters.setdefault(base, []).append(item)
    return clusters


def select_best_media_candidate(
    query: str,
    candidates: list[dict],
    platform: str | None = None,
) -> tuple[dict | None, float, str]:
    if not candidates:
        return (None, 0.0, "no_candidates")
    scored: list[tuple[dict, float, str, str]] = []
    for c in candidates:
        title = c.get("title", "") or c.get("name", "") or ""
        url = c.get("url", "") or c.get("href", "") or ""
        s = score_media_candidate(query, title, url, platform)
        scored.append((c, s, title, url))
    scored.sort(key=lambda x: -x[1])
    best_candidate, best_score, best_title, best_url = scored[0]
    if best_score >= 0.70:
        if len(scored) >= 2:
            second_score = scored[1][1]
            if best_score - second_score < 0.15:
                # ── Issue 5: YouTube tie-break for short specific queries ──
                if platform == "youtube" and is_youtube_watch_url(best_url):
                    query_tokens = {
                        w.lower() for w in query.split() if w.lower() not in _STOP_WORDS
                    }
                    if 1 <= len(query_tokens) <= 4:
                        best_title_tokens = set(normalize_title(best_title).split())
                        if best_title_tokens and all(t in best_title_tokens for t in query_tokens):
                            return (best_candidate, best_score, "selected")
                # ── Part E: YouTube fuzzy cluster autoplay ──
                # If top candidates share the same normalized base title cluster,
                # they are variants of the same song — auto-select the top one.
                clusters = _cluster_candidates_by_base_title(scored)
                top_base = _normalize_base_title(best_title)
                if top_base and top_base in clusters:
                    # Count how many of the top N are in this cluster
                    top_n = min(3, len(scored))
                    cluster_top_n = sum(
                        1 for i in range(top_n) if _normalize_base_title(scored[i][2]) == top_base
                    )
                    if cluster_top_n >= 2 and best_score >= 0.70:
                        return (best_candidate, best_score, "selected")
                return (None, best_score, "ambiguous")
        return (best_candidate, best_score, "selected")
    if best_score >= 0.40:
        return (None, best_score, "low_confidence")
    return (None, best_score, "low_confidence")


def clean_youtube_watch_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    vid_match = re.search(r"(?:v=|/v/|youtu\.be/)([A-Za-z0-9_-]{11})", u)
    if not vid_match:
        return ""
    video_id = vid_match.group(1)
    if "/shorts/" in u:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}"


_WORD_ORDINALS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
}


def parse_ordinal_media_command(text: str) -> dict | None:
    if not text or not text.strip():
        return None
    t = text.strip().lower()
    t = re.sub(r"^(?:play|open|select)\s+", "", t)
    t = t.strip()
    ordinal = None
    m = re.match(r"the\s+(first|second|third|fourth|fifth)\s+(?:video|one|item|result)", t)
    if m:
        ordinal = _WORD_ORDINALS.get(m.group(1))
    if not ordinal:
        m = re.match(r"(first|second|third|fourth|fifth)\s+(?:video|one|item|result)", t)
        if m:
            ordinal = _WORD_ORDINALS.get(m.group(1))
    if not ordinal:
        m = re.match(r"the\s+(first|second|third|fourth|fifth)\s+one", t)
        if m:
            ordinal = _WORD_ORDINALS.get(m.group(1))
    if not ordinal:
        m = re.match(r"(first|second|third|fourth|fifth)", t)
        if m:
            ordinal = _WORD_ORDINALS.get(m.group(1))
    if not ordinal:
        m = re.match(r"(?:video|result|number|item)\s+(?:number\s+)?(\d+)", t)
        if m:
            ordinal = int(m.group(1))
    if not ordinal:
        m = re.match(r"(\d+)\s*(?:st|nd|rd|th)\s*(?:video|one|item|result)", t)
        if m:
            ordinal = int(m.group(1))
    if not ordinal or ordinal < 1 or ordinal > 5:
        return None
    return {"ordinal": ordinal, "action": "play", "platform_hint": "youtube"}


def filter_youtube_candidates(candidates: list[dict]) -> list[dict]:
    result: list[dict] = []
    for c in candidates:
        url = c.get("url", "") or c.get("href", "") or ""
        if c.get("is_ad", False):
            continue
        kind = c.get("kind", "") or ""
        if kind == "channel":
            continue
        if not url:
            continue
        if is_youtube_rejected_url(url):
            continue
        if not is_youtube_watch_url(url):
            continue
        result.append(c)
    return result
