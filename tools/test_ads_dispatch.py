"""Tests for the Meta Ads dispatch lane: intent classification (main.py),
the separate ads brief (era_dispatch._build_ads_brief / build_brief), and
run_task's ads-specific gates (enabled check, freelance-account override).

Built 2026-07-20 after live-verifying (a) the facebook-ads MCP is reachable
from a headless `claude -p` run under .claude-freelance (confirmed via a real
probe: mcp__facebook-ads__ads_create_ad_set and friends listed), and (b) the
interactive "claude.ai Facebook" connector shown as "connected" in `/mcp`
does NOT carry into headless runs at all — a claude.ai-account connector and
a project/account-registered MCP server are different things, and only the
latter is reachable headless. That distinction is why ads dispatch needs its
own config_dir override rather than trusting whatever account happens to be
"connected" interactively.

No live API/network calls anywhere in this file — subprocess.run is mocked
for run_task tests, and brief-building is pure string assembly.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import era_dispatch as ed  # noqa: E402
import main  # noqa: E402

# Captured BEFORE the autouse fixture below replaces it, so the preflight's own
# tests can still exercise the real function against a temp credentials file.
_REAL_CONNECTION_PROBLEM = ed._ads_connection_problem


@pytest.fixture(autouse=True)
def _isolate_from_this_machine(monkeypatch, tmp_path):
    """run_task's ads preflight reads Era's REAL .credentials.json. Whether this
    machine's Meta token happens to be expired today must not decide whether the
    dispatch tests pass — so every test starts with the preflight open.

    Also keeps run logs out of the real ~/.era/cm_logs: these tests fake the
    subprocess, and their `{"result": "ok"}` transcripts were landing in the same
    folder as genuine runs (dozens of them by 2026-09-20), which made the folder
    unreadable when hunting for a real failure."""
    monkeypatch.setattr(ed, "_ads_connection_problem", lambda _config_dir: None)
    monkeypatch.setattr(ed, "_CM_LOG_DIR", tmp_path / "cm_logs")
    # A real ads session on this machine must not turn a test build into a resume,
    # and a test must never probe or start the real OmniRoute gateway.
    monkeypatch.setattr(ed, "_ADS_SESSIONS_PATH", tmp_path / "ads_sessions.json")
    monkeypatch.setattr(ed, "_ensure_gateway", lambda _env: None)


# ---------------------------------------------------------------------------
# Intent classification — ads vs organic
# ---------------------------------------------------------------------------


def test_ads_orders_classify_as_ads():
    cases = [
        "run a Meta ad campaign for Delta Cuisine",
        "boost this post",
        "what is the ad budget",
        "publicité pour la Ceram Quartz",
        "run some facebook ads",
        "paid campaign for the Rosieres pack",
        "sponsor this reel",
        "budget publicitaire pour Ceram Quartz",
    ]
    for text in cases:
        assert main._infer_cm_task_type(text) == "ads", text


def test_organic_campaign_still_classifies_as_campaign():
    """The bare word 'campaign'/'campagne' alone must NOT trigger ads — only
    ad-specific vocabulary should. This is the exact false-positive risk the
    _ADS_INTENT regex comment calls out."""
    assert main._infer_cm_task_type("build a promo campaign for Delta Cuisine") == "campaign"
    assert main._infer_cm_task_type("prepare a campagne for Ceram Quartz") == "campaign"


def test_revise_approve_publish_rush_unaffected_by_ads_intent():
    assert main._infer_cm_task_type("change reel 1's texture") == "revise"
    assert main._infer_cm_task_type("approve the plan") == "approve"
    assert main._infer_cm_task_type("publish it now") == "publish"
    assert main._infer_cm_task_type("urgent campaign for Delta Cuisine") == "rush"


def test_ad_creative_is_not_an_ads_order():
    """Making the picture and spending the money are different lanes. These
    orders name no spend, so they must reach adcreative — which generates but
    cannot touch an ad account."""
    for text in [
        "make ad creative for the Polaris promo",
        "I need ad visuals for Delta Cuisine",
        "crée une bannière pour Ceram Quartz",
        "resize it for stories",
        "give me the ad formats for this reel",
    ]:
        assert main._infer_cm_task_type(text) == "adcreative", text


def test_spend_vocabulary_still_beats_ad_creative():
    """An order that names spend must reach the lane that ASKS about budget,
    never the one that silently generates — even when it also says 'creative'."""
    for text in [
        "run a Meta ads campaign with new ad creative",
        "boost this post",
        "budget publicitaire pour de nouveaux visuels",
    ]:
        assert main._infer_cm_task_type(text) == "ads", text


def test_ad_creative_never_swallows_a_correction_or_a_publish():
    """revise is also where record_feedback captures the lesson, so a correction
    routed to adcreative would lose the user's preference silently."""
    assert main._infer_cm_task_type("change the ad visual's texture") == "revise"
    assert main._infer_cm_task_type("publish the ad creative") == "publish"


# ---------------------------------------------------------------------------
# Ads OPERATIONS — reading and changing campaigns that already exist
# (added 2026-09-19). _ADS_INTENT only knew how to START a campaign, so "how are
# my campaigns doing" / "add budget" fell through to the organic content lanes.
# ---------------------------------------------------------------------------

_REPORT_ORDERS = [
    "how are my campaigns doing",
    "give me the report of the ads",
    "campaign results for Ceram Quartz",
    "analyse the SAVANA campaign",
    "check my ads",
    "how is the followers campaign doing",
    "did the ads work",
    "résultats des campagnes",
    "bilan de la campagne Ceram Quartz",
]
_MANAGE_ORDERS = [
    "extend the SAVANA campaign to 30 Sep",
    "add more budget to the campaign",
    "increase the budget to $6 a day",
    "pause the followers campaign",
    "resume the Ventes campaign",
    "expand the audience of the campaign",
    "change the audience to Tunis only",
    "prolonge la campagne de 3 jours",
    "augmente le budget",
    "scale the winning ad set",
    # mixed: must reach the brief that is allowed to act
    "check the results and add budget if it is winning",
]


