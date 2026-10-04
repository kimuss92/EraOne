"""Era's own write path into the vault (actions/vault_writer.py).

This is what closed the gap behind a real, visible failure: asked to interview
her user and fill in VAULT-INDEX's Profile section, Era ran the whole interview
and then had nowhere to put the answers — every memory tool she had writes to a
JSON store under memory/, none of them to the vault.

The properties under test are the ones whose failure is silent or destructive:

  * APPEND BEFORE CREATE — a model asked to "save this" ten times must not leave
    ten near-duplicate notes.
  * FRONTMATTER IS OWNED HERE — everything that decides whether a note reaches a
    brief lives in it, and getting it wrong fails without an error.
  * REFUSE RATHER THAN GUESS — an unrecognized brand files one brand's strategy
    into the other's folder, and a write into a nightly-regenerated note is
    destroyed by the next pass.
  * NEVER DESTRUCTIVE — no deletes, no whole-note overwrites; a section replace
    touches one section.
"""

from __future__ import annotations

import pytest

from actions.vault_writer import vault_note
from core import cm_vault

BRANDS = {
    "delta_cuisine": ["delta", "delta cuisine", "dc"],
    "ceram_quartz": ["ceram", "quartz", "ceram quartz"],
}


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
    (root / "VAULT-INDEX.md").write_text(
        "---\ntype: index\n---\n# VAULT INDEX\n\n## Vault Structure\n\nstuff\n\n"
        "## Profile\n\n[FILL IN: ...]\n",
        encoding="utf-8",
    )
    (root / "02 - Delta Cuisine" / "Delta_Social_Strategy.md").write_text(
        "---\nbrand: delta_cuisine\ntype: reference\n---\n# Strategy\n\nFive reels a week.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)

    import era_dispatch as ed

    monkeypatch.setattr(
        ed,
        "resolve_brand",
        lambda name: next(
            (
                k
                for k, al in BRANDS.items()
                if name.strip().lower() in al or name.strip().lower() == k
            ),
            None,
        ),
    )
    return root


class TestTheInterviewCase:
    def test_overwriting_an_existing_section_is_refused_here_and_routed_to_the_gate(self, vault):
        """vault_note is ungated, so it must not be a fast path around the staged replace
        (found by the 2026-10-04 security review: it overwrote a section with no yes)."""
        before = (vault / "VAULT-INDEX.md").read_text(encoding="utf-8")
        out = vault_note(
            {
                "content": "I move fast. Conclusion first.",
                "title": "VAULT-INDEX",
                "section": "Profile",
            }
        )
        assert out.startswith("Could not overwrite") and "vault_bridge" in out
        assert (vault / "VAULT-INDEX.md").read_text(encoding="utf-8") == before

    def test_the_interview_answers_land_through_the_staged_replace(self, vault):
        """The case this was built for still works — with the user's yes in the loop."""
        from core import vault_bridge

        pid, _, diff = vault_bridge.stage_replace(
            vault_bridge.Caller(),
            {
                "title": "VAULT-INDEX",
                "section": "Profile",
                "content": "I move fast. Conclusion first.",
            },
        )
        assert "+I move fast. Conclusion first." in diff
        assert vault_bridge.run({"action": "replace", "_approved_plan": {"plan_id": pid}}).success
        body = (vault / "VAULT-INDEX.md").read_text(encoding="utf-8")
        assert "I move fast. Conclusion first." in body and "[FILL IN: ...]" not in body
        assert "## Vault Structure" in body and "stuff" in body  # not a note rewrite

    def test_a_missing_section_appends_rather_than_writing_nothing(self, vault):
        vault_note({"content": "some answer", "title": "VAULT-INDEX", "section": "Nonexistent"})
        body = (vault / "VAULT-INDEX.md").read_text(encoding="utf-8")
        assert "## Nonexistent" in body and "some answer" in body
        assert "## Profile" in body  # untouched


class TestAppendBeforeCreate:
    def test_an_existing_note_is_appended_to(self, vault):
        out = vault_note(
            {
                "content": "Never mix two stone families.",
                "title": "Delta Social Strategy",
                "brand": "delta",
            }
        )
        body = (vault / "02 - Delta Cuisine" / "Delta_Social_Strategy.md").read_text(
            encoding="utf-8"
        )

        assert "Added to" in out
        assert "Five reels a week." in body  # the original survives
        assert "Never mix two stone families." in body

    def test_matching_is_loose_enough_to_find_the_real_file(self, vault):
        # The model says "delta social strategy"; the file is
        # Delta_Social_Strategy.md. A strict match would create a duplicate.
        vault_note({"content": "x", "title": "delta social strategy", "brand": "delta"})
        assert len(list((vault / "02 - Delta Cuisine").glob("*.md"))) == 1

    def test_a_new_title_creates_a_note_with_full_frontmatter(self, vault):
        out = vault_note({"content": "Captions end with the address.", "title": "Caption Rules"})
        path = vault / "04 - Global" / "Caption_Rules.md"

        assert "Created" in out and path.is_file()
        body = path.read_text(encoding="utf-8")
        assert "project: meta" in body
        assert "type: reference" in body  # the default, and what ranks first
        assert "brand:" not in body  # global note — a brand would hide it


class TestBrandRouting:
    def test_a_brand_note_lands_in_that_brands_folder_with_its_key(self, vault):
        vault_note(
            {"content": "Slab names must be exact.", "title": "Slab Rules", "brand": "Ceram Quartz"}
        )
        body = (vault / "03 - Ceram Quartz" / "Slab_Rules.md").read_text(encoding="utf-8")
        assert "brand: ceram_quartz" in body
        assert "project: ceram-quartz" in body  # hyphen for jaredrhod, underscore for Era

    def test_an_unrecognized_brand_is_refused_not_guessed(self, vault):
        out = vault_note({"content": "x", "title": "Foo", "brand": "Delta Quartz"})
        # Guessing files one brand's strategy into the other's folder — the exact
        # failure the whole isolation layer exists to prevent.
        assert "won't guess" in out
        assert not list((vault / "02 - Delta Cuisine").glob("Foo.md"))
        assert not list((vault / "03 - Ceram Quartz").glob("Foo.md"))

    def test_written_notes_reach_the_right_brief_and_only_that_one(self, vault):
        vault_note({"content": "MARKER_DELTA", "title": "Delta Rule", "brand": "delta"})
        delta = cm_vault.render_vault_context(
            "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
        )
        ceram = cm_vault.render_vault_context(
            "ceram_quartz", BRANDS["ceram_quartz"], other_brands=BRANDS
        )
        assert "MARKER_DELTA" in delta
        assert "MARKER_DELTA" not in ceram


class TestRefusals:
    @pytest.mark.parametrize(
        "title",
        ["Era_Run_Log_delta_cuisine", "Era_Learned_Preferences_delta_cuisine", "Era_Vault_Health"],
    )
    def test_regenerated_notes_are_refused(self, vault, title):
        (vault / "02 - Delta Cuisine" / f"{title}.md").write_text("# log\n", encoding="utf-8")
        out = vault_note({"content": "hand-written line", "title": title, "brand": "delta"})
        # Accepting would look like success and be destroyed by the next nightly
        # pass — silently.
        assert "rebuild automatically" in out
        assert "hand-written line" not in (vault / "02 - Delta Cuisine" / f"{title}.md").read_text(
            "utf-8"
        )

    def test_empty_content_saves_nothing(self, vault):
        assert "nothing to save" in vault_note({"content": "   ", "title": "X"})

    def test_a_missing_vault_reports_instead_of_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "gone")
        assert "can't reach the vault" in vault_note({"content": "x", "title": "X"})

    def test_no_title_asks_rather_than_inventing_a_filename(self, vault):
        assert "need a title" in vault_note({"content": "something worth keeping"})


