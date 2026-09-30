# actions/file_controller.py
import difflib
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.utils import (
    format_size,
    get_desktop,
    get_documents,
    get_downloads,
    get_music,
    get_pictures,
    get_videos,
    is_safe_path,
)

if TYPE_CHECKING:
    pass

try:
    import send2trash

    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop": get_desktop(),
        "downloads": get_downloads(),
        "documents": get_documents(),
        "pictures": get_pictures(),
        "music": get_music(),
        "videos": get_videos(),
        "home": Path.home(),
    }
    lower = raw.strip().lower()
    if lower in shortcuts:
        return shortcuts[lower]
    candidate = Path(raw).expanduser()
    # A bare/relative name ("ABC", "ABC/claude automation") that isn't here is
    # almost never meant relative to Era's cwd — resolve it against folders Era
    # already knows (favorites + recently opened) when that yields ONE match.
    if raw.strip() and not candidate.is_absolute() and not candidate.exists():
        try:
            from actions.path_anchors import resolve_known_one

            known = resolve_known_one(raw)
            if known is not None:
                return known
        except Exception:
            pass
    return candidate


def _cloud_present(target: Path, retries: int = 4, delay: float = 0.15) -> bool:
    """Existence check that tolerates OneDrive / cloud-sync latency.

    On OneDrive Known-Folder-Move paths (Desktop, Documents, ...) and other
    cloud-backed drives, ``Path.exists()`` can transiently return False for a
    file that is physically present — the reparse-point placeholder is briefly
    unstattable while the sync engine hydrates or refreshes it. A single
    ``exists()`` gate therefore spuriously reports "not found" and callers must
    not treat one False as authoritative.

    This checks both the followed path (``exists``) and the link itself
    (``lexists``, which sees a dehydrated placeholder that ``exists`` misses),
    retrying briefly to ride out the hydration window.
    """
    s = str(target)
    for attempt in range(retries):
        try:
            if os.path.exists(s) or os.path.lexists(s):
                return True
        except OSError:
            # Transient cloud I/O error — treat as "unknown", keep polling.
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    return False


def _recover_source(name: str) -> Path | None:
    """Locate a bare filename across the standard known folders + cwd.

    Used when a move/copy is handed an absolute source path that isn't present
    (often a path *guessed* by an upstream step whose ``open`` reported only a
    basename). Mirrors the recovery ``open_file_or_folder`` performs so an
    operation issued right after an open lands on the file's real location
    instead of dead-ending on the wrong folder.
    """
    if not name:
        return None
    for base in (
        Path.cwd(),
        get_desktop(),
        get_downloads(),
        get_documents(),
        get_pictures(),
        get_music(),
        get_videos(),
        Path.home(),
    ):
        try:
            cand = base / name
            if _cloud_present(cand, retries=2):
                return cand
        except Exception:
            continue
    return None


def _safe_trash(target: Path) -> str:
    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


# ── SKIP DIRECTORIES ──────────────────────────────────────────────────────────
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "$Recycle.Bin",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
}

MAX_WALK_DEPTH = 6


# ── WINDOWS PATH DETECTION ───────────────────────────────────────────────────
# Allow spaces in paths (only stop at quotes or string end)
_WINDOWS_PATH_RE = __import__("re").compile(r'[A-Za-z]:[\\/][^\s"]*')
_WINDOWS_PATH_QUOTED_RE = __import__("re").compile(r'"([A-Za-z]:[\\/][^"]+)"')
_WINDOWS_PATH_DRIVE_RE = __import__("re").compile(r"[A-Za-z]:[\\/]")


def _is_windows_path(text: str) -> bool:
    """Check if text contains a Windows path."""
    stripped = text.strip()
    if _WINDOWS_PATH_QUOTED_RE.search(stripped):
        return True
    if _WINDOWS_PATH_RE.search(stripped):
        return True
    return False


# Natural-language filler that voice commands append after a path
# ("open D:\\dir please", "... for me now"). Trimmed only from the trailing
# position, so genuine spaced path components ("D:\\My Documents") are kept.
_TRAILING_FILLER = {
    "please",
    "now",
    "thanks",
    "thank",
    "you",
    "for",
    "me",
    "sir",
    "okay",
    "ok",
    "era",
    "quickly",
    "fast",
}


def _extract_windows_path(text: str) -> str | None:
    """Extract Windows path from text, handling quotes, spaces and trailing filler."""
    stripped = text.strip()
    m = _WINDOWS_PATH_QUOTED_RE.search(stripped)
    if m:
        return m.group(1)
    m = _WINDOWS_PATH_RE.search(stripped)
    if m:
        start = m.start()
        rest = stripped[start:]
        end = len(rest)
        for ch in ('"', ",", "!", "?"):
            idx = rest.find(ch, 1)
            if idx > 0 and idx < end:
                end = idx
        candidate = rest[:end].rstrip()
        # Drop trailing filler words appended by natural speech.
        tokens = candidate.split(" ")
        while len(tokens) > 1 and tokens[-1].lower().strip(".,!?") in _TRAILING_FILLER:
            tokens.pop()
        return " ".join(tokens)
    return None


# ── DRIVE DISCOVERY ──────────────────────────────────────────────────────────
def _get_available_drives() -> list[Path]:
    """Detect all available drives on Windows. Non-failing on other OS."""
    if _OS != "Windows":
        return []
    drives = []
    try:
        import ctypes

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if bitmask & 1:
                p = Path(f"{letter}:\\")
                if p.exists():
                    drives.append(p)
            bitmask >>= 1
    except Exception:
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:\\")
            try:
                if p.exists():
                    drives.append(p)
            except Exception:
                continue
    return drives


# ── SEARCH CONTEXT (follow-up support) ──────────────────────────────────────
class _SearchContext:
    """Lightweight follow-up context for file/folder searches."""

    def __init__(self):
        self.query: str = ""
        self.item_type: str = "both"
        self.drive_hint: str = ""
        self.parent_hint: str = ""
        self.candidates: list[dict] = []
        self.timestamp: float = 0.0

    def update(
        self,
        query: str = "",
        item_type: str = "both",
        drive_hint: str = "",
        parent_hint: str = "",
        candidates: list[dict] | None = None,
    ):
        self.query = query or self.query
        self.item_type = item_type or self.item_type
        self.drive_hint = drive_hint or self.drive_hint
        self.parent_hint = parent_hint or self.parent_hint
        self.candidates = candidates if candidates is not None else self.candidates
        self.timestamp = time.time()

    def is_fresh(self, max_age: float = 300.0) -> bool:
        return (time.time() - self.timestamp) < max_age

    def reset(self):
        self.__init__()


