r"""
vault_audit.py — what is WRONG with the era_brain vault. Read-only, always.

WHY THIS EXISTS
  Reading the vault into a brief (cm_vault.render_vault_context) and writing a
  run's outcome back (cm_vault.append_run_log) both already worked. What did not
  exist was anything that CHECKED the vault — the filing rules lived in prose, in
  the `vault-organizer` skill, to be applied by hand whenever someone remembered.

  That is the wrong place for them, because every failure found here is SILENT.
  A note with `brand: delta-cuisine` (hyphen, not underscore) is not an error
  anywhere: it simply stops appearing in every future brief, and the first
  symptom is a content plan that ignores a rule you wrote months ago. Same for a
  note that grew past the per-note cap and is now truncated mid-sentence in every
  run, and same for a brand whose notes collectively overflow the brief budget.
  None of it raises. None of it shows up in a log you read. It just quietly
  degrades what Era knows.

WHY IT IS SPLIT FROM vault_maintenance.py
  Nothing in this module writes. That is a property worth having in the type
  system rather than in a comment: `tools/era_doctor.py` imports THIS module, so
  running the doctor cannot mutate the vault even if a future edit to the repair
  logic gets it wrong. Repair, promotion and the scheduled routine all live next
  door in vault_maintenance.py, which imports from here.

  A Finding therefore carries `fix` as a STRING NAME, not a callable — the
  decision about what is auto-fixable belongs with the checks, but the ability to
  carry it out deliberately does not live here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from core import cm_vault
from core.cm_vault import (
    _MAX_BRIEF_CHARS,
    _MAX_NOTE_CHARS,
    _PARA_ORDER,
    VaultNote,
)
from core.logger import log_event, setup_logger

audit_logger = setup_logger("era_one.vault_audit")

# A note this short carries no instruction — it is a stub someone started and
# left. Reported, never touched: an empty note is often a deliberate placeholder.
_MIN_BODY_CHARS = 40

# Past this fraction of the brief budget, the NEXT note added to the vault is
# likely to be the one silently dropped. Warning here is the whole point — the
# budget has been hit twice before, and both times the symptom was a brief that
# looked fine while missing a rule.
_BUDGET_WARN_RATIO = 0.80

# Frontmatter values that mean the author considers a Project finished. Only an
# explicit statement counts; see the deletion rule in the module docstring.
_DONE_STATUSES = {"done", "completed", "complete", "finished", "shipped", "archived", "closed"}

# A Projects note untouched this long is REPORTED as possibly finished. Never
# acted on — "old" and "stale" are not the same thing.
_STALE_PROJECT_DAYS = 90

# Frontmatter switch a note can set to stay out of CM briefs while remaining in
# the vault (and in this audit). Era's own generated notes use it so they cannot
# double-inject content the brief already carries from another source.
_BRIEF_OPT_OUT = "era_brief"

_SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}


@dataclass
class Finding:
    """One thing the audit noticed. `fix` is set only when it is auto-applicable."""

    severity: str  # error | warn | info
    kind: str
    detail: str
    path: str = ""
    fix: str = ""  # "" = needs a human

    def line(self) -> str:
        where = f" [{self.path}]" if self.path else ""
        tail = f" (auto-fixable: {self.fix})" if self.fix else ""
        return f"{self.severity.upper()}: {self.kind} — {self.detail}{where}{tail}"


# ── helpers ──────────────────────────────────────────────────────────────────


def _brand_registry() -> dict[str, list[str]]:
    """{brand_key: aliases} from brands.yaml. {} when it cannot be read.

    Degrades rather than raising for the same reason every other vault call does:
    a maintenance pass that cannot run is a nuisance, one that raises into a
    scheduler thread is an outage.
    """
    try:
        import era_dispatch as ed

        cfg = ed.load_brands()
    except Exception as exc:
        log_event(audit_logger, "vault.audit.brands_unavailable", "WARNING", error=str(exc))
        return {}
    out: dict[str, list[str]] = {}
    for key, brand in (cfg.get("brands") or {}).items():
        out[str(key).lower()] = [str(a) for a in (brand.get("aliases") or [])]
    return out


def _folder_brand(note: VaultNote, known: dict[str, list[str]]) -> str | None:
    """The brand a note's SUBFOLDER claims (Areas/Ceram_Quartz -> ceram_quartz).

    Under the numbered layout the TOP-level folder names the brand
    (`02 - Delta Cuisine`); under the legacy PARA one it was the second segment
    (`Areas/Delta_Cuisine/`). Both are read, so the audit keeps working either
    side of the migration.

    None for a global folder, for anything starting `_` (the vault's own
    convention for "deliberately not one brand's"), and for a brand the registry
    does not know.
    """
    try:
        parts = note.path.relative_to(cm_vault.VAULT_DIR).parts
    except ValueError:
        return None

    candidates = []
    if parts:  # numbered layout: "02 - Delta Cuisine" -> delta_cuisine
        candidates.append(parts[0].split(" - ", 1)[-1])
    if len(parts) >= 3 and not parts[1].startswith("_"):  # legacy PARA
        candidates.append(parts[1])

    for raw in candidates:
        if raw.startswith("_"):
            continue
        slug = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if slug in known:
            return slug
    return None


def _reaches_brief(note: VaultNote) -> bool:
    """True when this note is actually rendered into CM briefs.

    Delegates to VaultNote.briefable so the audit and the loader can never
    disagree about what a brief contains — a checker that models the rule
    separately from the code it checks eventually reports on a rule nobody
    implements. Findings about brief COST only make sense for notes that have one.
    """
    return note.briefable


def _is_era_owned(note: VaultNote) -> bool:
    """True for the notes Era writes herself. They follow different rules —
    they are regenerated or appended to, so 'missing frontmatter' style findings
    about them would be reporting a bug in this file rather than in the vault."""
    return note.title.startswith(("Era_Run_Log", "Era_Learned_Preferences", "Era_Vault_Health"))


# ── the audit ────────────────────────────────────────────────────────────────


def audit() -> list[Finding]:
    """Every consistency problem currently in the vault, worst first.

    Never raises. An unreachable vault is one finding, not an exception — the
    caller is usually a scheduler thread or the doctor, and both want a report.
    """
    if not cm_vault.vault_available():
        return [
            Finding(
                "error",
                "vault_missing",
                f"Vault not found at {cm_vault.VAULT_DIR} — every CM brief is "
                "running with no vault context at all",
            )
        ]

    known = _brand_registry()
    notes = cm_vault.load_vault()
    findings: list[Finding] = []

    # A top-level folder outside PARA is invisible: cm_vault derives a note's
    # folder from the first path segment and only ever walks these four, so
    # everything beneath a fifth folder is dropped from every brief without a word.
    for child in sorted(cm_vault.VAULT_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if child.name not in _PARA_ORDER:
            count = sum(1 for _ in child.rglob("*.md"))
            findings.append(
                Finding(
                    "error",
                    "non_para_folder",
                    f"'{child.name}/' is not a PARA folder, so its {count} note(s) "
                    "are read by nothing — move them under Areas/Resources/Projects/Archive",
                    path=child.name,
                )
            )

    for note in notes:
        folder_brand = _folder_brand(note, known)

        # An unrecognized `brand:` is the worst case in the vault: _matches_brand
        # returns False for EVERY brand, so the note is excluded everywhere. A
        # typo ("delta-cuisine", "Delta Cuisine") reads exactly like a
        # deliberately-scoped note and costs the same as deleting it.
        if note.brand and known and note.brand not in known:
            findings.append(
                Finding(
                    "error",
                    "unknown_brand",
                    f"brand: '{note.brand}' matches no brand in brands.yaml — this "
                    f"note is excluded from EVERY brief. Known: {sorted(known)}",
                    path=note.rel,
                )
            )
        elif folder_brand and note.brand and note.brand != folder_brand:
            findings.append(
                Finding(
                    "error",
                    "brand_folder_mismatch",
                    f"filed under {folder_brand} but claims brand: {note.brand} — "
                    "one of the two is wrong and only you know which",
                    path=note.rel,
                )
            )
        elif folder_brand and not note.brand and not _is_era_owned(note):
            # Falls back to tag/filename matching, which is weaker and breaks
            # the moment the note is renamed.
            findings.append(
                Finding(
                    "warn",
                    "missing_brand_frontmatter",
                    f"filed under {folder_brand}/ but has no `brand:` field — it "
                    "currently matches only by tag or filename",
                    path=note.rel,
                    fix="add_brand_frontmatter",
                )
            )

        # Over-cap notes are cut at a section boundary now, with a marker naming
        # what was dropped (see cm_vault._trim_note) — so this is no longer silent
        # data loss, and the severity depends entirely on WHAT is being cut.
        #
        #   reference / guide  a RULE, cut in half. The model gets some of the
        #                      rule and no way to know a condition is missing.
        #                      Worth fixing, always.
        #   plan               a working document — a concept batch, a campaign.
        #                      It degrades gracefully: the CM reads the first
        #                      sections and is told to open the file for the rest.
        #                      Reported for information, not as a problem.
        #
        # Scoped to notes that actually REACH a brief: an Archive or
        # `era_brief: false` note is never rendered, so its length costs nothing.
        # A checker that cries wolf gets muted, and four permanently-warning
        # campaign notes is exactly how that starts.
        if len(note.body) > _MAX_NOTE_CHARS and _reaches_brief(note):
            is_rule = note.note_type in ("reference", "guide")
            findings.append(
                Finding(
                    "warn" if is_rule else "info",
                    "note_truncated",
                    f"{len(note.body)} chars exceeds the {_MAX_NOTE_CHARS}-char cap. "
                    + (
                        "This is a RULE — the brief gets part of it and cannot tell "
                        "which condition is missing. Split it along the "
                        "rule / procedure / instance seam."
                        if is_rule
                        else "It is a working document, so the brief takes the first "
                        "sections and is told to open the file for the rest. Fine to "
                        "leave; split only if the CM keeps missing the later parts."
                    ),
                    path=note.rel,
                )
            )

        if len(note.body) < _MIN_BODY_CHARS:
            findings.append(
                Finding(
                    "info",
                    "empty_note",
                    f"only {len(note.body)} chars of body — a stub, contributing nothing",
                    path=note.rel,
                )
            )

        if note.folder == "Projects":
            status = (note.frontmatter.get("status") or "").strip().lower()
            if status in _DONE_STATUSES:
                findings.append(
                    Finding(
                        "info",
                        "finished_project",
                        f"status: {status} — belongs in Archive/, where it stays "
                        "queryable but stops steering new plans",
                        path=note.rel,
                        fix="archive_note",
                    )
                )
            elif note.mtime and (time.time() - note.mtime) > _STALE_PROJECT_DAYS * 86400:
                days = int((time.time() - note.mtime) / 86400)
                findings.append(
                    Finding(
                        "info",
                        "aging_project",
                        f"untouched for {days} days — if it is finished, set "
                        "`status: done` and the next pass will archive it",
                        path=note.rel,
                    )
                )

    findings.extend(_audit_budget(known))

    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), f.kind, f.path))
    return findings


def _audit_budget(known: dict[str, list[str]]) -> list[Finding]:
    """Is any brand's vault context about to overflow the brief budget?

    This is the check that has actually fired in production, twice. Overflow does
    not fail — render_vault_context simply skips the notes that do not fit, so a
    brief silently loses a rule while looking completely healthy.
    """
    findings: list[Finding] = []
    for brand_key, aliases in known.items():
        try:
            notes = cm_vault.notes_for_brand(brand_key, aliases, other_brands=known)
        except Exception as exc:
            findings.append(
                Finding("warn", "budget_check_failed", f"{brand_key}: {exc}", path=brand_key)
            )
            continue

        used = 0
        dropped: list[str] = []
        for note in notes:
            size = min(len(note.body), _MAX_NOTE_CHARS) + len(note.folder) + len(note.title) + 16
            if used + size > _MAX_BRIEF_CHARS:
                dropped.append(note.rel)
                continue
            used += size

        if dropped:
            findings.append(
                Finding(
                    "error",
                    "brief_budget_exceeded",
                    f"{brand_key}: {len(dropped)} note(s) do not fit the "
                    f"{_MAX_BRIEF_CHARS}-char brief budget and are dropped silently "
                    f"— {', '.join(dropped[:4])}",
                    path=brand_key,
                )
            )
        elif used > _MAX_BRIEF_CHARS * _BUDGET_WARN_RATIO:
            findings.append(
                Finding(
                    "warn",
                    "brief_budget_pressure",
                    f"{brand_key}: {used}/{_MAX_BRIEF_CHARS} chars "
                    f"({used / _MAX_BRIEF_CHARS:.0%}) — the next note added is "
                    "likely to be the one dropped",
                    path=brand_key,
                )
            )
    return findings


def doctor() -> dict:
    """Audit-only summary for tools/era_doctor.py. Changes nothing."""
    findings = audit()
    return {
        "vault_dir": str(cm_vault.VAULT_DIR),
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warn"),
        "info": sum(1 for f in findings if f.severity == "info"),
        "top": [f.line() for f in findings[:5]],
    }
