r"""
cm_vault.py — read/write bridge between Era's community-management lane and the
Obsidian vault at obsidian_era/era_brain.

WHY THIS EXISTS
  Before this module, `era_dispatch.build_brief()` rendered a brief purely from
  brands.yaml. That made the CM stateless: it could not read the standing
  strategy notes, the material libraries or the campaign history you keep in
  Obsidian, and nothing a run learned ever came back. Every job restarted from
  zero and you re-explained the same preferences.

  The vault is the durable, human-editable half of Era's brand memory —
  brands.yaml holds the machine-critical routing (account ids, footers, gates),
  the vault holds the *judgement* (strategy, what worked, what you rejected).
  This module reads the second half into the brief and writes what a run learned
  back into it, so the two accumulate instead of drifting.

VAULT LAYOUT (jaredrhod AI Memory Vault, migrated 2026-07-31)
  era_brain/
    VAULT-INDEX.md      the operating manual
    00 - Inbox          unfiled capture      — never briefed
    01 - Daily Notes    one dated log a day  — never briefed
    02 - Delta Cuisine  everything that brand owns: charter, rules, campaigns
    03 - Ceram Quartz   same
    04 - Global         rules true for BOTH brands
    05 - Archive        finished work        — never briefed
    06 - Resources      reference that is not brand knowledge

  See _NUMBERED_FOLDERS / _BRIEF_FOLDERS / _TYPE_RANK below for the three
  decisions that layout implies, and VaultNote.briefable for the single gate
  every one of them funnels through.

  Notes are matched to a brand by frontmatter `brand:`, by `project:`, by the
  numbered folder, by a `tags:` entry, or by the brand's aliases in the filename.
  A note with none of those is global and reaches every brand.

SAFETY
  Every read is defensive: a missing vault (OneDrive offline, path moved) must
  degrade to "no vault context" and NEVER raise into the dispatch path — a CM
  job that would otherwise run fine must not die because a note is unreadable.
  Writes only ever append to Era's own log notes under a dedicated heading; this
  module never edits or deletes a note you wrote by hand. (Era's live write path
  for everything else is actions/vault_writer.py, which has the same rule.)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

from core.logger import log_event, setup_logger

vault_logger = setup_logger("era_one.cm_vault")

# The vault root. Overridable so a second machine (or a test) can point elsewhere
# without editing code. Default is the vault the user actually keeps.
DEFAULT_VAULT = Path.home() / "OneDrive" / "Documents" / "obsidian_era" / "era_brain"
VAULT_DIR = Path(os.getenv("ERA_VAULT_DIR", str(DEFAULT_VAULT)))

# ── VAULT LAYOUT ────────────────────────────────────────────────────────────
# The vault uses jaredrhod's AI Memory Vault layout (numbered folders, one per
# project, github.com/jaredrhod/ai-memory-vault) — migrated 2026-07-31 from the
# earlier bare-PARA one. BOTH are still recognized, deliberately: the old names
# are what every pre-migration note and test uses, and a loader that only
# understood the new shape would have made the migration a flag day with no way
# back. Two layouts cost one dict; a failed migration costs the CM's whole memory.
#
# Note the numbering is NOT authority. `02 - Delta Cuisine` does not outrank
# `03 - Ceram Quartz`; they are peers, and a brand only ever sees its own notes
# plus the global ones. Authority comes from `type:` (below).
_NUMBERED_FOLDERS = (
    "00 - Inbox",
    "01 - Daily Notes",
    "02 - Delta Cuisine",
    "03 - Ceram Quartz",
    "04 - Global",
    "05 - Archive",
    "06 - Resources",
    "07 - Brico Kitchen",  # added 2026-09-25 (ads-only brand)
)
_LEGACY_PARA_FOLDERS = ("Areas", "Resources", "Projects", "Archive", "Logs")
_PARA_ORDER = _NUMBERED_FOLDERS + _LEGACY_PARA_FOLDERS

# Folders whose notes may reach a brief at all.
#
# Excluded, and why each one:
#   05 - Archive / Archive  finished work. Indexed so "what did we post in May"
#                           is answerable, but a stale campaign in a live brief
#                           is how a CM re-runs a finished promo.
#   01 - Daily Notes/Logs   a record of what HAPPENED. A brief carries
#                           instructions about what to MAKE next.
#   00 - Inbox              unfiled capture. Nothing here has been reviewed yet,
#                           and unreviewed text must never steer a live post.
_BRIEF_FOLDERS = (
    "02 - Delta Cuisine",
    "03 - Ceram Quartz",
    "07 - Brico Kitchen",
    "04 - Global",
    "06 - Resources",
    # legacy
    "Areas",
    "Resources",
    "Projects",
)

# ── AUTHORITY ───────────────────────────────────────────────────────────────
# What decides which note wins the character budget.
#
# It used to be the top-level folder: Areas > Resources > Projects. That worked
# only because the folder name doubled as the note's KIND, and it stops working
# the moment folders are per-brand (everything Delta Cuisine owns lives in
# `02 - Delta Cuisine`, rules and campaigns alike).
#
# So rank by what the note IS, which jaredrhod's own frontmatter already records:
#
#   reference  a standing rule or library — brand assets, material lists,
#              operating principles. The thing project notes are instances OF.
#   guide      a runbook or prompt system. How to do the thing, reusably.
#   plan       a campaign, batch or weekly plan. One instance, with an end date.
#   index      a folder map. Navigation, not content — never briefed.
#   log        dated history — never briefed.
#
# The invariant this preserves is the one that has already bitten twice: a rule
# must never lose its slot in the brief to an example of itself.
_TYPE_RANK = {"reference": 0, "guide": 1, "plan": 2, "": 3}
_NEVER_BRIEFED_TYPES = frozenset({"index", "log"})

# A brief has a budget. Past roughly this much vault text the signal-to-noise of
# the brief collapses and the headless run starts ignoring the gates instead.
# Raised from 14000 to 19000 on 2026-07-20: Delta Cuisine alone now has 5
# legitimate Areas+Resources notes (wardrobe prompt system, brand assets,
# operating principles) that together with the 2 global production-standard
# notes run to ~26000 chars — comfortably fitting all of Areas+Resources for
# both brands needs headroom well past the old cap. Still bounded, and the
# `continue`-not-`break` behavior below means going over just means a
# lower-priority note gets skipped, never a silent full-context collapse.
# RAISED 2026-07-22 (Karim): measured real usage was already past the old
# 19000 cap — Delta Cuisine alone needs ~48700 chars to fit every current
# note untruncated, silently starving Resources notes (a wardrobe accessory
# reference, the AI-model cost-control note) behind Areas notes that are
# folder-ranked first. 55000 covers both brands' current totals with room
# to grow before this needs revisiting again.
_MAX_BRIEF_CHARS = 55000
_MAX_NOTE_CHARS = 4500

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class VaultNote:
    """One parsed vault note. `body` excludes the frontmatter block."""

    path: Path
    folder: str  # top-level folder, e.g. "02 - Delta Cuisine" (or a legacy PARA name)
    title: str
    body: str
    frontmatter: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    brand: str | None = None
    mtime: float = 0.0

    @property
    def rel(self) -> str:
        try:
            return str(self.path.relative_to(VAULT_DIR)).replace("\\", "/")
        except ValueError:
            return self.path.name

    @property
    def note_type(self) -> str:
        """jaredrhod's `type:` field, lowercased. '' when the note doesn't say.

        Falls back to the LEGACY folder name for a pre-migration note that has no
        `type:` yet, so the old vault keeps ranking correctly through the
        migration instead of collapsing to one undifferentiated bucket.
        """
        declared = (self.frontmatter.get("type") or "").strip().lower()
        if declared:
            return declared
        return {
            "Areas": "reference",
            "Resources": "reference",
            "Projects": "plan",
            "Logs": "log",
        }.get(self.folder, "")

    @property
    def rank(self) -> int:
        return _TYPE_RANK.get(self.note_type, _TYPE_RANK[""])

    @property
    def briefable(self) -> bool:
        """Can this note reach a CM brief at all?

        Four independent ways out, each a different kind of "not an instruction":
        a folder that holds history or unfiled capture, an explicit
        `era_brief: false`, an archived status, or a type that is navigation
        (`index`) or history (`log`).
        """
        if self.folder not in _BRIEF_FOLDERS:
            return False
        if str(self.frontmatter.get("era_brief", "")).strip().lower() in ("false", "no", "0"):
            return False
        if (self.frontmatter.get("status") or "").strip().lower() == "archived":
            return False
        return self.note_type not in _NEVER_BRIEFED_TYPES


def vault_available() -> bool:
    """True when the vault root is present and readable."""
    try:
        return VAULT_DIR.is_dir()
    except OSError:
        return False


def _parse_frontmatter(text: str) -> tuple[dict[str, str], list[str], str]:
    """Minimal YAML-frontmatter parse: flat `key: value` pairs plus `tags`.

    Deliberately NOT yaml.safe_load — vault notes are hand-written and a single
    malformed line (an unquoted colon in a French sentence, a stray tab) would
    take the whole loader down. A hand-rolled scan degrades to "no frontmatter"
    on garbage instead of raising, which is the behaviour the dispatch path
    needs. We only ever read scalars and the tags list here.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, [], text

    front: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip().lower()] = value.strip().strip("\"'")

    raw_tags = front.get("tags", "")
    tags = [
        t.strip().strip("\"'").lower()
        for t in raw_tags.strip("[]").split(",")
        if t.strip().strip("\"'")
    ]
    return front, tags, text[match.end() :]