_search_context = _SearchContext()


def _parse_folder_intent(text: str) -> tuple[str, str, str, str]:
    """Parse query, item_type, drive_hint, parent_hint from user text."""
    text_lower = text.lower().strip()
    text_orig = text.strip()

    item_type = "both"
    if any(w in text_lower for w in (" folder", "directory", "dossier")):
        item_type = "folder"
    if any(w in text_lower for w in ("file", "document")):
        if "folder" not in text_lower:
            item_type = "file"

    drive_hint = ""
    drive_match = re.search(r"\b([A-Za-z]):", text)
    if drive_match:
        drive_hint = f"{drive_match.group(1).upper()}:"

    parent_hint = ""
    parent_patterns = [
        (r"\bunder\s+(.+?)(?:\s+folder\b|$)", True),
        (r"\bin\s+(?:the\s+)?(.+?)(?:\s+folder\b|$)", True),
        (r"\binside\s+(?:the\s+)?(.+?)(?:\s+folder\b|$)", True),
        (r"\bon\s+(.+?)(?:\s+folder\b|$)", True),
    ]
    for pat, use_orig in parent_patterns:
        m = re.search(pat, text_lower)
        if m:
            if use_orig:
                start = m.start(1)
                end = m.end(1)
                parent_hint = text_orig[start:end].strip().rstrip(".")
            else:
                parent_hint = m.group(1).strip().rstrip(".")
            break

    query = text_lower
    remove_words = (
        "open",
        "check",
        "find",
        "show",
        "the",
        "a",
        "an",
        "folder",
        "directory",
        "dossier",
        "please",
        "where",
        "is",
        "what",
        "locate",
        "search",
    )
    for word in remove_words:
        query = re.sub(r"\b" + re.escape(word) + r"\b", " ", query)
    for pat, _ in parent_patterns:
        query = re.sub(pat, " ", query)
    if drive_match:
        query = re.sub(r"\b" + re.escape(drive_match.group(1).lower()) + r":", " ", query)
    query = re.sub(r"\s+", " ", query).strip()

    if parent_hint and parent_hint.lower() in query:
        query = re.sub(re.escape(parent_hint.lower()), " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip()

    if not query and parent_hint:
        query = parent_hint

    return query, item_type, drive_hint, parent_hint


# ── CANDIDATE RESOLVER ────────────────────────────────────────────────────────
def _compact_key(text: str) -> str:
    """Create a compact key by removing accents, lowering, keeping only alphanumeric."""
    import unicodedata

    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "", text)
    return text


def _compute_match(query: str, item_name: str, item_path: Path) -> tuple[str, float]:
    """Compute match type and raw score for a query against an item name."""
    query_lower = query.lower()
    name_lower = item_name.lower()
    stem = Path(item_name).stem.lower()

    if name_lower == query_lower:
        return ("exact", 1.0)

    if stem == query_lower:
        return ("stem", 0.95)

    query_compact = _compact_key(query)
    name_compact = _compact_key(item_name)
    if query_compact and name_compact:
        if query_compact == name_compact:
            return ("compact_exact", 0.90)
        if query_compact in name_compact:
            return ("compact_contains", 0.80)

    if query_lower in name_lower:
        pos = name_lower.index(query_lower)
        pos_score = 1.0 - (pos / max(len(name_lower), 1))
        return ("contains", 0.7 + (pos_score * 0.15))

    ext_query = query_lower if query_lower.startswith(".") else f".{query_lower}"
    if item_path.suffix.lower() == ext_query:
        return ("extension", 0.6)

    ratio = difflib.SequenceMatcher(None, query_lower, name_lower).ratio()
    if ratio > 0.6:
        return ("fuzzy", ratio * 0.5)

    return ("none", 0.0)


def _compute_final_score(
    raw_score: float,
    match_type: str,
    item_type: str,
    item_type_filter: str,
    root_priority: int,
    modified_time: float,
    path_depth: int,
) -> float:
    """Compute final normalized score (0.0 - 1.0)."""
    score = raw_score * 100

    if item_type_filter == "both":
        score += 5
    elif item_type == item_type_filter:
        score += 10
    else:
        score -= 5

    score += max(0, 10 - (root_priority * 2))

    if modified_time:
        age_days = (time.time() - modified_time) / 86400
        if age_days < 1:
            score += 8
        elif age_days < 7:
            score += 5
        elif age_days < 30:
            score += 2

    score += max(0, 10 - path_depth)

    return score / 125


