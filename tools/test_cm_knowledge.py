"""Tests for the CM knowledge layer: vault reading (core/cm_vault.py) and
feedback learning (core/cm_learning.py), plus their wiring into build_brief.

The properties under test are the ones whose failure is expensive and silent:

  * BRAND ISOLATION — a note or a lesson belonging to one brand must never
    reach another brand's brief. This is the same class of failure as posting
    to the wrong account, and it fails quietly (a plausible-looking brief with
    the wrong brand's rules in it), so it is tested from several angles.
  * DEGRADE, NEVER RAISE — a missing vault, an unreadable note or a corrupt
    lesson store must cost context, never the job. A CM run that dies because
    OneDrive is offline is strictly worse than one that runs without notes.
  * RULES OUTRANK INSTANCES — Areas/Resources notes must win the brief's
    character budget over Projects notes, or standing rules get starved out by
    verbose campaign notes.

Every test redirects the module-level paths to tmp_path, so running the suite
never reads or writes the user's real vault or lesson store.
"""

import importlib
import json

import pytest

from core import cm_learning, cm_vault

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BRANDS = {
    "delta_cuisine": ["delta", "delta cuisine", "dc"],
    "ceram_quartz": ["ceram", "ceram quartz", "quartz", "ceramquartz"],
}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A small vault mirroring the real PARA layout and frontmatter style."""
    root = tmp_path / "era_brain"
    _write(
        root / "Areas" / "Delta_Social_Strategy.md",
        "---\ntags: [area, delta-cuisine, social]\nbrand: delta_cuisine\n---\n"
        "# Delta strategy\nFive to six reels per week. DELTA_MARKER\n",
    )
    _write(
        root / "Areas" / "Quartz_Market_Research.md",
        "---\ntags: [area, ceram-quartz]\nbrand: ceram_quartz\n---\n"
        "# Quartz research\nCarousels beat reels. CERAM_MARKER\n",
    )
    # Brand ownership by TAG only, with no `brand:` field — the case that used
    # to leak into the other brand's brief.
    _write(
        root / "Resources" / "Tile_Material_Library.md",
        "---\ntags: [resource, ceram-quartz, materials]\n---\n"
        "# Tiles\nOne texture per post. TAGGED_CERAM_MARKER\n",
    )
    _write(
        root / "Resources" / "Kitchen_Design_Trends.md",
        "---\ntags: [resource, trends]\n---\n# Trends\nWarm woods are back. GLOBAL_MARKER\n",
    )
    _write(
        root / "Projects" / "Delta_Showroom_Launch.md",
        "---\ntags: [project, delta-cuisine]\nbrand: delta_cuisine\n---\n"
        "# Showroom\nLaunch in September. DELTA_PROJECT_MARKER\n",
    )
    # Must be ignored entirely — plugin data is not content.
    _write(root / ".obsidian" / "plugins" / "dataview" / "main.js", "var x = 1;")

    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    return root


@pytest.fixture
def lessons(tmp_path, monkeypatch):
    """Redirect the lesson store so tests never touch memory/cm_lessons.json."""
    monkeypatch.setattr(cm_learning, "_MEMORY_DIR", tmp_path)
    monkeypatch.setattr(cm_learning, "_LESSONS_PATH", tmp_path / "cm_lessons.json")
    monkeypatch.setattr(cm_learning, "_EVENTS_PATH", tmp_path / "cm_feedback.jsonl")
    return tmp_path


# ---------------------------------------------------------------------------
# Vault — indexing and parsing
# ---------------------------------------------------------------------------


def test_load_vault_indexes_markdown_and_skips_obsidian_internals(vault):
    titles = {n.title for n in cm_vault.load_vault()}
    assert "Delta_Social_Strategy" in titles
    assert "main" not in titles, ".obsidian plugin files must never be indexed"


def test_frontmatter_is_parsed_and_stripped_from_body(vault):
    note = next(n for n in cm_vault.load_vault() if n.title == "Delta_Social_Strategy")
    assert note.brand == "delta_cuisine"
    assert "area" in note.tags and "delta-cuisine" in note.tags
    assert not note.body.startswith("---"), "frontmatter must not survive into the body"
    assert "DELTA_MARKER" in note.body


def test_malformed_frontmatter_degrades_instead_of_raising(vault):
    """A hand-written note with an unquoted colon must not take the loader down."""
    _write(
        vault / "Areas" / "Broken.md",
        "---\nthis line: has: too many: colons\n[not a mapping\n---\n\nBody survives.\n",
    )
    notes = cm_vault.load_vault()
    broken = next(n for n in notes if n.title == "Broken")
    assert "Body survives." in broken.body


def test_missing_vault_returns_empty_and_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "does_not_exist")
    assert cm_vault.vault_available() is False
    assert cm_vault.load_vault() == []
    assert cm_vault.render_vault_context("delta_cuisine", ["delta"]) == ""


# ---------------------------------------------------------------------------
# Vault — brand isolation (the expensive-failure property)
# ---------------------------------------------------------------------------


def test_brand_notes_and_global_notes_are_included(vault):
    titles = {
        n.title
        for n in cm_vault.notes_for_brand(
            "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
        )
    }
    assert "Delta_Social_Strategy" in titles
    assert "Kitchen_Design_Trends" in titles, "untagged notes are shared context"


def test_other_brands_notes_are_excluded_by_frontmatter(vault):
    titles = {
        n.title
        for n in cm_vault.notes_for_brand(
            "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
        )
    }
    assert "Quartz_Market_Research" not in titles


def test_other_brands_notes_are_excluded_by_tag_alone(vault):
    """Regression: a note owned by a TAG with no `brand:` field used to leak.

    Tile_Material_Library carries `tags: [resource, ceram-quartz]` and no
    `brand:` key. Trusting only the `brand:` field for exclusion let it render
    into Delta Cuisine's brief — a Ceram Quartz material rule steering a Delta
    Cuisine kitchen.
    """
    delta = {
        n.title
        for n in cm_vault.notes_for_brand(
            "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
        )
    }
    ceram = {
        n.title
        for n in cm_vault.notes_for_brand(
            "ceram_quartz", BRANDS["ceram_quartz"], other_brands=BRANDS
        )
    }
    assert "Tile_Material_Library" not in delta
    assert "Tile_Material_Library" in ceram


def test_rendered_context_carries_no_other_brand_content(vault):
    text = cm_vault.render_vault_context(
        "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
    )
    assert "DELTA_MARKER" in text
    assert "CERAM_MARKER" not in text
    assert "TAGGED_CERAM_MARKER" not in text


# ---------------------------------------------------------------------------
# Vault — budget and ordering
# ---------------------------------------------------------------------------


def test_rules_outrank_instances_when_the_budget_is_tight(vault, monkeypatch):
    """Regression: verbose Projects notes used to starve out Resources notes.

    Ranking brand-specificity above folder rank meant a brand's long campaign
    notes consumed the whole budget and the global generation-standards note —
    a direct control on credit spend — silently stopped reaching the brief.
    """
    _write(
        vault / "Projects" / "Delta_Huge_Campaign.md",
        "---\ntags: [project, delta-cuisine]\nbrand: delta_cuisine\n---\n"
        "# Huge\n" + ("padding paragraph. " * 400),
    )
    monkeypatch.setattr(cm_vault, "_MAX_BRIEF_CHARS", 2500)

    text = cm_vault.render_vault_context(
        "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
    )
    assert "GLOBAL_MARKER" in text, "a Resources rule must outrank a Projects instance"


def test_long_notes_are_truncated_not_dropped(vault, monkeypatch):
    _write(
        vault / "Areas" / "Delta_Long.md",
        "---\nbrand: delta_cuisine\n---\n# Long\nSTART_MARKER "
        + ("filler " * 3000)
        + " END_MARKER\n",
    )
    monkeypatch.setattr(cm_vault, "_MAX_NOTE_CHARS", 400)
    text = cm_vault.render_vault_context(
        "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
    )
    assert "START_MARKER" in text
    assert "END_MARKER" not in text
    assert "truncated" in text


def test_context_states_that_the_brand_lock_wins(vault):
    """The vault must never be able to re-point routing, and must say so."""
    text = cm_vault.render_vault_context(
        "delta_cuisine", BRANDS["delta_cuisine"], other_brands=BRANDS
    )
    assert "BRAND LOCK" in text


# ---------------------------------------------------------------------------
# Vault — write-back
# ---------------------------------------------------------------------------


def test_append_run_log_creates_then_appends(vault):
    first = cm_vault.append_run_log("delta_cuisine", "weekly", "plan the week", "ok", "3 files")
    assert first is not None and first.exists()
    cm_vault.append_run_log("delta_cuisine", "revise", "change reel 1", "ok")

    text = first.read_text(encoding="utf-8")
    assert text.count("- **") == 2, "each run appends exactly one entry"
    assert "plan the week" in text and "change reel 1" in text
    assert "brand: delta_cuisine" in text, "Era's own note is brand-tagged for recall"


def test_append_run_log_is_noop_without_a_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "gone")
    assert cm_vault.append_run_log("delta_cuisine", "weekly", "order", "ok") is None


# ---------------------------------------------------------------------------
# Learning — capture, classification, reinforcement
# ---------------------------------------------------------------------------


def test_correction_is_recorded_with_the_users_own_words(lessons):
    cm_learning.record_feedback(
        "delta_cuisine", "change reel 1's texture, use darker wood", "revise"
    )
    stored = cm_learning.lessons_for("delta_cuisine")
    assert len(stored) == 1
    assert stored[0]["text"] == "change reel 1's texture, use darker wood"
    assert stored[0]["polarity"] == "avoid"
    assert stored[0]["category"] == "material"


def test_approval_is_recorded_as_a_preference(lessons):
    cm_learning.record_feedback("delta_cuisine", "approve reel 2, that hook is perfect", "approve")
    assert cm_learning.lessons_for("delta_cuisine")[0]["polarity"] == "prefer"


def test_repeated_feedback_reinforces_instead_of_duplicating(lessons):
    """The item number is which asset you looked at, not what you were teaching."""
    cm_learning.record_feedback(
        "delta_cuisine", "change reel 1 texture, that laminate is wrong", "revise"
    )
    cm_learning.record_feedback(
        "delta_cuisine", "change reel 3 texture, that laminate is wrong", "revise"
    )
    stored = cm_learning.lessons_for("delta_cuisine")
    assert len(stored) == 1, "same lesson from different items must merge"
    assert stored[0]["hits"] == 2
    assert "firm rule" in cm_learning.render_lessons("delta_cuisine")


def test_distinct_feedback_does_not_merge(lessons):
    cm_learning.record_feedback("delta_cuisine", "change reel 1's texture to darker wood", "revise")
    cm_learning.record_feedback(
        "delta_cuisine", "the schedule is wrong, never post on Sunday", "revise"
    )
    assert len(cm_learning.lessons_for("delta_cuisine")) == 2


def test_lessons_are_brand_scoped(lessons):
    cm_learning.record_feedback("delta_cuisine", "never use that laminate texture again", "revise")
    assert cm_learning.lessons_for("ceram_quartz") == []
    assert "laminate" not in cm_learning.render_lessons("ceram_quartz")


def test_trivial_orders_are_ignored(lessons):
    assert cm_learning.record_feedback("delta_cuisine", "ok", "revise") is None
    assert cm_learning.record_feedback("", "change reel 1's texture", "revise") is None


def test_store_is_capped(lessons, monkeypatch):
    monkeypatch.setattr(cm_learning, "_MAX_LESSONS_PER_BRAND", 5)
    for i in range(20):
        cm_learning.record_feedback(
            "delta_cuisine", f"change the {i} colour tone to something else", "revise"
        )
    assert len(cm_learning.lessons_for("delta_cuisine")) <= 5


def test_corrupt_lesson_store_degrades_to_empty(lessons):
    (lessons / "cm_lessons.json").write_text("{not valid json", encoding="utf-8")
    assert cm_learning.lessons_for("delta_cuisine") == []
    assert cm_learning.render_lessons("delta_cuisine") == ""


def test_events_are_appended_as_jsonl(lessons):
    cm_learning.record_feedback("delta_cuisine", "change reel 1's texture please", "revise")
    cm_learning.record_feedback("delta_cuisine", "approve reel 2 it is perfect", "approve")
    lines = (lessons / "cm_feedback.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["brand"] == "delta_cuisine" for line in lines)


def test_render_is_empty_when_nothing_learned(lessons):
    assert cm_learning.render_lessons("delta_cuisine") == ""


# ---------------------------------------------------------------------------
# Wiring into the brief
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch(vault, lessons, monkeypatch):
    import era_dispatch

    importlib.reload(era_dispatch)
    monkeypatch.setattr(era_dispatch.cm_vault, "VAULT_DIR", vault)
    monkeypatch.setattr(era_dispatch.cm_learning, "_MEMORY_DIR", lessons)
    monkeypatch.setattr(era_dispatch.cm_learning, "_LESSONS_PATH", lessons / "cm_lessons.json")
    monkeypatch.setattr(era_dispatch.cm_learning, "_EVENTS_PATH", lessons / "cm_feedback.jsonl")
    return era_dispatch


def test_brief_carries_vault_and_lessons_for_the_right_brand(dispatch):
    cfg = dispatch.load_brands()
    dispatch.cm_learning.record_feedback(
        "delta_cuisine", "change reel 1's texture, that laminate is wrong", "revise"
    )

    brief = dispatch.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "weekly",
        "plan the week",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )

    assert "DELTA_MARKER" in brief, "vault context must reach the brief"
    assert "that laminate is wrong" in brief, "learned lessons must reach the brief"
    assert "CERAM_MARKER" not in brief
    assert "TAGGED_CERAM_MARKER" not in brief


def test_brief_still_builds_when_the_vault_is_gone(dispatch, tmp_path, monkeypatch):
    """Context is a bonus. Losing it must never cost the job."""
    monkeypatch.setattr(dispatch.cm_vault, "VAULT_DIR", tmp_path / "gone")
    cfg = dispatch.load_brands()
    brief = dispatch.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "weekly",
        "plan the week",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "BRAND LOCK" in brief
    assert "TASK TYPE: weekly" in brief


def test_brief_survives_a_raising_context_source(dispatch, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("vault exploded")

    monkeypatch.setattr(dispatch.cm_vault, "render_vault_context", boom)
    monkeypatch.setattr(dispatch.cm_learning, "render_lessons", boom)
    cfg = dispatch.load_brands()
    brief = dispatch.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "weekly",
        "plan the week",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "BRAND LOCK" in brief


def test_build_brief_works_without_all_brands(dispatch):
    """Backwards compatibility: existing callers omit the new kwarg."""
    cfg = dispatch.load_brands()
    brief = dispatch.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "weekly",
        "plan the week",
        defaults=cfg.get("defaults"),
    )
    assert "BRAND LOCK" in brief


# ---------------------------------------------------------------------------
# Self-critique QC gate — the quality bar that turns "compliant but flat"
# first drafts into post-ready assets before the user ever sees them.
# ---------------------------------------------------------------------------


def _brief_for(dispatch, task_type):
    cfg = dispatch.load_brands()
    return dispatch.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        task_type,
        "plan the week",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )


def _brief_for_brand(dispatch, brand_key, task_type, order="plan the week"):
    cfg = dispatch.load_brands()
    return dispatch.build_brief(
        brand_key,
        cfg["brands"][brand_key],
        task_type,
        order,
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )


@pytest.mark.parametrize(
    "task_type", ["weekly", "campaign", "rush", "revise", "approve", "adcreative"]
)
def test_qc_gate_present_for_producing_lanes(dispatch, task_type):
    """Every lane that generates a visual must carry the self-critique bar."""
    brief = _brief_for(dispatch, task_type)
    assert "SELF-CRITIQUE GATE" in brief
    # The six checks and the re-render rule are the teeth — spot-check the load-bearing ones.
    assert "LIVED-IN" in brief
    assert "SELECTIVE re-render" in brief
    assert "TWO re-rolls" in brief
    assert "reel-art-director" in brief


@pytest.mark.parametrize("task_type", ["publish", "ads"])
def test_qc_gate_absent_for_non_producing_lanes(dispatch, task_type):
    """Publish reuses already-approved assets; ads reuses organic creative.
    Neither generates a new visual, so the production QC gate must not appear."""
    brief = _brief_for(dispatch, task_type)
    assert "SELF-CRITIQUE GATE" not in brief


# ---------------------------------------------------------------------------
# brands.yaml — the merged design rules must survive edits to the file
# ---------------------------------------------------------------------------


def test_delta_kitchen_formula_is_intact():
    """The three-material formula is the brand's fixed build rule."""
    import era_dispatch

    kitchen = era_dispatch.load_brands()["brands"]["delta_cuisine"]["design_rules"]["kitchen"]
    assert set(kitchen["composition_formula"]) == {"stone", "wood", "plain"}
    assert "dark glass" in kitchen["tall_cabinetry"].lower()
    assert "black matte aluminium" in kitchen["plinth"].lower()
    for brand in ("whirlpool", "rosières"):
        assert brand in kitchen["appliances"].lower()


