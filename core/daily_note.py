r"""
daily_note.py — Era's entry in the shared daily log.

WHY THIS EXISTS
  Era already writes what a run did into `Areas/<Brand>/Era_Run_Log_<brand>.md`
  (see cm_vault.append_run_log). That answers "what has Era done for Delta
  Cuisine", and it is the right shape for a brand's history.

  It does not answer "what happened today" — across both brands, across the
  scheduled runs and the ones ordered by voice. No per-brand log can: the day is
  split across two of them, in the order each brand happened to run, and nothing
  joins them back up. That is the view that goes missing between sessions.

  The shape is jaredrhod's daily note (github.com/jaredrhod/ai-memory-vault): one
  dated file per day, appended to rather than overwritten, with an `## Index`
  block at the top so a day with six entries stays scannable instead of becoming
  a wall of prose. Era writes hers into her own vault.

WHERE IT WRITES, AND WHY IT COSTS NOTHING
  `era_brain/Logs/`, inside Era's own vault. `Logs/` is listed in
  cm_vault._PARA_ORDER but deliberately NOT in _BRIEF_FOLDERS, which means it is
  indexed and findable but never rendered into a brief.

  That distinction is the whole design. A day log is a record of what HAPPENED;
  a brief carries instructions about what to MAKE next. Feeding the first into
  the second would spend the brand's character budget — already at 85% for Delta
  Cuisine — narrating history that no upcoming post needs, and starve out a
  standing rule to do it.

DEGRADE, NEVER RAISE
  Same contract as every other vault call. A missing vault, a OneDrive stall, a
  locked file: all cost a log line, never the CM job that just succeeded.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from core import cm_vault
from core.logger import log_event, setup_logger

daily_logger = setup_logger("era_one.daily_note")

# Era's own vault — the same root cm_vault reads, so there is one vault and one
# ERA_VAULT_DIR to point at a different machine, not two that can drift apart.
_DAILY_DIR = "01 - Daily Notes"
_TEMPLATE = "Daily Note Template.md"


def _vault_root() -> Path:
    """Read cm_vault.VAULT_DIR live rather than binding it at import.

    The tests (and a second machine) redirect the vault by setting
    cm_vault.VAULT_DIR; a module-level copy taken at import time would keep
    pointing at the real vault and write test data into it.
    """
    return cm_vault.VAULT_DIR


# A day accumulates several runs, so entries are numbered rather than merged —
# the numbering is read back off the file, never tracked in memory, so a restart
# mid-day continues the sequence instead of restarting it at 1.
_SESSION_RE = re.compile(r"^## Session (\d+)", re.MULTILINE)
_INDEX_HEADING = "## Index"


def vault_available() -> bool:
    try:
        return (_vault_root() / _DAILY_DIR).is_dir()
    except OSError:
        return False


def _month_folder(when: datetime) -> str:
    """`07 - July 2026` — the convention the vault already uses."""
    return f"{when.month:02d} - {when.strftime('%B %Y')}"


def _local_time(when: datetime) -> str:
    """`3:58 PM`. The vault's rule is local time, never UTC, and no zero-padding.

    Built by hand rather than with a strftime flag because the no-pad flag is
    platform-split (`%-I` on POSIX, `%#I` on Windows) and Era runs on Windows
    while the tests are meant to be portable.
    """
    hour = when.hour % 12 or 12
    return f"{hour}:{when.strftime('%M %p')}"


def note_path(when: datetime) -> Path:
    """Where today's note lives, preferring one that ALREADY exists.

    Canonically `Logs/07 - July 2026/2026-07-31.md`, but a note sitting flat in
    `Logs/` is honoured where it is. Writing blind to the canonical path would
    create a SECOND file for a day that already had one, and two partial records
    of the same day — each missing what the other has — is the one failure a log
    like this cannot survive.
    """
    name = f"{when.strftime('%Y-%m-%d')}.md"
    root = _vault_root() / _DAILY_DIR
    canonical = root / _month_folder(when) / name
    if canonical.is_file():
        return canonical
    flat = root / name
    if flat.is_file():
        return flat
    return canonical


def _expand_placeholders(text: str, when: datetime) -> str:
    """Fill Obsidian's `{{...}}` template tokens.

    Obsidian expands these through its Templates plugin, at the moment a human
    clicks "insert template". Nothing expands them when a file is written
    directly, so copying the template verbatim produces a note literally titled
    `# {{Day of week}}, {{Month}} {{Day}}, {{Year}}` — which is what the first
    run of this module did. Anything still unrecognized is stripped rather than
    left in place: a visible `{{...}}` in a real note reads as a broken file.
    """
    day = str(when.day)
    subs = {
        "{{date:YYYY-MM-DD}}": when.strftime("%Y-%m-%d"),
        "{{Day of week}}": when.strftime("%A"),
        "{{Month}}": when.strftime("%B"),
        "{{Day}}": day,
        "{{Year}}": str(when.year),
        "{{date}}": when.strftime("%Y-%m-%d"),
        "{{time}}": _local_time(when),
        "{{topic}}": "",
    }
    for token, value in subs.items():
        text = text.replace(token, value)
    return re.sub(r"\{\{[^}]*\}\}", "", text)


def _read_template() -> str | None:
    try:
        text = (_vault_root() / _DAILY_DIR / _TEMPLATE).read_text(encoding="utf-8")
    except OSError:
        return None
    # Strip the template's own explanatory HTML comments and its placeholder
    # Session 1 block — a real session is substituted below.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    head, sep, _ = text.partition("## Session 1")
    text = head if sep else text
    # Drop the template's EXAMPLE index bullet. It is instructional filler
    # ("**Session topic name** — one-line summary..."), and carrying it into a
    # real note leaves a permanent fake entry at the top of the day.
    text = re.sub(r"^- \*\*Session topic name\*\*.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"


def _new_note_body(when: datetime) -> str:
    """A fresh daily note. Uses the vault's own template when it is readable, so
    a change to the template propagates instead of being silently overridden by a
    copy hard-coded here."""
    template = _read_template()
    if template:
        return _expand_placeholders(template, when)
    # Fallback only — the template is missing or unreadable. Matches its shape so
    # a note written on a bad day still merges cleanly with the rest.
    return (
        "---\nstatus: active\nproject: personal\ntype: log\n"
        f"created: {when.strftime('%Y-%m-%d')}\n---\n\n"
        f"# {when.strftime('%A, %B')} {when.day}, {when.year}\n\n"
        f"{_INDEX_HEADING}\n\n"
    )


def _next_session_number(text: str) -> int:
    found = [int(n) for n in _SESSION_RE.findall(text)]
    return (max(found) + 1) if found else 1


def append_session(
    topic: str,
    *,
    done: list[str] | None = None,
    in_progress: list[str] | None = None,
    decisions: list[str] | None = None,
    notes_touched: list[str] | None = None,
    when: datetime | None = None,
) -> Path | None:
    """Add one session block to today's day log. Returns the path, or None when
    the vault is unreachable. Never raises.

    Appends — never overwrites. A second run today lands as the next
    `## Session N` underneath the first, and adds its line to the `## Index`
    block so a busy day stays scannable instead of becoming a wall of prose.
    """
    if not vault_available():
        log_event(
            daily_logger, "daily.vault_missing", "WARNING", vault=str(_vault_root() / _DAILY_DIR)
        )
        return None

    when = when or datetime.now().astimezone()
    path = note_path(when)

    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as exc:
        log_event(daily_logger, "daily.read_failed", "WARNING", path=str(path), error=str(exc))
        return None

    body = existing or _new_note_body(when)
    number = _next_session_number(body)

    def _bullets(items: list[str] | None) -> str:
        return "".join(f"- {i.strip()}\n" for i in (items or []) if i and i.strip()) or "- \n"

    session = (
        f"\n## Session {number} — {_local_time(when)}: {topic.strip()}\n\n"
        f"### What Got Done\n{_bullets(done)}\n"
        f"### What's Still In Progress\n{_bullets(in_progress)}\n"
        f"### Decisions Made\n{_bullets(decisions)}\n"
        f"### Notes Touched\n{_bullets(notes_touched)}\n"
    )

    # The Index block is written BEFORE the body, per the template's own
    # instruction — it is what makes a day with six entries readable.
    index_line = f"- **{topic.strip()}** — {(done or ['run completed'])[0].strip()}\n"
    if _INDEX_HEADING in body:
        head, sep, rest = body.partition(_INDEX_HEADING)
        # Land at the END of the existing index list — after the last bullet,
        # before whatever section follows — so the index stays in the same order
        # as the sessions below it.
        lines = rest.split("\n")
        cut = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
        while cut > 0 and not lines[cut - 1].strip():
            cut -= 1
        # `cut` can reach 0 when the index is still empty (a note created from the
        # template, whose example bullet was stripped). Inserting there would weld
        # the first bullet onto the heading itself — `## Index- **Delta...`.
        # One blank line always separates the heading from its list.
        if cut == 0:
            lines.insert(0, "")
            cut = 1
        lines.insert(cut, index_line.rstrip("\n"))
        body = head + sep + "\n".join(lines)
    else:
        body = body.rstrip() + f"\n\n{_INDEX_HEADING}\n\n{index_line}"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.rstrip() + "\n" + session, encoding="utf-8")
    except OSError as exc:
        log_event(daily_logger, "daily.write_failed", "WARNING", path=str(path), error=str(exc))
        return None

    log_event(
        daily_logger, "daily.session_logged", "INFO", path=str(path), session=number, topic=topic
    )
    return path


def doctor() -> dict:
    """Reachability summary for tools/era_doctor.py."""
    ok = vault_available()
    today = note_path(datetime.now().astimezone())
    return {
        "daily_dir": str(_vault_root() / _DAILY_DIR),
        "exists": ok,
        "template": (_vault_root() / _DAILY_DIR / _TEMPLATE).is_file() if ok else False,
        "today_note": str(today) if ok else "",
        "today_exists": today.is_file() if ok else False,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(doctor(), ensure_ascii=False, indent=2))
