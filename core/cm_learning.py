r"""
cm_learning.py — the community-management lane's memory of your feedback.

WHY THIS EXISTS
  Era already had a feedback memory (memory/feedback_memory.py), but it learns
  about DESKTOP CONTROL — "you opened Netflix when you shouldn't have". The CM
  lane had nothing. Every correction you gave a brand run ("change reel 1's
  texture", "the hook is too commercial") was spent on that one run and then
  lost, so the next week's plan repeated the mistake and you re-explained it.

  This module makes the CM lane accumulate. Every correction and every approval
  is recorded against the BRAND, distilled into short standing lessons, and
  rendered back into the next brief. Corrections teach it what to stop doing;
  approvals teach it what to keep doing.

WHAT A "LESSON" IS
  A short, brand-scoped, human-readable rule derived from something you actually
  said, with a category, a hit count and a timestamp. It is NOT a model weight
  and NOT a summary of the run — it is your own words, kept verbatim, so you can
  read the store and see exactly what Era thinks you want. Anything Era cannot
  attribute to something you said does not become a lesson.

WHY VERBATIM, AND WHY BRAND-SCOPED
  Paraphrasing feedback is how a preference quietly mutates into something you
  never asked for, so the user's own sentence is what gets stored and replayed.
  Scoping by brand is the same discipline as the Brand Lock: a rule you set for
  Ceram Quartz's captions must never silently start steering Delta Cuisine.

RELATIONSHIP TO THE VAULT
  The vault (core/cm_vault.py) holds strategy you WRITE deliberately. This holds
  preferences Era INFERRED from what you said in passing. Both feed the brief;
  only the vault is authoritative when they disagree, because you wrote it on
  purpose. Lessons that prove durable are worth promoting into a vault note by
  hand — `python -m core.cm_learning promote <brand>` prints them ready to paste.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core.logger import log_event, setup_logger

learning_logger = setup_logger("era_one.cm_learning")

_MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"
_EVENTS_PATH = _MEMORY_DIR / "cm_feedback.jsonl"
_LESSONS_PATH = _MEMORY_DIR / "cm_lessons.json"

# A brief can carry only so much standing instruction before the gates start
# getting ignored. Keep the strongest signal, drop the tail.
_MAX_LESSONS_PER_BRAND = 24
_MAX_RENDERED = 14

# Categories exist so repeated feedback about the same THING reinforces one
# lesson instead of piling up near-duplicates. Ordered: the first match wins,
# so the more specific patterns must come first.
_CATEGORIES: tuple[tuple[str, str], ...] = (
    (
        "material",
        r"\b(texture|mat[ée]riau|material|swatch|quartz|c[ée]ramique|ceramic|bois|wood|laqu|lacquer|laminat|marbre|marble|finition|finish)\b",
    ),
    ("wardrobe", r"\b(dressing|wardrobe|placard|module|penderie|accessoir|accessor)\b"),
    (
        "kitchen",
        r"\b(cuisine|kitchen|[îi]lot|island|plan de travail|countertop|cr[ée]dence|backsplash|meuble|cabinet|plinthe|plinth)\b",
    ),
    (
        "caption",
        r"\b(caption|l[ée]gende|description|texte|copy|hashtag|emoji|footer|arabe|arabic|fran[çc]ais|french|ton|tone|wording)\b",
    ),
    (
        "hook",
        r"\b(hook|accroche|opening|first (?:line|second)|titre|headline|cta|call to action)\b",
    ),
    (
        "visual",
        r"\b(image|visuel|visual|render|rendu|photo|lumi[èe]re|light|couleur|colou?r|composition|cadrage|framing|angle|plan)\b",
    ),
    (
        "video",
        r"\b(reel|r[ée]el|video|vid[ée]o|clip|montage|transition|musique|music|voix|voice|rythme|pacing|dur[ée]e|length)\b",
    ),
    ("format", r"\b(carousel|carrousel|slide|story|post|format|ratio|portrait|vertical)\b"),
    (
        "schedule",
        r"\b(horaire|schedule|timing|jour|day|heure|hour|semaine|week|fr[ée]quence|frequen|cadence|trop souvent|too often)\b",
    ),
    ("offer", r"\b(offre|offer|promo|remise|discount|prix|price|tarif|dt\b|campagne|campaign)\b"),
)

# Phrases that mean "this was wrong" vs "this was right". Feedback in French,
# English and Tunisian-flavoured French all land here, because that is how the
# orders actually arrive.
_NEGATIVE = re.compile(
    r"\b(change|changer|chang[ée]|corrige|correct|fix|redo|refais|refaire|"
    r"rewrite|r[ée][ée]cris|replace|remplace|don'?t|do not|ne pas|jamais|never|"
    r"stop|arr[êe]te|wrong|faux|mauvais|bad|pas bon|trop|too |instead|plut[ôo]t|"
    r"avoid|[ée]vite|no more|plus de)\b",
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r"\b(approve|approuv|validate|valide|perfect|parfait|exactly|exactement|"
    r"good|bon|bien|nice|love|j'aime|keep|garde|continue|comme [çc]a|like this)\b",
    re.IGNORECASE,
)


@dataclass
class Lesson:
    """One standing preference for one brand, in the user's own words."""

    brand: str
    category: str
    polarity: str  # "avoid" | "prefer"
    text: str  # the user's sentence, verbatim
    hits: int = 1
    first_seen: str = ""
    last_seen: str = ""
    task_types: list[str] = field(default_factory=list)

    def key(self) -> str:
        return f"{self.category}|{self.polarity}|{_normalize(self.text)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tokens(text: str) -> set[str]:
    """Content words of a sentence, with the item number stripped.

    "change reel 1's texture" and "change reel 3's texture" are the SAME lesson
    about material choice — the item number is which asset you were looking at,
    not what you were teaching. Dropping it is what lets repeated feedback
    reinforce instead of accumulating as separate near-duplicates.
    """
    text = re.sub(r"\b(reel|carousel|carrousel|slide|post)\s*#?\d+\b", "", text, flags=re.I)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return {w for w in text.split() if len(w) > 3}


