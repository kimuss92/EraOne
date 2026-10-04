r"""core/vault_bridge.py — the one safe door into the Obsidian vault for tools and the crew.

Actions: read, search, create, append, replace. Nothing else — there is no delete,
and no confirm/reject: approval is the user's own "yes" through
PendingConfirmationStore, so a caller can never approve its own write.

WHY A BRIDGE AND NOT vault_writer
  vault_writer owns frontmatter and placement for Era's own voice path. It has no
  lock, no atomic write and no read-back, and the vault is synced live by OneDrive
  while the headless CM and the 03:10 maintenance also write it. The bridge adds the
  three things that path lacks, and vault_writer now borrows them (`vault_write_lock`,
  `write_verified`) so every writer shares one lock.

THE WRITE CONTRACT
  1. One cross-process lock around read-modify-write (`vault_write_lock`: an in-process
     mutex plus an O_EXCL lock file in ~/.era; era_dispatch.FileLock leaked under a
     thread burst on Windows, so the bridge does not use it).
  2. Temp file in the same folder, then os.replace — never a half-written note.
  3. Re-read and compare, twice, with a short settle between: OneDrive can rewrite a
     file between our write and our report. A mismatch is a FAILURE, never "done".

REPLACE IS STAGED
  `stage_replace` computes the new text and a diff and parks them in `_STAGED` keyed
  by an unguessable plan id. Only the id travels through the confirmation store
  (as the carried `_approved_plan`). Apply pops the plan (single use), re-checks the
  note's hash against the one staged, and refuses on any drift. There is no path that
  replaces without a staged plan.

BRAND ISOLATION AND ZONES
  A scoped caller (a crew bot) reads only its own brands' notes plus global ones, by
  the same ownership rule the CM brief uses (cm_vault._matches_brand), failing closed
  if the brand catalog cannot be read. The free writes (create/append) are limited to
  the caller's `write_zones`; the default for bots is the Inbox, which is never
  briefed, because unreviewed agent text must not steer a live post (cm-lane
  invariant). `replace` is exempt from zones: the user reviews it, so it is the one
  write that may reach a briefed note.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from core import cm_vault
from core.logger import log_event, setup_logger
from core.tool_result import ToolResult

bridge_logger = setup_logger("era_one.vault_bridge")

ACTIONS = ("read", "search", "create", "append", "replace")
UNGATED_ACTIONS = frozenset({"read", "search", "create", "append"})
GATED_ACTIONS = frozenset({"replace"})

ALL_ZONES = frozenset({"inbox", "brand", "global", "other"})
_INBOX = "00 - Inbox"
_GLOBAL = "04 - Global"
_BRAND_FOLDERS = ("02 - Delta Cuisine", "03 - Ceram Quartz", "07 - Brico Kitchen")
# Longer than PendingConfirmationStore's 600 s PLUS the slow Nabar review that can sit
# between staging and arming: the store must be what expires first, never the plan.
_PLAN_TTL_S = 1800
_STAGED_MAX = 32
_MAX_READ_CHARS = 4000
_MAX_NOTE_BYTES = 1_000_000
# One write may not be large enough to push a note past what the bridge will edit again
# (a single oversized append would lock the note out of every later append/replace).
_MAX_CONTENT_CHARS = 100_000
_FM_RE = cm_vault._FRONTMATTER_RE
_STOPWORDS = frozenset(
    "the a an is are was were what which who whom whose did do does we i you our us my of "
    "about for to in on at and or it this that these those how when where why have has had "
    "me tell find show please le la les un une de des du et est que qu quel quelle quels "
    "dans pour sur nous notre".split()
)


@dataclass(frozen=True)
class Caller:
    """Who is asking. The default is Era acting for the user: unrestricted."""

    actor: str = "era"
    brands: frozenset[str] | None = None  # None = every brand
    write_zones: frozenset[str] = ALL_ZONES


class _Refusal(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type, self.message = error_type, message


class VaultLockUnavailable(OSError):
    """The shared write lock could not be taken. Nothing was written."""


class VaultWriteMismatch(OSError):
    """The note on disk is not what we wrote (a sync clobbered it)."""


# ── shared lock + verified write (also used by vault_writer) ────────────────
_PROC_LOCK = threading.Lock()  # threads in one process queue here, not on the file
_LOCK_STALE_S = 60.0  # a vault write takes well under a second; this only frees a crash


def _lock_path() -> Path:
    """Beside era_dispatch's lock (outside OneDrive, redirected by ERA_LOCK_FILE in tests)."""
    base = Path(os.getenv("ERA_LOCK_FILE", str(Path.home() / ".era" / "era.lock")))
    return base.with_name("vault_write.lock")