def test_delta_wardrobe_has_three_distinct_configurations():
    import era_dispatch

    configs = era_dispatch.load_brands()["brands"]["delta_cuisine"]["design_rules"]["wardrobe"][
        "allowed_configurations"
    ]
    assert len(configs) == len(set(configs)) == 3
    assert any("combination" in c.lower() for c in configs)


def test_delta_wardrobe_accessories_are_mandatory_but_capped():
    import era_dispatch

    acc = era_dispatch.load_brands()["brands"]["delta_cuisine"]["design_rules"]["wardrobe"][
        "accessories"
    ]
    assert acc["required"] is True
    assert "3" in acc["select"], "presence is mandatory, quantity stays capped"


def test_both_brands_point_at_their_master_magnific_space():
    import era_dispatch

    brands = era_dispatch.load_brands()["brands"]
    assert "a243e483-a215-4f17-895c-0896895bc3f3" in brands["delta_cuisine"]["magnific_space"]
    assert "a24c6c83-0bd2-48db-bdfc-49d0e30b7a28" in brands["ceram_quartz"]["magnific_space"]


# ---------------------------------------------------------------------------
# Ceram Quartz design rules — this brand had NO design_rules block until
# 2026-07-29, which meant build_brief's design_note rendered EMPTY for every
# Ceram Quartz run. The failure was invisible: the brief still looked complete,
# it just carried none of the brand's visual law. These tests exist so that
# regression cannot come back unnoticed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("brand_key", ["delta_cuisine", "ceram_quartz"])
def test_every_active_brand_has_design_rules(brand_key):
    import era_dispatch

    rules = era_dispatch.load_brands()["brands"][brand_key].get("design_rules")
    assert rules, f"{brand_key} has no design_rules — its brief's design_note will be empty"