def _detect_onedrive() -> list[Path]:
    """Detect OneDrive folders. Optional, non-failing."""
    roots = []
    for env_var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        val = os.environ.get(env_var, "")
        if val:
            p = Path(val)
            if p.exists() and p.is_dir():
                roots.append(p)
    home_onedrive = Path.home() / "OneDrive"
    if home_onedrive.exists() and home_onedrive.is_dir():
        roots.append(home_onedrive)
    seen = set()
    unique = []
    for r in roots:
        key = str(r.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _get_project_folder() -> Path | None:
    """Return the current project root if identifiable."""
    from core.utils import BASE_DIR

    if BASE_DIR.exists() and BASE_DIR.is_dir():
        return BASE_DIR
    return None


def _list_fixed_drives() -> list[Path]:
    """Enumerate local fixed drives (C:, D:, …) so search covers the whole PC.

    Skips removable/network drives to avoid slow scans over USB sticks and
    mapped shares. Returns [] on non-Windows.
    """
    if _OS != "Windows":
        return []
    drives: list[Path] = []
    try:
        import ctypes
        import string

        DRIVE_FIXED = 3
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
                    p = Path(root)
                    if p.exists():
                        drives.append(p)
            except Exception:
                continue
    except Exception:
        return []
    return drives


def _get_search_roots(drive_hint: str = "") -> list[Path]:
    """Get controlled search roots. If drive_hint is set, prioritize that drive.

    High-value user folders come first (best scores / shallowest depth), then
    every local fixed drive so any file or folder on the machine is reachable.
    """
    roots = []
    if drive_hint:
        drive_path = Path(f"{drive_hint[0].upper()}:\\")
        if drive_path.exists():
            roots.append(drive_path)
    roots.append(get_desktop())
    roots.append(get_downloads())
    roots.append(get_documents())
    roots.extend(_detect_onedrive())
    project = _get_project_folder()
    if project:
        roots.append(project)
    # Whole-PC coverage: append every fixed drive at lowest priority.
    roots.extend(_list_fixed_drives())
    seen = set()
    unique = []
    for r in roots:
        try:
            key = str(r.resolve()).lower() if _OS == "Windows" else str(r.resolve())
            if key not in seen:
                seen.add(key)
                if r.exists() and r.is_dir():
                    unique.append(r)
        except Exception:
            continue
    return unique


def find_file_folder_candidates(
    query: str,
    roots: list[Path] | None = None,
    item_type: str = "both",
    extension: str | None = None,
    max_results: int = 10,
    timeout_sec: float = 5.0,
    drive_hint: str = "",
    parent_hint: str = "",
) -> dict:
    """Find file/folder candidates matching query across controlled roots."""
    # Was the caller scoping the search to specific roots? The FTS index covers
    # the default whole-machine root set only, so an explicit `roots` argument
    # must be honored via the os.walk pass below rather than the index.
    explicit_roots = roots is not None
    if roots is None:
        roots = _get_search_roots(drive_hint=drive_hint)

    if parent_hint and not query:
        query = parent_hint

    # ── Try FTS5 index first (fast path) ────────────────────────────────────
    try:
        from actions.file_indexer import get_file_index

        idx = get_file_index()
        if not explicit_roots and idx.is_ready and query:
            index_result = idx.search(
                query=query,
                item_type=item_type,
                extension=extension,
                max_results=max_results,
                timeout_sec=timeout_sec,
            )
            candidates = index_result.get("candidates", [])
            if candidates:
                # Apply parent_hint filter if needed
                if parent_hint:
                    ph_lower = parent_hint.lower()
                    candidates = [
                        c
                        for c in candidates
                        if ph_lower in Path(c["path"]).parts or ph_lower in c["path"].lower()
                    ]
                # Ensure sorted by score descending
                candidates.sort(key=lambda x: x["score"], reverse=True)
                candidates = candidates[:max_results]
                _search_context.update(
                    query=query,
                    item_type=item_type,
                    drive_hint=drive_hint,
                    parent_hint=parent_hint,
                    candidates=candidates,
                )
                return {
                    "status": "complete",
                    "candidates": candidates,
                    "query": query,
                    "message": None,
                }
    except Exception:
        pass  # Fall through to os.walk

    # ── Fallback: os.walk-based search ───────────────────────────────────────
    start_time = time.time()
    candidates = []
    seen: set[Path] = set()
    status = "complete"
    parent_found: Path | None = None

    if parent_hint:
        for root in roots:
            if time.time() - start_time > timeout_sec:
                status = "partial"
                break
            if not root.exists() or not root.is_dir():
                continue
            try:
                for dirpath, dirnames, _ in os.walk(root):
                    if time.time() - start_time > timeout_sec:
                        status = "partial"
                        break
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    rel_depth = len(Path(dirpath).relative_to(root).parts)
                    if rel_depth >= MAX_WALK_DEPTH:
                        dirnames.clear()
                        continue
                    for dname in dirnames:
                        if dname.lower() == parent_hint.lower():
                            parent_found = Path(dirpath) / dname
                            break
                    if parent_found:
                        break
            except Exception:
                continue
            if parent_found:
                break

        if parent_found:
            roots = [parent_found]

    for root_idx, root in enumerate(roots):
        if time.time() - start_time > timeout_sec:
            status = "partial"
            break

        if not root.exists() or not root.is_dir():
            continue

        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if time.time() - start_time > timeout_sec:
                    status = "partial"
                    break

                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

                try:
                    dirpath_path = Path(dirpath)
                except Exception:
                    continue

                if not is_safe_path(dirpath_path):
                    continue

                rel_depth = len(dirpath_path.relative_to(root).parts)
                if rel_depth >= MAX_WALK_DEPTH:
                    dirnames.clear()

                path_depth = len(dirpath_path.parts)

                if item_type in ("folder", "both"):
                    for dname in dirnames:
                        if time.time() - start_time > timeout_sec:
                            status = "partial"
                            break
                        try:
                            dp = dirpath_path / dname
                            if dp in seen or not dp.is_dir():
                                continue
                            match_type, raw_score = _compute_match(query, dname, dp)
                            if match_type == "none":
                                continue
                            final_score = _compute_final_score(
                                raw_score, match_type, "folder", item_type, root_idx, 0, path_depth
                            )
                            candidates.append(
                                {
                                    "name": dname,
                                    "path": str(dp),
                                    "type": "folder",
                                    "score": round(final_score, 4),
                                    "match_type": match_type,
                                    "modified": "unknown",
                                    "source": root.name,
                                }
                            )
                            seen.add(dp)
                        except Exception:
                            continue

                if item_type in ("file", "both"):
                    for fname in filenames:
                        if time.time() - start_time > timeout_sec:
                            status = "partial"
                            break
                        try:
                            fp = dirpath_path / fname
                            if fp in seen or not fp.is_file():
                                continue
                            if extension and fp.suffix.lower() != extension.lower():
                                continue
                            match_type, raw_score = _compute_match(query, fname, fp)
                            if match_type == "none":
                                continue
                            try:
                                mtime = fp.stat().st_mtime
                                modified_str = time.strftime(
                                    "%Y-%m-%d %H:%M", time.localtime(mtime)
                                )
                            except Exception:
                                mtime = 0
                                modified_str = "unknown"
                            final_score = _compute_final_score(
                                raw_score,
                                match_type,
                                "file",
                                item_type,
                                root_idx,
                                mtime,
                                path_depth,
                            )
                            candidates.append(
                                {
                                    "name": fname,
                                    "path": str(fp),
                                    "type": "file",
                                    "score": round(final_score, 4),
                                    "match_type": match_type,
                                    "modified": modified_str,
                                    "source": root.name,
                                }
                            )
                            seen.add(fp)
                        except Exception:
                            continue

        except PermissionError:
            continue
        except Exception:
            continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[:max_results]

    message = None
    if status == "partial":
        message = "Search timed out — showing partial results. Please refine your search."

    _search_context.update(
        query=query,
        item_type=item_type,
        drive_hint=drive_hint,
        parent_hint=parent_hint,
        candidates=candidates,
    )

    return {
        "status": status,
        "candidates": candidates,
        "query": query,
        "message": message,
    }


def _should_auto_open(candidates: list[dict]) -> tuple[bool, str | None]:
    """Decide whether to auto-open the top candidate."""
    if not candidates:
        return False, "No matches found"

    top = candidates[0]

    if top["match_type"] == "fuzzy":
        return False, "Fuzzy match — please verify"

    if len(candidates) == 1 and top["score"] >= 0.85:
        return True, None

    if len(candidates) >= 2:
        if top["score"] >= 0.85 and candidates[1]["score"] < 0.60:
            return True, None
        if (top["score"] - candidates[1]["score"]) < 0.15:
            return False, "Multiple close matches"

    if top["score"] < 0.70:
        return False, "Low confidence match"

    if len(candidates) == 1 and top["score"] >= 0.70:
        return True, None

    return False, "Ambiguous results"


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        items = []
        for item in sorted(target.iterdir()):
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                size = format_size(item.stat().st_size)
                items.append(f"📄 {item.name} ({size})")

        if not items:
            return f"Directory is empty: {target.name}/"

        return f"Contents of {target.name}/ ({len(items)} items):\n" + "\n".join(items)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {e}"


def _backup_file(target: Path) -> str | None:
    """Create a timestamped backup before overwriting. Returns backup path or None on failure."""
    if not target.exists():
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.parent / f"{target.name}.bak-{ts}"
        shutil.copy2(str(target), str(backup))
        return str(backup)
    except Exception:
        return None


def _ensure_openable_extension(target: Path) -> Path:
    """Give an extensionless new file a ``.txt`` suffix.

    A file written with text content but no extension shows up on Windows as a
    generic "File" with no associated application, so ``os.startfile`` (the
    ``open`` action) fails and the user cannot double-click it either. Names
    that already carry a suffix, dotfiles (``.gitignore``) and the empty name
    (target is the directory itself) are left untouched.
    """
    name = target.name
    if not name or name.startswith("."):
        return target
    if target.suffix:
        return target
    return target.with_name(name + ".txt")


def create_file(path: str, name: str = "", content: str = "", overwrite: bool = False) -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if name:
            target = _ensure_openable_extension(target)
        if not is_safe_path(target):
            return f"Access denied: {target}"
        if target.exists():
            if not overwrite:
                return f"File already exists: {target.name}. Set overwrite=true to replace it."
            backup_path = _backup_file(target)
            if backup_path is None:
                return f"Backup failed, refusing to overwrite: {target.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result = f"File created: {target.name}"
        if any(key for key in locals() if key == "backup_path" and backup_path):
            result += f" (backup: {Path(backup_path).name})"
        return result
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not is_safe_path(target):
            return f"Access denied: {target}"
        target.mkdir(parents=True, exist_ok=True)
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not is_safe_path(target):
            return f"Access denied: {target}"
        # A missing file is the success state for deletion: the user asked for
        # the file gone, and it is gone. Reporting "Not found" as an error made
        # every "delete this" whose target was already absent read as a failure.
        # Check with the cloud-aware probe so a OneDrive placeholder that exists()
        # misses is still counted as present.
        if not _cloud_present(target):
            return f"Already gone: {target.name} — nothing to delete."

        protected = {
            get_desktop(),
            get_downloads(),
            get_documents(),
            get_pictures(),
            get_music(),
            get_videos(),
            Path.home(),
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        return _safe_trash(target)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {e}"


def move_file(path: str, name: str = "", destination: str = "", overwrite: bool = False) -> str:
    # Resolve to absolute paths to handle virtual drives / OneDrive / shortcuts.
    try:
        base = _resolve_path(path).resolve()
        src = (base / name).resolve() if name else base.resolve()
        dst_raw = _resolve_path(destination).resolve() if destination else None
    except Exception as e:
        return f"Path resolution failed: {e} (source='{path}', dest='{destination}')"

    try:
        # Verify both sides exist/resolve before any mutation.
        # Use a cloud-aware presence check: on OneDrive Known-Folder paths a
        # single exists() can spuriously return False for a file that is present
        # but momentarily un-hydrated. Only after the retry/lexists check fails
        # do we attempt to recover the true location by filename.
        if not _cloud_present(src):
            # The given absolute path isn't present. It may be a path *guessed*
            # by an upstream step (e.g. an open that reported only a basename),
            # so recover the real location by searching the known folders + cwd.
            src_name = name or src.name
            recovered = _recover_source(src_name)
            if recovered is not None:
                src = recovered.resolve()
            else:
                # Legacy home-dir fallback, then give up with an actionable error.
                alt = (Path.home() / name).resolve() if name else Path(path).resolve()
                if _cloud_present(alt):
                    src = alt
                else:
                    return (
                        f"Source not found (abs: {src} | alt: {alt}): "
                        f"'{src_name}' is not at {src} nor in "
                        f"Desktop/Documents/Downloads/Home/cwd "
                        f"(path='{path}' name='{name}'). If it was just opened, "
                        f"the open may have located it elsewhere — pass the "
                        f"absolute path shown by the open result."
                    )
        if dst_raw is None:
            return "No destination specified (absolute/partial/shortcut unresolved)."
        dst = dst_raw
        if not is_safe_path(src):
            return f"Access denied (source): {src} — path outside allowed zone"
        if not is_safe_path(dst):
            return f"Access denied (destination): {dst} — path outside allowed zone"

        # Handle OneDrive / virtual-drive discrepancies: verify parent dirs writable
        dst_parent = dst.parent if dst.is_dir() or not dst.exists() else dst.parent
        if dst.is_dir():
            dst = dst / src.name

        dst = dst.resolve()
        if dst.exists():
            if not overwrite:
                return f"Destination already exists (abs: {dst}): {dst.name}. Set overwrite=true to replace."
            try:
                backup_path = _backup_file(dst)
                if backup_path is None:
                    return f"Backup failed (abs: {dst.name}), refusing overwrite."
            except Exception as be:
                return f"Backup error on {dst.name} (abs: {dst}): {be}"

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except Exception as me:
            return f"Destination parent mkdir failed (abs: {dst.parent}): {me} — access denied or path invalid"

        try:
            shutil.move(str(src), str(dst))
        except FileNotFoundError as fnf:
            return f"Move broken — source disappeared mid-transfer (abs source: {src}): {fnf}"
        except PermissionError as pe:
            return f"Move broken — access denied / file locked (abs src: {src}, dst: {dst}): {pe}"
        except OSError as ose:
            # OneDrive virtual-drive / sync locks often surface as generic OSError
            return (
                f"Move broken — path/virtual-drive discrepancy (abs src: {src}, dst: {dst}): {ose}"
            )

        result = f"Moved: {src.name} → {dst.parent.name}/ (abs src={src}, dst={dst})"
        if "backup_path" in locals() and backup_path:
            result += f" (backup: {Path(backup_path).name})"
        return result

    except Exception as e:
        return f"Could not move (source abs={src if 'src' in dir() else 'unresolved'}, dest={dst_raw if 'dst_raw' in dir() else 'unresolved'}): {type(e).__name__}: {e}"


def copy_file(path: str, name: str = "", destination: str = "", overwrite: bool = False) -> str:
    try:
        base = _resolve_path(path)
        src = (base / name) if name else base
        dst = _resolve_path(destination) if destination else None

        if not _cloud_present(src):
            recovered = _recover_source(name or src.name)
            if recovered is not None:
                src = recovered
            else:
                return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not is_safe_path(src):
            return f"Access denied (source): {src}"
        if not is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        if dst.exists():
            if not overwrite:
                return f"Destination already exists: {dst.name}. Set overwrite=true to replace it."
            backup_path = _backup_file(dst)
            if backup_path is None:
                return f"Backup failed, refusing to overwrite: {dst.name}"

        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))

        result = f"Copied: {src.name} → {dst.parent.name}/"
        if "backup_path" in locals() and backup_path:
            result += f" (backup: {Path(backup_path).name})"
        return result

    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_path = target.parent / new_name
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        target.rename(new_path)
        return f"Renamed: {target.name} → {new_name}"

    except Exception as e:
        return f"Could not rename: {e}"


