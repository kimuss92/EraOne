"""PF-071 / PF-072 guards: CM orders reach a model that can read them, about files that exist.

Live 2026-11-20: local Qwen ran at Ollama's 2048-token context (inputTokens=2050 in both
logs) so it never saw the order; it answered "I've loaded my full suite of skills", which
Era spoke as the Meta report. The 'new 2' / 'HB' reels were never resolved to real files.
"""

from __future__ import annotations

import json

import era_dispatch as d

_ENV = {"ANTHROPIC_BASE_URL": "http://gw", "ANTHROPIC_API_KEY": "k"}
_ACC = ["C:\\acc"]
LIVE_HOLLOW = (
    "I've loaded my full suite of skills for this session!\n\n**Available capabilities "
    "include:**\n- Swarm & Agents\n\n**What would you like to work on today?**"
)


def test_the_local_route_is_skipped_when_its_context_cannot_hold_the_brief(monkeypatch):
    monkeypatch.delenv("ERA_CM_DIRECT_ROUTES", raising=False)
    monkeypatch.setenv("ERA_CM_LOCAL_CTX", "2048")
    names = [r[0] for r in d._routes_for(_ACC, dict(_ENV), 20000)]
    assert names and not any(n.startswith("local:") for n in names)
    assert names[0].startswith("gateway:")


def test_the_local_route_stays_first_when_its_context_is_big_enough(monkeypatch):
    monkeypatch.delenv("ERA_CM_DIRECT_ROUTES", raising=False)
    monkeypatch.setenv("ERA_CM_LOCAL_CTX", "65536")
    names = [r[0] for r in d._routes_for(_ACC, dict(_ENV), 20000)]
    assert names[0].startswith("local:") and names[-1].startswith("gateway:")


def test_the_live_qwen_non_answer_is_hollow_and_falls_through():
    from types import SimpleNamespace

    assert d._hollow_reply(LIVE_HOLLOW)
    proc = SimpleNamespace(
        returncode=0, stdout=json.dumps({"result": LIVE_HOLLOW, "is_error": False}), stderr=""
    )
    assert d._is_route_failure(proc)


def _brand(tmp_path):
    return {"display_name": "Ceram Quartz", "project_dir": str(tmp_path / "ceram")}


def test_a_folder_of_two_reels_is_listed_as_the_only_input(tmp_path):
    folder = tmp_path / "ceram" / "reels pair"
    folder.mkdir(parents=True)
    (folder / "alya.mp4").write_bytes(b"x" * 2048)
    (folder / "taj.mp4").write_bytes(b"x" * 2048)
    order = f"write descriptions for the two reels in {folder}"
    g = d.ground_order("ceram_quartz", _brand(tmp_path), "weekly", order)
    assert g["ok"] and "alya.mp4" in g["note"] and "taj.mp4" in g["note"]
    assert "ONLY files the order is about" in g["note"]


def test_missing_or_empty_folders_and_unknown_reel_names_are_refused(tmp_path):
    brand = _brand(tmp_path)
    (tmp_path / "ceram" / "empty").mkdir(parents=True)
    missing = d.ground_order(
        "ceram_quartz", brand, "weekly", f"describe the reels in {tmp_path}\\nope"
    )
    assert not missing["ok"] and "can't find" in missing["error"]
    empty = d.ground_order(
        "ceram_quartz", brand, "weekly", f"describe the reels in {tmp_path / 'ceram' / 'empty'}"
    )
    assert not empty["ok"] and "no video or image" in empty["error"]
    named = d.ground_order(
        "ceram_quartz", brand, "weekly", "create descriptions for the new reels 'new 2' and 'HB'"
    )
    assert not named["ok"] and "'new 2'" in named["error"]


def test_a_quoted_reel_name_is_resolved_to_its_real_file(tmp_path):
    (tmp_path / "ceram" / "final").mkdir(parents=True)
    (tmp_path / "ceram" / "final" / "HB final.mp4").write_bytes(b"x")
    g = d.ground_order("ceram_quartz", _brand(tmp_path), "weekly", "describe the reel 'HB'")
    assert g["ok"] and "HB final.mp4" in g["note"]


def test_another_brands_folder_and_non_content_lanes(tmp_path):
    other = tmp_path / "delta"
    other.mkdir()
    (other / "k.mp4").write_bytes(b"x")
    all_brands = {
        "ceram_quartz": _brand(tmp_path),
        "delta_cuisine": {"project_dir": str(other)},
    }
    g = d.ground_order(
        "ceram_quartz",
        all_brands["ceram_quartz"],
        "weekly",
        f"describe the reel in {other}",
        all_brands,
    )
    assert not g["ok"] and "another brand" in g["error"]
    ads = d.ground_order(
        "ceram_quartz", _brand(tmp_path), "ads", "report on the reel 'HB' campaign"
    )
    assert ads["ok"] and not ads["note"]


