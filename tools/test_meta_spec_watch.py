"""The weekly Meta-changes watch: it flags real differences, tolerates rewording, never edits
anything, and never says "up to date" when it could not check."""

import datetime as dt
import json

import pytest

import era_dispatch as ed
from core import meta_spec_watch as w

DAY = dt.date(2026, 11, 20)


def _digest(**topics):
    return {
        tid: {"title": tid, "rules": rules}
        for tid, rules in topics.items()
    }


def _reply(digest):
    return "Here you go:\n" + json.dumps(digest)


CTA_V1 = ["Send WhatsApp message needs WhatsApp", "Learn more is available", "Contact us is available"] * 1 + [
    f"rule {i}" for i in range(7)
]


def test_parse_takes_the_first_json_object_and_ignores_unknown_topics():
    text = _reply({**_digest(cta_by_objective=["a", "b"]), "made_up": {"title": "x", "rules": ["y"]}}) + " trailing"
    out = w.parse_digest(text)
    assert list(out) == ["cta_by_objective"] and out["cta_by_objective"]["rules"] == ["a", "b"]
    assert w.parse_digest("no json at all") == {} and w.parse_digest("") == {}


def test_a_reworded_statement_is_not_a_change_but_a_new_rule_is():
    old = _digest(cta_by_objective=CTA_V1)
    reworded = _digest(cta_by_objective=["SEND WHATSAPP MESSAGE needs WhatsApp!"] + CTA_V1[1:])
    assert w.diff_digests(old, reworded) == []
    changed = _digest(cta_by_objective=CTA_V1[:5] + ["A new condition: WhatsApp calls are required", "Another new rule", "A third new rule"])
    out = w.diff_digests(old, changed)
    assert out and out[0]["topic"] == "cta_by_objective" and out[0]["added"] and out[0]["removed"]


def test_a_topic_that_could_not_be_retrieved_is_not_reported_as_a_change():
    old = _digest(cta_by_objective=CTA_V1)
    assert w.diff_digests(old, _digest(cta_by_objective=[])) == []
    assert w.diff_digests({}, old) == []


def test_baseline_then_quiet_then_a_real_change_flags_and_writes_an_inbox_note(monkeypatch, tmp_path):
    monkeypatch.setattr("core.cm_vault.VAULT_DIR", tmp_path / "vault")
    base = _digest(cta_by_objective=CTA_V1, messaging_ads=["x1", "x2", "x3", "x4", "x5"])
    st = w.check(lambda p: _reply(base), today=DAY)
    assert st["ok"] and st["baseline"] and st["changed"] == []
    st = w.check(lambda p: _reply(base), today=DAY)
    assert st["ok"] and not st["baseline"] and st["changed"] == [] and st["acknowledged"]
    new = _digest(cta_by_objective=["brand new rule %d" % i for i in range(8)], messaging_ads=base["messaging_ads"]["rules"])
    st = w.check(lambda p: _reply(new), today=DAY)
    assert st["changed"] == ["cta_by_objective"] and not st["acknowledged"]
    note = (tmp_path / "vault" / "00 - Inbox" / "Meta_Specs_Change_2026-11-20.md").read_text(encoding="utf-8")
    assert "ADDED: brand new rule 0" in note and "era_brief: false" in note
    # the baseline is NOT replaced until a human acks
    assert json.loads(w.DIGEST_PATH.read_text(encoding="utf-8"))["digest"]["cta_by_objective"]["rules"] == CTA_V1
    assert "META MAY HAVE CHANGED" in w.watch_note(today=DAY)
    st = w.ack(today=DAY)
    assert st["changed"] == [] and st["acknowledged"] and w.watch_note(today=DAY) == ""
    assert json.loads(w.DIGEST_PATH.read_text(encoding="utf-8"))["digest"]["cta_by_objective"]["rules"][0].startswith("brand new rule")