def _load_note(path: Path) -> VaultNote | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log_event(vault_logger, "cm.vault.read_failed", "WARNING", path=str(path), error=str(exc))
        return None

    front, tags, body = _parse_frontmatter(text)
    try:
        folder = path.relative_to(VAULT_DIR).parts[0]
    except (ValueError, IndexError):
        folder = ""

    return VaultNote(
        path=path,
        folder=folder,
        title=path.stem,
        body=body.strip(),
        frontmatter=front,
        tags=tags,
        brand=(front.get("brand") or "").strip().lower() or None,
        mtime=path.stat().st_mtime if path.exists() else 0.0,
    )


def load_vault(folders: tuple[str, ...] = _PARA_ORDER) -> list[VaultNote]:
    """Index the vault. Returns [] when the vault is missing — never raises."""
    if not vault_available():
        log_event(vault_logger, "cm.vault.missing", "WARNING", vault_dir=str(VAULT_DIR))
        return []

    notes: list[VaultNote] = []
    for folder in folders:
        root = VAULT_DIR / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            # .obsidian holds plugin data and workspace state, never content.
            if ".obsidian" in path.parts:
                continue
            note = _load_note(path)
            if note and note.body:
                notes.append(note)
    return notes


def _needles(brand_key: str, aliases: list[str]) -> set[str]:
    """Normalized tokens that identify a brand in frontmatter, tags or filenames.

    Aliases of 2 chars or less ("dc") are kept only as exact tokens, never as
    substrings — "dc" matches far too much inside ordinary words to be used for
    containment matching.
    """
    return {brand_key.lower()} | {
        a.lower().replace(" ", "_").replace("-", "_") for a in aliases if len(a) > 2
    }