def test_report_orders_route_to_ads_in_report_mode():
    from app.cm_intents import ads_mode

    for text in _REPORT_ORDERS:
        assert main._infer_cm_task_type(text) == "ads", text
        assert ads_mode(text) == "report", text


def test_manage_orders_route_to_ads_in_manage_mode():
    from app.cm_intents import ads_mode

    for text in _MANAGE_ORDERS:
        assert main._infer_cm_task_type(text) == "ads", text
        assert ads_mode(text) == "manage", text


def test_start_a_campaign_is_still_build_mode():
    from app.cm_intents import ads_mode

    for text in ["run a Meta ad campaign", "boost this post", "run a campaign", ""]:
        assert ads_mode(text) == "build", text


def test_ads_operations_never_capture_organic_or_unrelated_orders():
    """MANAGE can change live spend, so it must not fire on look-alikes. 'stop the
    ad' on a video and 'review the campaign plan' (an organic Gate-1 order) are the
    two the regexes were written around."""
    for text in [
        "stop the ad",
        "skip the ad",
        "stop the video",
        "pause the music",
        "review the campaign plan for Delta Cuisine",
        "plan this week's posts",
        "what is the budget for my holiday",
        "performance of my laptop",
        "report a bug",
        "check my email",
    ]:
        assert main._infer_cm_task_type(text) != "ads", text
    # organic lanes keep their orders
    assert main._infer_cm_task_type("review the campaign plan for Delta Cuisine") == "campaign"
    assert main._infer_cm_task_type("change reel 1's texture") == "revise"
    assert main._infer_cm_task_type("approve the plan") == "approve"


def test_ads_operations_count_as_cm_orders():
    from app.cm_intents import is_cm_order

    for text in _REPORT_ORDERS + _MANAGE_ORDERS:
        assert is_cm_order(text), text
    assert not is_cm_order("stop the ad")


# ---------------------------------------------------------------------------
# _render_ads_audiences
# ---------------------------------------------------------------------------


def test_render_ads_audiences_empty_list_returns_empty_string():
    assert ed._render_ads_audiences([]) == ""
    assert ed._render_ads_audiences(None) == ""


def test_render_ads_audiences_renders_all_present_fields():
    audiences = [
        {
            "name": "Test Audience",
            "insight": "some insight",
            "geo": "Tunisia",
            "age_range": "25-44",
            "languages": ["fr", "ar"],
            "interests": ["a", "b"],
            "retargeting": "365 days",
        }
    ]
    rendered = ed._render_ads_audiences(audiences)
    assert "Test Audience" in rendered
    assert "some insight" in rendered
    assert "Tunisia" in rendered
    assert "25-44" in rendered
    assert "fr, ar" in rendered
    assert "a, b" in rendered
    assert "365 days" in rendered


def test_render_ads_audiences_tolerates_missing_optional_fields():
    rendered = ed._render_ads_audiences([{"name": "Bare Audience"}])
    assert "Bare Audience" in rendered
    # Must not raise or emit "None" for absent optional fields.
    assert "None" not in rendered


# ---------------------------------------------------------------------------
# build_brief(task_type="ads") — the separate ads brief
# ---------------------------------------------------------------------------


def _cfg():
    return ed.load_brands()


def test_ads_brief_is_structurally_separate_from_organic_brief():
    cfg = _cfg()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    # The organic brief's closing instruction directly contradicts the ads
    # mandate to always ask for budget — it must never appear here.
    assert "NEVER stop mid-run to ask" not in brief
    assert "DECIDE, DO NOT INTERROGATE" not in brief
    assert "DESIGN RULES" not in brief
    assert "CREDIT DISCIPLINE" not in brief
    assert "PLAN-FIRST MODE" not in brief


def test_ads_brief_states_the_account_lock():
    cfg = _cfg()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "act_33183820" in brief
    assert "AD ACCOUNT LOCK" in brief


def test_ads_brief_names_the_excluded_account_as_never_use():
    cfg = _cfg()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "act_1033832265075508" in brief
    assert "EXCLUDED" in brief
    assert "never select it" in brief.lower() or "NEVER use" in brief


def test_ads_brief_mandates_asking_for_budget_every_time():
    cfg = _cfg()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "YOU MUST ASK" in brief
    assert "When should this campaign end?" in brief
    assert "What's the budget?" in brief
    assert "NO standing budget cap" in brief


def test_ads_brief_mandates_always_paused():
    cfg = _cfg()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "ALWAYS PAUSED" in brief
    assert "PAUSED, no exceptions" in brief