def _acquire_file(path: Path, deadline: float) -> int:
    """Atomic O_EXCL create. Waiters never open the file — they only stat it — because
    on Windows a reader's open handle makes the holder's release fail silently, which
    leaked era_dispatch.FileLock under a 10-thread burst (found by the bridge tests)."""
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            return fd
        except (FileExistsError, PermissionError):  # held, or mid-delete on Windows
            try:
                if time.time() - path.stat().st_mtime > _LOCK_STALE_S:
                    victim = path.with_name(f"{path.name}.stale.{os.getpid()}.{time.time_ns()}")
                    os.rename(path, victim)  # the rename is the atomic arbiter
                    victim.unlink(missing_ok=True)
                    continue
            except OSError:
                pass  # vanished or lost the race: just retry
            if time.monotonic() > deadline:
                raise VaultLockUnavailable("another writer holds the vault lock") from None
            time.sleep(0.02)


def _release_file(fd: int, path: Path) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    for _ in range(100):  # a scanner/sync client may briefly hold the file open
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.02)


@contextmanager
def vault_write_lock(timeout: float = 15.0):
    """The one write lock every vault writer shares (bridge, vault_writer, maintenance)."""
    deadline = time.monotonic() + timeout
    if not _PROC_LOCK.acquire(timeout=timeout):
        raise VaultLockUnavailable("another writer holds the vault lock")
    try:
        path = _lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultLockUnavailable(f"vault lock unavailable: {exc}") from exc
        fd = _acquire_file(path, deadline)
        try:
            yield
        finally:
            _release_file(fd, path)
    finally:
        _PROC_LOCK.release()


def _settle_s() -> float:
    try:
        return max(0.0, float(os.getenv("ERA_VAULT_VERIFY_SETTLE_S", "0.25")))
    except ValueError:
        return 0.25