def _matches_brand(
    note: VaultNote,
    brand_key: str,
    aliases: list[str],
    other_brands: dict[str, list[str]] | None = None,
) -> bool | None:
    """True = this brand's note, False = another brand's, None = global.

    The tri-state matters: a note belonging to a DIFFERENT brand must be
    actively excluded (cross-brand contamination is the one thing the whole
    system is built to prevent), while a note belonging to NO brand is shared
    context that every brand should see.

    Ownership is claimed by the `brand:` field, by a brand TAG, or by the brand
    name in the filename. Tags count: `tags: [resource, ceram-quartz]` with no
    `brand:` field is still Ceram Quartz's note, and letting it through to Delta
    Cuisine on the grounds that only `brand:` is authoritative is exactly the
    cross-brand bleed this function exists to stop.
    """
    mine = _needles(brand_key, aliases)
    # `project:` is jaredrhod's field for "what this note serves", and in this
    # vault a project IS a brand — so `project: delta-cuisine` is as strong a
    # claim as `brand:`. The folder counts too: everything under
    # `02 - Delta Cuisine/` belongs to Delta Cuisine whether or not the author
    # remembered the frontmatter, which is what makes the numbered layout safer
    # than the flat one it replaced rather than merely tidier.
    claims = [
        note.brand or "",
        note.frontmatter.get("project", ""),
        note.folder,
        *note.tags,
        note.title.lower(),
    ]
    claims = [c.lower().replace("-", "_").replace(" ", "_") for c in claims if c]

    if any(any(n in c for n in mine) for c in claims):
        return True

    for other_key, other_aliases in (other_brands or {}).items():
        if other_key.lower() == brand_key.lower():
            continue
        theirs = _needles(other_key, other_aliases)
        if any(any(n in c for n in theirs) for c in claims):
            return False

    # Unknown `brand:` value — some brand, but not this one. Exclude.
    if note.brand and not any(n in note.brand for n in mine):
        return False
    return None