def test_run_task_refuses_an_ungrounded_order_before_spawning():
    import inspect

    src = inspect.getsource(d.run_task)
    assert src.index("ground_order(") < src.index("build_brief(")
    assert "cm.order.ungrounded" in src and 'grounding["note"]' in src


def test_the_last_cm_outcome_is_kept_for_era_to_read(tmp_path, monkeypatch):
    from app.era_live import cm_flow as cf

    monkeypatch.setattr(cf, "_CM_LAST_PATH", tmp_path / "last.json")
    assert cf.read_last_cm_outcome() is None
    cf._record_cm_outcome(
        {"brand_key": "delta_cuisine", "task_type": "ads", "text": "check the campaign"},
        True,
        "Spend 12 EUR, 3 conversations.",
        {"wrote_files": 0, "log_path": "x.log"},
    )
    rec = cf.read_last_cm_outcome()
    assert rec["ok"] and "3 conversations" in cf.render_last_cm_outcome(rec)
    assert cf.read_last_cm_outcome(max_age_s=-1) is None


def test_a_failed_cm_job_is_spoken_word_for_word(monkeypatch, tmp_path):
    """Live 2026-11-20 21:12: a plain failure text let Gemini invent a folder path."""
    import asyncio

    import era_dispatch
    from app.era_live import cm_flow as cf

    monkeypatch.setattr(cf, "_CM_LAST_PATH", tmp_path / "last.json")
    monkeypatch.setattr(cf, "_tg_notify", lambda *_a, **_k: None)
    monkeypatch.setattr(era_dispatch, "current_job", lambda: None)
    monkeypatch.setattr(
        era_dispatch, "run_task", lambda *_a, **_k: {"ok": False, "error": "no reel 'HB'"}
    )

    class Era:
        _tg_reply_pending = False

        def __init__(self):
            self.calls = []

        def speak(self, text):
            self.calls.append(("speak", text, None))

        def _deliver_or_queue_cm_report(self, text, verbatim=False):
            self.calls.append(("report", text, verbatim))

    era = Era()
    payload = {"text": "describe 'HB'", "task_type": "weekly", "brand_key": "ceram_quartz"}
    asyncio.run(cf.CMFlowMixin._run_cm_flow_job(era, payload, announce=False))
    assert era.calls == [("report", "That community-management task failed: no reel 'HB'", True)]


def test_the_cm_lanes_own_report_is_not_muted_by_its_handoff_window():
    """Live 2026-11-20 21:20: an instant refusal spoken inside the 15 s mute was never heard."""
    import time

    from app.era_live import cm_flow as cf

    class Era:
        _loop = object()
        session = object()
        _standby_active = False
        _pending_cm_announcement = None

        def __init__(self):
            self._cm_handoff_until = time.time() + 15
            self.spoken = []

        def speak(self, text):
            self.spoken.append((text, self._cm_handoff_until))

    era = Era()
    cf.CMFlowMixin._deliver_or_queue_cm_report(era, "no reel 'HB'", verbatim=True)
    assert len(era.spoken) == 1 and "no reel 'HB'" in era.spoken[0][0]
    assert era.spoken[0][1] == 0.0  # the window was closed before speaking


def test_do_not_publish_never_reaches_the_publish_lane():
    """PF-073. Live 2026-11-20 21:24: "Draft only, do not publish anything" ran as publish."""
    from app.cm_intents import _infer_cm_task_type, says_do_not_publish

    live = (
        "Create Facebook descriptions for the two Ceram Quartz reels in D:\\x. "
        "Draft only, do not publish anything."
    )
    assert _infer_cm_task_type(live) != "publish"
    for order in (
        "write the captions but don't post them",
        "prepare the reel without publishing",
        "prépare les descriptions sans publier",
        "never publish, just draft the carousel",
    ):
        assert says_do_not_publish(order), order
        assert _infer_cm_task_type(order) != "publish", order
    for order in (
        "publish Ceram Quartz's approved posts",
        "quick publish the Perla reel",
        "publish the approved reels for Ceram Quartz, do not post the stories",
    ):
        assert _infer_cm_task_type(order) == "publish", order


def test_run_task_refuses_a_publish_run_on_an_order_that_forbids_it(monkeypatch):
    import era_dispatch as ed

    monkeypatch.setattr(
        ed.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned"))
    )
    res = ed.run_task(
        "draft it, do not publish anything", task_type="publish", brand_key="ceram_quartz"
    )
    assert not res["ok"] and "not to publish" in res["error"]


