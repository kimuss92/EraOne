r"""
vault_maintenance.py — the routine that ACTS on what vault_audit.py found.

Three things, in the order sync() does them: repair the unambiguous findings,
rewrite the learned-preference notes, and record the result inside the vault.
The checks themselves live in core/vault_audit.py, which never writes — see its
docstring for why that split is load-bearing.

THE DELETION RULE — NOTHING THE USER WROTE IS EVER DELETED
  The vault is the half of Era's brand memory that the USER WROTE. An automated
  process that deletes from it can destroy work in a way no other failure in this
  system can, and "stale" is a judgement a timestamp cannot make: a strategy note
  untouched for six months is not stale, it is settled. So the strongest action
  available against user content is a MOVE into Archive/ — which removes a note
  from future briefs while keeping it queryable — and even that only fires on an
  explicit `status: done` the user wrote themselves. Age alone is reported, never
  acted on.

  The one exception is not an exception to the spirit: promote_lessons() removes
  its OWN generated note when a brand no longer has firm lessons. That file is
  rebuilt from memory/cm_lessons.json on every pass and holds nothing that is not
  also there, so removing it loses no information — whereas leaving it would keep
  asserting rules that no longer have the repetitions to justify them.

AUTO-FIX BOUNDARY
  A fix runs automatically only when the vault's own structure already answers
  it unambiguously. Adding `brand: ceram_quartz` to a note sitting in
  `Areas/Ceram_Quartz/` is such a case. Resolving a note in `Areas/Ceram_Quartz/`
  whose frontmatter says `brand: delta_cuisine` is NOT — the folder and the
  frontmatter disagree and only the author knows which is right, so it is
  reported as an error and left alone. Guessing there is how one brand's strategy
  ends up in another brand's brief, which is the one failure this whole subsystem
  exists to prevent.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core import cm_vault
from core.logger import log_event, setup_logger
from core.vault_audit import (
    _BRIEF_OPT_OUT,
    Finding,
    _brand_registry,
    _folder_brand,
    audit,
)

maint_logger = setup_logger("era_one.vault_maintenance")


# ── repair ───────────────────────────────────────────────────────────────────


def _insert_brand_frontmatter(path: Path, brand_key: str) -> bool:
    """Add `brand: <key>` to a note's frontmatter without touching anything else.

    Body content is never rewritten — only a line inserted into the frontmatter
    block (or a block created when there is none). The one-line diff is what
    makes this safe to run unattended on notes the user wrote.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    match = cm_vault._FRONTMATTER_RE.match(text)
    if match:
        block = match.group(1)
        if re.search(r"^\s*brand\s*:", block, re.MULTILINE):
            return False  # already there; nothing to do
        new = f"---\n{block}\nbrand: {brand_key}\n---\n"
        updated = new + text[match.end() :]
    else:
        today = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
        updated = f"---\nbrand: {brand_key}\ncreated: {today}\n---\n\n{text.lstrip()}"

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError:
        return False
    return True


def _archive_note(path: Path) -> Path | None:
    """Move a finished note into the matching Archive/ subfolder.

    A move, never a delete — Archive is indexed (so "what did we run in May" is
    still answerable) but excluded from _BRIEF_FOLDERS, which is exactly the
    "retire it without losing it" semantics PARA is built around.
    """
    try:
        parts = path.relative_to(cm_vault.VAULT_DIR).parts
    except ValueError:
        return None
    dest = cm_vault.VAULT_DIR / "Archive" / Path(*parts[1:])
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}_{int(time.time())}{dest.suffix}")
        path.replace(dest)
    except OSError:
        return None
    return dest


def apply_fixes(findings: list[Finding], *, dry_run: bool = True) -> list[str]:
    """Carry out the auto-fixable findings. Returns one line per action.

    `dry_run=True` by default and everywhere it is scheduled: this edits a
    hand-maintained vault, and a maintenance pass that surprises you is one you
    stop trusting. The caller opts in.
    """
    known = _brand_registry()
    actions: list[str] = []

    for finding in findings:
        if not finding.fix or not finding.path:
            continue
        path = cm_vault.VAULT_DIR / finding.path
        if not path.is_file():
            continue

        if finding.fix == "add_brand_frontmatter":
            note = cm_vault._load_note(path)
            brand = _folder_brand(note, known) if note else None
            if not brand:
                continue
            if dry_run:
                actions.append(f"would add `brand: {brand}` to {finding.path}")
            elif _insert_brand_frontmatter(path, brand):
                actions.append(f"added `brand: {brand}` to {finding.path}")

        elif finding.fix == "archive_note":
            if dry_run:
                actions.append(f"would move {finding.path} -> Archive/")
            else:
                dest = _archive_note(path)
                if dest:
                    actions.append(
                        f"moved {finding.path} -> {dest.relative_to(cm_vault.VAULT_DIR)}"
                    )

    return actions


