"""Tests for the vault audit (core/vault_audit.py) and the routine that acts
on what it finds (core/vault_maintenance.py).

Every check in the audit guards a failure that is COMPLETELY SILENT — a note
that stops reaching briefs, a note truncated mid-sentence in every run, a brand
whose notes overflow the budget. Nothing raises, nothing logs, the brief just
comes out quieter. So the properties under test are:

  * DETECTION — each silent failure produces a finding of the right severity.
  * THE AUTO-FIX BOUNDARY — a repair runs only where the vault's own structure
    gives exactly one answer. An ambiguous case (folder and frontmatter
    disagreeing about the brand) must be REPORTED and left alone; guessing there
    is how one brand's strategy leaks into another's brief.
  * NOTHING THE USER WROTE IS EVER DELETED — the strongest action against their
    notes is a move into Archive/, and only on a `status:` they wrote themselves.
  * DEGRADE, NEVER RAISE — this runs on a scheduler thread; a maintenance sweep
    that can take Era's scheduler down is worse than the drift it was checking.

Every test redirects the vault path to tmp_path, so the suite never reads or
writes the user's real vault.
"""

import pytest

from core import cm_vault, vault_audit, vault_maintenance

BRANDS = {
    "delta_cuisine": ["delta", "delta cuisine", "dc"],
    "ceram_quartz": ["ceram", "ceram quartz", "quartz"],
}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A clean, correctly-filed vault. Each test dirties it in one specific way."""
    root = tmp_path / "era_brain"
    # Bodies are comfortably over _MIN_BODY_CHARS: a stub is itself a finding,
    # so a fixture built from one-liners would report on itself.
    _write(
        root / "Areas" / "Delta_Cuisine" / "Delta_Social_Strategy.md",
        "---\ntags: [area, delta-cuisine]\nbrand: delta_cuisine\n---\n"
        "# Delta strategy\nFive to six reels per week, one material family per "
        "kitchen, and the hero shot always renders at 2K.\n",
    )
    _write(
        root / "Areas" / "_Global" / "CM_Operating_Principles.md",
        "---\ntags: [area, global]\n---\n# Principles\nNever auto-publish: the plan "
        "gate and the asset gate are separate, and both need my signature.\n",
    )
    for folder in ("Resources", "Projects", "Archive"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    monkeypatch.setattr(vault_maintenance.cm_vault, "VAULT_DIR", root)
    monkeypatch.setattr(vault_audit.cm_vault, "VAULT_DIR", root)
    monkeypatch.setattr(vault_audit, "_brand_registry", lambda: BRANDS)
    monkeypatch.setattr(vault_maintenance, "_brand_registry", lambda: BRANDS)
    return root


def _kinds(findings):
    return {f.kind for f in findings}


def _of_kind(findings, kind):
    return [f for f in findings if f.kind == kind]


class TestCleanVault:
    def test_a_correctly_filed_vault_reports_nothing(self, vault):
        assert vault_audit.audit() == []

    def test_a_missing_vault_is_a_finding_not_an_exception(self, vault, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        monkeypatch.setattr(vault_maintenance.cm_vault, "VAULT_DIR", vault / "gone")
        findings = vault_audit.audit()
        # The caller is a scheduler thread or the doctor. Both want a report.
        assert _kinds(findings) == {"vault_missing"}


class TestSilentExclusion:
    def test_an_unknown_brand_value_is_an_error(self, vault):
        # This is the worst case in the vault: _matches_brand returns False for
        # EVERY brand, so a hyphen instead of an underscore costs the same as
        # deleting the note — with no error anywhere.
        _write(
            vault / "Areas" / "Delta_Cuisine" / "Typo.md",
            "---\nbrand: delta-cuisine\n---\n# Oops\nA rule nobody will ever read.\n",
        )
        found = _of_kind(vault_audit.audit(), "unknown_brand")
        assert len(found) == 1
        assert found[0].severity == "error"
        assert not found[0].fix  # cannot be guessed

    def test_a_non_para_folder_is_an_error(self, vault):
        # cm_vault only ever walks the four PARA folders, so anything under a
        # fifth is read by nothing.
        _write(vault / "Inbox" / "Draft.md", "# Draft\nSomething I meant to file.\n")
        found = _of_kind(vault_audit.audit(), "non_para_folder")
        assert len(found) == 1
        assert found[0].severity == "error"

    def test_truncation_is_flagged_only_where_it_costs_something(self, vault):
        big = "x" * (vault_audit._MAX_NOTE_CHARS + 500)
        _write(
            vault / "Projects" / "Delta_Cuisine" / "Huge.md",
            f"---\nbrand: delta_cuisine\n---\n# Huge\n{big}\n",
        )
        # Archive is excluded from _BRIEF_FOLDERS, so its length is free. A
        # checker that cries wolf gets muted, and this one guards a failure
        # nothing else catches.
        _write(
            vault / "Archive" / "Delta_Cuisine" / "Old.md",
            f"---\nbrand: delta_cuisine\n---\n# Old\n{big}\n",
        )
        paths = {f.path for f in _of_kind(vault_audit.audit(), "note_truncated")}
        assert paths == {"Projects/Delta_Cuisine/Huge.md"}

    def test_budget_overflow_is_an_error_and_names_the_dropped_notes(self, vault, monkeypatch):
        monkeypatch.setattr(vault_audit, "_MAX_BRIEF_CHARS", 300)
        for i in range(4):
            _write(
                vault / "Resources" / "Delta_Cuisine" / f"Ref{i}.md",
                f"---\nbrand: delta_cuisine\n---\n# Ref{i}\n" + ("y" * 200),
            )
        found = _of_kind(vault_audit.audit(), "brief_budget_exceeded")
        assert found and found[0].severity == "error"
        assert "delta_cuisine" in found[0].detail

    def test_budget_pressure_warns_before_anything_is_dropped(self, vault, monkeypatch):
        # Sized so the brand's notes land between the 80% warning line and the
        # hard cap: the point is the warning that arrives while everything still
        # fits, which is the only moment it is cheap to act on.
        monkeypatch.setattr(vault_audit, "_MAX_BRIEF_CHARS", 1000)
        _write(
            vault / "Resources" / "Delta_Cuisine" / "Ref.md",
            "---\nbrand: delta_cuisine\n---\n# Ref\n" + ("y" * 600),
        )
        findings = vault_audit.audit()
        assert not _of_kind(findings, "brief_budget_exceeded")
        found = _of_kind(findings, "brief_budget_pressure")
        assert found and found[0].severity == "warn"


class TestAutoFixBoundary:
    def test_missing_brand_is_added_from_the_folder(self, vault):
        _write(
            vault / "Areas" / "Ceram_Quartz" / "Quartz_Notes.md",
            "---\ntags: [area]\n---\n# Notes\nStanding rules for the slabs: never mix "
            "two stone families in one render, and always name the finish.\n",
        )
        findings = vault_audit.audit()
        found = _of_kind(findings, "missing_brand_frontmatter")
        assert len(found) == 1 and found[0].fix == "add_brand_frontmatter"

        vault_maintenance.apply_fixes(findings, dry_run=False)

        text = (vault / "Areas" / "Ceram_Quartz" / "Quartz_Notes.md").read_text(encoding="utf-8")
        assert "brand: ceram_quartz" in text
        # The one-line diff is what makes this safe to run unattended on notes
        # the user wrote: body content is never rewritten.
        assert "Standing rules for the slabs: never mix" in text
        assert "tags: [area]" in text
        assert vault_audit.audit() == []

    def test_a_disagreement_between_folder_and_frontmatter_is_never_guessed(self, vault):
        # Only the author knows which of the two is right. Picking one is how
        # cross-brand contamination happens.
        path = vault / "Areas" / "Ceram_Quartz" / "Confused.md"
        original = (
            "---\nbrand: delta_cuisine\n---\n# Confused\nWhose rule is this? Neither "
            "the folder nor the frontmatter is obviously the mistake.\n"
        )
        _write(path, original)

        findings = vault_audit.audit()
        found = _of_kind(findings, "brand_folder_mismatch")
        assert len(found) == 1
        assert found[0].severity == "error"
        assert not found[0].fix

        assert vault_maintenance.apply_fixes(findings, dry_run=False) == []
        assert path.read_text(encoding="utf-8") == original

    def test_dry_run_is_the_default_and_changes_nothing(self, vault):
        path = vault / "Areas" / "Ceram_Quartz" / "Quartz_Notes.md"
        _write(path, "---\ntags: [area]\n---\n# Notes\nStanding rules.\n")
        actions = vault_maintenance.apply_fixes(vault_audit.audit())
        assert actions and actions[0].startswith("would add")
        assert "brand:" not in path.read_text(encoding="utf-8")


class TestArchivingNeverDeletes:
    def test_a_project_the_user_marked_done_is_moved_not_removed(self, vault):
        src = vault / "Projects" / "Delta_Cuisine" / "Showroom_Launch.md"
        _write(src, "---\nbrand: delta_cuisine\nstatus: done\n---\n# Launch\nIt shipped.\n")

        findings = vault_audit.audit()
        assert _of_kind(findings, "finished_project")[0].fix == "archive_note"
        vault_maintenance.apply_fixes(findings, dry_run=False)

        dest = vault / "Archive" / "Delta_Cuisine" / "Showroom_Launch.md"
        assert not src.exists()
        assert dest.is_file()
        # Archive is indexed but excluded from briefs: retired, not lost.
        assert "It shipped." in dest.read_text(encoding="utf-8")

    def test_age_alone_is_reported_and_never_acted_on(self, vault):
        import os
        import time

        path = vault / "Projects" / "Delta_Cuisine" / "Old_Campaign.md"
        _write(path, "---\nbrand: delta_cuisine\n---\n# Old\nA campaign from a while ago.\n")
        old = time.time() - (vault_audit._STALE_PROJECT_DAYS + 30) * 86400
        os.utime(path, (old, old))

        findings = vault_audit.audit()
        found = _of_kind(findings, "aging_project")
        assert len(found) == 1
        # "Untouched for six months" is not the same as "finished" — a settled
        # strategy looks identical to an abandoned one from a timestamp.
        assert not found[0].fix
        vault_maintenance.apply_fixes(findings, dry_run=False)
        assert path.is_file()


class TestLessonPromotion:
    def test_only_repeated_feedback_becomes_a_written_rule(self, vault, monkeypatch):
        from core import cm_learning

        monkeypatch.setattr(
            cm_learning,
            "lessons_for",
            lambda brand: [
                {
                    "polarity": "avoid",
                    "category": "material",
                    "text": "no glossy lacquer",
                    "hits": 3,
                },
                {"polarity": "avoid", "category": "hook", "text": "a one-off remark", "hits": 1},
            ],
        )
        path = vault_maintenance.promote_lessons("delta_cuisine")
        body = path.read_text(encoding="utf-8")

        assert "no glossy lacquer" in body  # said 3x -> a rule
        assert "a one-off remark" not in body  # said once -> a reaction
        # Verbatim: paraphrasing is how a preference mutates into something the
        # user never asked for.
        assert "said 3×" in body

    def test_the_promoted_note_never_re_enters_the_brief(self, vault, monkeypatch):
        from core import cm_learning

        monkeypatch.setattr(
            cm_learning,
            "lessons_for",
            lambda brand: [
                {"polarity": "avoid", "category": "material", "text": "MARKER_LESSON", "hits": 4}
            ],
        )
        vault_maintenance.promote_lessons("delta_cuisine")
        # cm_learning.render_lessons() already carries these into every brief;
        # a second copy would spend vault budget restating them.
        context = cm_vault.render_vault_context("delta_cuisine", BRANDS["delta_cuisine"])
        assert "MARKER_LESSON" not in context

    def test_a_lesson_that_loses_its_hits_stops_being_stated(self, vault, monkeypatch, tmp_path):
        from core import cm_learning

        monkeypatch.setattr(
            cm_learning,
            "lessons_for",
            lambda brand: [{"polarity": "avoid", "category": "hook", "text": "x y z", "hits": 2}],
        )
        path = vault_maintenance.promote_lessons("delta_cuisine")
        assert path.is_file()

        # An empty-but-VALID store: the lessons really are gone.
        store = tmp_path / "cm_lessons.json"
        store.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cm_learning, "_LESSONS_PATH", store)
        monkeypatch.setattr(cm_learning, "lessons_for", lambda brand: [])
        assert vault_maintenance.promote_lessons("delta_cuisine") is None
        # Leaving it would assert rules that no longer have the hits to justify them.
        assert not path.is_file()

    def test_an_unreadable_store_does_not_wipe_the_note(self, vault, monkeypatch, tmp_path):
        from core import cm_learning

        monkeypatch.setattr(
            cm_learning,
            "lessons_for",
            lambda brand: [{"polarity": "avoid", "category": "hook", "text": "x y z", "hits": 2}],
        )
        path = vault_maintenance.promote_lessons("delta_cuisine")
        assert path.is_file()

        # _load_lessons() degrades a corrupt or transiently-unreadable store to
        # {}, and this vault lives on OneDrive where a read failing for a few
        # seconds is ordinary. "Unreadable" must not be read as "no lessons".
        store = tmp_path / "cm_lessons.json"
        store.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr(cm_learning, "_LESSONS_PATH", store)
        monkeypatch.setattr(cm_learning, "lessons_for", lambda brand: [])

        assert vault_maintenance.promote_lessons("delta_cuisine") is None
        assert path.is_file()


class TestSyncRoutine:
    def test_sync_writes_a_health_note_that_stays_out_of_the_brief(self, vault):
        report = vault_maintenance.sync(apply=False, promote=False)
        health = vault / vault_maintenance._HEALTH_NOTE
        assert health.is_file()
        assert "era_brief: false" in health.read_text(encoding="utf-8")
        assert report.summary()

    def test_the_health_note_records_what_needs_a_human(self, vault):
        _write(
            vault / "Areas" / "Delta_Cuisine" / "Typo.md",
            "---\nbrand: delta-cuisine\n---\n# Oops\nA rule nobody will ever read.\n",
        )
        vault_maintenance.sync(apply=False, promote=False)
        body = (vault / vault_maintenance._HEALTH_NOTE).read_text(encoding="utf-8")
        assert "Needs your attention" in body
        assert "delta-cuisine" in body

    def test_sync_never_raises(self, vault, monkeypatch):
        # It runs on a scheduler thread. An exception here takes down every other
        # scheduled job with it.
        monkeypatch.setattr(
            vault_maintenance, "audit", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        report = vault_maintenance.sync(apply=True)
        assert any(f.kind == "sync_failed" for f in report.findings)


class TestRunLogRotation:
    """Rotation must not damage the note it is trimming.

    The live note's own preamble says "Era only ever appends under `## Run log`" —
    so the literal heading appears TWICE in the file: once inside backticks in
    that sentence, once as the real heading. Rotation used
    `text.partition("## Run log")`, which matches the prose mention first, and
    rebuilt the file as `head + heading + entries` — eating the closing backtick,
    the full stop, and the real heading on every single rotation. Delta Cuisine's
    log had been chewed down to a sentence ending mid-word before this was caught.
    """

    def _log(self, vault, entries: int):
        path = cm_vault._log_note_path("delta_cuisine")
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            f"- **2026-07-{(i % 28) + 1:02d} 10:00** · `weekly` · **ok**\n    - order: run {i}"
            for i in range(entries)
        )
        path.write_text(
            "---\nbrand: delta_cuisine\n---\n\n# Era Run Log — delta_cuisine\n\n"
            "Written by Era automatically after every community-management run. "
            "Safe to read, annotate and prune by hand — Era only ever appends "
            f"under `{cm_vault._LOG_HEADING}`.\n\n{cm_vault._LOG_HEADING}\n\n{body}\n",
            encoding="utf-8",
        )
        return path

    def test_rotation_keeps_the_preamble_and_the_real_heading(self, vault):
        path = self._log(vault, cm_vault._LOG_ROTATE_AT + 5)

        cm_vault._rotate_run_log(path, "delta_cuisine")

        text = path.read_text(encoding="utf-8")
        assert "Safe to read, annotate and prune by hand" in text
        assert "appends under `## Run log`." in text  # backtick AND full stop intact
        assert len([ln for ln in text.splitlines() if ln.strip() == "## Run log"]) == 1

    def test_rotation_moves_the_overflow_and_keeps_the_newest(self, vault):
        path = self._log(vault, cm_vault._LOG_ROTATE_AT + 5)

        cm_vault._rotate_run_log(path, "delta_cuisine")

        live = path.read_text(encoding="utf-8")
        assert live.count("- **2026-07-") == cm_vault._LOG_KEEP_LIVE
        assert "run 24" in live  # newest survives in the live note
        assert "run 0" not in live  # oldest rotated out
        archive = cm_vault._archive_log_note_path("delta_cuisine").read_text(encoding="utf-8")
        assert "run 0" in archive

    def test_rotation_is_idempotent_below_the_threshold(self, vault):
        path = self._log(vault, 3)
        before = path.read_text(encoding="utf-8")
        cm_vault._rotate_run_log(path, "delta_cuisine")
        assert path.read_text(encoding="utf-8") == before

    def test_repeated_rotation_never_erodes_the_header(self, vault):
        # The original bug was cumulative: every rotation ate a little more.
        path = self._log(vault, cm_vault._LOG_ROTATE_AT + 5)
        for _ in range(3):
            cm_vault._rotate_run_log(path, "delta_cuisine")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    "\n"
                    + "\n".join(
                        f"- **2026-08-{i + 1:02d} 10:00** · `weekly` · **ok**" for i in range(15)
                    )
                    + "\n"
                )
        text = path.read_text(encoding="utf-8")
        assert "appends under `## Run log`." in text
        assert "# Era Run Log — delta_cuisine" in text
