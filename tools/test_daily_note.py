"""Era's day log (core/daily_note.py) and the `01 - Daily Notes/` contract.

Two properties carry the weight here:

  * IT NEVER REACHES A BRIEF. `01 - Daily Notes` is walked by cm_vault (so the
    log is indexed and findable) and deliberately absent from _BRIEF_FOLDERS. A
    day log is a record of what HAPPENED; a brief carries instructions about what
    to MAKE next. Feeding the first into the second spends the brand's character
    budget on history no upcoming post needs.
  * IT APPENDS, NEVER OVERWRITES. Several runs land on the same day, from the
    scheduler and from voice orders. Two partial records of one day — each
    missing what the other has — is the one failure this cannot survive.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core import cm_vault, daily_note, vault_audit


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "era_brain"
    for folder in (
        "00 - Inbox",
        "01 - Daily Notes",
        "02 - Delta Cuisine",
        "03 - Ceram Quartz",
        "04 - Global",
        "05 - Archive",
        "06 - Resources",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    return root


def _template(root, body: str) -> None:
    (root / "01 - Daily Notes" / "Daily Note Template.md").write_text(body, encoding="utf-8")


WHEN = datetime(2026, 7, 31, 16, 7)


class TestNeverReachesABrief:
    def test_daily_notes_is_indexed_but_not_briefed(self):
        # The distinction IS the mechanism — in _PARA_ORDER so it is walked and
        # findable, out of _BRIEF_FOLDERS so it is never rendered.
        assert "01 - Daily Notes" in cm_vault._PARA_ORDER
        assert "01 - Daily Notes" not in cm_vault._BRIEF_FOLDERS

    def test_a_day_log_never_enters_the_rendered_context(self, vault):
        (vault / "02 - Delta Cuisine" / "Rule.md").write_text(
            "---\nbrand: delta_cuisine\ntype: reference\n---\n# Rule\nFive reels a week.\n",
            encoding="utf-8",
        )
        daily_note.append_session("Delta Cuisine — weekly", done=["MARKER_DAYLOG"], when=WHEN)

        context = cm_vault.render_vault_context("delta_cuisine", ["delta"])
        assert "Five reels a week" in context
        assert "MARKER_DAYLOG" not in context

    def test_daily_notes_is_not_flagged_as_a_stray_top_level_folder(self, vault, monkeypatch):
        # vault_audit errors on any top-level folder outside PARA, because notes
        # under one are read by nothing. Logs must not trip that.
        monkeypatch.setattr(vault_audit, "_brand_registry", lambda: {"delta_cuisine": ["delta"]})
        daily_note.append_session("Delta Cuisine — weekly", done=["ran"], when=WHEN)
        kinds = {f.kind for f in vault_audit.audit()}
        assert "non_para_folder" not in kinds


class TestAppendNeverOverwrites:
    def test_a_second_run_becomes_session_2(self, vault):
        daily_note.append_session("Delta Cuisine — weekly", done=["first run"], when=WHEN)
        path = daily_note.append_session("Ceram Quartz — revise", done=["second run"], when=WHEN)

        body = path.read_text(encoding="utf-8")
        assert "## Session 1" in body and "## Session 2" in body
        assert "first run" in body and "second run" in body

    def test_the_session_number_is_read_off_the_file_not_memory(self, vault):
        # A restart mid-day must continue the sequence, not reset it to 1.
        daily_note.append_session("A", done=["a"], when=WHEN)
        daily_note.append_session("B", done=["b"], when=WHEN)
        path = daily_note.note_path(WHEN)
        assert daily_note._next_session_number(path.read_text(encoding="utf-8")) == 3

    def test_an_existing_flat_note_is_honoured_where_it_sits(self, vault):
        # Writing blind to the canonical month-subfolder path would create a
        # SECOND file for a day that already had one.
        flat = vault / "01 - Daily Notes" / "2026-07-31.md"
        flat.write_text("# Friday, July 31, 2026\n\n## Index\n\n## Session 1 — earlier\n", "utf-8")

        path = daily_note.append_session("Later run", done=["later"], when=WHEN)

        assert path == flat
        assert not (vault / "01 - Daily Notes" / "07 - July 2026" / "2026-07-31.md").exists()
        assert "## Session 2" in flat.read_text(encoding="utf-8")

    def test_every_run_adds_a_line_to_the_index(self, vault):
        daily_note.append_session("Delta Cuisine — weekly", done=["first"], when=WHEN)
        path = daily_note.append_session("Ceram Quartz — revise", done=["second"], when=WHEN)
        index = path.read_text(encoding="utf-8").split("## Session 1")[0]
        assert "**Delta Cuisine — weekly**" in index
        assert "**Ceram Quartz — revise**" in index


class TestTemplateHandling:
    def test_obsidian_placeholders_are_expanded(self, vault):
        _template(
            vault,
            "---\ncreated: {{date:YYYY-MM-DD}}\n---\n\n# {{Day of week}}, {{Month}} {{Day}}, {{Year}}\n\n## Index\n",
        )
        path = daily_note.append_session("Run", done=["ran"], when=WHEN)
        body = path.read_text(encoding="utf-8")
        # Obsidian only expands these when a human inserts the template; nothing
        # expands them on a direct write, so a verbatim copy titles the note
        # "# {{Day of week}}, {{Month}}...".
        assert "# Friday, July 31, 2026" in body
        assert "created: 2026-07-31" in body
        assert "{{" not in body

    def test_the_templates_example_index_bullet_is_dropped(self, vault):
        _template(
            vault,
            "---\n---\n\n# {{Day of week}}\n\n## Index\n\n- **Session topic name** — one-line summary\n",
        )
        body = daily_note.append_session("Run", done=["ran"], when=WHEN).read_text(encoding="utf-8")
        # Instructional filler. Carried through, it leaves a permanent fake entry
        # at the top of a real day.
        assert "Session topic name" not in body

    def test_the_first_bullet_does_not_weld_onto_the_index_heading(self, vault):
        _template(vault, "---\n---\n\n# {{Day of week}}\n\n## Index\n")
        body = daily_note.append_session("Run", done=["ran"], when=WHEN).read_text(encoding="utf-8")
        assert "## Index- " not in body
        assert "## Index\n- **Run**" in body

    def test_a_missing_template_still_produces_a_usable_note(self, vault):
        path = daily_note.append_session("Run", done=["ran"], when=WHEN)
        body = path.read_text(encoding="utf-8")
        assert "# Friday, July 31, 2026" in body
        assert "type: log" in body


class TestDegradeNeverRaise:
    def test_a_missing_vault_returns_none_instead_of_raising(self, tmp_path, monkeypatch):
        # This runs off the back of a CM job that already succeeded. A log failure
        # must never turn that into a failure.
        monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "nope")
        assert daily_note.append_session("Run", done=["ran"], when=WHEN) is None

    def test_doctor_reports_without_writing(self, vault):
        info = daily_note.doctor()
        assert info["exists"] is True
        assert info["today_exists"] is False
        assert not list((vault / "01 - Daily Notes").glob("**/2*.md"))
