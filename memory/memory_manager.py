import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from threading import RLock

from core.utils import get_memory_dir
from memory.memory_audit import _summarize, audit_event
from memory.safe_store import safe_write_json, validate_json_file
from memory.transient_filter import is_transient_memory_key


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
# Resolved through core.utils so ERA_MEMORY_DIR redirects this store too. The
# local get_base_dir() above stays for the frozen-executable case and for any
# caller still reading BASE_DIR. No import cycle: core.utils pulls in nothing
# from memory.*.
MEMORY_PATH = get_memory_dir() / "long_term.json"
# RLock (not Lock): update_memory() holds this for its whole read-modify-write
# cycle and calls load_memory()/save_memory() internally, which also acquire
# it — a plain Lock would self-deadlock on that nested acquisition.
_lock = RLock()
MAX_VALUE_LENGTH = 380
# NOTE: this bounds on-disk storage only. What actually gets injected into
# the LLM's context each turn is separately capped by
# format_memory_for_prompt_ranked() (top_k=15, 2000-char result cap), so this
# ceiling does not need to be small — it was previously 2200, which is why
# permanent facts (the user's name, a relationship, a "never mention my son"
# instruction) kept getting silently evicted by _trim_to_limit within days.
MEMORY_MAX_CHARS = 60000

# Categories that hold durable, hand-told facts (identity, relationships,
# standing behavioral rules, dev-reported fixes, and the user's own feedback) —
# never auto-trimmed for size. user_feedback is protected because the user
# explicitly wants their corrections prioritised and persisted; it is also
# pinned into the prompt by format_memory_for_prompt_ranked. Only ephemeral
# categories (notes, projects, preferences, wishes, favorite_paths) are eligible
# for eviction; the protected categories are only touched as a last resort if
# trimming everything else still isn't enough (should not happen at this budget).
_PROTECTED_CATEGORIES = frozenset(
    {
        "identity",
        "relationships",
        "operational_instruction",
        "developer_feedback",
        "user_feedback",
    }
)

VALID_CATEGORIES = {
    "identity",
    "preferences",
    "projects",
    "relationships",
    "wishes",
    "notes",
    "operational_instruction",
    "developer_feedback",
    "favorite_paths",
    "user_feedback",
}

_WILDCARD_KEYWORDS = {"*", "all", "**all**", "everything"}  # noqa: S105

# Module-level dedup toggle (replaces function-attribute pattern)
_DEDUP_ENABLED = True

_KNOWN_CATEGORIES_ORDER = [
    "identity",
    "preferences",
    "projects",
    "relationships",
    "wishes",
    "notes",
    "operational_instruction",
    "favorite_paths",
    "user_feedback",
    "developer_feedback",
]
_KNOWN_CATEGORIES_SET = frozenset(_KNOWN_CATEGORIES_ORDER)


def _parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return value > 0
    return default


def _parse_int(value, default, min_val=None, max_val=None):
    if isinstance(value, (int, float)):
        result = int(value)
    elif isinstance(value, str):
        try:
            result = int(value)
        except (ValueError, TypeError):
            return default
    else:
        return default
    if min_val is not None:
        result = max(result, min_val)
    if max_val is not None:
        result = min(result, max_val)
    return result


def _empty_memory() -> dict:
    return {
        "identity": {},
        "preferences": {},
        "projects": {},
        "relationships": {},
        "wishes": {},
        "notes": {},
        "operational_instruction": {},
        "favorite_paths": {},
        "developer_feedback": {},
        "user_feedback": {},
    }


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] Load error: {e}")
            # Before giving up entirely, try self-healing from the most
            # recent backup (safe_write_json keeps the last few versions of
            # every write). Silently returning empty memory here previously
            # meant a single corrupted write — e.g. a OneDrive sync hiccup
            # on this file — looked identical to "the user has no memory,"
            # even though a good copy usually still exists a moment away.
            try:
                result = validate_json_file(MEMORY_PATH)
                if result["status"] == "CORRUPTED_RECOVERED":
                    print(f"[Memory] Recovered from backup: {result['message']}")
                    data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        base = _empty_memory()
                        for key in base:
                            if key not in data:
                                data[key] = {}
                        return data
            except Exception as recovery_error:
                print(f"[Memory] Backup recovery also failed: {recovery_error}")
            return _empty_memory()