def _output_search_roots(hint: Path) -> list[Path]:
    """Directories to recursively search when a shallow/static path misses.

    Campaign artifacts (e.g. plan.md) are saved inside nested
    'output.../<month>/<campaign>/' folders whose exact path Era does not track.
    Resolve to the nearest 'output' ancestor of the requested path, then fall
    back to top-level 'output*' folders in the usual workspaces.
    """
    roots: list[Path] = []

    # 1. Nearest existing ancestor of the hint whose name starts with 'output'.
    for parent in [hint, *hint.parents]:
        try:
            if parent.name.lower().startswith("output") and parent.is_dir():
                roots.append(parent)
                break
        except Exception:
            continue

    # 2. Otherwise, the nearest existing ancestor directory of the hint.
    if not roots:
        for parent in hint.parents:
            try:
                if parent.is_dir():
                    roots.append(parent)
                    break
            except Exception:
                continue

    # 3. Top-level 'output*' folders in the standard workspaces.
    bases = [Path.cwd(), _get_project_folder(), get_documents(), get_desktop()]
    bases.extend(_detect_onedrive())
    for base in bases:
        if not base:
            continue
        try:
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if child.is_dir() and child.name.lower().startswith("output"):
                    roots.append(child)
        except Exception:
            continue

    # De-dupe; keep only safe, existing directories.
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        try:
            key = str(r.resolve()).lower()
            if key not in seen and r.is_dir() and is_safe_path(r, allow_protected=True):
                seen.add(key)
                unique.append(r)
        except Exception:
            continue
    return unique