def notes_for_brand(
    brand_key: str,
    aliases: list[str] | None = None,
    folders: tuple[str, ...] = _BRIEF_FOLDERS,
    other_brands: dict[str, list[str]] | None = None,
) -> list[VaultNote]:
    """This brand's notes plus global notes, ordered by usefulness to a brief.

    Two starvation bugs have shown up here, on two different axes, and the
    ordering below is shaped to avoid both:

    1. KIND FIRST, not folder. `reference` notes hold RULES ("one material family
       per kitchen", "hero shot at 2K, the rest at 1K"); `plan` notes hold
       INSTANCES of those rules. A handful of verbose campaign notes once ate the
       whole character budget and starved out the global standards — a rule must
       never lose its slot to an example of itself.

       This used to rank on the top-level FOLDER, which worked only while the
       folder name doubled as the note's kind (Areas/Resources/Projects). Under
       the per-brand numbered layout everything Delta Cuisine owns shares one
       folder, so the rank moved to `type:` where it always belonged.
    2. WITHIN a rank, INTERLEAVE brand-specific and global notes rather than
       draining one group before the other gets a turn. As this brand
       accumulated its own reference notes (a wardrobe prompt system, brand-asset
       references), a strict "brand-specific first" ordering let them consume the
       whole budget before the GLOBAL ones — the AI-generation model choices, a
       direct credit-spend control — were ever reached. Interleaving guarantees
       both kinds get an early turn per rank instead of one draining first.
    """
    aliases = aliases or []
    by_rank: dict[int, dict[bool, list[VaultNote]]] = {}
    for note in load_vault(folders):
        # One gate, four reasons (see VaultNote.briefable): a folder that holds
        # history or unfiled capture, an explicit `era_brief: false`, an archived
        # status, or a type that is navigation/history. Applied HERE and not in
        # load_vault() so the doctor and the maintenance audit still see
        # everything.
        if not note.briefable:
            continue
        verdict = _matches_brand(note, brand_key, aliases, other_brands)
        if verdict is False:
            continue
        bucket = by_rank.setdefault(note.rank, {True: [], False: []})
        bucket[verdict is True].append(note)

    ordered: list[VaultNote] = []
    for rank in sorted(by_rank):
        buckets = by_rank[rank]
        brand_notes = sorted(buckets[True], key=lambda n: -n.mtime)
        global_notes = sorted(buckets[False], key=lambda n: -n.mtime)
        for pair in zip_longest(brand_notes, global_notes):
            for note in pair:
                if note is not None:
                    ordered.append(note)

    return ordered