@pytest.mark.parametrize("brand_key", ["delta_cuisine", "ceram_quartz"])
def test_design_rules_actually_reach_the_brief(dispatch, brand_key):
    brief = _brief_for_brand(dispatch, brand_key, "adcreative")
    assert "DESIGN RULES" in brief


def test_ceram_quartz_palette_and_claims_are_locked():
    """The two things this brand's content most easily drifts on: an invented
    accent colour, and an invented product benefit."""
    import era_dispatch

    rules = era_dispatch.load_brands()["brands"]["ceram_quartz"]["design_rules"]
    palette = rules["palette"]
    assert "#133C4F" in palette["petrol_teal_primary"]
    assert "#18E4E4" in palette["accent_cyan"]
    assert "never" in palette

    claims = rules["approved_claims"]
    # The slide-2 set is FIXED and ordered — it does not rotate per product.
    for fact in ("FAIBLE ABSORPTION", "LONGUE DURÉE DE VIE", "RÉSISTANT AUX PRODUITS"):
        assert fact in claims["slide_2_fixed_set"]


def test_ceram_quartz_never_list_keeps_the_brands_apart():
    import era_dispatch

    never = " ".join(era_dispatch.load_brands()["brands"]["ceram_quartz"]["design_rules"]["never"])
    assert "Delta Cuisine" in never, "cross-brand asset bleed must stay an explicit ban"
    assert "TikTok" in never, "this brand is Instagram + Facebook only"