def write_verified(path: Path, text: str) -> None:
    """Atomic write, then compare what is on disk to what we meant to write.

    Keeps the note's existing line endings (new notes use LF). Raises OSError — and
    VaultWriteMismatch when the read-back differs — so callers report failure.
    """
    path = Path(path)
    eol = "\n"
    try:
        if path.exists() and b"\r\n" in path.read_bytes():
            eol = "\r\n"
    except OSError:
        pass
    data = text.replace("\r\n", "\n").replace("\n", eol).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Obsidian/OneDrive holding the target open
                if attempt == 3:
                    raise
                time.sleep(0.15 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    for check in range(2):
        if check:
            time.sleep(_settle_s())
        if path.read_bytes() != data:
            raise VaultWriteMismatch(f"{path.name} changed on disk after it was written")


# ── helpers ─────────────────────────────────────────────────────────────────
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _brand_catalog() -> dict[str, list[str]] | None:
    try:
        import era_dispatch as ed

        brands = (ed.load_brands() or {}).get("brands") or {}
        return {k: list((v or {}).get("aliases") or []) for k, v in brands.items()}
    except Exception:  # noqa: BLE001 — scoped callers then fail closed
        return None


def _hidden(parts) -> bool:
    """Any dot-folder or dot-file (.obsidian, .trash, case variants, `.obsidian.`)."""
    return any(str(p).startswith(".") for p in parts)


def _inside_vault(path: Path) -> bool:
    """True when `path` really lives under the vault. A junction/symlink INSIDE the
    vault that points elsewhere resolves outside it, and must be neither read nor written."""
    try:
        return path.resolve().is_relative_to(cm_vault.VAULT_DIR.resolve())
    except OSError:
        return False


def _load_note_retry(path: Path):
    """cm_vault._load_note, retried briefly: on Windows a concurrent atomic replace of
    the same note makes one read raise PermissionError, which _load_note turns into None —
    that must not be reported as a missing note."""
    for attempt in range(4):
        note = cm_vault._load_note(path)
        if note is not None or not path.exists():
            return note
        time.sleep(0.05 * (attempt + 1))
    return None


def _readable(caller: Caller, note: cm_vault.VaultNote) -> bool:
    if caller.brands is None:
        return True
    if not note.folder:
        # The note's folder claim could not be derived (VAULT_DIR not canonical): the
        # isolation rule would silently fail OPEN. Fail closed instead.
        return False
    catalog = _brand_catalog()
    verdicts = []
    for key in caller.brands:
        others = {k: a for k, a in (catalog or {}).items() if k != key}
        aliases = (catalog or {}).get(key, [])
        verdicts.append(cm_vault._matches_brand(note, key, aliases, others))
    return any(v is True for v in verdicts) or (
        catalog is not None and all(v is None for v in verdicts)
    )


def _zone(path: Path) -> str:
    try:
        top = path.resolve().relative_to(cm_vault.VAULT_DIR.resolve()).parts[0]
    except (ValueError, IndexError):
        return "other"
    if top == _INBOX:
        return "inbox"
    if top == _GLOBAL:
        return "global"
    return "brand" if top in _BRAND_FOLDERS else "other"


def _locate(params: dict) -> Path | None:
    """Resolve `path` (vault-relative) or `title` to a note. None = no such note."""
    from actions import vault_writer as vw

    root = cm_vault.VAULT_DIR.resolve()
    raw = str(params.get("path") or "").strip()
    if raw:
        norm = raw.replace("\\", "/")
        if os.path.isabs(raw) or re.match(r"^[A-Za-z]:", raw) or norm.startswith("/"):
            raise _Refusal("PathRefused", "Paths must be relative to the vault.")
        if ".." in Path(norm).parts or _hidden(Path(norm).parts):
            raise _Refusal("PathRefused", "That path is outside the vault's notes.")
        p = (root / norm).resolve()
        if not p.is_relative_to(root):
            raise _Refusal("PathRefused", "That path resolves outside the vault.")
        if p.suffix.lower() != ".md":
            p = p.with_name(p.name + ".md")
        return p if p.is_file() else None
    title = str(params.get("title") or "").strip()
    if not title:
        raise _Refusal("MissingArgument", "Give a path or a title.")
    found = vw._find_note(title, vw._resolve_brand(params.get("brand")))
    if found is None:
        return None
    if not _inside_vault(found) or _hidden(found.resolve().relative_to(root).parts):
        raise _Refusal("PathRefused", "That note resolves outside the vault's notes.")
    return found


def _require_note(caller: Caller, params: dict) -> tuple[Path, cm_vault.VaultNote]:
    path = _locate(params)
    if path is None:
        raise _Refusal("NoteNotFound", "Note not found in the vault.")
    from actions import vault_writer as vw

    if any(path.stem.startswith(p) for p in vw._REGENERATED):
        raise _Refusal(
            "RegeneratedNote", "That note is rebuilt automatically; edits would be lost."
        )
    note = _load_note_retry(path)
    if note is None or not _readable(caller, note):
        # Same answer for "missing" and "not yours": no brand-existence oracle.
        raise _Refusal("NoteNotFound", "Note not found in the vault.")
    return path, note


def _check_write_zone(caller: Caller, path: Path) -> None:
    if _zone(path) not in caller.write_zones:
        raise _Refusal(
            "ZoneRefused",
            f"{caller.actor} may not write to {_zone(path)} notes; save it to the Inbox instead.",
        )


def _text(path: Path) -> str:
    if path.stat().st_size > _MAX_NOTE_BYTES:
        raise _Refusal("NoteTooLarge", "That note is too large to edit safely.")
    try:
        # utf-8-sig: a BOM would otherwise hide the frontmatter fence from every check
        # (a replace would silently drop the frontmatter). The rewrite carries no BOM.
        return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise _Refusal("NotUtf8", "That note is not valid UTF-8; edit it by hand.") from exc


def _check_content_size(content: str) -> None:
    if len(content) > _MAX_CONTENT_CHARS:
        raise _Refusal(
            "ContentTooLarge",
            f"That is {len(content):,} characters; split it into notes under "
            f"{_MAX_CONTENT_CHARS:,}.",
        )


def _rel(path: Path) -> str:
    return path.resolve().relative_to(cm_vault.VAULT_DIR.resolve()).as_posix()


# ── read / search ───────────────────────────────────────────────────────────
def _read(caller: Caller, params: dict) -> ToolResult:
    path, note = _require_note(caller, params)
    text = _text(path)
    match = _FM_RE.match(text)
    body = text[match.end() :] if match else text
    shown = body.strip()[:_MAX_READ_CHARS]
    return ToolResult.ok(
        message=f"{_rel(path)}\n\n{shown}",
        data={
            "rel": _rel(path),
            "sha256": _sha(text),
            "chars": len(body),
            "truncated": len(body.strip()) > _MAX_READ_CHARS,
            "frontmatter": note.frontmatter,
            "body": body.strip()[:20000],
        },
    )


def _search(caller: Caller, params: dict) -> ToolResult:
    query = str(params.get("query") or "").strip()
    tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 2]
    if not tokens:
        raise _Refusal("MissingArgument", "Give something to search for.")
    limit = max(1, min(int(params.get("limit") or 5), 10))
    # Voice questions are full of filler ("what is the pricing"); a note that merely says
    # "what is the" must not outrank the one NAMED Pricing. Stems (first 5 letters) let
    # "families" find "family". Still keyword search — not semantic.
    content_words = [t for t in tokens if t not in _STOPWORDS] or tokens
    stems = [t[:5] if len(t) > 5 else t for t in content_words]
    hits = []
    for note in cm_vault.load_vault():
        if _hidden(note.rel.split("/")) or not _inside_vault(note.path):
            continue  # .trash, .obsidian variants, junctions that point out of the vault
        if not _readable(caller, note):
            continue
        title, tagline, body = note.title.lower(), " ".join(note.tags).lower(), note.body.lower()
        score = sum(
            (5 if t in title else 0)
            + (2 if t in tagline or t in note.rel.lower() else 0)
            + min(body.count(t), 5)
            for t in stems
        )
        if score <= 0:
            continue
        tokens = stems
        first = min((body.find(t) for t in tokens if t in body), default=-1)
        start = max(0, first - 100) if first >= 0 else 0
        snippet = re.sub(r"\s+", " ", note.body[start : start + 240]).strip()
        hits.append((score, note.mtime, note, snippet))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    results = [
        {"rel": n.rel, "title": n.title, "score": s, "type": n.note_type, "snippet": sn}
        for s, _, n, sn in hits[:limit]
    ]
    if not results:
        return ToolResult.ok(message="No notes matched.", data={"results": []})
    lines = [f"Found {len(results)} note{'s' if len(results) != 1 else ''}."]
    lines += [f"- {r['rel']}: {r['snippet']}" for r in results]
    return ToolResult.ok(message="\n".join(lines), data={"results": results})