def _trim_note(body: str) -> str:
    """Cut an over-long note at a SECTION boundary, and say what was dropped.

    The old cut was `body[:_MAX_NOTE_CHARS]` — a hard slice. On a note of five
    reel concepts that lands mid-sentence inside concept four, and the model then
    reads half an instruction as if it were whole. Worse, the marker said only
    "[... note truncated ...]": the CM could not tell whether it had the rule or
    a fragment of it, so it filled the gap by inventing.

    So: fall back to the last section heading (then the last paragraph) before the
    cap, and name what is missing. A model told "3 of 5 sections shown, open the
    file for the rest" can go and read it; one handed a severed sentence cannot
    know it needs to.
    """
    if len(body) <= _MAX_NOTE_CHARS:
        return body

    window = body[:_MAX_NOTE_CHARS]
    # Prefer a heading boundary, then a blank line; a hard slice only if the note
    # has no structure at all within the window.
    heads = list(re.finditer(r"^##+\s", window, re.MULTILINE))
    cut = heads[-1].start() if len(heads) > 1 else window.rfind("\n\n")
    if cut < _MAX_NOTE_CHARS // 3:  # too little kept — the boundary is unhelpful
        cut = len(window)

    kept, dropped = body[:cut].rstrip(), len(body) - cut
    total_sections = len(re.findall(r"^##+\s", body, re.MULTILINE))
    shown = len(re.findall(r"^##+\s", kept, re.MULTILINE))
    detail = (
        f"{shown} of {total_sections} sections shown"
        if total_sections
        else f"{cut} of {len(body)} chars shown"
    )
    return (
        f"{kept}\n\n[... {dropped} more characters in this note — {detail}. "
        "Open the file itself if you need the rest; do not guess at what is missing.]"
    )


def render_vault_context(
    brand_key: str,
    aliases: list[str] | None = None,
    task_type: str = "weekly",
    other_brands: dict[str, list[str]] | None = None,
) -> str:
    """The vault block injected into the CM brief. '' when there is nothing.

    Presented as authoritative standing context, because that is what it is:
    these notes are the user's own written strategy. But brands.yaml still wins
    on routing — a note can never re-point an account id, and says so here.
    """
    notes = notes_for_brand(brand_key, aliases, other_brands=other_brands)
    if not notes:
        return ""

    # `continue` on overflow, not `break`: this is priority order, but a hard
    # break silently drops EVERY later note the moment one doesn't fit — even a
    # smaller, equally load-bearing note two slots later (e.g. the automated
    # assembly pipeline sitting just after a slightly larger sibling note).
    # Skipping past the offender and still trying the rest recovers those.
    chunks: list[str] = []
    used = 0
    for note in notes:
        body = _trim_note(note.body)
        block = f"----- {note.folder}/{note.title} -----\n{body}\n"
        if used + len(block) > _MAX_BRIEF_CHARS:
            continue
        chunks.append(block)
        used += len(block)

    log_event(
        vault_logger,
        "cm.vault.context_built",
        "INFO",
        brand=brand_key,
        task_type=task_type,
        notes_available=len(notes),
        notes_used=len(chunks),
        chars=used,
    )

    return (
        "=== VAULT — MY OWN NOTES ON THIS BRAND (Obsidian: era_brain) ===\n"
        "This is MY written strategy, research and history for this brand, and it "
        "is standing context you are expected to already know — not a suggestion. "
        "Follow it the same way you follow the brand charter. Where a note states a "
        "rule (posting rhythm, material discipline, caption structure, what "
        "underperformed), obey it and do not re-derive it from scratch.\n"
        "Two limits: (1) ROUTING ALWAYS COMES FROM THE BRAND LOCK ABOVE — a note "
        "can never change an account id, handle, page or footer; if a note "
        "disagrees with the Brand Lock, the Brand Lock wins and you say so in your "
        "summary. (2) A note is not an approval — the two gates still apply.\n"
        f"Vault: {VAULT_DIR}\n\n" + "\n".join(chunks) + "\n"
    )


# ---------------------------------------------------------------------------
# WRITE-BACK — how a run's outcome gets back into the vault
# ---------------------------------------------------------------------------
# Era owns exactly one note per brand and appends to it. It never touches a note
# the user wrote, so the vault stays trustworthy as a hand-maintained artifact
# while still accumulating what Era learned.