def _walk_find_by_name(filename: str, roots: list[Path], timeout_sec: float = 5.0) -> list[Path]:
    """Recursively find files named `filename` (case-insensitive) under `roots`.

    Honors SKIP_DIRS / MAX_WALK_DEPTH and returns matches newest-first so the
    most recent campaign plan surfaces first.
    """
    target_lower = filename.lower()
    found: list[Path] = []
    seen: set[str] = set()
    start = time.time()
    for root in roots:
        if time.time() - start > timeout_sec:
            break
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if time.time() - start > timeout_sec:
                    break
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                try:
                    dp = Path(dirpath)
                    rel_depth = len(dp.relative_to(root).parts)
                except Exception:
                    continue
                if rel_depth >= MAX_WALK_DEPTH:
                    dirnames.clear()
                for fn in filenames:
                    if fn.lower() == target_lower:
                        fp = dp / fn
                        try:
                            key = str(fp.resolve()).lower()
                        except Exception:
                            continue
                        if key not in seen and is_safe_path(fp, allow_protected=True):
                            seen.add(key)
                            found.append(fp)
        except Exception:
            continue

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except Exception:
            return 0.0

    found.sort(key=_mtime, reverse=True)
    return found


def _read_text_capped(target: Path, max_chars: int) -> str:
    content = target.read_text(encoding="utf-8", errors="ignore")
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n[Truncated — {len(content)} total chars]"
    return content


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not is_safe_path(target):
            return f"Access denied: {target}"

        if not target.exists():
            # The static path missed — the file may live in a nested campaign
            # subfolder. Recursively walk the output roots to locate it by name
            # so approvals against plan.md still work.
            filename = target.name
            if filename:
                matches = _walk_find_by_name(filename, _output_search_roots(target))
                if matches:
                    primary = matches[0]
                    body = _read_text_capped(primary, max_chars)
                    header = f"[Found in nested folder: {primary.parent}]\n\n"
                    if len(matches) > 1:
                        others = "\n".join(f"  - {m.parent}" for m in matches[1:6])
                        header += (
                            f"[{len(matches)} copies of {filename} exist; showing the "
                            f"most recently modified. Other locations:\n{others}\n"
                            "Name the campaign folder to read a specific one.]\n\n"
                        )
                    return header + body
            return f"File not found: {target.name}"

        if not target.is_file():
            return f"Not a file: {target.name}"

        return _read_text_capped(target, max_chars)

    except Exception as e:
        return f"Could not read file: {e}"


def write_file(
    path: str, name: str = "", content: str = "", append: bool = False, overwrite: bool = False
) -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if name:
            target = _ensure_openable_extension(target)
        if not is_safe_path(target):
            return f"Access denied: {target}"
        if target.exists() and not append:
            if not overwrite:
                return (
                    f"File already exists: {target.name}. "
                    "Set overwrite=true to replace it, or append=true to append."
                )
            backup_path = _backup_file(target)
            if backup_path is None:
                return f"Backup failed, refusing to overwrite: {target.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended to" if append else "Written to"
        result = f"{action}: {target.name}"
        if not append and "backup_path" in locals() and backup_path:
            result += f" (backup: {Path(backup_path).name})"
        return result
    except Exception as e:
        return f"Could not write file: {e}"