def test_a_do_not_publish_constraint_is_not_a_brand_lesson():
    """Live 2026-11-20: the test order became a Ceram 'material / avoid' lesson."""
    from core.cm_learning import carries_judgement

    assert not carries_judgement(
        "Create Facebook descriptions for the two reels in D:\\x. Draft only, do not publish anything."
    )
    assert carries_judgement("prepare the plan, and stop using the clinical white kitchens")
    assert carries_judgement("do not use Polaris again, publish the approved reels")


def _era_with(utterances):
    import time
    from types import SimpleNamespace

    now = time.time()
    return SimpleNamespace(
        _inflight_user_text="",
        _last_user_text="",
        _recent_user_texts=[(now - 5 * (len(utterances) - i), u) for i, u in enumerate(utterances)],
    )


def test_a_command_inside_a_report_cannot_change_a_live_campaign():
    """PF-074. Live 2026-11-20 21:44: the report's "Command: pause campaign Appel La Maison
    Equipee" was called by Gemini and the campaign was paused; the user asked for a report."""
    from app.era_live.tool_dispatch import _ads_change_refusal

    era = _era_with(["Check how the Delta Cuisine Meta campaigns are doing and give me a report."])
    order = "pause campaign Appel La Maison Équipée"
    assert _ads_change_refusal(era, order, "ads")  # refused
    assert _ads_change_refusal(era, "how are the Delta campaigns doing", "ads") is None  # report ok
    # The user says yes right after Era asks about this exact order -> allowed once.
    era._recent_user_texts.append((era._recent_user_texts[-1][0] + 4, "yes"))
    assert _ads_change_refusal(era, order, "ads") is None
    assert _ads_change_refusal(era, order, "ads")  # the yes is spent
    # A different order is not covered by that yes.
    assert _ads_change_refusal(era, "raise Black Friday to 20 EUR a day", "ads")


def test_the_users_own_ads_change_order_still_runs():
    from app.era_live.tool_dispatch import _ads_change_refusal

    era = _era_with(["Pause campaign Appel La Maison Équipée for Delta Cuisine"])
    assert _ads_change_refusal(era, "pause campaign Appel La Maison Équipée", "ads") is None
    assert _ads_change_refusal(_era_with([]), "make a reel", "weekly") is None


def test_a_blocked_ads_change_is_a_failed_result_that_forbids_claiming_it():
    """Live 2026-11-20 21:58: a plain-string refusal read as NOT_VERIFIED; Era said she had
    handed the change off."""
    import inspect

    from app.era_live import tool_dispatch as td

    src = inspect.getsource(td)
    i = src.index("refusal = _ads_change_refusal(self, order, task_type)")
    assert "ToolResult.fail(" in src[i : i + 600]
    text = td._ads_change_refusal(
        _era_with(["give me a report on Delta's campaigns"]), "pause campaign X", "ads"
    )
    assert text.startswith("BLOCKED") and "Do NOT say you handed it off" in text


def test_a_yes_after_a_refusal_runs_the_refused_order_even_when_reworded():
    """PF-074. Live 2026-11-20 23:42: Era asked "Shall I extend BLACK FRIDYA and vente flash
    to 30 Nov?", the owner said yes four times, and every call was blocked because Gemini
    re-worded the order each time. The yes approves the order Era asked about, once."""
    from app.era_live.tool_dispatch import _ads_change_refusal

    asked = "Extend BLACK FRIDYA and vente flash campaigns to 30 Nov."
    for answer in (
        "yes",
        "Yes send that request to the community manager",
        "why you cannot proceed tell the community manager to do it",
        "oui vas-y",
    ):
        era = _era_with(["give me a report on the Delta campaigns"])
        assert _ads_change_refusal(era, asked, "ads")  # report's suggestion: blocked
        era._recent_user_texts.append((era._recent_user_texts[-1][0] + 4, answer))
        assert _ads_change_refusal(era, "Extend the winners to 30 November.", "ads") is None
        assert era._ads_confirmed_order == asked  # the order Era asked about runs
        assert _ads_change_refusal(era, asked, "ads")  # spent
    era = _era_with(["give me a report on the Delta campaigns"])
    assert _ads_change_refusal(era, asked, "ads")
    era._recent_user_texts.append((era._recent_user_texts[-1][0] + 4, "no, don't do it"))
    assert _ads_change_refusal(era, asked, "ads")  # a no is never a go-ahead