def test_ads_brief_carries_no_cross_brand_leakage():
    cfg = _cfg()
    dc_brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    cq_brief = ed.build_brief(
        "ceram_quartz",
        cfg["brands"]["ceram_quartz"],
        "ads",
        "boost this post",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "act_1698581228193609" not in dc_brief
    assert "act_33183820" not in cq_brief
    assert "Claude DC Audience" not in cq_brief
    assert "Claude CQ AUDIENCE" not in dc_brief


def test_ads_brief_carries_vault_and_lessons_context(tmp_path, monkeypatch):
    """Past performance findings / brand voice are still useful for ad copy,
    unlike the organic-specific sections this brief otherwise skips.

    Seeds its OWN note rather than relying on whatever happens to be in the
    vault. Until conftest isolated ERA_VAULT_DIR this test read the user's real
    vault, so it passed on the strength of production data — it would have gone
    green even if the ads brief had stopped requesting vault context entirely,
    as long as the user still had notes.
    """
    from core import cm_vault

    vault = tmp_path / "era_brain"
    (vault / "Areas").mkdir(parents=True)
    (vault / "Areas" / "Delta_Voice.md").write_text(
        "---\nbrand: delta_cuisine\n---\n# Voice\n"
        "Warm, concrete, never salesy. MARKER_VAULT_NOTE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cm_vault, "VAULT_DIR", vault)

    cfg = _cfg()
    brief = ed.build_brief(
        "delta_cuisine",
        cfg["brands"]["delta_cuisine"],
        "ads",
        "run a campaign",
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    assert "VAULT" in brief
    assert "MARKER_VAULT_NOTE" in brief


# ---------------------------------------------------------------------------
# The three ads briefs: build / report / manage
# ---------------------------------------------------------------------------


def _ads_brief(brand_key, order):
    cfg = _cfg()
    return ed.build_brief(
        brand_key,
        cfg["brands"][brand_key],
        "ads",
        order,
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )


def test_report_brief_is_read_only_and_contradicts_nothing():
    """A report must not change anything, so the create brief's 'created PAUSED'
    and 'you must ask for a budget' rules have no business being in it — a rule the
    model is told to ignore is one it half-obeys."""
    brief = _ads_brief("ceram_quartz", "how are my campaigns doing")
    assert "MODE: REPORT" in brief and "READ-ONLY" in brief
    assert "Do NOT create, edit, pause, activate" in brief
    assert "ALWAYS PAUSED" not in brief
    assert "YOU MUST ASK" not in brief
    # the verdict vocabulary Karim reads
    for verdict in ("SUCCESS", "ON TRACK", "TOO EARLY", "NEEDS CHANGES", "FAILING"):
        assert verdict in brief
    # each recommendation must be a complete, say-it-back command (stateless runs)
    assert "ONE-LINE COMMAND" in brief
    assert "no memory of this report" in brief


def test_manage_brief_gates_spend_and_is_stateless():
    brief = _ads_brief("ceram_quartz", "extend the SAVANA campaign")
    assert "MODE: MANAGE" in brief
    assert "COMPLETE COMMAND" in brief  # a complete order IS the confirmation
    assert "NOT" in brief and "confirmed" in brief  # a vague one is not
    assert "SPEND GUARD" in brief and "TOTAL added" in brief
    assert "+25%" in brief  # scaling discipline
    assert "read it back" in brief  # verify every change
    assert "publish-all" in brief  # never publish the account's unrelated drafts
    assert "act_1698581228193609" in brief  # account lock still present


def test_report_and_manage_carry_the_account_lock_and_no_cross_brand_leak():
    for order in ("how are my campaigns doing", "extend the campaign"):
        cq = _ads_brief("ceram_quartz", order)
        dc = _ads_brief("delta_cuisine", order)
        assert "AD ACCOUNT LOCK" in cq and "AD ACCOUNT LOCK" in dc
        assert "act_1698581228193609" in cq and "act_1698581228193609" not in dc
        assert "act_33183820" in dc and "act_33183820" not in cq
        assert "act_1033832265075508" in dc  # the excluded look-alike stays named


def test_ceram_quartz_briefs_carry_karims_goals_and_baselines():
    """Followers + sales, judged against the account's OWN history."""
    for order in ("how are my campaigns doing", "extend the campaign", "run a campaign"):
        brief = _ads_brief("ceram_quartz", order)
        assert "PRIMARY GOAL" in brief and "followers" in brief.lower()
        assert "cost per message conversation" in brief
        assert "USD" in brief
    # Delta Cuisine has no goals block: the brief must degrade, not print "None".
    dc = _ads_brief("delta_cuisine", "how are my campaigns doing")
    assert "PRIMARY GOAL" not in dc and "None" not in dc


def test_build_and_manage_briefs_carry_the_no_auto_greeting_rule():
    """Karim's standing rule (2026-09-19): on messaging ads the customer types
    their own request. The rule has to reach the HEADLESS run, which never sees
    the interactive memory it was recorded in."""
    for order in ("run a campaign", "extend the campaign"):
        brief = _ads_brief("ceram_quartz", order)
        assert "NO AUTOMATED MESSAGE" in brief
        assert "types their OWN request" in brief
        assert "obsolete prices" in brief
    # a read-only report never edits an ad, so the rule is not its concern
    assert "NO AUTOMATED MESSAGE" not in _ads_brief("ceram_quartz", "how are my campaigns doing")


def test_build_brief_is_unchanged_by_the_new_modes():
    """The original create behaviour is the default, and still asks + pauses."""
    brief = _ads_brief("ceram_quartz", "run a Meta ad campaign")
    assert "YOU MUST ASK" in brief and "ALWAYS PAUSED" in brief
    assert "MODE: REPORT" not in brief and "MODE: MANAGE" not in brief
    assert "USD" in brief  # example budget line now uses the account currency


def test_detector_failure_falls_back_to_build_never_to_manage(monkeypatch):
    """If mode detection breaks, the safe direction is the create brief (which
    always pauses and always asks) — never a brief that may change live spend."""
    import app.cm_intents as ci

    def boom(_text):
        raise RuntimeError("detector down")

    monkeypatch.setattr(ci, "ads_mode", boom)
    brief = _ads_brief("ceram_quartz", "extend the campaign")
    assert "MODE: MANAGE" not in brief and "ALWAYS PAUSED" in brief


# ---------------------------------------------------------------------------
# Why "how are my campaigns doing" got no answer (2026-09-20) — each cause pinned
# ---------------------------------------------------------------------------


def _write_creds(tmp_path, **fb_entry):
    entry = {"serverName": "facebook-ads", "accessToken": "x", **fb_entry}
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"mcpOAuth": {"facebook-ads|abc123": entry}}), encoding="utf-8"
    )
    return str(tmp_path)


