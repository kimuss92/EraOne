r"""
vault_writer.py — how Era writes into her own Obsidian vault, live, in conversation.

WHY THIS EXISTS
  Era could already read the vault (core/cm_vault.py) and the headless CM lane
  could append run logs to it. What she could NOT do was write to it while
  talking to you. Her memory tools — save_memory, record_feedback,
  manage_preference — all write to JSON stores under memory/, none of which is
  the vault.

  That gap had a visible symptom: asked to interview her user and fill in the
  Profile section of VAULT-INDEX.md, Era conducted the whole interview and then
  had nowhere to put the answers. They were spoken, acknowledged, and lost.

  The vault is meant to be the second brain — "anything I tell you must be
  saved." A second brain the agent can read but not write is a filing cabinet
  she is standing outside of.

WHY IT IS NOT JUST open()
  Everything that decides whether a note reaches a CM brief lives in frontmatter,
  and getting it wrong fails SILENTLY: a `brand:` typo excludes a note from every
  brief; a missing `type:` drops it to the bottom of the ranking behind every
  campaign note; a note dropped in the wrong folder is read by nothing. So this
  module owns the frontmatter and the placement, and the model only supplies
  content plus a couple of coarse hints.

APPEND BEFORE CREATE
  The default is to add to an existing note, because that is the vault's own rule
  (fewer, fuller notes) and because a model asked to "save this" will otherwise
  spawn a new near-duplicate note every time. Creating is a deliberate act here,
  not the fallback.

SAFETY
  Never deletes. Never overwrites a whole note — the only in-place edit is
  replacing the body of ONE named section, which is what the Profile case needs.
  Refuses outright on Era's own regenerated notes (run logs, learned preferences,
  vault health): those are rebuilt from other sources, so a hand-written line in
  them is guaranteed to be destroyed later, and silently.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from core import cm_vault
from core.logger import log_event, setup_logger
from core.utils import get_memory_dir

writer_logger = setup_logger("era_one.vault_writer")

# Notes Era regenerates from another source. A line written here would be
# overwritten by the next nightly pass, so refusing is kinder than accepting.
_REGENERATED = ("Era_Run_Log", "Era_Learned_Preferences", "Era_Vault_Health")

# Over this, cm_vault truncates the note mid-sentence in every brief, silently.
# Warn at the point of writing, where it is still cheap to split.
_SOFT_LIMIT = cm_vault._MAX_NOTE_CHARS

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _slug(brand_key: str | None) -> str:
    """jaredrhod's `project:` value. `meta` when the note is not a brand's."""
    return brand_key.replace("_", "-") if brand_key else "meta"


def _target_folder(brand_key: str | None) -> Path:
    return cm_vault._brand_folder(brand_key) if brand_key else cm_vault.VAULT_DIR / "04 - Global"


def _resolve_brand(raw: str | None) -> str | None:
    """Accept 'Delta Cuisine', 'delta', 'delta_cuisine' — or nothing.

    Goes through era_dispatch.resolve_brand so a spoken brand name lands on the
    same key the CM routes on. An UNRECOGNIZED name returns None rather than
    guessing: a wrong brand here writes one brand's strategy into the other's
    folder, which is the failure this whole subsystem exists to prevent.
    """
    name = (raw or "").strip()
    if not name:
        return None
    try:
        import era_dispatch as ed

        return ed.resolve_brand(name)
    except Exception:
        return None


def _find_note(title: str, brand_key: str | None) -> Path | None:
    """An existing note by title, searched brand-folder first then whole vault.

    Matching is case- and separator-insensitive because the model will say
    "delta social strategy" for a file named `Delta_Social_Strategy.md`, and
    failing to match would create a duplicate rather than append.
    """

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    want = norm(Path(title).stem)
    if not want:
        return None
    roots = [_target_folder(brand_key), cm_vault.VAULT_DIR]
    for root in roots:
        try:
            for p in sorted(root.rglob("*.md")):
                if ".obsidian" in p.parts:
                    continue
                if norm(p.stem) == want:
                    return p
        except OSError:
            continue
    return None


def _frontmatter_block(brand_key: str | None, note_type: str, title: str) -> str:
    lines = [
        "---",
        "status: active",
        f"project: {_slug(brand_key)}",
        f"type: {note_type}",
    ]
    if brand_key:
        lines.append(f"brand: {brand_key}")
    lines += [
        f"created: {datetime.now().astimezone().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    return "\n".join(lines)


def _replace_section(text: str, heading: str, body: str) -> str | None:
    """Swap the body under one `## heading`, leaving the rest untouched.

    Returns None when the heading is not there, so the caller can fall back to
    appending rather than silently writing nothing. Anchored to line start — a
    heading NAME can legitimately appear in prose, and matching that instead
    would splice the new text into the middle of a sentence. (This exact bug ate
    the run log's `## Run log` heading once already.)
    """
    pattern = re.compile(rf"^##+\s*{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    rest = text[match.end() :]
    nxt = re.search(r"^##+\s", rest, re.MULTILINE)
    tail = rest[nxt.start() :] if nxt else ""
    return text[: match.end()] + "\n\n" + body.strip() + "\n\n" + tail


# ── Offline spool ───────────────────────────────────────────────────────────
# The vault lives on OneDrive, so `vault_available()` returning False is a
# routine transient (sync stall, path briefly detached), not a permanent
# condition. This module used to answer that by DISCARDING the note: Era said
# "I can't reach the vault, nothing was saved" and whatever the user had just
# dictated was gone. For a subsystem whose entire premise is "anything I tell
# you must be saved", losing content to a five-second sync hiccup is the worst
# available outcome.
#
# So an unreachable vault (or a failed write) parks the note here instead, and
# the next successful save flushes the backlog in order. Nothing is ever
# dropped silently; Era says she is holding it.
_SPOOL_NAME = "vault_pending.jsonl"
# Bounded: a vault offline for a long time must not grow an unbounded file.
_SPOOL_MAX = 200


def _spool_path() -> Path:
    """Resolved per call, not at import — the test suite sets ERA_MEMORY_DIR."""
    return get_memory_dir() / _SPOOL_NAME


def _spool(parameters: dict) -> bool:
    """Park one note for later. Best effort — never raises into the tool."""
    try:
        path = _spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        held = _read_spool()
        held.append(
            {
                "parameters": {
                    k: parameters.get(k)
                    for k in ("content", "title", "brand", "note_type", "section")
                },
                "held_at": datetime.now().astimezone().isoformat(),
            }
        )
        held = held[-_SPOOL_MAX:]
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in held) + "\n",
            encoding="utf-8",
        )
        log_event(writer_logger, "vault.note_held", "WARNING", queued=len(held))
        return True
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        log_event(writer_logger, "vault.hold_failed", "ERROR", error=str(exc))
        return False


def _read_spool() -> list[dict]:
    try:
        path = _spool_path()
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # one bad line must not strand the rest
            if isinstance(item, dict) and isinstance(item.get("parameters"), dict):
                out.append(item)
        return out
    except Exception:  # noqa: BLE001
        return []


def _flush_spool() -> int:
    """Replay held notes, oldest first. Returns how many landed.

    Stops at the first failure and keeps the remainder queued, so a vault that
    goes away again mid-flush loses nothing and preserves ordering.
    """
    held = _read_spool()
    if not held:
        return 0
    written = 0
    try:
        for item in list(held):
            ok, _ = _perform_save(item["parameters"])
            if not ok:
                break
            held.pop(0)
            written += 1
        path = _spool_path()
        if held:
            path.write_text(
                "\n".join(json.dumps(i, ensure_ascii=False) for i in held) + "\n",
                encoding="utf-8",
            )
        elif path.exists():
            path.unlink()
    except Exception as exc:  # noqa: BLE001
        log_event(writer_logger, "vault.flush_failed", "WARNING", error=str(exc))
    if written:
        log_event(writer_logger, "vault.notes_flushed", "INFO", count=written)
    return written


def _perform_save(parameters: dict) -> tuple[bool, str]:
    """One vault write. Returns (landed, line-to-say).

    Split out of save_note so the offline spool can replay a held note through
    exactly the same path that wrote it the first time — a replay that took a
    different route would be a second implementation to keep in sync, and the
    frontmatter/placement rules are precisely what must not drift.

    `landed` False means the note is NOT in the vault and the caller should
    hold it. A REFUSAL (unknown brand, regenerated note) reports True: it is a
    decision, not a failure, and retrying it forever would never succeed.
    """
    content = (parameters.get("content") or "").strip()
    if not content:
        return True, "There was nothing to save, sir."

    if not cm_vault.vault_available():
        return False, ""

    raw_brand = parameters.get("brand")
    brand_key = _resolve_brand(raw_brand)
    if raw_brand and not brand_key:
        # Guessing would file it under the wrong brand, which is the one failure
        # mode worth refusing over.
        return True, (
            f"I don't recognize the brand \"{raw_brand}\", sir, and I won't guess — "
            "tell me Delta Cuisine or Ceram Quartz and I'll file it."
        )

    note_type = (parameters.get("note_type") or "reference").strip().lower()
    if note_type not in ("reference", "guide", "plan"):
        note_type = "reference"

    title = (parameters.get("title") or "").strip()
    section = (parameters.get("section") or "").strip()

    path = _find_note(title, brand_key) if title else None
    created = False

    if path is None:
        if not title:
            return True, "Which note should that go in, sir? I need a title to file it under."
        safe = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip().replace(" ", "_") or "Note"
        path = _target_folder(brand_key) / f"{safe}.md"
        created = True

    if any(path.stem.startswith(p) for p in _REGENERATED):
        return True, (
            f"'{path.stem}' is one I rebuild automatically, sir — anything written there "
            "would be overwritten tonight. Tell me a different note and it will stick."
        )

    try:
        if created:
            body = _frontmatter_block(brand_key, note_type, title) + content + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        else:
            existing = path.read_text(encoding="utf-8")
            replaced = _replace_section(existing, section, content) if section else None
            if replaced is not None:
                body = replaced
            else:
                stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
                lead = f"\n\n## {section}\n\n" if section else f"\n\n<!-- added {stamp} -->\n"
                body = existing.rstrip() + lead + content + "\n"
            path.write_text(body, encoding="utf-8")
    except OSError as exc:
        log_event(writer_logger, "vault.write_failed", "WARNING", path=str(path), error=str(exc))
        # Not a decision — the vault was there and the write still failed
        # (sync lock, permissions). Held rather than lost.
        return False, ""

    log_event(
        writer_logger,
        "vault.note_saved",
        "INFO",
        path=str(path),
        brand=brand_key or "global",
        note_type=note_type,
        created=created,
        chars=len(content),
    )

    where = path.relative_to(cm_vault.VAULT_DIR).as_posix()
    verb = "Created" if created else ("Updated" if section else "Added to")
    note = f"{verb} {where}, sir."

    # Warn at the point of writing, while splitting is still cheap. Past the cap
    # the tail is cut from every brief and nothing says so.
    try:
        grown = len(path.read_text(encoding="utf-8"))
        if grown > _SOFT_LIMIT and cm_vault._load_note(path).briefable:
            note += (
                f" It's now {grown} characters, past the {_SOFT_LIMIT} cap — the tail "
                "will be cut from briefs until it's split."
            )
    except Exception:
        pass
    return True, note


def save_note(parameters: dict, player=None) -> str:
    """Tool-facing save: flush anything held, write this note, hold on failure.

    parameters:
      content   (required) what to write, already in the user's own words.
      title     which note. Existing -> appended to; new -> created.
      brand     'delta cuisine' / 'ceram quartz' / omitted for a global note.
      note_type reference | guide | plan   (default reference — a thing the user
                just told Era is a standing fact far more often than it is a
                campaign, and reference is what ranks first in a brief.)
      section   a `## heading` to replace instead of appending at the end.
    """
    if not (parameters.get("content") or "").strip():
        return "There was nothing to save, sir."

    # Drain the backlog first, so held notes land in the order they were given
    # and this note does not jump the queue.
    flushed = _flush_spool() if cm_vault.vault_available() else 0

    landed, note = _perform_save(parameters)
    if landed:
        if flushed:
            held_note = f" I also filed {flushed} note{'s' if flushed != 1 else ''} I was holding."
            note += held_note
        return note

    if _spool(parameters):
        return (
            "I can't reach the vault just now, sir — I'm holding that note and "
            "will file it the moment it's back."
        )
    return f"I can't reach the vault at {cm_vault.VAULT_DIR}, sir — nothing was saved."


def read_note(parameters: dict, player=None) -> str:
    """Read a vault note back, so Era can quote what she already knows."""
    title = (parameters.get("title") or "").strip()
    if not title:
        return "Which note, sir?"
    if not cm_vault.vault_available():
        return f"I can't reach the vault at {cm_vault.VAULT_DIR}, sir."

    path = _find_note(title, _resolve_brand(parameters.get("brand")))
    if path is None:
        return f"There's no note called '{title}' in the vault, sir."
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"I couldn't read that note, sir — {exc}"

    match = _FM_RE.match(text)
    body = text[match.end() :] if match else text
    where = path.relative_to(cm_vault.VAULT_DIR).as_posix()
    return f"{where}:\n\n{body.strip()[:4000]}"


def vault_note(parameters: dict, player=None) -> str:
    """Tool entry point. `action` is save (default) or read."""
    action = (parameters.get("action") or "save").strip().lower()
    if action == "read":
        return read_note(parameters, player)
    return save_note(parameters, player)