# ── learned-preference promotion ─────────────────────────────────────────────

_PREF_HEADER = "Era_Learned_Preferences"


def promote_lessons(brand_key: str, *, min_hits: int = 2) -> Path | None:
    """Write this brand's FIRM lessons into a vault note the user can read.

    Closes the loop the CLAUDE.md capture section describes: corrections were
    being recorded into memory/cm_lessons.json and replayed into briefs, but they
    lived in a JSON file the user never opens. The vault is where they can
    actually be reviewed, argued with, and promoted by hand into real strategy.

    Only lessons said more than once (`min_hits`) are written. A one-off remark
    is a reaction to one asset; a repeated one is a rule, and only rules belong
    in a document you are meant to trust.

    The note carries `era_brief: false`, so it never re-enters the brief. The
    lessons already reach it through cm_learning.render_lessons(), and a second
    copy would spend the vault budget restating what the brief just said.
    Regenerated wholesale each pass — it is derived data, not a log.
    """
    if not cm_vault.vault_available():
        return None
    try:
        from core import cm_learning

        lessons = [
            e for e in cm_learning.lessons_for(brand_key) if int(e.get("hits", 1)) >= min_hits
        ]
    except Exception as exc:
        log_event(maint_logger, "vault.maint.lessons_failed", "WARNING", error=str(exc))
        return None

    # Through the resolver, not a hand-built path: the brand folder is numbered
    # now, and adding a third brand renumbers the ones after it. A hard-coded
    # folder name would strand this note where nothing reads it.
    path = cm_vault._brand_folder(brand_key) / f"{_PREF_HEADER}_{brand_key}.md"

    if not lessons:
        # Nothing firm to say. Remove a stale note rather than leaving one that
        # claims rules which no longer have the hits to justify them.
        #
        # But only when the store is genuinely EMPTY, not merely unreadable:
        # _load_lessons() degrades a corrupt or transiently-unreadable store to
        # {} by design, and this vault lives on OneDrive, where a read failing
        # for a few seconds is ordinary. Without this check a flaky read would
        # delete every brand's preference note and report it as "no lessons".
        # Parse the file directly rather than going through _load_lessons(),
        # which cannot tell the two apart: it returns {} for a genuinely empty
        # store AND for one it failed to read. Only a raise means "unreadable".
        store = cm_learning._LESSONS_PATH
        try:
            store_readable = not store.exists() or isinstance(
                json.loads(store.read_text(encoding="utf-8")), dict
            )
        except Exception:
            store_readable = False
        try:
            if store_readable and path.is_file():
                path.unlink()
        except OSError:
            pass
        return None

    stamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        f"tags: [area, {brand_key}, era-learned]",
        f"brand: {brand_key}",
        f"{_BRIEF_OPT_OUT}: false",
        f"updated: {stamp[:10]}",
        "---",
        "",
        f"# Learned preferences — {brand_key}",
        "",
        f"_Generated by Era on {stamp}. Rebuilt from scratch on every vault "
        "maintenance pass — edits here are overwritten._",
        "",
        "These are **your own words**, kept verbatim, from corrections and "
        "approvals you gave this brand. Only things you said more than once "
        f"(≥{min_hits}×) appear. Era already replays them into every brief; this "
        "note exists so you can see what she thinks you want — and move anything "
        "you agree with into a real strategy note, where it becomes authoritative.",
        "",
    ]

    for polarity, heading in (("avoid", "Do not repeat"), ("prefer", "Keep doing")):
        group = [e for e in lessons if e.get("polarity") == polarity]
        if not group:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for entry in sorted(group, key=lambda e: -int(e.get("hits", 1))):
            hits = int(entry.get("hits", 1))
            cat = entry.get("category", "general")
            lines.append(f"- **({cat}, said {hits}×)** — {entry.get('text', '').strip()}")
        lines.append("")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log_event(maint_logger, "vault.maint.promote_failed", "WARNING", error=str(exc))
        return None
    return path


# ── the routine ──────────────────────────────────────────────────────────────

_HEALTH_NOTE = "04 - Global/Era_Vault_Health.md"