def find_files(
    name: str = "",
    extension: str = "",
    path: str = "home",
    max_results: int = 20,
    item_type: str = "both",
    recursive: bool = True,
    drive_hint: str = "",
    parent_hint: str = "",
) -> str:
    try:
        # Known-path hits (favorites / recently opened) come first: they are
        # certain, instant, and not limited by the scoped search root below.
        known: list[dict] = []
        if name and not extension:
            try:
                from actions.path_anchors import resolve_known

                for h in resolve_known(name, item_type=item_type):
                    if is_safe_path(h, allow_protected=True):
                        kind = "folder" if h.is_dir() else "file"
                        known.append({"name": h.name, "path": str(h), "type": kind})
            except Exception:
                known = []

        # No path given → whole machine (was: Desktop only, which is why a
        # folder on D: was reported missing in 25 ms). Skip that scan when a
        # known path already answered the question.
        if not path:
            if known:
                result = {"status": "complete", "candidates": []}
                search_path = None
            else:
                search_path = None
                result = find_file_folder_candidates(
                    query=name or extension or "",
                    item_type=item_type,
                    extension=extension if extension else None,
                    max_results=min(int(max_results), 50),
                    timeout_sec=15.0,
                    drive_hint=drive_hint,
                    parent_hint=parent_hint,
                )
        else:
            search_path = _resolve_path(path)
            if not is_safe_path(search_path):
                return f"Access denied: {search_path}"
            if not search_path.exists():
                return f"Search path not found: {path}"
            result = find_file_folder_candidates(
                query=name or extension or "",
                roots=[search_path],
                item_type=item_type,
                extension=extension if extension else None,
                max_results=min(int(max_results), 50),
                drive_hint=drive_hint,
                parent_hint=parent_hint,
            )

        hard_limit = min(int(max_results), 50)
        seen_paths = {c["path"].lower() for c in known}
        candidates = known + [
            c for c in result["candidates"] if c["path"].lower() not in seen_paths
        ]
        candidates = candidates[:hard_limit]
        if not candidates:
            query_display = name or extension or "files"
            where = f"{search_path.name}/" if search_path else "this PC"
            return f"No {query_display} found in {where}"

        lines = []
        for c in candidates:
            icon = "📁" if c["type"] == "folder" else "📄"
            lines.append(f"{icon} {c['name']} — {Path(c['path']).parent}")

        status_msg = ""
        if result["status"] == "partial":
            status_msg = "\n⚠️ Search timed out — showing partial results."

        return f"Found {len(candidates)} result(s):\n" + "\n".join(lines) + status_msg

    except Exception as e:
        return f"Search error: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(count, 50)
    try:
        search_path = _resolve_path(path)
        if not is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        files = []
        for item in search_path.rglob("*"):
            if item.is_file():
                try:
                    files.append((item.stat().st_size, item))
                except Exception:
                    continue

        files.sort(reverse=True)
        top = files[:count]

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, f in top:
            lines.append(f"  {format_size(size):>10}  {f.name}  ({f.parent})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage = shutil.disk_usage(target)
        pct = usage.used / usage.total * 100
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {format_size(usage.total)}\n"
            f"  Used  : {format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop() -> str:
    type_map = {
        "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {
            ".pdf",
            ".doc",
            ".docx",
            ".txt",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".csv",
            ".odt",
            ".ods",
            ".odp",
        },
        "Videos": {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Music": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code": {
            ".py",
            ".js",
            ".ts",
            ".html",
            ".css",
            ".json",
            ".xml",
            ".cpp",
            ".java",
            ".cs",
            ".go",
            ".rs",
            ".sh",
        },
    }

    desktop = get_desktop()
    moved, skipped = [], []

    try:
        for item in desktop.iterdir():
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.name in {k for k in type_map}:
                continue

            ext = item.suffix.lower()
            target_dir = desktop / "Others"
            for folder, exts in type_map.items():
                if ext in exts:
                    target_dir = desktop / folder
                    break

            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name

            if new_path.exists():
                skipped.append(item.name)
                continue

            shutil.move(str(item), str(new_path))
            moved.append(f"{item.name} → {target_dir.name}/")

        result = f"Desktop organized: {len(moved)} files moved."
        if moved:
            preview = moved[:8]
            result += "\n" + "\n".join(preview)
            if len(moved) > 8:
                result += f"\n... and {len(moved) - 8} more."
        if skipped:
            result += f"\n{len(skipped)} file(s) skipped (name conflict)."
        return result

    except Exception as e:
        return f"Could not organize desktop: {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name": target.name,
            "Type": "Folder" if target.is_dir() else "File",
            "Size": format_size(stat.st_size),
            "Location": str(target.parent),
            "Created": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"


def open_file_or_folder(path: str, name: str = "") -> str:
    try:
        raw_input = f"{path} {name}".strip()

        # ── Direct resolution first ──
        # The common tool shape is open_file_or_folder(<dir>, name=<file>).
        # Resolve and open it directly before the natural-language heuristics
        # below, which would otherwise glue path+name with a space and misparse
        # it (e.g. "C:\\dir test.txt") into a non-existent path.
        if not (name and _is_windows_path(name)):
            try:
                base = _resolve_path(path)
                direct = (base / name) if name else base
                if direct.exists() and is_safe_path(direct, allow_protected=True):
                    return _open_path(direct)
            except Exception:
                pass

        # ── Fix 3: Detect path-like strings in the `name` parameter ──
        # When voice commands pass a full path in `name` instead of `path`
        combined = name if name and _is_windows_path(name) else raw_input
        if _is_windows_path(combined):
            extracted = _extract_windows_path(combined)
            if extracted:
                target = Path(extracted)
                if not target.exists():
                    return f"Path not found: {extracted}"
                if not is_safe_path(target, allow_protected=True):
                    return f"Access denied: {extracted}"
                return _open_path(target)

        if _is_windows_path(raw_input):
            extracted = _extract_windows_path(raw_input)
            if extracted:
                target = Path(extracted)
                if not target.exists():
                    return f"Path not found: {extracted}"
                if not is_safe_path(target, allow_protected=True):
                    return f"Access denied: {extracted}"
                return _open_path(target)

        base = _resolve_path(path)
        target = (base / name) if name else base

        if target.exists() and is_safe_path(target, allow_protected=True):
            return _open_path(target)

        if not name:
            return f"Sir, I could not not locate any file or folder at '{path}'."

        # ── Known-path hierarchy before the whole-machine scan ──
        # "ABC" is a component of a saved favorite / recently opened path, so
        # its location is already known — no drive walk (which timed out) needed.
        known_msg = _open_from_known(name)
        if known_msg:
            return known_msg

        query, item_type, drive_hint, parent_hint = _parse_folder_intent(raw_input)

        if not query and _search_context.is_fresh():
            query = _search_context.query
            if not drive_hint:
                drive_hint = _search_context.drive_hint
            if not parent_hint:
                parent_hint = _search_context.parent_hint
            if item_type == "both":
                item_type = _search_context.item_type

        result = find_file_folder_candidates(
            query=query or name,
            item_type=item_type,
            drive_hint=drive_hint,
            parent_hint=parent_hint,
            max_results=10,
            timeout_sec=15.0,
        )

        candidates = result["candidates"]

        if not candidates:
            if parent_hint and parent_hint.lower() != query.lower():
                return (
                    f"Parent folder '{parent_hint}' not found, or no match for '{query}' inside it."
                )
            return f"Sir, I could not locate any file or folder matching '{query or name}'."

        should_open, reason = _should_auto_open(candidates)

        if should_open:
            top = candidates[0]
            target_path = Path(top["path"])
            if target_path.exists() and is_safe_path(target_path):
                return _open_path(target_path)

        lines = []
        for i, c in enumerate(candidates[:5], 1):
            icon = "📁" if c["type"] == "folder" else "📄"
            lines.append(f"{i}. {icon} {c['name']} ({c['source']})")

        return (
            f"I found {len(candidates)} match(es) for '{query or name}':\n"
            + "\n".join(lines)
            + "\nWhich one should I open?"
        )

    except Exception as e:
        return f"Failed to open path: {e}"


def _open_from_known(name: str, item_type: str = "both") -> str | None:
    """Open ``name`` via known anchors (favorites / recently opened paths).

    One match → open it. Several → list them for the user to choose. None →
    return None so the caller falls through to the regular search.
    """
    try:
        from actions.path_anchors import resolve_known

        hits = [
            h
            for h in resolve_known(name, item_type=item_type)
            if is_safe_path(h, allow_protected=True)
        ]
    except Exception:
        return None
    if not hits:
        return None
    if len(hits) == 1:
        return _open_path(hits[0])
    lines = [f"{i}. {h}" for i, h in enumerate(hits[:5], 1)]
    return f"I know {len(hits)} places named '{name}':\n" + "\n".join(lines) + "\nWhich one?"


def _open_path(target: Path, reveal_file: bool = False) -> str:
    """Open a validated target path with existence check.
    On Windows, uses os.startfile for BOTH files and folders — launches
    the default app for files instead of just highlighting in Explorer."""
    if not target.exists():
        return f"Path no longer exists: {target}"
    if not is_safe_path(target, allow_protected=True):
        return f"Access denied: {target}"

    # Track opened path for frecency scoring
    try:
        from actions.file_indexer import get_file_index

        get_file_index().record_open(str(target))
    except Exception:
        pass

    # Surface the resolved absolute path so a follow-up move/copy/rename acts on
    # the file's real location instead of guessing a folder from the basename.
    # (A fuzzy-search open can resolve to a path the caller never supplied.)
    try:
        abs_target = target.resolve()
    except Exception:
        abs_target = target

    if _OS == "Windows":
        try:
            os.startfile(str(target))
            kind = "file" if target.is_file() else "folder"
            return f"Opened {kind}: {target.name} (path: {abs_target})"
        except Exception as e:
            # Fallback: try explorer for folders
            if target.is_dir():
                subprocess.run(["explorer", str(target)])
                return f"Opened folder: {target.name}"
            return f"Failed to open {target.name}: {e}"
    elif _OS == "Darwin":
        subprocess.run(["open", str(target)])
        kind = "file" if target.is_file() else "folder"
        return f"Opened {kind}: {target.name} (path: {abs_target})"
    else:
        target_dir = str(target.parent if target.is_file() else target)
        subprocess.run(["xdg-open", target_dir])
        return f"Opened directory containing: {target.name} (path: {abs_target})"


def _resolve_open_target(path: str, name: str = "") -> Path | None:
    """Resolve a path/name pair the same way open_file_or_folder does, without
    opening anything — used by actions that need the target first (open_with).
    Mirrors open_file_or_folder's precedence: try the direct base/name join
    first (the common tool-call shape) before the natural-language path
    heuristics, which would otherwise glue path+name into a bogus path
    (e.g. "C:\\dir test.txt")."""
    if not (name and _is_windows_path(name)):
        try:
            base = _resolve_path(path)
            direct = (base / name) if name else base
            if direct.exists():
                return direct
        except Exception:
            pass

    raw_input = f"{path} {name}".strip()
    combined = name if name and _is_windows_path(name) else raw_input
    if _is_windows_path(combined):
        extracted = _extract_windows_path(combined)
        if extracted:
            return Path(extracted)

    base = _resolve_path(path)
    return (base / name) if name else base


def open_with_picker(path: str, name: str = "") -> str:
    """Open the OS-native 'Open With' chooser for a file, so the user can
    pick any installed application themselves (Adobe or otherwise) instead
    of Era guessing an install path that may not exist on this machine."""
    target = _resolve_open_target(path, name=name)
    if not target or not target.exists():
        return f"Path not found: {path} {name}".strip()
    if not is_safe_path(target, allow_protected=True):
        return f"Access denied: {target}"
    if target.is_dir():
        return f"'{target.name}' is a folder — the 'open with' picker only applies to files."

    if _OS == "Windows":
        try:
            subprocess.run(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(target)])
            return f"Opened the 'Open With' picker for {target.name}."
        except Exception as e:
            return f"Failed to open the app picker for {target.name}: {e}"
    elif _OS == "Darwin":
        subprocess.run(["open", "-R", str(target)])
        return f"Revealed {target.name} in Finder — right-click it and choose 'Open With'."
    else:
        subprocess.run(["xdg-open", str(target.parent)])
        return f"Opened the containing folder for {target.name} — choose 'Open With' from there."