def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory

    # No manual backup here: save_memory() (our only caller) hands the
    # trimmed result straight to safe_write_json(), which already backs up
    # whatever is currently on disk — pre-trim state — before overwriting it,
    # and prunes old versions automatically. A second, separately-named
    # backup here used to accumulate forever with no retention limit (it was
    # the main contributor to memory/backups/ growing unbounded).

    entries = _all_entries(memory)
    # Evict from ephemeral categories first (oldest-updated first). Durable
    # categories (_PROTECTED_CATEGORIES) are only considered afterward, so a
    # pile of stale notes/projects/preferences never costs the user their
    # name, a relationship, or a standing behavioral rule.
    ephemeral = [e for e in entries if e[0] not in _PROTECTED_CATEGORIES]
    protected = [e for e in entries if e[0] in _PROTECTED_CATEGORIES]
    ephemeral.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    protected.sort(key=lambda t: t[2].get("updated", "0000-00-00"))

    for cat, key, entry in ephemeral + protected:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        old_summary = _summarize(entry)
        del memory[cat][key]
        reason = (
            "over_memory_limit"
            if cat not in _PROTECTED_CATEGORIES
            else "over_memory_limit_protected_fallback"
        )
        print(f"[Memory] Trimmed {cat}/{key}")
        audit_event(
            operation="trim",
            category=cat,
            key=key,
            old_value_summary=old_summary,
            new_value_summary="",
            source_tool="memory_manager",
            reason=reason,
            allowed=True,
        )
    return memory


def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    with _lock:
        safe_write_json(MEMORY_PATH, memory)
    # Audit the overall save (per-category summary)
    for cat, items in memory.items():
        if isinstance(items, dict):
            for key, entry in items.items():
                audit_event(
                    operation="save",
                    category=cat,
                    key=key,
                    old_value_summary="",
                    new_value_summary=_summarize(entry),
                    source_tool="memory_manager",
                    reason="save_memory",
                    allowed=True,
                )


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    # Hold the lock across the whole read-modify-write cycle, not just the
    # individual load_memory()/save_memory() calls inside it. Two callers on
    # different threads (e.g. two tool calls updating memory close together)
    # could otherwise both load the same pre-update snapshot, and whichever
    # saves second would silently clobber the first one's write. _lock is an
    # RLock so the nested load_memory()/save_memory() calls below don't
    # self-deadlock.
    with _lock:
        return _update_memory_locked(memory_update)