def test_interior_design_checklist_is_not_pushed_at_ceram_quartz(dispatch):
    """The `interior-design` skill is Delta Cuisine's (kitchen/wardrobe material
    law). Now that Ceram Quartz has design_rules too, the shared design_note must
    not point its runs at another brand's checklist."""
    dc = _brief_for_brand(dispatch, "delta_cuisine", "adcreative")
    cq = _brief_for_brand(dispatch, "ceram_quartz", "adcreative")
    assert "pre-generation checklist" in dc
    assert "pre-generation checklist" not in cq


# ---------------------------------------------------------------------------
# Magnific ad-creative lane. Two capabilities LOOK available and are not
# (Agents, Brand Kit), and the account bills despite showing "unlimited" — all
# three fail silently, so the brief has to carry the warnings verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("brand_key", ["delta_cuisine", "ceram_quartz"])
def test_magnific_block_reaches_producing_briefs(dispatch, brand_key):
    brief = _brief_for_brand(dispatch, brand_key, "adcreative")
    assert "MAGNIFIC — GENERATION ENGINE" in brief
    # The cost gate: unlimited does not apply here, and simulate is free.
    assert "COST CEILING" in brief
    assert "simulate_flows" in brief
    # The dead-parameter trap — an invented brandKitId does not error, it
    # silently changes the output.
    assert "BRAND KIT IS UNAVAILABLE" in brief
    assert "brandKitId" in brief