# ── create / append ─────────────────────────────────────────────────────────
def _create(caller: Caller, params: dict) -> ToolResult:
    from actions import vault_writer as vw

    title = str(params.get("title") or "").strip()
    content = str(params.get("content") or "").strip()
    if not title or not content:
        raise _Refusal("MissingArgument", "Creating a note needs a title and content.")
    _check_content_size(content)
    raw_brand = params.get("brand")
    brand = vw._resolve_brand(raw_brand)
    if raw_brand and not brand:
        raise _Refusal("UnknownBrand", f"Unrecognized brand '{raw_brand}'; not guessing.")
    if brand and caller.brands is not None and brand not in caller.brands:
        raise _Refusal("BrandOutOfScope", f"{caller.actor} is not scoped to that brand.")
    note_type = str(params.get("note_type") or "reference").strip().lower()
    if note_type not in ("reference", "guide", "plan"):
        note_type = "reference"
    # \w keeps accented and non-Latin letters (French/Arabic titles); stripping to ASCII
    # turned "Réunion équipe" into "Runion_quipe" and every Arabic title into "Note.md",
    # so a second one was refused as "exists" and then APPENDED to the first.
    safe = re.sub(r"[^\w \-]", "", title).strip().replace(" ", "_") or "Note"
    if any(safe.startswith(p) for p in vw._REGENERATED):
        raise _Refusal("RegeneratedNote", "That name is reserved for automatically rebuilt notes.")

    to_inbox = (
        ("brand" not in caller.write_zones) if brand else ("global" not in caller.write_zones)
    )
    folder = cm_vault.VAULT_DIR / _INBOX if to_inbox else vw._target_folder(brand)
    path = folder / f"{safe}.md"
    _check_write_zone(caller, path)
    front = vw._frontmatter_block(brand, note_type, title).replace(
        "\n---\n", f"\ncreated_by: {caller.actor}\n---\n", 1
    )
    body = front + content + "\n"
    if not _inside_vault(path):
        raise _Refusal("PathRefused", "That location resolves outside the vault.")
    rel = _rel(path)  # BEFORE the write: nothing may fail after bytes have landed
    with vault_write_lock():
        if path.exists():
            raise _Refusal("NoteExists", "A note with that name exists; use append.")
        write_verified(path, body)
    log_event(bridge_logger, "vault.bridge.created", "INFO", rel=rel, actor=caller.actor)
    return ToolResult.ok(message=f"Created {rel}.", data={"rel": rel, "sha256": _sha(body)})