def _update_memory_locked(memory_update: dict) -> dict:
    memory = load_memory()

    # Per-KEY triage, not per-batch. A transient key or a dedup hit removes
    # that one key from the pending update and nothing else; both used to
    # `return memory` on the first match, which silently discarded every
    # remaining fact in the same call. With single-key callers that was
    # invisible, but it made any multi-fact write lossy in a way no caller
    # could detect.
    pending: dict = memory_update
    dedup_hit = False
    if _DEDUP_ENABLED:
        pending = {}
        for category, cat_items in memory_update.items():
            if not isinstance(cat_items, dict):
                pending[category] = cat_items
                continue
            keep: dict = {}
            for key, value_item in cat_items.items():
                if not isinstance(value_item, dict) or "value" not in value_item:
                    keep[key] = value_item
                    continue
                # Block transient keys from long-term memory
                if is_transient_memory_key(category, key):
                    audit_event(
                        operation="block",
                        category=category,
                        key=key,
                        new_value_summary=_summarize(value_item),
                        source_tool="memory_manager",
                        reason="transient_key",
                        allowed=False,
                    )
                    print(f"[Memory] Blocked transient key: {category}/{key}")
                    continue
                new_val = _truncate_value(str(value_item["value"]))
                dup = find_duplicate_memory(category, key, new_val, memory)
                if dup is not None:
                    old_summary = _summarize(dup)
                    _maybe_update_duplicate_entry(dup)
                    audit_event(
                        operation="dedup",
                        category=category,
                        key=key,
                        old_value_summary=old_summary,
                        new_value_summary=new_val,
                        source_tool="memory_manager",
                        reason="near_duplicate_replacement",
                        allowed=True,
                    )
                    dedup_hit = True
                    continue
                keep[key] = value_item
            if keep:
                pending[category] = keep

    # Snapshot the pre-update values so the audit records what was actually
    # replaced. Reading them after _recursive_update (as this did) reported
    # the NEW value as the old one, making the trail useless for exactly the
    # question it exists to answer: what did this write overwrite?
    old_summaries: dict[tuple[str, str], str] = {}
    for category, cat_items in pending.items():
        if not isinstance(cat_items, dict):
            continue
        # A corrupted store can leave a category holding something other than a
        # dict; the audit snapshot must not be the thing that raises over it.
        existing_cat = memory.get(category)
        if not isinstance(existing_cat, dict):
            continue
        for key in cat_items:
            old_entry = existing_cat.get(key)
            old_summaries[(category, key)] = _summarize(old_entry) if old_entry else ""

    changed = _recursive_update(memory, pending) if pending else False
    if changed:
        for category, cat_items in pending.items():
            if not isinstance(cat_items, dict):
                continue
            for key, value_item in cat_items.items():
                if not isinstance(value_item, dict) or "value" not in value_item:
                    continue
                audit_event(
                    operation="save",
                    category=category,
                    key=key,
                    old_value_summary=old_summaries.get((category, key), ""),
                    new_value_summary=_truncate_value(str(value_item["value"])),
                    source_tool="memory_manager",
                    reason="update_memory",
                    allowed=True,
                )
    # One save covers both the new values and any access_count/recency bumps
    # the dedup pass made on entries already in `memory`.
    if changed or dedup_hit:
        save_memory(memory)
    if changed:
        print(f"[Memory] Saved: {list(pending.keys())}")
    return memory


