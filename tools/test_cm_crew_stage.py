"""The Crew's stage in the CM ads lane: Laura -> Tom/Maya/Nabar make a creative package
that the ads brief carries; a crew failure never costs the job."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import era_dispatch as ed
from crew import cm_stage

SKILL = Path(ed.__file__).parent / ".claude" / "skills" / "era-ads-manager" / "SKILL.md"
ADS_BUILD = "launch meta ads for the last reel, 14 days, 5 dollars a day"


def _ok(text="OBJECTIVE: Messages\nHEADLINES: ..."):
    return lambda params, **kw: SimpleNamespace(success=True, message=text, error="")


def test_prompts_state_the_skill_limits_and_forbid_publishing():
    task = cm_stage.build_task("delta_cuisine", "Delta Cuisine", ADS_BUILD)
    assert ADS_BUILD in task and "cannot publish" in task
    lim = cm_stage.FIELD_LIMITS
    copy = cm_stage._copy_task("Delta Cuisine", ADS_BUILD, "angle")
    review = cm_stage._review_task("x")
    for text in (copy, review):
        assert str(lim["headline"]) in text and str(lim["primary_text"]) in text
    # one source of truth: the limits the prompts state are the limits the skill states
    skill = SKILL.read_text(encoding="utf-8")
    assert (
        f"primary text {lim['primary_text']} chars, headline {lim['headline']}, "
        f"description {lim['description']}" in skill
    )


class _Crew:
    """Records the bots asked, in order, and answers per bot ('' = a failed turn)."""

    def __init__(self, **replies):
        self.replies, self.asked = replies, []

    def __call__(self, params, **kw):
        self.asked.append((params["bot"], params["task"], kw))
        text = self.replies.get(params["bot"], "")
        return SimpleNamespace(
            success=bool(text), message=text, error="" if text else "Hermes stalled"
        )


def test_the_package_runs_tom_then_maya_then_nabar_each_fed_the_one_before():
    crew = _Crew(tom="ANGLE: warm kitchens", maya="H1: Votre cuisine", nabar="Verdict: pass")
    pkg = cm_stage.creative_package("delta_cuisine", "Delta Cuisine", ADS_BUILD, runner=crew)
    assert [b for b, *_ in crew.asked] == ["tom", "maya", "nabar"]
    assert "ANGLE: warm kitchens" in crew.asked[1][1]  # Maya was handed Tom's direction
    assert "H1: Votre cuisine" in crew.asked[2][1]  # Nabar reviews Maya's copy
    assert crew.asked[0][2] == {"user_text": ADS_BUILD, "origin": "cm"}
    assert pkg["ok"] and "NOT PRODUCED" not in pkg["text"]
    assert all(h in pkg["text"] for h in ("(Tom)", "(Maya)", "(Nabar)"))


def test_a_failed_step_is_reported_and_later_steps_that_need_it_are_skipped():
    crew = _Crew(tom="", maya="never asked", nabar="never asked")
    pkg = cm_stage.creative_package("delta_cuisine", "Delta Cuisine", ADS_BUILD, runner=crew)
    assert [b for b, *_ in crew.asked] == ["tom"]
    assert pkg["ok"] is False and "Hermes stalled" in pkg["reason"]
    assert "CREW DID NOT CONTRIBUTE" in cm_stage.render_note(pkg)


def test_a_partial_package_is_used_and_says_what_is_missing():
    crew = _Crew(tom="ANGLE: x", maya="COPY: y", nabar="")
    pkg = cm_stage.creative_package("delta_cuisine", "Delta Cuisine", ADS_BUILD, runner=crew)
    assert pkg["ok"] and "NOT PRODUCED: REVIEW - Hermes stalled" in pkg["text"]


def test_a_crew_that_raises_never_raises_out():
    def boom(params, **kw):
        raise RuntimeError("boom")

    pkg = cm_stage.creative_package("delta_cuisine", "Delta Cuisine", ADS_BUILD, runner=boom)
    assert pkg["ok"] is False and pkg["reason"]


def _brand():
    return ed.load_brands()["brands"]["delta_cuisine"]


def _stub(monkeypatch, pkg):
    monkeypatch.setenv("ERA_CM_CREW", "1")
    calls = []
    monkeypatch.setattr(
        cm_stage, "creative_package", lambda *a, **k: calls.append(a) or pkg
    )
    return calls


def test_an_ads_build_carries_the_crew_package_in_the_brief(monkeypatch):
    calls = _stub(monkeypatch, {"ok": True, "text": "ANGLE: warm family kitchens", "reason": ""})
    note = ed._crew_note_for("delta_cuisine", _brand(), "ads", ADS_BUILD)
    assert len(calls) == 1 and "ANGLE: warm family kitchens" in note
    cfg = ed.load_brands()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        ADS_BUILD,
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
        crew_note=note,
    )
    assert "CREW CREATIVE PACKAGE" in brief and "ANGLE: warm family kitchens" in brief
    assert brief.index("CREW CREATIVE PACKAGE") < brief.index("ORDER FROM USER")


def test_reports_other_lanes_and_the_off_switch_never_call_the_crew(monkeypatch):
    calls = _stub(monkeypatch, {"ok": True, "text": "X", "reason": ""})
    assert ed._crew_note_for("delta_cuisine", _brand(), "ads", "how are my campaigns doing") == ""
    assert ed._crew_note_for("delta_cuisine", _brand(), "weekly", "plan the week") == ""
    monkeypatch.setenv("ERA_CM_CREW", "0")
    assert ed._crew_note_for("delta_cuisine", _brand(), "ads", ADS_BUILD) == ""
    assert calls == []


def test_a_down_crew_leaves_the_job_running_with_a_visible_notice(monkeypatch):
    _stub(monkeypatch, {"ok": False, "text": "", "reason": "Hermes stalled"})
    note = ed._crew_note_for("delta_cuisine", _brand(), "ads", ADS_BUILD)
    assert "CREW DID NOT CONTRIBUTE" in note and "Hermes stalled" in note


def test_the_crew_is_grounded_in_the_brand_facts_and_told_not_to_invent():
    facts = cm_stage.facts_block(_brand())
    assert "kitchen" in facts and "wardrobe" in facts and "derja" in facts
    assert "not Paris" in facts and "Do NOT invent" in facts
    crew = _Crew(tom="ANGLE", maya="HEADLINE: Hi", nabar="ok")
    cm_stage.creative_package("delta_cuisine", "Delta Cuisine", ADS_BUILD, runner=crew, brand=_brand())
    assert all("BRAND FACTS" in task for bot, task, _ in crew.asked[:2])


def test_limits_are_measured_by_code_not_trusted_from_the_model():
    copy = (
        "**Headlines:**\n"
        "HEADLINE: Dîner en 15 min ?\n"  # 17
        "HEADLINE: Six heures. Frigo vide. Sauvetage de 15 min.\n"  # 52: over 27
        "PRIMARY: " + "x" * 126 + "\n"  # over 125
        "PRIMARY: Court.\n"
        "DESCRIPTION: Commandez aujourd'hui.\n"
        "CTA: Send Message\n"
    )
    report, failures = cm_stage.check_limits(copy)
    assert len(report) == 5 and len(failures) == 2
    assert any(r.startswith("OK  headline 17/27") for r in report)
    crew = _Crew(tom="ANGLE", maya=copy, nabar="Verdict: fix two")
    pkg = cm_stage.creative_package("delta_cuisine", "Delta Cuisine", ADS_BUILD, runner=crew)
    assert "LIMIT CHECK (measured by code" in pkg["text"] and "DO NOT USE (over the limit)" in pkg["text"]
    assert "Sauvetage de 15 min" in pkg["text"].split("DO NOT USE")[1]


def test_nabars_json_report_is_shown_as_a_sentence():
    raw = '{"findings": [], "no_testable_surface": "no artifact id"}'
    assert cm_stage._tidy_review(raw) == "No findings raised. Could not verify: no artifact id"
    assert cm_stage._tidy_review("Verdict: pass") == "Verdict: pass"


def test_literal_backslash_n_from_a_small_model_still_splits_the_lines():
    copy = "HEADLINE: Votre cuisine rêvée\\nPRIMARY: Imaginez l'espace.\\nDESCRIPTION: Voir"
    report, failures = cm_stage.check_limits(copy)
    assert len(report) == 3 and failures == []


def test_a_json_report_followed_by_a_trailing_note_is_still_tidied():
    raw = '{"findings": [{"severity": "warn", "issue": "Too long"}]}\n(Hermes stopped.)'
    assert cm_stage._tidy_review(raw) == "Findings: warn: Too long"


def test_the_skill_carries_a_verification_date_and_a_stale_one_is_flagged():
    import datetime

    age = cm_stage.specs_age_days(today=datetime.date(2026, 11, 20))
    assert age == 0  # verified the day it was last updated (2026-11-20)
    assert cm_stage._SPECS_MAX_AGE_DAYS == 30  # owner 2026-11-20: Meta changes often
    assert cm_stage.specs_age_note(today=datetime.date(2026, 12, 1)) == ""
    stale = cm_stage.specs_age_note(today=datetime.date(2027, 6, 1))
    assert "DAYS OLD" in stale and "ads-guide" in stale
    assert "NO VERIFICATION DATE" in cm_stage.specs_age_note(skill_text="nothing here")


def test_a_stale_reference_reaches_the_ads_build_brief(monkeypatch):
    monkeypatch.setattr(
        cm_stage, "specs_age_note", lambda *a, **k: "=== META SPECS REFERENCE IS 400 DAYS OLD ===\n"
    )
    assert "400 DAYS OLD" in ed._meta_currency_note_for("ads", ADS_BUILD)
    assert ed._meta_currency_note_for("weekly", "plan the week") == ""
    assert ed._meta_currency_note_for("ads", "how are my campaigns doing") == ""


# ── Motion Design skill and the crew's use of it (PF-059) ─────────────────────


def test_a_literal_path_or_title_key_from_a_bot_is_understood():
    """Live 2026-11-19: Tom sent {"path|title": ...} (the signature, verbatim) and the read failed."""
    from crew.capabilities import _pick

    assert _pick({"path|title": "04 - Global/Motion_Design_Skill_Guide.md"}, "path", "title") == {
        "path": "04 - Global/Motion_Design_Skill_Guide.md"
    }
    assert _pick({"path|title": "Motion Design guide"}, "path", "title") == {
        "title": "Motion Design guide"
    }
    # an explicit key wins; nothing else is disturbed
    assert _pick({"path|title": "x.md", "path": "real.md", "brand": "b"}, "path", "title", "brand") == {
        "path": "real.md",
        "brand": "b",
    }
    assert _pick({"path": "a.md"}, "path", "title") == {"path": "a.md"}


def test_the_motion_design_skill_is_installed_narrowly_with_its_files():
    root = Path(ed.__file__).parent / ".claude" / "skills" / "motion-design"
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    head = skill.split("---")[1]
    assert "name: motion-design" in head and "Use ONLY when the user explicitly asks" in head
    assert "never in a headless/CM run unless the order says motion design" in head
    for rel in ("scripts/render.mjs", "scripts/mix.py", "templates/template-3d.html",
                "references/style-rules.md"):
        assert (root / rel).is_file(), rel


def test_the_crew_prompts_know_the_motion_design_skill_and_its_limits():
    prompts = Path(ed.__file__).parent / "config" / "crew" / "prompts"
    tom = (prompts / "tom.md").read_text(encoding="utf-8")
    assert "motion-design" in tom and "Motion Design" in tom and "1080x1920" in tom
    assert "never say a video exists" in tom
    assert "motion-design" in (prompts / "maya.md").read_text(encoding="utf-8")
    assert "Motion Design" in (prompts / "nabar.md").read_text(encoding="utf-8")