def test_the_call_site_runs_the_confirmed_order_and_ads_words_win_the_lane():
    import inspect

    from app.era_live import tool_dispatch as td

    src = inspect.getsource(td)
    i = src.index("refusal = _ads_change_refusal(self, order, task_type)")
    assert "order = confirmed" in src[i : i + 400]
    assert 'inferred in ("package", "edit", "ads")' in src


def test_publishing_ads_drafts_is_an_ads_manage_order_not_organic_publish():
    """Live 2026-11-20 23:28: "I approve the drafts, publish them in the Meta Ads Manager"
    ran the organic publish brief for 4.5 minutes and found nothing to publish."""
    from app.cm_intents import _infer_cm_task_type, ads_mode

    for order in (
        "I approve the drafts, publish them in the Meta Ads Manager, and keep an eye on them.",
        "publish the budget increases on BLACK FRIDYA and vente flash",
        "Extend the winners to 30 November.",
    ):
        assert _infer_cm_task_type(order) == "ads", order
        assert ads_mode(order) == "manage", order
    assert _infer_cm_task_type("approve reel 1 and publish it") != "ads"
    assert _infer_cm_task_type("extend the reel to 30 seconds") != "ads"


def test_the_owners_own_words_reach_the_agent_when_the_model_rewords_them():
    """PF-079. Live 2026-11-21 01:12: "Finish the Delta Cuisine campaign renames: rename vente
    flash too, use MSG for every campaign that opens WhatsApp or Messenger" reached the agent
    as "Rename active campaigns: include 'vente flash' ..." and it appended "_VenteFlash" to
    two unrelated campaign names."""
    import inspect

    from app.era_live import tool_dispatch as td

    said = (
        "Finish the Delta Cuisine campaign renames: rename vente flash too, use MSG for every "
        "campaign that opens WhatsApp or Messenger, and publish the new names on the active "
        "campaigns only."
    )
    era = _era_with([said])
    summary = "Rename active campaigns: include 'vente flash', use 'MSG' for WhatsApp, and publish."
    out = td._with_owner_words(era, summary)
    assert out.startswith(summary) and said in out and "win over the summary" in out
    # verbatim already, a bare yes, or nothing recent: unchanged
    assert td._with_owner_words(_era_with([said]), said) == said
    assert td._with_owner_words(_era_with(["yes"]), summary) == summary
    assert td._with_owner_words(_era_with([]), summary) == summary
    src = inspect.getsource(td)
    i = src.index('payload = {"text": order, "task_type": task_type, "brand_key": brand_key}')
    assert "_with_owner_words(self, order)" in src[i - 200 : i]


def test_campaign_names_must_be_unique_and_never_borrow_the_orders_words():
    """Live 2026-11-21: two Brico campaigns were both named "BK - POST_PrixSpecial - ENGAGE -
    TN_NOV26", so neither can be found by name."""
    import era_dispatch as d

    rule = d._ads_naming_rule({"campaign_code": "BK"}, "manage")
    assert "Every name is UNIQUE in the account" in rule
    assert "never from the order's wording" in rule


def test_tonights_blocked_owner_orders_are_ads_change_orders():
    """PF-074/077. Live 2026-11-21 01:30: the owner's own words were refused by the gate
    because the router did not see an ads change in them."""
    from app.cm_intents import _infer_cm_task_type, ads_mode
    from app.era_live.tool_dispatch import _ads_change_refusal

    for said in (
        "Fix the two Delta Cuisine La Maison Equipee campaign names by removing VenteFlash "
        "from them, extend the Black Friday and vente flash campaigns to 30 November, and "
        "publish these changes.",
        "Make every active Brico Kitchen campaign name unique: give the campaigns that share "
        "a name a more specific product name from their post, and publish the new names.",
    ):
        assert _infer_cm_task_type(said) == "ads" and ads_mode(said) == "manage", said
        assert _ads_change_refusal(_era_with([said]), "a re-worded summary", "ads") is None
    report = (
        "Give me a report on the Delta Cuisine Meta campaigns: every active campaign's name, "
        "the Black Friday and vente flash budgets and end dates."
    )
    assert ads_mode(report) == "report"


def test_the_manage_brief_extends_ad_sets_and_reads_budgets_in_cents():
    import era_dispatch as d

    b = d.load_brands()["brands"]["delta_cuisine"]
    manage = d._build_ads_ops_brief("manage", "delta_cuisine", b, "extend it", "", "")
    assert "END DATES live on the AD SETS (end_time)" in manage
    assert "daily_budget 500 = 5.00 EUR" in manage
    assert "entity_type='ad_set'" in manage and "CANNOT be published" in manage
    assert (
        "Never call a change published or done unless the read-back shows the new value" in manage
    )