def test_preflight_blocks_an_expired_token_that_cannot_renew(tmp_path):
    """The actual root cause: expired 18 Sep, no refresh token — every run since
    reached Meta and came back empty."""
    cfg = _write_creds(tmp_path, expiresAt=int((time.time() - 86400) * 1000))
    problem = _REAL_CONNECTION_PROBLEM(cfg)
    assert problem and "expired" in problem and "/mcp" in problem
    assert "Nothing was changed" in problem


def test_preflight_lets_an_expired_token_through_when_it_can_renew(tmp_path):
    """Claude Code renews an expired access token itself when a refresh token
    exists. Blocking here would CREATE the outage this check exists to prevent."""
    cfg = _write_creds(tmp_path, expiresAt=int((time.time() - 86400) * 1000), refreshToken="r")
    assert _REAL_CONNECTION_PROBLEM(cfg) is None


def test_preflight_lets_a_live_token_through(tmp_path):
    cfg = _write_creds(tmp_path, expiresAt=int((time.time() + 86400) * 1000))
    assert _REAL_CONNECTION_PROBLEM(cfg) is None


def test_preflight_reports_a_missing_connector(tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps({"mcpOAuth": {}}), encoding="utf-8")
    assert "isn't signed in" in _REAL_CONNECTION_PROBLEM(str(tmp_path))


def test_preflight_degrades_to_open_on_unreadable_state(tmp_path):
    """A check that can wrongly stop a run costs more than the wasted run it
    prevents: missing or corrupt state means 'cannot judge', never 'blocked'."""
    assert _REAL_CONNECTION_PROBLEM(str(tmp_path)) is None  # no file at all
    (tmp_path / ".credentials.json").write_text("{not json", encoding="utf-8")
    assert _REAL_CONNECTION_PROBLEM(str(tmp_path)) is None


def test_run_task_stops_at_the_preflight_without_spawning_a_paid_run(monkeypatch):
    monkeypatch.setattr(ed, "_ads_connection_problem", lambda _d: "Meta Ads' sign-in expired.")

    def boom(*a, **k):
        raise AssertionError("a paid headless run must not start when Meta is signed out")

    monkeypatch.setattr(ed.subprocess, "run", boom)
    result = ed.run_task("how are my campaigns doing", task_type="ads", brand_key="ceram_quartz")
    assert result["ok"] is False
    assert "sign-in expired" in result["error"]


def test_explain_run_failure_says_the_cause_not_the_json_blob():
    ex = ed._explain_run_failure
    assert "signed out" in ex(
        "Failed to authenticate: OAuth session expired and could not be refreshed"
    )
    assert "signed out" in ex("Not logged in · Please run /login")
    assert "gateway" in ex("API Error: Connection refused — a firewall or proxy may be blocking it")
    assert "isn't recognised" in ex('[claude-code:unrecognized_model] {"model":"auto/best-coding"}')
    assert "work limit" in ex("errors:['Reached maximum budget ($4)'] budget_exhausted")
    assert "rate-limiting" in ex("429 too many requests")
    assert ex("").startswith("unknown")
    assert len(ex("x" * 5000)) <= 300


def test_budget_exhausted_beats_the_unrecognized_model_noise_line():
    """The 2026-09-20 report's TRUE cause (terminal_reason=budget_exhausted, in
    stdout) sat next to an `unrecognized_model` line on stderr. The budget cause
    must win — reporting 'model not recognised' sent the debugging the wrong way."""
    combined = (
        'errors:["Reached maximum budget ($4)"] budget_exhausted\n'
        '[claude-code:unrecognized_model] {"model":"auto/best-coding","query_source":"sdk"}'
    )
    out = ed._explain_run_failure(combined)
    assert "work limit" in out and "recognised" not in out


def test_failed_run_is_judged_on_stdout_reason_not_stderr_noise(monkeypatch):
    """The live log had stderr='[claude-code:unrecognized_model]…' while stdout's
    JSON said the OAuth session had expired. The old code took stderr first and
    reported the wrong problem."""

    class Fake:
        returncode = 1
        stdout = json.dumps(
            {"is_error": True, "result": "Failed to authenticate: OAuth session expired"}
        )
        stderr = '[claude-code:unrecognized_model] {"model":"auto/best-coding"}'

    monkeypatch.setattr(ed.subprocess, "run", lambda *a, **k: Fake())
    r = ed.run_task("run a campaign", task_type="ads", brand_key="ceram_quartz")
    assert r["ok"] is False
    assert "signed out" in r["error"] and "recognised" not in r["error"]
    assert "unrecognized_model" in r["raw_error"]  # full text kept for the log


def test_budget_exhausted_terminal_reason_is_surfaced_from_stdout(monkeypatch):
    """A budget-capped run has NO `result` field — only terminal_reason and errors.
    run_task must still explain it (this is the exact shape of the failed report)."""

    class Fake:
        returncode = 1
        stdout = json.dumps(
            {
                "is_error": True,
                "subtype": "error_max_budget_usd",
                "terminal_reason": "budget_exhausted",
                "errors": ["Reached maximum budget ($15)"],
            }
        )
        stderr = (
            '[claude-code:unrecognized_model] {"model":"auto/best-coding","query_source":"sdk"}'
        )

    monkeypatch.setattr(ed.subprocess, "run", lambda *a, **k: Fake())
    r = ed.run_task("how are my campaigns doing", task_type="ads", brand_key="ceram_quartz")
    assert r["ok"] is False
    assert "work limit" in r["error"] and "recognised" not in r["error"]


def _fake_ok(captured):
    class Ok:
        returncode = 0
        stdout = json.dumps({"result": "Two campaigns are on track."})
        stderr = ""

    def run(cmd, **kw):
        captured["cmd"] = cmd
        return Ok()

    return run