def _normalize(text: str) -> str:
    """Collapse a sentence to a stable comparison key."""
    return " ".join(sorted(_tokens(text)))[:220]


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Jaccard overlap of content words — near-duplicate feedback merges.

    Exact-match merging almost never fires in practice (you phrase the same
    correction differently each time), which meant the hit count stayed at 1 and
    a preference you had stated four times still read as a one-off suggestion.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def carries_judgement(text: str) -> bool:
    """True when this order actually states a preference, rather than just asking
    for work.

    The distinction the lane gate could not make. `classify()` falls back to
    polarity "avoid" when NEITHER pattern matches, which makes "no signal at all"
    indistinguishable from "a negative signal" — so it can never be used to decide
    whether something is worth recording. This asks the narrower question and
    answers it honestly: did a correction or approval marker actually fire?

    That is what lets a preference buried in a routine order be captured. "Prepare
    this week's plan" teaches nothing and is skipped; "prepare this week's plan,
    and stop using the clinical white kitchens" is a rule the next run needs, and
    under the old lane-only gate it was thrown away because the lane said
    `weekly`.
    """
    body = (text or "").strip()
    if len(body) < 8:
        return False
    return bool(_NEGATIVE.search(body) or _POSITIVE.search(body))


def classify(text: str) -> tuple[str, str]:
    """(category, polarity) for a piece of feedback."""
    lowered = text.lower()
    category = "general"
    for name, pattern in _CATEGORIES:
        if re.search(pattern, lowered, re.IGNORECASE):
            category = name
            break

    neg, pos = bool(_NEGATIVE.search(text)), bool(_POSITIVE.search(text))
    # A mixed order ("approve reels 2 and 3, change reel 1") is teaching a
    # correction — the correction is the part carrying new information.
    polarity = "avoid" if neg else ("prefer" if pos else "avoid")
    return category, polarity


def _load_lessons() -> dict[str, list[dict]]:
    if not _LESSONS_PATH.exists():
        return {}
    try:
        data = json.loads(_LESSONS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        # A corrupt store must never block a run. Losing lessons degrades
        # quality; raising here would stop the brand job entirely.
        log_event(learning_logger, "cm.learning.load_failed", "WARNING", error=str(exc))
        return {}


def _save_lessons(data: dict[str, list[dict]]) -> None:
    try:
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LESSONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_LESSONS_PATH)
    except OSError as exc:
        log_event(learning_logger, "cm.learning.save_failed", "WARNING", error=str(exc))