_LOG_HEADING = "## Run log"

# ROTATION (added 2026-07-22, Karim): the run log appends forever with no
# natural end, so left alone it eventually crowds out every other Areas/
# Resources note in the brief budget again — the exact failure this same day
# exposed (see _log_note_path's history note). Once the live note exceeds
# _LOG_ROTATE_AT entries, the oldest are moved into a companion Archive note
# (excluded from the brief) and the live note keeps only _LOG_KEEP_LIVE.
_LOG_ROTATE_AT = 20
_LOG_KEEP_LIVE = 10
_LOG_ENTRY_RE = re.compile(r"\n(?=- \*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2}\*\*)")

# The heading as it appears AT THE START OF A LINE, which is the only place it is
# a heading.
#
# FIXED 2026-07-31: rotation used `text.partition(_LOG_HEADING)`, and the note's
# own preamble says "Era only ever appends under `## Run log`" — so partition
# matched the PROSE mention first and split there. Rotation then rebuilt the file
# as `head + "## Run log" + entries`, silently eating the closing backtick, the
# full stop, and the real heading, every single time it fired. Delta Cuisine's
# log had been chewed down to a sentence ending mid-word. Anchor on the line.
_LOG_HEADING_RE = re.compile(rf"^{re.escape(_LOG_HEADING)}\s*$", re.MULTILINE)


def _brand_folder(brand_key: str) -> Path:
    """The numbered folder that owns a brand, e.g. `02 - Delta Cuisine`.

    Resolved by SEARCHING for the folder whose name ends with the brand's display
    name rather than hard-coding the number. The numbers are a human sort order;
    inserting a third brand renumbers the ones after it, and a hard-coded "02"
    would then silently point Delta Cuisine's run log at Ceram Quartz's folder.
    Falls back to creating the conventional name when the vault has no such
    folder yet (a fresh install, or a brand added before its folder exists).
    """
    display = " ".join(part.capitalize() for part in brand_key.split("_"))
    try:
        for child in sorted(VAULT_DIR.iterdir()):
            if child.is_dir() and child.name.split(" - ", 1)[-1].lower() == display.lower():
                return child
    except OSError:
        pass
    return VAULT_DIR / display


def charter_path(brand_key: str) -> Path:
    """Where this brand's long-form charter lives, now that the vault owns it.

    Imported into the vault on 2026-07-31; the copies still sitting in each
    brand's `project_dir` are stale and the brief says so explicitly. Kept
    `era_brief: false` on purpose — the charter is 12-20 KB, the CM opens the
    file directly, and injecting it would displace every other note in the brief.
    """
    display = " ".join(part.capitalize() for part in brand_key.split("_"))
    return _brand_folder(brand_key) / f"{display.replace(' ', '_')}_Charter.md"


def _log_note_path(brand_key: str) -> Path:
    # FIXED 2026-07-22 (Karim): this pointed at "Areas/" (root) while the
    # 2026-07-21 vault reorg moved every brand note into "Areas/<Brand>/" —
    # this constant was never updated to match, so Era silently forked each
    # brand's run log into a fresh near-empty file at the old root path,
    # stranding the real history (18 DC entries, 10 CQ entries) in the
    # subfolder copy where nothing ever appended to it again. Match the
    # vault's actual brand-subfolder convention.
    return _brand_folder(brand_key) / f"Era_Run_Log_{brand_key}.md"


def _archive_log_note_path(brand_key: str) -> Path:
    display = " ".join(part.capitalize() for part in brand_key.split("_"))
    return VAULT_DIR / "05 - Archive" / display / f"Era_Run_Log_{brand_key}_Archive.md"