@dataclass
class SyncReport:
    findings: list[Finding] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def summary(self) -> str:
        if not self.findings:
            return "Vault is consistent — nothing to report."
        return (
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len(self.findings) - len(self.errors) - len(self.warnings)} note(s); "
            f"{len(self.actions)} fix(es) {'applied' if self.applied else 'available'}."
        )

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "applied": self.applied,
            "findings": [
                {"severity": f.severity, "kind": f.kind, "path": f.path, "detail": f.detail}
                for f in self.findings
            ],
            "actions": self.actions,
            "promoted": self.promoted,
        }


def sync(*, apply: bool = False, promote: bool = True) -> SyncReport:
    """One full maintenance pass: audit, optionally repair, promote, record.

    This is the routine the scheduler runs and the doctor reports on. It never
    raises — a maintenance sweep that can take down Era's scheduler thread is a
    worse problem than the drift it was checking for.
    """
    report = SyncReport(applied=apply)
    try:
        report.findings = audit()
        report.actions = apply_fixes(report.findings, dry_run=not apply)

        if promote:
            for brand_key in _brand_registry():
                path = promote_lessons(brand_key)
                if path:
                    report.promoted.append(str(path.relative_to(cm_vault.VAULT_DIR)))

        # Re-audit after a real repair, so the health note records the state the
        # vault is actually IN rather than the state it was in before the fixes.
        if apply and report.actions:
            report.findings = audit()

        _write_health_note(report)
    except Exception as exc:  # noqa: BLE001 — see docstring
        log_event(maint_logger, "vault.maint.sync_failed", "ERROR", error=str(exc))
        report.findings.append(Finding("error", "sync_failed", str(exc)))

    log_event(
        maint_logger,
        "vault.maint.sync",
        "WARNING" if report.errors else "INFO",
        errors=len(report.errors),
        warnings=len(report.warnings),
        actions=len(report.actions),
        applied=apply,
    )
    return report


def _write_health_note(report: SyncReport) -> None:
    """Record the pass inside the vault itself.

    In the vault rather than only in era.log because that is where the user
    already is when they are thinking about the vault — a health report they have
    to remember to go and run is a health report nobody reads. Era-owned and
    `era_brief: false`, so it never spends brief budget describing itself.
    """
    if not cm_vault.vault_available():
        return
    path = cm_vault.VAULT_DIR / _HEALTH_NOTE
    stamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")

    lines = [
        "---",
        "tags: [area, era-log, vault-health]",
        f"{_BRIEF_OPT_OUT}: false",
        f"updated: {stamp[:10]}",
        "---",
        "",
        "# Vault health",
        "",
        f"_Last checked {stamp} — {report.summary()}_",
        "",
        "Rebuilt on every maintenance pass. Nothing here was deleted: the "
        "strongest action this routine takes is moving a finished note into "
        "`Archive/`.",
        "",
    ]

    if not report.findings:
        lines += ["Everything is filed correctly and every note fits the brief budget.", ""]
    for severity, heading in (
        ("error", "Needs your attention"),
        ("warn", "Worth fixing"),
        ("info", "For information"),
    ):
        group = [f for f in report.findings if f.severity == severity]
        if not group:
            continue
        lines += [f"## {heading}", ""]
        lines += [f"- {f.detail}" + (f" — `{f.path}`" if f.path else "") for f in group]
        lines.append("")

    if report.actions:
        lines += [
            "## Fixes " + ("applied" if report.applied else "available (not yet applied)"),
            "",
        ]
        lines += [f"- {a}" for a in report.actions]
        lines.append("")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log_event(maint_logger, "vault.maint.health_write_failed", "WARNING", error=str(exc))


if __name__ == "__main__":
    import argparse
    import sys

    # The findings are full of em-dashes and accented brand names, and a Windows
    # console defaults to cp1252 — without this the report prints mojibake and
    # looks broken when it is merely being transcoded.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="vault_maintenance", description="Audit and tidy the era_brain Obsidian vault."
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the auto-fixable repairs (default: report only).",
    )
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    ap.add_argument(
        "--no-promote", action="store_true", help="Skip rewriting the learned-preference notes."
    )
    args = ap.parse_args()

    result = sync(apply=args.apply, promote=not args.no_promote)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.summary())
        for item in result.findings:
            print(f"  {item.line()}")
        for action in result.actions:
            print(f"  -> {action}")
        for promoted in result.promoted:
            print(f"  -> promoted lessons into {promoted}")
    raise SystemExit(1 if result.errors else 0)