# ── ✅ NEW SYSTEM ACTION: OPEN LAST COMPLETED DOWNLOAD ────────────────────────
def open_last_download() -> str:
    """Queries your system Downloads folder and reveals the absolute latest file."""
    try:
        dl_dir = get_downloads()
        if not dl_dir.exists() or not dl_dir.is_dir():
            return "Sir, your Downloads directory could not be resolved safely."

        # Pull valid files while ignoring dynamic/unfinished web browser chunks
        files = [
            f
            for f in dl_dir.iterdir()
            if f.is_file() and not f.name.endswith((".tmp", ".crdownload", ".part"))
        ]

        if not files:
            # Fallback to absolute file mapping if completely empty or custom engine named
            files = [f for f in dl_dir.iterdir() if f.is_file()]

        if not files:
            return "Sir, your Downloads folder is currently empty."

        # Sort files by real-time modification timestamp descending
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        latest_file = files[0]

        return open_file_or_folder(str(dl_dir), name=latest_file.name)
    except Exception as e:
        return f"Failed to track down last download: {e}"


# ── PATH FAVORITES ───────────────────────────────────────────────────────────
def _save_favorite(name: str, path: str = "") -> str:
    """Save a folder path to favorites in long-term memory."""
    from memory.memory_manager import update_memory

    if not path or not name:
        return "Both name and path are required to save a favorite."
    target = Path(path)
    if not target.exists():
        return f"Path not found: {path}"
    if not is_safe_path(target):
        return f"Access denied: {path}"
    update_memory({"favorite_paths": {name: {"value": str(target.resolve())}}})
    return f"Saved favorite '{name}' -> {target.resolve()}"