def test_report_and_manage_runs_are_spend_capped_and_build_is_not(monkeypatch):
    for order, capped in [
        ("how are my campaigns doing", True),
        ("extend the SAVANA campaign to 30 Sep", True),
        ("run a Meta ad campaign", False),
    ]:
        got = {}
        monkeypatch.setattr(ed.subprocess, "run", _fake_ok(got))
        ed.run_task(order, task_type="ads", brand_key="ceram_quartz")
        assert ("--max-budget-usd" in got["cmd"]) is capped, order
        if capped:
            assert got["cmd"][got["cmd"].index("--max-budget-usd") + 1] == str(
                ed._ADS_OPS_MAX_BUDGET_USD
            )


def _run_in_thread(order):
    box = {}

    def go():
        box["r"] = ed.run_task(order, task_type="ads", brand_key="ceram_quartz")

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, box


def test_read_only_report_does_not_wait_behind_a_serialized_job(monkeypatch):
    """Karim's report waited 11-40 minutes behind weekly content runs. A report
    touches neither Magnific nor Adobe, the resources the one-at-a-time queue
    protects — so with that queue occupied it must still finish."""
    monkeypatch.setattr(ed.subprocess, "run", _fake_ok({}))
    monkeypatch.setattr(
        ed, "current_job", lambda: {"brand": "delta_cuisine", "task_type": "weekly"}
    )
    assert ed._GLOBAL_LOCK.acquire(timeout=5)
    try:
        t, box = _run_in_thread("how are my campaigns doing")
        t.join(timeout=15)
        assert not t.is_alive(), "read-only report blocked behind the serialized queue"
        assert box["r"]["ok"] is True
        assert box["r"]["queued_behind"] is None
    finally:
        ed._GLOBAL_LOCK.release()


def test_everything_else_is_still_serialized(monkeypatch):
    """The bypass must stay narrow: a build (and any content lane) still queues."""
    monkeypatch.setattr(ed.subprocess, "run", _fake_ok({}))
    assert ed._GLOBAL_LOCK.acquire(timeout=5)
    t = None
    try:
        t, box = _run_in_thread("run a Meta ad campaign")
        t.join(timeout=1.5)
        assert t.is_alive(), "a build order must wait for the serialized queue"
    finally:
        ed._GLOBAL_LOCK.release()
    t.join(timeout=15)
    assert not t.is_alive() and box["r"]["ok"] is True