@pytest.mark.parametrize("task_type", ["publish", "ads"])
def test_magnific_block_absent_from_non_generating_lanes(dispatch, task_type):
    """publish reuses an approved file, ads reuses organic creative. Neither
    carries mcp__magnific in its allowlist, so a flow contract there is dead text."""
    brief = _brief_for_brand(dispatch, "delta_cuisine", task_type)
    assert "MAGNIFIC — GENERATION ENGINE" not in brief


def test_flow_ids_do_not_leak_between_brands(dispatch):
    """Same isolation property as vault notes: one brand's approved flow must
    never appear in another brand's brief."""
    import era_dispatch

    brands = era_dispatch.load_brands()["brands"]
    dc_only = {"otqh829FPM", "sG1l8e2GTt"}  # DC interior flows
    cq_only = {"CQXtEEyuoQ", "LYSswO4tgm"}  # CQ slab flows

    for key, foreign in (("delta_cuisine", cq_only), ("ceram_quartz", dc_only)):
        owned = {f["id"] for f in brands[key]["magnific"]["approved_flows"]}
        assert not (owned & foreign), f"{key} lists another brand's flow"
        brief = _brief_for_brand(dispatch, key, "adcreative")
        for flow_id in foreign:
            assert flow_id not in brief


@pytest.mark.parametrize("brand_key", ["delta_cuisine", "ceram_quartz"])
def test_unaffordable_flows_are_banned_not_merely_unlisted(brand_key):
    """The UGC flows cost 10621 credits — more than the whole balance. Leaving
    them merely absent from approved_flows is not enough: the brief has to name
    them, or a plausible-looking order could still reach for one."""
    import era_dispatch

    mag = era_dispatch.load_brands()["brands"][brand_key]["magnific"]
    banned = {f["id"] for f in mag["banned_flows"]}
    assert {"19cr4rEVPs", "aLUfShW4BA"} <= banned
    assert mag["credit_ceiling_per_run"] > 0
    assert mag["require_simulate"] is True