class TestGuardrails:
    def test_growing_past_the_cap_is_reported_at_write_time(self, vault):
        out = vault_note(
            {
                "content": "x" * (cm_vault._MAX_NOTE_CHARS + 100),
                "title": "Big Note",
                "brand": "delta",
            }
        )
        # Past the cap the tail is cut from every brief and nothing says so.
        # Splitting is cheapest at the moment of writing.
        assert "past the" in out and "cap" in out

    def test_reading_a_note_back_strips_frontmatter(self, vault):
        out = vault_note({"action": "read", "title": "Delta Social Strategy", "brand": "delta"})
        assert "Five reels a week." in out
        assert "brand: delta_cuisine" not in out

    def test_reading_a_missing_note_says_so(self, vault):
        assert "no note called" in vault_note({"action": "read", "title": "Nope"})


class TestOfflineSpool:
    """The vault sits on OneDrive, so `vault_available()` returning False is a
    routine transient, not a permanent state. It used to DISCARD the note —
    Era said "nothing was saved" and whatever the user had just dictated was
    gone. For a subsystem whose premise is "anything I tell you must be
    saved", that is the worst available outcome.
    """

    @pytest.fixture
    def spool_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "memstore"
        d.mkdir()
        monkeypatch.setenv("ERA_MEMORY_DIR", str(d))
        return d

    def test_unreachable_vault_holds_instead_of_dropping(self, vault, spool_dir, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        reply = vault_note(
            {"action": "save", "title": "Showroom Rules", "content": "Opens at nine."}
        )
        assert "holding" in reply.lower()
        held = (spool_dir / "vault_pending.jsonl").read_text(encoding="utf-8")
        assert "Opens at nine." in held

    def test_held_note_lands_when_the_vault_returns(self, vault, spool_dir, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        vault_note({"action": "save", "title": "Showroom Rules", "content": "Opens at nine."})

        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault)
        reply = vault_note(
            {"action": "save", "title": "Showroom Rules", "content": "Closed Sundays."}
        )
        body = (vault / "04 - Global" / "Showroom_Rules.md").read_text(encoding="utf-8")
        assert "Opens at nine." in body
        assert "Closed Sundays." in body
        assert "holding" in reply.lower() or "was holding" in reply.lower()

    def test_held_notes_keep_their_order(self, vault, spool_dir, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        for n in ("first", "second", "third"):
            vault_note({"action": "save", "title": "Ordered", "content": n})

        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault)
        vault_note({"action": "save", "title": "Ordered", "content": "fourth"})
        body = (vault / "04 - Global" / "Ordered.md").read_text(encoding="utf-8")
        positions = [body.index(n) for n in ("first", "second", "third", "fourth")]
        assert positions == sorted(positions)

    def test_spool_is_cleared_after_a_successful_flush(self, vault, spool_dir, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        vault_note({"action": "save", "title": "Ordered", "content": "held"})
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault)
        vault_note({"action": "save", "title": "Ordered", "content": "live"})
        assert not (spool_dir / "vault_pending.jsonl").exists()

    def test_a_refusal_is_not_retried_forever(self, vault, spool_dir):
        """An unrecognized brand is a DECISION, not a transport failure. Holding
        it would replay the same refusal on every future save."""
        reply = vault_note(
            {
                "action": "save",
                "title": "Whatever",
                "content": "something",
                "brand": "not a real brand",
            }
        )
        assert "don't recognize" in reply
        assert not (spool_dir / "vault_pending.jsonl").exists()

    def test_empty_content_is_never_held(self, vault, spool_dir, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        assert "nothing to save" in vault_note({"action": "save", "content": "   "}).lower()
        assert not (spool_dir / "vault_pending.jsonl").exists()

    def test_corrupt_spool_line_does_not_strand_the_rest(self, vault, spool_dir, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        vault_note({"action": "save", "title": "Ordered", "content": "good one"})
        spool = spool_dir / "vault_pending.jsonl"
        spool.write_text("{not json\n" + spool.read_text(encoding="utf-8"), encoding="utf-8")

        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault)
        vault_note({"action": "save", "title": "Ordered", "content": "live"})
        body = (vault / "04 - Global" / "Ordered.md").read_text(encoding="utf-8")
        assert "good one" in body

    def test_holding_never_raises(self, vault, monkeypatch, tmp_path):
        """Degrade, never raise — the CM invariant. A blocked spool costs the
        note, never the tool call."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("ERA_MEMORY_DIR", str(blocked))
        monkeypatch.setattr(cm_vault, "VAULT_DIR", vault / "gone")
        reply = vault_note({"action": "save", "title": "X", "content": "y"})
        assert isinstance(reply, str) and reply