def test_asking_twice_joins_the_report_already_running(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a duplicate report must not start a second paid run")

    monkeypatch.setattr(ed.subprocess, "run", boom)
    key = ("ceram_quartz", "how are my campaigns doing")
    with ed._READONLY_INFLIGHT_LOCK:
        ed._READONLY_INFLIGHT.add(key)
    try:
        r = ed.run_task("How are my campaigns  doing", task_type="ads", brand_key="ceram_quartz")
    finally:
        with ed._READONLY_INFLIGHT_LOCK:
            ed._READONLY_INFLIGHT.discard(key)
    assert r["ok"] is True and r["duplicate"] is True
    assert r["wrote_files"] is None  # None must not trip the "wrote 0 files" caveat


def test_report_releases_its_inflight_slot_when_done(monkeypatch):
    monkeypatch.setattr(ed.subprocess, "run", _fake_ok({}))
    ed.run_task("how are my campaigns doing", task_type="ads", brand_key="ceram_quartz")
    assert not ed._READONLY_INFLIGHT, (
        "a finished report must free its slot or the next ask is refused"
    )


def test_ops_briefs_are_spoken_and_do_not_recite_the_lock():
    for order in ("how are my campaigns doing", "extend the SAVANA campaign"):
        brief = _ads_brief("ceram_quartz", order)
        assert "YOUR FINAL MESSAGE IS SPOKEN ALOUD" in brief
        assert "Do NOT recite this lock" in brief
        assert "never present an empty pull as a report" in brief
        assert "State this Ad Account Lock back" not in brief
    # build still recites it — that is the safety check before creating anything
    assert "State this Ad Account Lock back" in _ads_brief("ceram_quartz", "run a campaign")


# ---------------------------------------------------------------------------
# run_task — ads.enabled gate + freelance-account override
# ---------------------------------------------------------------------------


def test_run_task_refuses_when_ads_disabled(monkeypatch):
    cfg = ed.load_brands()
    fake_brand = dict(cfg["brands"]["delta_cuisine"])
    fake_brand["ads"] = {**fake_brand["ads"], "enabled": False}
    fake_cfg = {**cfg, "brands": {**cfg["brands"], "delta_cuisine": fake_brand}}
    monkeypatch.setattr(ed, "load_brands", lambda: fake_cfg)

    result = ed.run_task("run an ad campaign", task_type="ads", brand_key="delta_cuisine")

    assert result["ok"] is False
    assert "not enabled" in result["error"]


def test_run_task_ads_overrides_config_dir_to_freelance(monkeypatch):
    """Delta Cuisine's OWN account (.claude-delta) never had facebook-ads
    registered — only .claude-freelance did. An ads task for ANY brand must
    use the freelance account, not that brand's normal account."""
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = '{"result": "ok"}'
        stderr = ""

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        captured["config_dir"] = env.get("CLAUDE_CONFIG_DIR")
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(ed.subprocess, "run", fake_run)

    result = ed.run_task(
        "run a Meta ad campaign for Delta Cuisine", task_type="ads", brand_key="delta_cuisine"
    )

    assert result["ok"] is True
    assert captured["config_dir"] == r"C:\Users\nesri\.claude-freelance"
    assert (
        captured["config_dir"] != ed.load_brands()["brands"]["delta_cuisine"]["claude_config_dir"]
    )


def test_run_task_ads_allowed_tools_include_facebook_ads_and_composio(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = '{"result": "ok"}'
        stderr = ""

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(ed.subprocess, "run", fake_run)
    ed.run_task("boost this post", task_type="ads", brand_key="ceram_quartz")

    idx = captured["cmd"].index("--allowedTools")
    allowed = captured["cmd"][idx + 1]
    assert "mcp__facebook-ads" in allowed
    assert "mcp__composio" in allowed
    assert "mcp__magnific" not in allowed  # ads reuses assets, doesn't generate


def test_run_task_ads_refuses_when_ads_config_dir_unset(monkeypatch):
    cfg = ed.load_brands()
    fake_defaults = {**cfg["defaults"]}
    fake_defaults.pop("ads_claude_config_dir", None)
    fake_cfg = {**cfg, "defaults": fake_defaults}
    monkeypatch.setattr(ed, "load_brands", lambda: fake_cfg)

    result = ed.run_task("run an ad campaign", task_type="ads", brand_key="delta_cuisine")

    assert result["ok"] is False
    assert "ads_claude_config_dir" in result["error"]


# ---------------------------------------------------------------------------
# Ads build follow-ups resume the same session (2026-09-25)
# ---------------------------------------------------------------------------


def _fake_session_runs(calls, outcomes):
    """Each call pops the next (returncode, stdout) outcome and records cmd+input."""

    def run(cmd, **kw):
        calls.append({"cmd": cmd, "input": kw.get("input")})
        code, out = outcomes.pop(0)

        class P:
            returncode = code
            stdout = out
            stderr = ""

        return P()

    return run


def test_build_answer_resumes_the_session_that_asked(monkeypatch):
    calls = []
    outcomes = [
        (0, json.dumps({"result": "When should it end? Budget?", "session_id": "s-1"})),
        (0, json.dumps({"result": "Built, paused.", "session_id": "s-2"})),
    ]
    monkeypatch.setattr(ed.subprocess, "run", _fake_session_runs(calls, outcomes))
    ed.run_task("launch meta ads for the last story", task_type="ads", brand_key="ceram_quartz")
    ed.run_task("end 2026-10-09, 3 dollars a day", task_type="ads", brand_key="ceram_quartz")
    assert "--resume" not in calls[0]["cmd"]
    assert calls[1]["cmd"][calls[1]["cmd"].index("--resume") + 1] == "s-1"
    assert "3 dollars a day" in calls[1]["input"]
    assert "FOLLOW-UP" in calls[1]["input"]
    assert ed._load_ads_session("ceram_quartz") == "s-2"


def test_failed_resume_falls_back_to_the_full_brief(monkeypatch):
    ed._save_ads_session("ceram_quartz", "gone")
    calls = []
    outcomes = [
        (1, "No conversation found with session ID: gone"),
        (0, json.dumps({"result": "Built, paused.", "session_id": "s-3"})),
    ]
    monkeypatch.setattr(ed.subprocess, "run", _fake_session_runs(calls, outcomes))
    res = ed.run_task("end 2026-10-09, 3 dollars a day", task_type="ads", brand_key="ceram_quartz")
    assert res["ok"]
    assert "--resume" in calls[0]["cmd"] and "--resume" not in calls[1]["cmd"]
    assert "Ad Account Lock" in calls[1]["input"] or "AD ACCOUNT" in calls[1]["input"].upper()


def test_stale_or_other_brand_session_is_not_resumed(monkeypatch):
    ed._save_ads_session("delta_cuisine", "dc-1")
    assert ed._load_ads_session("ceram_quartz") is None
    data = json.loads(ed._ADS_SESSIONS_PATH.read_text(encoding="utf-8"))
    data["delta_cuisine"]["ts"] -= ed._ADS_RESUME_WINDOW_S + 1
    ed._ADS_SESSIONS_PATH.write_text(json.dumps(data), encoding="utf-8")
    assert ed._load_ads_session("delta_cuisine") is None


def test_reports_never_resume_a_build_session(monkeypatch):
    ed._save_ads_session("ceram_quartz", "s-1")
    got = {}
    monkeypatch.setattr(ed.subprocess, "run", _fake_ok(got))
    ed.run_task("how are my campaigns doing", task_type="ads", brand_key="ceram_quartz")
    assert "--resume" not in got["cmd"]


def test_build_brief_does_not_reask_a_budget_already_in_the_order():
    brief = _ads_brief("ceram_quartz", "launch meta ads, 14 days, 3 or 4 dollars a day")
    assert "do not ask me to repeat it" in brief
    assert "never a question" in brief


# ---------------------------------------------------------------------------
# OmniRoute gateway — Era starts it herself (2026-09-25)
# ---------------------------------------------------------------------------


def test_gateway_port_only_for_a_local_base_url():
    from core import omniroute as om

    assert om.gateway_port({"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128"}) == 20128
    assert om.gateway_port({"ANTHROPIC_BASE_URL": "http://localhost:9000/v1"}) == 9000
    assert om.gateway_port({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None
    assert om.gateway_port({}) is None


def test_gateway_is_left_alone_when_already_up(monkeypatch):
    from core import omniroute as om

    monkeypatch.setattr(om, "is_up", lambda port, timeout=1.0: True)
    monkeypatch.setattr(om.subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
    ok, detail = om.ensure_running(env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128"})
    assert ok and "already up" in detail


def test_gateway_is_started_when_down(monkeypatch, tmp_path):
    from core import omniroute as om

    state = {"up": False, "cmd": None, "env": None}
    monkeypatch.setattr(om, "is_up", lambda port, timeout=1.0: state["up"])
    monkeypatch.setattr(om, "_omniroute_cmd", lambda: tmp_path / "omniroute.cmd")
    monkeypatch.setattr(om, "_LOG_PATH", tmp_path / "omniroute-start.log")

    def fake_run(cmd, **kw):
        state.update(up=True, cmd=cmd, env=kw["env"])

    monkeypatch.setattr(om.subprocess, "run", fake_run)
    ok, detail = om.ensure_running(env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128"})
    assert ok and detail.startswith("started")
    assert "serve" in state["cmd"] and "--daemon" in state["cmd"]
    assert state["env"]["OMNIROUTE_SERVER_HOST"] == "127.0.0.1"
    assert state["env"]["PORT"] == "20128"


def test_gateway_failure_degrades_never_raises(monkeypatch):
    from core import omniroute as om

    def boom(*_a, **_k):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(om, "is_up", boom)
    ok, detail = om.ensure_running(env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:20128"})
    assert not ok and "socket exploded" in detail


def test_build_and_manage_briefs_source_creatives_from_published_posts_only():
    for order in ("launch meta ads for the last story", "extend the SAVANA campaign to 30 Sep"):
        brief = _ads_brief("ceram_quartz", order)
        assert "PUBLISHED CONTENT ONLY" in brief, order
        assert "FACEBOOK_GET_PAGE_POSTS page_id=788466647689882" in brief, order
        assert "object_story_id" in brief, order
        assert "NEVER upload" in brief, order


# ---------------------------------------------------------------------------
# Ads go direct on the subscription, OmniRoute as fallback (2026-09-25)
# ---------------------------------------------------------------------------


def _capture_runs(calls, outcomes):
    def run(cmd, **kw):
        calls.append({"cmd": cmd, "env": kw.get("env") or {}})
        code, out = outcomes.pop(0)

        class P:
            returncode = code
            stdout = out
            stderr = ""

        return P()

    return run


_GATEWAY = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:20128",
    "ANTHROPIC_MODEL": "auto/best-coding",
    "ANTHROPIC_API_KEY": "sk-test",
}


def test_ads_run_goes_direct_without_gateway_vars(monkeypatch):
    for k, v in _GATEWAY.items():
        monkeypatch.setenv(k, v)
    calls = []
    outcomes = [(0, json.dumps({"result": "Built, paused.", "session_id": "d-1"}))]
    monkeypatch.setattr(ed.subprocess, "run", _capture_runs(calls, outcomes))
    res = ed.run_task("run a Meta ad campaign", task_type="ads", brand_key="ceram_quartz")
    assert res["ok"] and len(calls) == 1
    assert calls[0]["cmd"][calls[0]["cmd"].index("--model") + 1] == ed._ADS_DIRECT_MODEL
    assert not any(k in calls[0]["env"] for k in _GATEWAY)


_FREELANCE, _DELTA = r"C:\p\.claude-freelance", r"C:\p\.claude-delta"
_AUTH_FAIL = (
    1,
    json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"}),
)
_BUILT = (0, json.dumps({"result": "Built, paused.", "session_id": "s-ok"}))


def _two_profiles(monkeypatch):
    for k, v in _GATEWAY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ed, "_claude_accounts", lambda _cfg: [_FREELANCE, _DELTA])
    real = dict(ed.load_brands())
    real["defaults"] = {**(real.get("defaults") or {}), "ads_claude_config_dir": _FREELANCE}
    monkeypatch.setattr(ed, "load_brands", lambda: real)


def test_a_signed_out_profile_fails_over_to_the_other_profile(monkeypatch):
    _two_profiles(monkeypatch)
    calls = []
    monkeypatch.setattr(ed.subprocess, "run", _capture_runs(calls, [_AUTH_FAIL, _BUILT]))
    res = ed.run_task("run a Meta ad campaign", task_type="ads", brand_key="ceram_quartz")
    assert res["ok"] and len(calls) == 2
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == _FREELANCE
    assert calls[1]["env"]["CLAUDE_CONFIG_DIR"] == _DELTA
    assert "--model" in calls[1]["cmd"] and "ANTHROPIC_BASE_URL" not in calls[1]["env"]


def test_both_profiles_down_falls_back_to_the_gateway(monkeypatch):
    _two_profiles(monkeypatch)
    calls = []
    runs = _capture_runs(calls, [_AUTH_FAIL, _AUTH_FAIL, _BUILT])
    monkeypatch.setattr(ed.subprocess, "run", runs)
    res = ed.run_task("run a Meta ad campaign", task_type="ads", brand_key="ceram_quartz")
    assert res["ok"] and len(calls) == 3
    assert "--model" not in calls[2]["cmd"]
    assert calls[2]["env"]["ANTHROPIC_BASE_URL"] == _GATEWAY["ANTHROPIC_BASE_URL"]


def test_a_profile_without_meta_is_never_an_ads_failover(monkeypatch):
    _two_profiles(monkeypatch)
    monkeypatch.setattr(ed, "_ads_connection_problem", lambda d: "no Meta" if d == _DELTA else None)
    calls = []
    monkeypatch.setattr(ed.subprocess, "run", _capture_runs(calls, [_AUTH_FAIL, _BUILT]))
    ed.run_task("run a Meta ad campaign", task_type="ads", brand_key="ceram_quartz")
    assert [c["env"]["CLAUDE_CONFIG_DIR"] for c in calls] == [_FREELANCE, _FREELANCE]
    assert "--model" not in calls[1]["cmd"]  # straight to the gateway, never Delta


def test_ads_move_to_the_profile_whose_meta_connection_works(monkeypatch):
    _two_profiles(monkeypatch)
    monkeypatch.setattr(
        ed, "_ads_connection_problem", lambda d: "expired" if d == _FREELANCE else None
    )
    calls = []
    monkeypatch.setattr(ed.subprocess, "run", _capture_runs(calls, [_BUILT]))
    assert ed.run_task("run a Meta ad campaign", task_type="ads", brand_key="ceram_quartz")["ok"]
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == _DELTA


def test_a_resume_runs_only_on_the_profile_that_owns_the_session(monkeypatch):
    _two_profiles(monkeypatch)
    ed._save_ads_session("ceram_quartz", "d-7", _DELTA)
    calls = []
    monkeypatch.setattr(ed.subprocess, "run", _capture_runs(calls, [_BUILT]))
    ed.run_task("end 2026-10-09, 3 dollars a day", task_type="ads", brand_key="ceram_quartz")
    assert "--resume" in calls[0]["cmd"] and calls[0]["env"]["CLAUDE_CONFIG_DIR"] == _DELTA


def test_a_real_task_failure_does_not_rerun_elsewhere(monkeypatch):
    calls = []
    outcomes = [(1, json.dumps({"is_error": True, "result": "Meta rejected the targeting spec"}))]
    monkeypatch.setattr(ed.subprocess, "run", _capture_runs(calls, outcomes))
    res = ed.run_task("run a Meta ad campaign", task_type="ads", brand_key="ceram_quartz")
    assert not res["ok"] and len(calls) == 1


def test_route_order_is_each_profile_direct_then_the_gateway():
    routes = ed._routes_for([_FREELANCE, _DELTA], dict(_GATEWAY))
    assert [r[0] for r in routes] == [
        "direct:.claude-freelance",
        "direct:.claude-delta",
        "gateway:.claude-freelance",
    ]
    assert routes[2][1]["ANTHROPIC_BASE_URL"] == _GATEWAY["ANTHROPIC_BASE_URL"]
    assert all(k not in routes[0][1] for k in _GATEWAY)


def test_briefs_point_at_the_ads_skill_by_absolute_path():
    assert ed._ADS_SKILL_PATH.is_file()
    for order in ("launch meta ads for the last story", "how are my campaigns doing"):
        assert str(ed._ADS_SKILL_PATH) in _ads_brief("ceram_quartz", order), order


def test_optimisation_orders_reach_manage_mode_2026_09_25():
    from app.cm_intents import ads_mode

    for order in (
        "switch placements to manual, remove Facebook Feed and Audience Network",
        "pause ad 52579562852040",
        "move the remaining packs budget to the Interactions campaigns",
        "optimize the packs campaign",
        "lower the cost per conversation of the packs campaign",
        "exclude ages 18 to 34 on the packs ad set",
    ):
        assert ads_mode(order) == "manage", order
    # A bare "ad" is still never a manage target, and builds stay builds.
    assert ads_mode("stop the ad on the video") == "build"
    assert ads_mode("launch meta ads for the last story, 14 days, 3 dollars a day") == "build"


# ---------------------------------------------------------------------------
# Brico Kitchen: ads-only brand sharing Delta Cuisine's account (2026-09-25)
# ---------------------------------------------------------------------------


def test_brico_kitchen_is_ads_only_and_refuses_organic_orders(monkeypatch):
    monkeypatch.setattr(ed.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    for lane in ("weekly", "publish", "campaign", "revise"):
        res = ed.run_task("plan the week", task_type=lane, brand_key="brico_kitchen")
        assert not res["ok"] and "ads only" in res["error"], lane


def test_brico_orders_resolve_to_brico_and_mixed_orders_ask():
    cfg = _cfg()
    assert ed.resolve_brand("boost the brico kitchen infinity post", cfg) == "brico_kitchen"
    assert ed.resolve_brand("delta cuisine dressing campaign", cfg) == "delta_cuisine"
    assert ed.resolve_brand("move brico budget to delta", cfg) is None


def test_shared_account_briefs_page_lock_each_brand_to_its_own_page():
    brico = _ads_brief("brico_kitchen", "boost the infinity pack post")
    delta = _ads_brief("delta_cuisine", "how are my campaigns doing")
    cq = _ads_brief("ceram_quartz", "how are my campaigns doing")
    assert "SHARED AD ACCOUNT" in brico and "promote page 100288952367054" in brico
    assert "Delta Cuisine (page 115558078454323)" in brico
    assert "SHARED AD ACCOUNT" in delta and "promote page 115558078454323" in delta
    assert "Brico Kitchen (page 100288952367054)" in delta
    assert "SHARED AD ACCOUNT" not in cq and "Brico" not in cq


def test_boost_only_rule_reaches_brico_and_no_one_else():
    assert "BOOST-ONLY BRAND" in _ads_brief("brico_kitchen", "boost the infinity pack post")
    assert "BOOST-ONLY BRAND" in _ads_brief("brico_kitchen", "how are my campaigns doing")
    for brand in ("delta_cuisine", "ceram_quartz"):
        assert "BOOST-ONLY BRAND" not in _ads_brief(brand, "run a meta ad campaign"), brand


def test_ads_runs_never_call_paid_analysis_plugins_2026_09_25():
    allowed = ed._DEFAULT_ALLOWED_TOOLS["ads"].split(",")
    assert "mcp__facebook-ads" in allowed and "mcp__composio" in allowed
    assert not any("adspirer" in t or "hypd" in t for t in allowed)


def test_report_and_manage_briefs_use_the_in_house_analyst():
    assert ed._ANALYST_SKILL_PATH.is_file()
    for order in ("how are my campaigns doing", "extend the SAVANA campaign to 30 Sep"):
        brief = _ads_brief("ceram_quartz", order)
        assert str(ed._ANALYST_SKILL_PATH) in brief, order
        assert "never call paid analysis plugins" in brief, order


def test_analysis_questions_are_read_only_reports():
    from app.cm_intents import ads_mode

    for order in (
        "why did the cost per conversation go up on the dressing campaign",
        "audit my meta ads",
        "what ads are my competitors running",
        "is the Tej Mahal ad tired",
        "compare my campaigns to the industry benchmark",
    ):
        assert ads_mode(order) == "report", order
    # A change order is still a change order, even with analysis words in it.
    assert ads_mode("lower the cost per conversation of the packs campaign") == "manage"
    assert ads_mode("launch meta ads for the last story, 14 days, 3 dollars a day") == "build"