def record_feedback(
    brand_key: str, order: str, task_type: str = "revise", outcome: str | None = None
) -> Lesson | None:
    """Turn one CM order into a lesson for this brand. Returns it, or None.

    Called from the dispatch path for the lanes that carry judgement — a
    `revise` order IS a correction, and an `approve` order IS a confirmation.
    Planning and publishing orders carry no preference signal and are skipped,
    otherwise the store fills with "plan the week" and drowns the real lessons.
    """
    order = (order or "").strip()
    if not brand_key or len(order) < 8:
        return None

    category, polarity = classify(order)
    lesson = Lesson(
        brand=brand_key,
        category=category,
        polarity=polarity,
        text=order[:400],
        first_seen=_now(),
        last_seen=_now(),
        task_types=[task_type],
    )

    try:
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        with _EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": _now(),
                        "brand": brand_key,
                        "task_type": task_type,
                        "order": order[:1000],
                        "category": category,
                        "polarity": polarity,
                        "outcome": outcome,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError as exc:
        log_event(learning_logger, "cm.learning.event_write_failed", "WARNING", error=str(exc))

    store = _load_lessons()
    lessons = store.setdefault(brand_key, [])
    for existing in lessons:
        same_topic = existing.get("category") == category and existing.get("polarity") == polarity
        if existing.get("key") == lesson.key() or (
            same_topic and _similar(existing.get("text", ""), lesson.text)
        ):
            existing["hits"] = int(existing.get("hits", 1)) + 1
            existing["last_seen"] = lesson.last_seen
            # Keep the newest phrasing: it reflects how you say it now, and the
            # hit count already records that you have said it before.
            existing["text"] = lesson.text
            existing["key"] = lesson.key()
            if task_type not in existing.get("task_types", []):
                existing.setdefault("task_types", []).append(task_type)
            break
    else:
        entry = asdict(lesson)
        entry["key"] = lesson.key()
        lessons.append(entry)

    # Prune by strength: repeated feedback outranks a one-off, recency breaks ties.
    lessons.sort(key=lambda e: (int(e.get("hits", 1)), e.get("last_seen", "")), reverse=True)
    store[brand_key] = lessons[:_MAX_LESSONS_PER_BRAND]
    _save_lessons(store)

    log_event(
        learning_logger,
        "cm.learning.recorded",
        "INFO",
        brand=brand_key,
        task_type=task_type,
        category=category,
        polarity=polarity,
        total_lessons=len(store[brand_key]),
    )
    return lesson


def lessons_for(brand_key: str) -> list[dict]:
    return _load_lessons().get(brand_key, [])


def render_lessons(brand_key: str) -> str:
    """The learned-preferences block for the brief. '' when nothing is known."""
    lessons = lessons_for(brand_key)
    if not lessons:
        return ""

    avoid = [entry for entry in lessons if entry.get("polarity") == "avoid"][:_MAX_RENDERED]
    prefer = [entry for entry in lessons if entry.get("polarity") == "prefer"][:_MAX_RENDERED]
    if not avoid and not prefer:
        return ""

    def fmt(entry: dict) -> str:
        hits = int(entry.get("hits", 1))
        repeat = f"  [said {hits}x — treat as a firm rule]" if hits > 1 else ""
        return (
            f'  • ({entry.get("category", "general")}) "{entry.get("text", "").strip()}"{repeat}\n'
        )

    block = (
        "=== WHAT I HAVE ALREADY TOLD YOU (learned from my past corrections) ===\n"
        "These are MY OWN WORDS from previous runs on this brand, recorded when I "
        "corrected or approved something. They are standing preferences, not "
        "history: apply them to THIS run before I have to repeat myself. If I said "
        "something more than once it is a firm rule, not a nudge.\n"
        "Where one of these conflicts with the brand charter or the vault notes, "
        "the charter and the vault win — say so in one line rather than guessing.\n\n"
    )
    if avoid:
        block += "CORRECTIONS I HAVE MADE — do not repeat these mistakes:\n"
        block += "".join(fmt(e) for e in avoid) + "\n"
    if prefer:
        block += "THINGS I APPROVED — keep doing these:\n"
        block += "".join(fmt(e) for e in prefer) + "\n"
    return block


def promote_text(brand_key: str) -> str:
    """Lessons formatted as Markdown, ready to paste into a vault note by hand."""
    lessons = lessons_for(brand_key)
    if not lessons:
        return f"No lessons recorded yet for {brand_key}."
    lines = [f"## Learned preferences — {brand_key}", ""]
    for category, _ in _CATEGORIES + (("general", ""),):
        group = [e for e in lessons if e.get("category") == category]
        if not group:
            continue
        lines.append(f"### {category}")
        for entry in group:
            mark = "AVOID" if entry.get("polarity") == "avoid" else "PREFER"
            hits = int(entry.get("hits", 1))
            suffix = f" _(said {hits}x)_" if hits > 1 else ""
            lines.append(f"- **{mark}** — {entry.get('text', '').strip()}{suffix}")
        lines.append("")
    return "\n".join(lines)


def stats() -> dict:
    store = _load_lessons()
    return {
        "lessons_path": str(_LESSONS_PATH),
        "events_path": str(_EVENTS_PATH),
        "brands": {k: len(v) for k, v in store.items()},
        "total": sum(len(v) for v in store.values()),
    }


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] == "promote" and len(argv) > 1:
        print(promote_text(argv[1]))
    elif argv and argv[0] == "show" and len(argv) > 1:
        print(render_lessons(argv[1]) or f"No lessons for {argv[1]}.")
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