def _rotate_run_log(path: Path, brand_key: str) -> None:
    """Move overflow entries out of the live log into the Archive companion.

    Never raises: a rotation failure must not turn a successful run-log
    append into a failed one. Silently no-ops on any parse/write problem.
    """
    try:
        text = path.read_text(encoding="utf-8")
        match = _LOG_HEADING_RE.search(text)
        if not match:
            return
        head, body = text[: match.start()], text[match.end() :]
        blocks = [b for b in _LOG_ENTRY_RE.split(body.strip("\n")) if b.strip()]
        if len(blocks) <= _LOG_ROTATE_AT:
            return

        overflow, keep = blocks[:-_LOG_KEEP_LIVE], blocks[-_LOG_KEEP_LIVE:]

        archive_path = _archive_log_note_path(brand_key)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if not archive_path.exists():
            archive_path.write_text(
                f"---\ntags: [area, {brand_key}, era-log, archive]\n"
                f"brand: {brand_key}\ncreated: "
                f"{datetime.now(UTC).astimezone().strftime('%Y-%m-%d')}\n---\n\n"
                f"# Era Run Log Archive — {brand_key}\n\n"
                "Historical run-log entries rotated out of the live note to keep "
                "it small. Read-only history — Archive is excluded from the CM "
                f"brief.\n\n{_LOG_HEADING}\n",
                encoding="utf-8",
            )
        with archive_path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n\n".join(overflow) + "\n")

        path.write_text(head + _LOG_HEADING + "\n\n" + "\n\n".join(keep) + "\n", encoding="utf-8")
    except OSError as exc:
        log_event(
            vault_logger,
            "cm.vault.log_rotate_failed",
            "WARNING",
            brand=brand_key,
            path=str(path),
            error=str(exc),
        )


def append_run_log(
    brand_key: str, task_type: str, order: str, outcome: str, detail: str = ""
) -> Path | None:
    """Append one run entry to this brand's Era-owned vault note.

    Returns the path written, or None when the vault is unavailable. Never
    raises: a vault write failing must not turn a successful CM run into a
    failed one.
    """
    if not vault_available():
        return None

    path = _log_note_path(brand_key)
    stamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"\n- **{stamp}** · `{task_type}` · **{outcome}**\n"
        f"    - order: {order.strip()[:300]}\n"
        + (f"    - {detail.strip()[:600]}\n" if detail.strip() else "")
    )

    try:
        # The shared vault write lock (core/vault_bridge, imported lazily because the
        # bridge imports this module). A held lock is an OSError, handled below like
        # any other failed write: the run log is best-effort, the job still succeeds.
        from core import vault_bridge

        with vault_bridge.vault_write_lock():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(
                    f"---\ntags: [area, {brand_key}, era-log]\n"
                    f"brand: {brand_key}\ncreated: {stamp[:10]}\n---\n\n"
                    f"# Era Run Log — {brand_key}\n\n"
                    "Written by Era automatically after every community-management run. "
                    "Safe to read, annotate and prune by hand — Era only ever appends "
                    f"under `{_LOG_HEADING}`.\n\n"
                    f"{_LOG_HEADING}\n",
                    encoding="utf-8",
                )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
            _rotate_run_log(path, brand_key)
    except OSError as exc:
        log_event(
            vault_logger,
            "cm.vault.write_failed",
            "WARNING",
            brand=brand_key,
            path=str(path),
            error=str(exc),
        )
        return None

    log_event(
        vault_logger,
        "cm.vault.run_logged",
        "INFO",
        brand=brand_key,
        task_type=task_type,
        outcome=outcome,
        path=str(path),
    )
    return path


def doctor() -> dict:
    """Connection check for tools/era_doctor.py and `python -m core.cm_vault`."""
    ok = vault_available()
    notes = load_vault() if ok else []
    by_folder: dict[str, int] = {}
    for note in notes:
        by_folder[note.folder] = by_folder.get(note.folder, 0) + 1
    return {
        "vault_dir": str(VAULT_DIR),
        "exists": ok,
        "notes": len(notes),
        "by_folder": by_folder,
        "brands_seen": sorted({n.brand for n in notes if n.brand}),
    }


if __name__ == "__main__":
    import json
    import sys

    argv = sys.argv[1:]
    if argv:
        print(render_vault_context(argv[0], argv[1:] or None))
    else:
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