def _list_favorites() -> str:
    """List all saved path favorites from long-term memory."""
    from memory.memory_manager import load_memory

    memory = load_memory()
    favs = memory.get("favorite_paths", {})
    if not favs:
        return "No saved favorites yet. Say 'save this folder as [name]' to add one."
    lines = ["Saved favorites:"]
    for key, entry in favs.items():
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            exists = Path(val).exists() if val else False
            icon = "[OK]" if exists else "[MISSING]"
            lines.append(f"  {icon} {key} -> {val}")
    return "\n".join(lines)


def _open_favorite(name: str) -> str:
    """Open a saved path favorite by name."""
    from memory.memory_manager import load_memory

    memory = load_memory()
    favs = memory.get("favorite_paths", {})
    if name.lower() in {k.lower() for k in favs}:
        for key, entry in favs.items():
            if key.lower() == name.lower():
                val = entry.get("value") if isinstance(entry, dict) else entry
                if val and Path(val).exists():
                    return open_file_or_folder(val)
                return f"Favorite '{name}' path no longer exists: {val}"
    # Fuzzy match
    import difflib

    matches = difflib.get_close_matches(name, list(favs.keys()), n=1, cutoff=0.4)
    if matches:
        key = matches[0]
        entry = favs[key]
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val and Path(val).exists():
            return open_file_or_folder(val)
        return f"Favorite '{key}' path no longer exists: {val}"
    # Not a favorite's name — but it may be a folder INSIDE a favorite's path
    # (ABC in D:\...\ABC\claude automation) or directly beneath one.
    known_msg = _open_from_known(name)
    if known_msg:
        return known_msg
    return f"Favorite '{name}' not found. Say 'list favorites' to see all saved paths."


# ── FILE INDEX MANAGEMENT ────────────────────────────────────────────────────


def _rebuild_index(params: dict) -> str:
    """Trigger a background rebuild of the FTS5 file index."""
    try:
        from actions.file_indexer import get_file_index

        idx = get_file_index()
        if idx.is_building:
            return "Index rebuild already in progress."
        roots = _get_search_roots()
        idx.rebuild(roots=roots, wait=False)
        return "Index rebuild started in background. Searches will use the index once complete."
    except Exception as e:
        return f"Index rebuild failed: {e}"


def _index_status() -> str:
    """Return the current state of the file index."""
    try:
        from actions.file_indexer import get_file_index

        idx = get_file_index()
        count = idx.file_count()
        last = idx.last_indexed_at()
        ready = idx.is_ready
        building = idx.is_building

        last_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "never"
        status = "building" if building else ("ready" if ready else "not initialized")
        return f"Index status: {status}\nEntries indexed: {count}\nLast indexed: {last_str}"
    except Exception as e:
        return f"Index status unavailable: {e}"


def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    path = params.get("path", "desktop")
    name = params.get("name", "")

    if player:
        player.write_log(f"[file] {action} {name or path}")

    try:
        if action == "list":
            return list_files(path)
        elif action == "create_file":
            return create_file(
                path,
                name=name,
                content=params.get("content", ""),
                overwrite=params.get("overwrite", False),
            )
        elif action in (
            "create_folder",
            "create_directory",
            "make_folder",
            "make_directory",
            "new_folder",
            "mkdir",
        ):
            return create_folder(path, name=name)
        elif action == "delete":
            return delete_file(path, name=name)
        elif action == "move":
            return move_file(
                path,
                name=name,
                destination=params.get("destination", ""),
                overwrite=params.get("overwrite", False),
            )
        elif action == "copy":
            return copy_file(
                path,
                name=name,
                destination=params.get("destination", ""),
                overwrite=params.get("overwrite", False),
            )
        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))
        elif action == "read":
            return read_file(path, name=name)
        elif action == "write":
            return write_file(
                path,
                name=name,
                content=params.get("content", ""),
                append=params.get("append", False),
                overwrite=params.get("overwrite", False),
            )
        elif action == "find":
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                # Unscoped find searches known paths + the whole PC, not Desktop.
                path=params.get("path") or "",
                max_results=min(int(params.get("max_results", 20)), 50),
                item_type=params.get("item_type", "both"),
                recursive=params.get("recursive", True),
                drive_hint=params.get("drive", ""),
                parent_hint=params.get("parent", ""),
            )
        elif action == "open":
            return open_file_or_folder(path, name=name)
        elif action == "open_with":
            return open_with_picker(path, name=name)
        # ✅ REGISTER NEW ROUTE ACTION
        elif action in ("last_download", "open_last_download"):
            return open_last_download()
        elif action == "largest":
            return get_largest_files(path=path, count=int(params.get("count", 10)))
        elif action == "disk_usage":
            return get_disk_usage(path)
        elif action == "organize_desktop":
            return organize_desktop()
        elif action == "info":
            return get_file_info(path, name=name)
        elif action == "save_favorite":
            return _save_favorite(name=name, path=path)
        elif action == "list_favorites":
            return _list_favorites()
        elif action == "open_favorite":
            return _open_favorite(name=name)
        elif action == "rebuild_index":
            return _rebuild_index(params)
        elif action == "index_status":
            return _index_status()
        else:
            return f"Unknown action: '{action}'"
    except Exception as e:
        return f"File controller error ({action}): {e}"
