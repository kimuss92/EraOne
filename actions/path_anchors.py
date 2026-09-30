# actions/path_anchors.py
"""Resolve bare folder/file names against paths Era already knows.

A saved favorite like ``D:\\Ctrl Z\\Freelence\\DC\\IG\\ABC\\claude automation``
proves that ``ABC`` exists and where it lives. The whole-machine search can't
use that: it walks drives under a timeout and scores thousands of fuzzy hits,
so "open ABC" timed out while the answer sat in memory the whole time.

Known paths ("anchors") come from two places:
  * favorite_paths in long-term memory (explicit bookmarks), and
  * the file index's opened_paths table (what the user actually opened).

A name matches an anchor when it equals (compact, accent/case-insensitive) one
of the anchor's path components, or a direct child of an anchor directory.
Multi-part names ("ABC/claude automation/output juillet") match on the first
part and are joined below it, so nested items never need the full path.

Pure lookups only — nothing here opens, creates or deletes anything.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_SEP_RE = re.compile(r"[\\/]+")

# Components too generic to act as an anchor on their own ("open users").
_GENERIC = {"users", "c", "d", "e", "appdata", "roaming", "local", "program files"}


def _compact(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _favorite_paths() -> list[Path]:
    try:
        from memory.memory_manager import load_memory

        favs = load_memory().get("favorite_paths", {}) or {}
    except Exception:
        return []
    out = []
    for entry in favs.values():
        val = entry.get("value") if isinstance(entry, dict) else entry
        if isinstance(val, str) and val.strip():
            out.append(Path(val.strip()))
    return out


def _recent_opened_paths(limit: int = 200) -> list[Path]:
    try:
        from actions.file_indexer import get_file_index

        return [Path(p) for p in get_file_index().recent_opened_paths(limit)]
    except Exception:
        return []


def anchor_paths() -> list[Path]:
    """Favorites first (explicit intent), then recently opened paths."""
    seen: set[str] = set()
    out: list[Path] = []
    for p in _favorite_paths() + _recent_opened_paths():
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _split(name: str) -> list[str]:
    return [p for p in _SEP_RE.split(name.strip().strip('"')) if p]


def resolve_known(
    name: str,
    item_type: str = "both",
    anchors: list[Path] | None = None,
) -> list[Path]:
    """Return existing paths for ``name`` derived from known anchors.

    Ordered by anchor priority; deduplicated. Empty when nothing matches.
    """
    parts = _split(name)
    if not parts or Path(name).is_absolute():
        return []
    head = _compact(parts[0])
    if not head or head in _GENERIC:
        return []
    tail = parts[1:]
    if anchors is None:
        anchors = anchor_paths()

    hits: list[Path] = []
    seen: set[str] = set()

    def _add(base: Path) -> None:
        cand = base.joinpath(*tail) if tail else base
        key = str(cand).lower()
        if key in seen:
            return
        try:
            if not cand.exists():
                return
            if item_type == "folder" and not cand.is_dir():
                return
            if item_type == "file" and not cand.is_file():
                return
        except OSError:
            return
        seen.add(key)
        hits.append(cand)

    for anchor in anchors:
        # 1. Ancestor-or-self component match: D:\...\ABC\claude automation → ABC
        p = anchor
        while True:
            if p.name and _compact(p.name) == head:
                _add(p)
            if p.parent == p:
                break
            p = p.parent
        # 2. Direct child of an anchor folder: favorite "IG" + name "ABC"
        try:
            if anchor.is_dir():
                for child in anchor.iterdir():
                    if _compact(child.name) == head:
                        _add(child)
        except OSError:
            continue
    return hits


def resolve_known_one(name: str, item_type: str = "both") -> Path | None:
    """Unique anchor-derived match, or None when absent or ambiguous."""
    hits = resolve_known(name, item_type=item_type)
    return hits[0] if len(hits) == 1 else None