def test_adcreative_hands_off_to_the_existing_approval_gates(dispatch):
    """The whole integration: ad creative must land in the SAME review queue as
    organic drafts, or the existing approve/publish lanes can never pick it up."""
    brief = _brief_for_brand(dispatch, "delta_cuisine", "adcreative")
    assert "AD-CREATIVE MODE" in brief
    assert "ASSET REVIEW" in brief
    assert "era_telegram.py" in brief
    # Gate 2's invariant: the model never signs the user's approval itself.
    assert "(approved)" in brief
    assert "_description.txt" in brief
    # ...and it must not think it may publish or spend.
    assert "PUBLISH MODE" not in brief


def test_adcreative_can_generate_but_ads_still_cannot(dispatch):
    """Making pictures and spending money stay two different orders. The ads
    lane's inability to generate is a deliberate invariant, not an oversight."""
    allowed = dispatch._DEFAULT_ALLOWED_TOOLS
    assert "mcp__magnific" in allowed["adcreative"]
    assert "mcp__magnific" not in allowed["ads"]
    assert "mcp__facebook-ads" not in allowed["adcreative"]


class TestFeedbackCaptureBreadth:
    """Which orders become lessons — the gate that had recorded 2 events ever.

    The lane alone was too narrow. `revise`/`approve` are judgement by
    definition, but a preference does not wait for the right lane: "prepare this
    week's plan, and stop using the clinical white kitchens" arrives as `weekly`,
    and the standing rule inside it was thrown away.

    It cannot simply widen to every lane either — then "plan the week" and
    "publish now" become lessons and bury the real ones. carries_judgement() is
    the discriminator.
    """

    @pytest.mark.parametrize(
        "order",
        [
            "Prepare this week's content plan and production.",
            "Publish all approved files now.",
            "plan the week",
        ],
    )
    def test_routine_orders_teach_nothing(self, order):
        assert cm_learning.carries_judgement(order) is False

    @pytest.mark.parametrize(
        "order",
        [
            "change reel 1's texture",
            "never use that hook again",
            "stop making the kitchens so clinical",
            "prepare this week's plan and stop using the white kitchens",
            "perfect, keep that tone",
        ],
    )
    def test_an_order_carrying_a_preference_is_captured(self, order):
        assert cm_learning.carries_judgement(order) is True

    def test_too_short_to_be_a_rule(self):
        # record_feedback already refuses under 8 chars; agreeing here keeps the
        # two gates from disagreeing about the same input.
        assert cm_learning.carries_judgement("no") is False
        assert cm_learning.carries_judgement("") is False

    def test_a_correction_inside_a_weekly_order_becomes_a_lesson(self, lessons):
        # The end-to-end shape of the bug: the lane says `weekly`, so the old gate
        # skipped it and the rule was lost.
        order = "prepare this week's plan, and stop using the clinical white kitchens"
        assert cm_learning.carries_judgement(order)

        cm_learning.record_feedback("delta_cuisine", order, "weekly")

        rendered = cm_learning.render_lessons("delta_cuisine")
        assert "clinical white kitchens" in rendered