def _append(caller: Caller, params: dict) -> ToolResult:
    content = str(params.get("content") or "").strip()
    if not content:
        raise _Refusal("MissingArgument", "Nothing to append.")
    _check_content_size(content)
    path, _ = _require_note(caller, params)
    _check_write_zone(caller, path)
    rel = _rel(path)  # BEFORE the write: nothing may fail after bytes have landed
    stamp = time.strftime("%Y-%m-%d")
    with vault_write_lock():
        existing = _text(path)  # read INSIDE the lock, or two appenders lose a line
        body = f"{existing.rstrip()}\n\n<!-- added {stamp} by {caller.actor} -->\n{content}\n"
        write_verified(path, body)
    log_event(bridge_logger, "vault.bridge.appended", "INFO", rel=rel, actor=caller.actor)
    return ToolResult.ok(message=f"Added to {rel}.", data={"rel": rel})


# ── replace (staged) ────────────────────────────────────────────────────────
@dataclass
class _Staged:
    path: Path
    base_sha: str
    new_text: str
    caller: Caller
    expires: float = field(default_factory=lambda: time.time() + _PLAN_TTL_S)


_STAGED: dict[str, _Staged] = {}
_STAGED_LOCK = threading.Lock()


def _replace_section(text: str, heading: str, body: str) -> str | None:
    """Swap the body under one `## heading`; None when the heading is absent.

    vault_writer's version lets `\\s*$` swallow the heading's newline, so replacing a
    section with identical text still rewrote the note. This one is line-anchored and
    idempotent, so a no-op replace is detectable and a staged diff shows only real change.
    """
    pattern = re.compile(rf"^##+[ \t]*{re.escape(heading)}[ \t]*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    rest = text[match.end() :]
    nxt = re.search(r"^##+\s", rest, re.MULTILINE)
    tail = rest[nxt.start() :] if nxt else ""
    return text[: match.end()] + "\n\n" + body.strip() + "\n" + (f"\n{tail}" if tail else "")


def _new_text(existing: str, content: str, section: str) -> str:
    if section:
        replaced = _replace_section(existing, section, content)
        if replaced is None:
            raise _Refusal("SectionNotFound", f"No '## {section}' section in that note.")
        return replaced
    match = _FM_RE.match(existing)
    head = match.group(0) if match else ""
    return head + content.strip() + "\n"


def stage_replace(caller: Caller, params: dict) -> tuple[str, str, str]:
    """Stage a replacement. Returns (plan_id, spoken summary, full unified diff)."""
    content = str(params.get("content") or "")
    if not content.strip():
        raise _Refusal("MissingArgument", "Replacing a note needs the new content.")
    _check_content_size(content)
    # No write-zone check here, on purpose: zones exist because create/append are free,
    # unreviewed writes. A replace is reviewed by the USER (they see the diff and must say
    # yes), so the zone rule would only make staged replace useless for scoped bots.
    # Brand isolation still applies: `_require_note` refuses another brand's note.
    path, _ = _require_note(caller, params)
    existing = _text(path)
    new = _new_text(existing, content, str(params.get("section") or "").strip())
    if new == existing:
        raise _Refusal("NoChange", "That would not change the note.")
    diff = "\n".join(
        difflib.unified_diff(
            existing.splitlines(), new.splitlines(), "current", "proposed", lineterm="", n=1
        )
    )
    changed = [
        ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]
    summary = _spoken_summary(_rel(path), changed)
    plan_id = secrets.token_hex(8)
    pinned = _pinned_plan_id()
    with _STAGED_LOCK:
        now = time.time()
        for pid in [k for k, v in _STAGED.items() if v.expires < now and k != pinned]:
            del _STAGED[pid]
        while len(_STAGED) >= _STAGED_MAX:
            # Never evict the plan the user's pending "yes" is about; spam would otherwise
            # turn their approval into NotStaged.
            victim = next((k for k in _STAGED if k != pinned), None)
            if victim is None:
                raise _Refusal("TooManyStaged", "Too many replacements are waiting; try later.")
            del _STAGED[victim]
        _STAGED[plan_id] = _Staged(path, _sha(existing), new, caller)
    return plan_id, summary, diff


# Sized under app.text_intents._SPOKEN_LIMIT (700) so the announced text is not cut again.
_SPOKEN_NEW = 380
_SPOKEN_OLD = 100


def _spoken_summary(rel: str, changed: list[str]) -> str:
    """What the user hears before saying yes: the NEW text first, then what it removes.

    The old summary led with the `-` lines and cut at 300 characters, so a large change
    filled the whole budget with deleted text and the text going IN was never spoken —
    exactly the part an injection would hide in. Truncation is stated, never silent.
    """
    added = [ln[1:].strip() for ln in changed if ln[0] == "+" and ln[1:].strip()]
    removed = [ln[1:].strip() for ln in changed if ln[0] == "-" and ln[1:].strip()]
    n_in = sum(1 for ln in changed if ln[0] == "+")
    new_text = " / ".join(added)
    old_text = " / ".join(removed)
    parts = [f"Replace in {rel}: {len(changed) - n_in} lines out, {n_in} in."]
    if new_text:
        shown = new_text[:_SPOKEN_NEW]
        cut = (
            f" (first {_SPOKEN_NEW} of {len(new_text)} characters)"
            if len(new_text) > _SPOKEN_NEW
            else ""
        )
        parts.append(f'New text{cut}: "{shown}".')
    else:
        parts.append("It adds no new text.")
    if old_text:
        cut = "..." if len(old_text) > _SPOKEN_OLD else ""
        parts.append(f'It removes: "{old_text[:_SPOKEN_OLD]}{cut}".')
    return " ".join(parts)


def _pinned_plan_id() -> str:
    """The plan id the currently pending confirmation refers to, if any."""
    try:
        from core.pending_confirmation import get_shared_store

        pending = get_shared_store().peek()
        plan = (pending.parameters or {}).get("_approved_plan") if pending else None
        return str(plan.get("plan_id")) if isinstance(plan, dict) else ""
    except Exception:  # noqa: BLE001 — pinning is a courtesy; staging must not fail on it
        return ""


def apply_staged(plan: dict | None) -> ToolResult:
    """Apply a plan the user approved. Single use; refuses on any drift."""
    plan_id = str(plan.get("plan_id") or "") if isinstance(plan, dict) else ""
    with _STAGED_LOCK:
        staged = _STAGED.pop(plan_id, None)
    if staged is None or staged.expires < time.time():
        raise _Refusal("NotStaged", "No staged replacement to apply; stage it again.")
    with vault_write_lock():
        current = _text(staged.path)
        if _sha(current) != staged.base_sha:
            raise _Refusal("Conflict", "The note changed since it was staged; nothing written.")
        write_verified(staged.path, staged.new_text)
    log_event(
        bridge_logger,
        "vault.bridge.replaced",
        "INFO",
        rel=_rel(staged.path),
        actor=staged.caller.actor,
    )
    return ToolResult.ok(
        message=f"Replaced content in {_rel(staged.path)}.",
        data={"rel": _rel(staged.path), "sha256": _sha(staged.new_text)},
    )


def stage(parameters: dict, caller: Caller | None = None) -> ToolResult:
    """Public staging for crew code: a ToolResult carrying the plan and the texts a
    reviewer needs (original, proposed, the note's hash and the unified diff)."""
    try:
        plan_id, summary, diff = stage_replace(caller or Caller(), dict(parameters or {}))
    except _Refusal as exc:
        return ToolResult.fail(message=exc.message, error_type=exc.error_type)
    except OSError as exc:
        return ToolResult.fail(message=f"Vault I/O problem: {exc}", error_type="VaultIOError")
    with _STAGED_LOCK:
        staged = _STAGED[plan_id]
    return ToolResult.ok(
        message=summary,
        data={
            "plan_id": plan_id,
            "summary": summary,
            "diff": diff,
            "rel": _rel(staged.path),
            "base_sha256": staged.base_sha,
            "current_sha256": _sha(_text(staged.path)),
            "proposed": staged.new_text,
            "original": _text(staged.path),
        },
    )


def read_text_now(rel: str) -> str:
    """The note's text as it is on disk right now ("" if it cannot be read). Lets a
    reviewer check a staged change against the note AFTER a slow review, not before."""
    try:
        return _text(cm_vault.VAULT_DIR / rel)
    except (OSError, _Refusal):
        return ""


def discard_staged(plan_id: str) -> bool:
    """Drop a staged plan that will not be offered for approval."""
    with _STAGED_LOCK:
        return _STAGED.pop(str(plan_id), None) is not None


def plan_replace_preview(parameters: dict) -> tuple[str, dict]:
    """confirmation_preview builder: stage now, carry only the plan id."""
    try:
        plan_id, summary, _ = stage_replace(Caller(), parameters)
    except _Refusal as exc:
        return f"I can't stage that: {exc.message}", {}
    return summary, {"_approved_plan": {"plan_id": plan_id}}


# ── entry point ─────────────────────────────────────────────────────────────
_HANDLERS = {"read": _read, "search": _search, "create": _create, "append": _append}


def run(parameters: dict, caller: Caller | None = None) -> ToolResult:
    """Tool entry point. Never raises; every failure is ToolResult.fail(error_type=)."""
    caller = caller or Caller()
    params = dict(parameters or {})
    action = str(params.get("action") or "").strip().lower()
    try:
        if action not in ACTIONS:
            raise _Refusal("UnknownAction", f"Unsupported vault_bridge action '{action}'.")
        if not cm_vault.vault_available():
            raise _Refusal("VaultUnavailable", "Vault unreachable; nothing was saved.")
        if action == "replace":
            return apply_staged(params.get("_approved_plan"))
        return _HANDLERS[action](caller, params)
    except _Refusal as exc:
        return ToolResult.fail(message=exc.message, error_type=exc.error_type, action_name=action)
    except VaultLockUnavailable as exc:
        log_event(bridge_logger, "vault.bridge.lock_unavailable", "WARNING", error=str(exc))
        return ToolResult.fail(
            message=f"Vault is busy; nothing was written. ({exc})",
            error=str(exc),
            error_type="LockUnavailable",
            action_name=action,
        )
    except VaultWriteMismatch as exc:
        log_event(bridge_logger, "vault.bridge.clobbered", "ERROR", error=str(exc))
        return ToolResult.fail(
            message="Write did not stick: the note on disk differs from what was written.",
            error=str(exc),
            error_type="WriteVerificationFailed",
            action_name=action,
        )
    except OSError as exc:
        log_event(bridge_logger, "vault.bridge.io_failed", "WARNING", error=str(exc))
        return ToolResult.fail(
            message=f"Vault I/O problem: {exc}", error_type="VaultIOError", action_name=action
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        log_event(bridge_logger, "vault.bridge.crashed", "ERROR", error=repr(exc))
        return ToolResult.fail(
            message=f"vault_bridge failed internally: {type(exc).__name__}",
            error=repr(exc),
            error_type="BridgeError",
            action_name=action,
        )