def test_a_check_that_cannot_run_says_so_and_keeps_an_open_change(monkeypatch):
    def boom(prompt):
        raise RuntimeError("gateway down")

    w._write(w.STATUS_PATH, {"checked_at": "2026-11-13", "ok": True, "changed": ["cta_by_objective"], "acknowledged": False})
    st = w.check(boom, today=DAY)
    assert st["ok"] is False and "gateway down" in st["reason"] and st["changed"] == ["cta_by_objective"]
    st = w.check(lambda p: "no json", today=DAY)
    assert st["ok"] is False and "no topic" in st["reason"]
    assert "META MAY HAVE CHANGED" in w.watch_note(today=DAY)  # still flagged, never silently cleared


def test_the_brief_note_covers_never_run_failed_stale_and_healthy(monkeypatch):
    w.STATUS_PATH.unlink()
    assert "NEVER RUN" in w.watch_note(today=DAY)
    w._write(w.STATUS_PATH, {"checked_at": "2026-11-19", "ok": False, "reason": "gateway down", "changed": [], "acknowledged": True})
    assert "NOT CURRENT" in w.watch_note(today=DAY) and "gateway down" in w.watch_note(today=DAY)
    w._write(w.STATUS_PATH, {"checked_at": "2026-10-01", "ok": True, "changed": [], "acknowledged": True})
    assert "NOT CURRENT" in w.watch_note(today=DAY)
    w._write(w.STATUS_PATH, {"checked_at": "2026-11-18", "ok": True, "changed": [], "acknowledged": True})
    assert w.watch_note(today=DAY) == ""


def test_the_watch_is_read_only_and_names_the_help_article_tool():
    prompt = w.digest_prompt()
    assert "READ-ONLY" in prompt and "ads_get_help_article" in prompt
    assert "Never create or change anything" in prompt
    assert {t["id"] for t in w.TOPICS} >= {"cta_by_objective", "conversion_locations", "messaging_ads", "ads_policy_changes"}


def test_the_ads_build_brief_carries_the_watch_note(monkeypatch):
    w._write(w.STATUS_PATH, {"checked_at": "2026-11-19", "ok": True, "changed": ["messaging_ads"], "acknowledged": False, "note": "inbox"})
    note = ed._meta_currency_note_for("ads", "launch meta ads for the last reel, 5 dollars a day")
    assert "META MAY HAVE CHANGED" in note and "messaging_ads" in note
    assert ed._meta_currency_note_for("weekly", "plan the week") == ""


def test_the_watch_is_scheduled_weekly_and_registered():
    import era_scheduler

    assert "meta_spec_watch" in era_scheduler.GLOBAL_TASK_MAP
    sched = ed.load_brands()["defaults"]["schedules"]["meta_spec_watch"]
    assert sched["enabled"] and sched["cron"] == "0 11 * * 1"  # Karim 2026-11-20: not 04:00


def test_catch_up_is_needed_when_there_is_no_recent_good_check():
    assert not w.needs_catchup(today=DAY)  # the conftest status is fresh and ok
    w._write(w.STATUS_PATH, {"checked_at": "2026-11-05", "ok": True, "changed": [], "acknowledged": True})
    assert w.needs_catchup(today=DAY)  # 15 days
    w._write(w.STATUS_PATH, {"checked_at": "2026-11-19", "ok": False, "reason": "x", "changed": [], "acknowledged": True})
    assert w.needs_catchup(today=DAY)  # failed
    w.STATUS_PATH.unlink()
    assert w.needs_catchup(today=DAY)  # never ran


def test_the_scheduler_queues_a_one_off_catch_up_when_era_missed_the_week(monkeypatch):
    import era_scheduler

    added = []
    monkeypatch.setattr(era_scheduler._scheduler, "add_job", lambda *a, **k: added.append((a, k)))
    cfg = ed.load_brands()
    w._write(w.STATUS_PATH, {"checked_at": "2026-10-01", "ok": True, "changed": [], "acknowledged": True})
    era_scheduler._schedule_meta_watch_catchup(cfg)
    assert any(k.get("id") == "_global:meta_spec_watch:catchup" for _, k in added)
    added.clear()
    w._write(w.STATUS_PATH, {"checked_at": dt.date.today().isoformat(), "ok": True, "changed": [], "acknowledged": True})
    era_scheduler._schedule_meta_watch_catchup(cfg)
    assert added == []