def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    lines = []

    identity = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("")
        lines.append("Preferences:")
        for key, entry in list(prefs.items())[:15]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    projects = memory.get("projects", {})
    if projects:
        lines.append("")
        lines.append("Active Projects / Goals:")
        for key, entry in list(projects.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    rels = memory.get("relationships", {})
    if rels:
        lines.append("")
        lines.append("People in their life:")
        for key, entry in list(rels.items())[:10]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    wishes = memory.get("wishes", {})
    if wishes:
        lines.append("")
        lines.append("Wishes / Plans / Wants:")
        for key, entry in list(wishes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    notes = memory.get("notes", {})
    if notes:
        lines.append("")
        lines.append("Other notes:")
        for key, entry in list(notes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key}: {val}")

    ops = memory.get("operational_instruction", {})
    if ops:
        lines.append("")
        lines.append("Strict Operational Instructions:")
        for key, entry in list(ops.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    dev = memory.get("developer_feedback", {})
    if dev:
        lines.append("")
        lines.append("Developer Patches & Bug Fix Feedback:")
        for key, entry in list(dev.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    favs = memory.get("favorite_paths", {})
    if favs:
        lines.append("")
        lines.append("Favorite Paths / Bookmarks:")
        for key, entry in list(favs.items())[:10]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    result = header + "\n".join(lines)
    if len(result) > 2000:
        result = result[:1997] + "…"

    return result + "\n"


def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {
        "identity",
        "preferences",
        "projects",
        "relationships",
        "wishes",
        "notes",
        "operational_instruction",
        "developer_feedback",
        "favorite_paths",
        "user_feedback",
    }
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def _category_display_order(memory_cats: set[str]) -> list[str]:
    known = [c for c in _KNOWN_CATEGORIES_ORDER if c in memory_cats]
    unknown = sorted(c for c in memory_cats if c not in _KNOWN_CATEGORIES_SET)
    return known + unknown


def list_memory_keys(
    category: str | None = None,
    full: bool = False,
    limit: int = 50,
    offset: int = 0,
    show_counts: bool = True,
) -> str:
    memory = load_memory()

    full_bool = _parse_bool(full, False)
    limit_int = _parse_int(limit, 50, min_val=1, max_val=200)
    offset_int = _parse_int(offset, 0, min_val=0)
    show_counts_bool = _parse_bool(show_counts, True)
    effective_limit = 200 if full_bool else limit_int

    if category is not None:
        raw = category.strip() if isinstance(category, str) else str(category).strip()
        if not raw:
            return "Error: category must be non-empty"
        if raw not in VALID_CATEGORIES and raw not in memory:
            return f"Invalid category: {raw}"
        items = memory.get(raw, {})
        if not items or not isinstance(items, dict):
            return "(empty)"
        keys = sorted(items.keys())
        total = len(keys)
        page = keys[offset_int : offset_int + effective_limit]

        lines: list[str] = []
        if show_counts_bool:
            lines.append(f"Total keys in {raw}: {total}  Showing {len(page)}")
            lines.append("")
        lines.append(f"{raw}:")
        for key in page:
            lines.append(f"  - {key}")

        if offset_int + effective_limit < total:
            lines.append("")
            lines.append("---")
            lines.append(
                f"Showing {len(page)} of {total} keys in {raw}. "
                f"Ask for offset {offset_int + effective_limit} to continue."
            )
        return "\n".join(lines)

    # No category — list all top-level dict categories in memory
    all_cat_keys: dict[str, list[str]] = {}
    total_keys = 0
    for cat in _category_display_order(set(memory.keys())):
        items = memory.get(cat, {})
        if isinstance(items, dict) and items:
            cat_keys = sorted(items.keys())
            all_cat_keys[cat] = cat_keys
            total_keys += len(cat_keys)

    if total_keys == 0:
        return "(empty)"

    # Flatten for deterministic pagination
    flat: list[tuple[str, str]] = []
    for cat, keys in all_cat_keys.items():
        for key in keys:
            flat.append((cat, key))

    page_flat = flat[offset_int : offset_int + effective_limit]

    # Re-group by category for display
    page_grouped: dict[str, list[str]] = {}
    for cat, key in page_flat:
        page_grouped.setdefault(cat, []).append(key)

    lines = []
    if show_counts_bool:
        lines.append(f"Total keys: {total_keys}  Showing {len(page_flat)}")
        lines.append("")

    for cat in _category_display_order(set(page_grouped.keys())):
        lines.append(f"{cat}:")
        for key in sorted(page_grouped[cat]):
            lines.append(f"  - {key}")

    if offset_int + effective_limit < total_keys:
        lines.append("")
        lines.append("---")
        next_offset = offset_int + effective_limit
        lines.append(
            f"Showing {len(page_flat)} of {total_keys} keys. "
            f"Ask for offset {next_offset} to continue."
        )

    return "\n".join(lines)


def forget(key: str, category: str = "notes") -> str:
    if not category or not isinstance(category, str) or not category.strip():
        return "Error: invalid category"
    cat = category.strip()

    memory = load_memory()

    if cat not in VALID_CATEGORIES:
        if cat not in memory or not isinstance(memory.get(cat), dict):
            return "Error: invalid category"

    if not key or not isinstance(key, str) or not key.strip():
        return "Error: key must be non-empty"
    if key.strip().lower() in _WILDCARD_KEYWORDS:
        return "Error: wildcard deletion is not allowed"
    key = key.strip()

    cat_dict = memory.get(cat, {})
    if not isinstance(cat_dict, dict):
        return f"Not found: {cat}/{key}"
    if key in cat_dict:
        old_summary = _summarize(cat_dict[key])
        # No manual backup here: save_memory() below hands off to
        # safe_write_json(), which already backs up the pre-delete on-disk
        # state and prunes old versions — see _trim_to_limit for the same
        # reasoning. A separate unpruned backup here just duplicated it.
        del cat_dict[key]
        memory[cat] = cat_dict
        save_memory(memory)
        audit_event(
            operation="delete",
            category=cat,
            key=key,
            old_value_summary=old_summary,
            new_value_summary="",
            source_tool="memory_manager",
            reason="explicit_forget",
            allowed=True,
        )
        return f"Forgotten: {cat}/{key}"
    return f"Not found: {cat}/{key}"


forget_memory = forget

# ---------------------------------------------------------------------------
# Step 13A — Deduplication helpers
# ---------------------------------------------------------------------------


def is_near_duplicate(value_a: str, value_b: str, threshold: float = 0.88) -> bool:
    if not value_a or not value_b:
        return False
    a = value_a.lower().strip()
    b = value_b.lower().strip()
    if a == b:
        return True
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return False
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    jaccard = len(intersection) / len(union)
    if jaccard >= threshold:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= threshold


def _same_value(a: str, b: str) -> bool:
    """Equality up to case and whitespace — nothing fuzzier."""
    return " ".join(str(a).lower().split()) == " ".join(str(b).lower().split())


def find_duplicate_memory(category: str, key: str, value: str, memory: dict) -> dict | None:
    cat = memory.get(category)
    if not isinstance(cat, dict):
        return None
    existing = cat.get(key)
    if isinstance(existing, dict) and "value" in existing:
        # SAME KEY is an assignment to a named slot, not an accidental
        # duplicate. Only a verbatim restatement is deduped (it just bumps
        # recency/access); anything else is the user CORRECTING the fact and
        # must overwrite.
        #
        # This branch used to dedup at 0.88 similarity, which silently threw
        # away every small correction — "Tuesday"->"Thursday", "50 percent"->
        # "70 percent", "Sara"->"Sarah", "October 3"->"October 8" all score
        # above that threshold. The stale value survived and kept being
        # injected into the prompt, so Era acknowledged the correction and
        # went on remembering the wrong thing. That was the single largest
        # source of "she forgot what I told her".
        #
        # Returning None (rather than falling through) is deliberate: the
        # caller named THIS slot, so a similar value parked under some other
        # key must not block the write either.
        return existing if _same_value(str(existing["value"]), value) else None
    for other_key, entry in cat.items():
        if other_key == key:
            continue
        if isinstance(entry, dict) and "value" in entry:
            if is_near_duplicate(str(entry["value"]), value, threshold=0.95):
                return entry
    return None


def _maybe_update_duplicate_entry(existing: dict) -> bool:
    existing["updated"] = datetime.now().strftime("%Y-%m-%d")
    count = existing.get("access_count", 0)
    if isinstance(count, (int, float)):
        existing["access_count"] = int(count) + 1
    else:
        existing["access_count"] = 1
    return True


def set_dedup_enabled(enabled: bool) -> None:
    global _DEDUP_ENABLED
    _DEDUP_ENABLED = enabled


# ---------------------------------------------------------------------------
# Step 13A — Ranked retrieval
# ---------------------------------------------------------------------------


def _parse_timestamp_for_recency(entry: dict) -> float:
    raw = entry.get("updated") or entry.get("updated_at")
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        try:
            dt = datetime.fromtimestamp(raw)
            return max(0.0, 1.0 - (datetime.now() - dt).days / 365.0)
        except (OSError, ValueError, OverflowError):
            return 0.0
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw, fmt)
                return max(0.0, 1.0 - (datetime.now() - dt).days / 365.0)
            except ValueError:
                continue
        return 0.0
    return 0.0


# Words that carry no retrieval signal. The model asks "what is my sister's
# name?", not "sister" — leaving these in inflated the denominator of every
# overlap ratio and let junk entries win on an incidental "my"/"is" match.
_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "know",
        "me",
        "much",
        "my",
        "of",
        "on",
        "or",
        "our",
        "remember",
        "she",
        "so",
        "tell",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whose",
        "why",
        "with",
        "you",
        "your",
    }
)


def _content_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens with stopwords removed.

    Falls back to the raw tokens when a query is nothing but stopwords, so a
    degenerate query still ranks by recency instead of scoring every entry 0.
    """
    raw = {t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t}
    content = raw - _QUERY_STOPWORDS
    return content or raw


def score_memory_entry(
    query: str, entry: dict, *, now: float | None = None, key: str = ""
) -> float:
    if not query:
        return 0.0
    value = str(entry.get("value") or "").lower()
    query_tokens = _content_tokens(query)
    value_tokens = _content_tokens(value)
    if not query_tokens:
        return 0.0
    token_overlap = len(query_tokens & value_tokens) / len(query_tokens)
    token_score = token_overlap * 0.50
    substr_bonus = 0.15 if any(qt in value for qt in query_tokens) else 0.0

    # ── Key matching ──
    # The KEY was never scored, so a query naming the slot outright
    # ("sister", "what is my name") ranked unrelated notes above the entry
    # that literally answers it — retrieve_memory_context returned noise for
    # the most natural way to ask. Coverage (how much of the key the query
    # names) is weighted above precision (how much of the query the key
    # explains), because a short key fully named in the query is the
    # strongest signal available.
    key_score = 0.0
    if key:
        key_tokens = _content_tokens(key)
        if key_tokens:
            shared = len(query_tokens & key_tokens)
            if shared:
                key_cov = shared / len(key_tokens)
                key_prec = shared / len(query_tokens)
                key_score = key_cov * 0.45 + key_prec * 0.15

    # Recency and popularity are TIEBREAKERS between entries that already
    # match, never relevance on their own. Awarding them unconditionally gave
    # every recent entry a free ~0.25, which is how a query with no answer in
    # memory ("showroom") still came back with three confident-looking but
    # entirely unrelated notes — and why any relevance floor was unenforceable.
    matched = token_score > 0.0 or substr_bonus > 0.0 or key_score > 0.0
    if matched:
        recency = _parse_timestamp_for_recency(entry) * 0.20
        access_count = entry.get("access_count", 0)
        access_bonus = min(int(access_count) if access_count else 0, 10) / 10 * 0.05
    else:
        recency = 0.0
        access_bonus = 0.0
    value_len = len(value)
    length_penalty = (
        max(0.0, (value_len - MAX_VALUE_LENGTH) / MAX_VALUE_LENGTH) * 0.10
        if value_len > MAX_VALUE_LENGTH
        else 0.0
    )
    score = token_score + substr_bonus + key_score + recency + access_bonus - length_penalty
    return max(0.0, min(1.0, score))


def _apply_semantic_scores(entries: list[dict], query: str) -> None:
    """Lift each entry's score to its semantic score when meaning beats words.

    `max`, never a sum: a lexically-matched entry can therefore never be pushed
    DOWN or dropped by adding semantic recall, so the worst case of this whole
    feature is the behaviour that existed before it. Semantic scores only ever
    rescue an entry the words missed — "what do I do for a living" -> `job`,
    which shares no token with the query at all.

    Silent by design when the local embedding model is unavailable: see
    memory/semantic_recall.py. Memory must not become the thing that breaks when
    a local service is down.
    """
    if not entries:
        return
    try:
        from memory import semantic_recall

        scores = semantic_recall.semantic_scores(
            query,
            [
                (e["category"], e["key"], semantic_recall.entry_text(e["key"], e["value"]))
                for e in entries
            ],
        )
    except Exception as e:  # never let recall fail because of the optional half
        print(f"[Memory] [SEMANTIC] unavailable, ranking lexically only: {e}")
        return
    for entry in entries:
        semantic = scores.get((entry["category"], entry["key"]), 0.0)
        if semantic > entry["score"]:
            entry["score"] = semantic
            entry["matched_by"] = "meaning"


def rank_memory_entries(
    memory: dict,
    query: str,
    top_k: int = 5,
    category: str | None = None,
    min_score: float = 0.0,
) -> list[dict]:
    if not isinstance(memory, dict):
        return []
    entries = []
    cats = [category] if category else list(memory.keys())
    for cat in cats:
        items = memory.get(cat)
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append(
                    {
                        "category": cat,
                        "key": key,
                        "value": entry.get("value", ""),
                        "updated": entry.get("updated"),
                        "updated_at": entry.get("updated_at"),
                        "access_count": entry.get("access_count", 0),
                        "score": score_memory_entry(query, entry, key=key),
                    }
                )
    if query:
        _apply_semantic_scores(entries, query)
        entries.sort(key=lambda e: e["score"], reverse=True)
        if min_score > 0.0:
            entries = [e for e in entries if e["score"] >= min_score]
        return entries[:top_k]
    entries.sort(key=lambda e: e.get("updated") or e.get("updated_at") or "", reverse=True)
    return entries[:top_k]


def retrieve_ranked_memory(query: str, top_k: int = 5, category: str | None = None) -> list[dict]:
    return rank_memory_entries(load_memory(), query, top_k, category)


# Relevance floor for the retrieve_memory_context tool. Without one, an
# unanswerable query ("showroom", when nothing in memory mentions a showroom)
# still returned the five least-irrelevant entries, scored on recency alone —
# so the model was handed confident-looking noise instead of "I don't have
# that". Prompt injection of the memory block is deliberately NOT floored:
# there the job is a recency digest, not an answer.
_RETRIEVAL_MIN_SCORE = 0.15


def retrieve_context_reinforced(query: str, top_k: int = 5) -> str:
    """Ranked retrieval that also reinforces what it returns.

    Each returned entry gets its access_count bumped (persisted atomically via
    safe_write_json), so memories the user actually asks about accumulate an
    access_bonus in score_memory_entry and rank higher in future retrievals
    and prompt injection. Retrieval is user-triggered and infrequent, so the
    extra write is cheap; it deliberately bypasses save_memory() to avoid
    emitting a full-file audit sweep for a read-side counter bump.
    """
    if not query:
        return ""
    with _lock:
        memory = load_memory()
        ranked = rank_memory_entries(memory, query, top_k, min_score=_RETRIEVAL_MIN_SCORE)
        if ranked:
            bumped = False
            for e in ranked:
                entry = memory.get(e["category"], {}).get(e["key"])
                if isinstance(entry, dict):
                    count = entry.get("access_count", 0)
                    entry["access_count"] = int(count) + 1 if isinstance(count, (int, float)) else 1
                    bumped = True
            if bumped:
                safe_write_json(MEMORY_PATH, memory)
    if not ranked:
        return ""
    lines: list[str] = []
    for entry in ranked:
        val = str(entry.get("value") or "")
        val = val[:120] + "..." if len(val) > 120 else val
        lines.append(f"{entry['category']}/{entry['key']}: {val}")
    result = "\n".join(lines)
    if len(result) > 2000:
        result = result[:1997] + "..."
    return result


def compact_memory_preview(memory: dict, query: str, top_k: int = 5) -> str:
    ranked = rank_memory_entries(memory, query, top_k)
    if not ranked:
        return ""
    lines: list[str] = []
    for entry in ranked:
        val = str(entry.get("value") or "")
        val = val[:120] + "..." if len(val) > 120 else val
        lines.append(f"{entry['category']}/{entry['key']}: {val}")
    result = "\n".join(lines)
    if len(result) > 2000:
        result = result[:1997] + "..."
    return result


# Durable categories: rules, patches, and user feedback that must ALWAYS reach
# the prompt in full. They are "pinned" — recency chatter (new notes/preferences)
# can never evict them, which is what prevents memory accumulation from silently
# dropping standing instructions. Ephemeral categories share the recency budget.
_PINNED_PROMPT_CATEGORIES = (
    "identity",
    "operational_instruction",
    "developer_feedback",
    "user_feedback",
)
# Hard cap kept generous so pinned rules always survive; ephemeral lines are the
# only thing trimmed when the budget is exceeded.
_PROMPT_MEMORY_CHAR_CAP = 4000


def _sorted_category_entries(memory: dict, cat: str) -> list[dict]:
    """All entries of one category, most-recent first, flattened for rendering."""
    items = memory.get(cat)
    if not isinstance(items, dict):
        return []
    out = []
    for key, entry in items.items():
        if isinstance(entry, dict) and "value" in entry:
            out.append(
                {
                    "category": cat,
                    "key": key,
                    "value": entry.get("value", ""),
                    "updated": entry.get("updated"),
                    "updated_at": entry.get("updated_at"),
                }
            )
    out.sort(key=lambda e: e.get("updated") or e.get("updated_at") or "", reverse=True)
    return out


def format_memory_for_prompt_ranked(memory: dict, query: str = "", top_k: int = 15) -> str:
    """Memory prompt for session-start injection with feedback prioritised.

    Durable rule/feedback categories (identity, operational_instruction,
    developer_feedback, user_feedback) are PINNED — always rendered in full and
    never dropped, so standing corrections keep applying no matter how much newer
    chatter accumulates. The remaining ephemeral categories (preferences, notes,
    projects, …) share a recency-ranked top_k budget. Under the char cap only
    ephemeral lines are trimmed; pinned rules are protected. Empty query = pure
    recency ordering; uses the passed memory object, never reloads disk.
    """
    if not memory:
        return ""

    # ── Pinned durable entries (always kept, grouped by category, recency order)
    pinned: dict[str, list[dict]] = {
        cat: _sorted_category_entries(memory, cat) for cat in _PINNED_PROMPT_CATEGORIES
    }

    # ── Ephemeral entries share the recency/top_k budget ──
    ephemeral_cats = [c for c in memory.keys() if c not in _PINNED_PROMPT_CATEGORIES]
    grouped: dict[str, list[dict]] = {}
    for cat in ephemeral_cats:
        ranked = rank_memory_entries(memory, query, top_k, category=cat)
        if ranked:
            grouped[cat] = ranked

    pinned_lines: list[str] = []
    ephemeral_lines: list[str] = []

    # ── Identity first (special field ordering) ──
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    id_entries = pinned.get("identity") or []
    if id_entries:
        known = {e["key"]: e for e in id_entries if e["key"] in id_fields}
        other = [e for e in id_entries if e["key"] not in id_fields]
        for field in id_fields:
            e = known.get(field)
            if e and str(e.get("value") or ""):
                pinned_lines.append(f"{field.replace('_', ' ').title()}: {e['value']}")
        for e in other:
            if str(e.get("value") or ""):
                pinned_lines.append(f"{e['key'].replace('_', ' ').title()}: {e['value']}")

    def _emit(entries: list[dict], header: str, into: list[str]) -> None:
        entries = [e for e in entries if str(e.get("value") or "")]
        if not entries:
            return
        if into:
            into.append("")
        into.append(header)
        for e in entries:
            into.append(f"  - {e['key'].replace('_', ' ').title()}: {e['value']}")

    # ── Pinned rule/feedback block — logical order, feedback prioritised ──
    _emit(
        pinned.get("operational_instruction") or [],
        "Strict Operational Instructions (ALWAYS FOLLOW):",
        pinned_lines,
    )
    _emit(pinned.get("developer_feedback") or [], "Developer Patches & Bug Fixes:", pinned_lines)
    _emit(
        pinned.get("user_feedback") or [],
        "User Feedback — Corrections & Preferences (PRIORITY — always honour):",
        pinned_lines,
    )

    # ── Ephemeral / contextual block (recency-ranked, trimmable) ──
    ephemeral_configs = [
        ("preferences", "Preferences:"),
        ("relationships", "People in their life:"),
        ("projects", "Active Projects / Goals:"),
        ("favorite_paths", "Favorite Paths / Bookmarks:"),
        ("notes", "Other notes:"),
        ("wishes", "Wishes / Plans / Wants:"),
    ]
    for cat, header in ephemeral_configs:
        _emit(grouped.get(cat) or [], header, ephemeral_lines)

    if not pinned_lines and not ephemeral_lines:
        return ""

    prefix = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    pinned_block = "\n".join(pinned_lines)

    # Fit ephemeral lines into the remaining budget; pinned rules are never cut.
    budget = _PROMPT_MEMORY_CHAR_CAP - len(prefix) - len(pinned_block)
    kept: list[str] = []
    used = 0
    for line in ephemeral_lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost

    body = pinned_block
    if kept:
        body += ("\n" if body else "") + "\n".join(kept)
    return prefix + body + "\n"
