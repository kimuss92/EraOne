"""PF-075 / PF-076 guards: lessons are preferences, not orders; any input the owner gives
(file of any type, link, sent attachment, misheard name or path) reaches the CM run.

Live 2026-11-20: the lesson store held one-off orders ("CONNECTIVITY TEST ONLY...", "Abort
all tasks...") rendered as firm rules; "New two and the HB" was the file "new 2"; files sent
on Telegram were ignored; "Activate the Meta campaign 52593521115440" went to the organic lane.
"""

from __future__ import annotations

import json
import time

import era_dispatch as d


def _brand(tmp_path):
    return {"display_name": "Ceram Quartz", "project_dir": str(tmp_path / "ceram")}


def test_any_file_type_is_an_input_and_text_is_inlined(tmp_path):
    folder = tmp_path / "outside the master folder"
    folder.mkdir()
    (folder / "brief.txt").write_text("Launch price 890 TND, Alya slab", encoding="utf-8")
    (folder / "catalog.pdf").write_bytes(b"%PDF-1.4")
    g = d.ground_order("ceram_quartz", _brand(tmp_path), "weekly", f"write captions from {folder}")
    assert g["ok"], g
    assert "[text] brief.txt" in g["note"] and "Launch price 890 TND" in g["note"]
    assert "[pdf] catalog.pdf" in g["note"]


def test_a_misheard_name_or_path_is_matched_to_the_real_one(tmp_path):
    reels = tmp_path / "ceram" / "final"
    reels.mkdir(parents=True)
    (reels / "new 2.mp4").write_bytes(b"x")
    (reels / "HB.mp4").write_bytes(b"x")
    g = d.ground_order(
        "ceram_quartz", _brand(tmp_path), "weekly", "descriptions for the reels 'New two' and 'hb'"
    )
    assert g["ok"] and "new 2.mp4" in g["note"] and "HB.mp4" in g["note"], g
    typo = tmp_path / "ceram" / "Final"  # wrong case / spacing in a spoken path
    g2 = d.ground_order(
        "ceram_quartz", _brand(tmp_path), "weekly", f"describe the reels in {typo}\\new2.mp4"
    )
    assert g2["ok"] and "new 2.mp4" in g2["note"] and g2["corrected"], g2


def test_links_are_handed_to_the_run(tmp_path):
    g = d.ground_order(
        "ceram_quartz",
        _brand(tmp_path),
        "weekly",
        "write a caption from https://example.com/alya-launch and keep it short",
    )
    assert g["ok"] and "https://example.com/alya-launch" in g["note"] and "WebFetch" in g["note"]


def test_the_file_i_sent_uses_the_last_attachment(tmp_path, monkeypatch):
    sent = tmp_path / "inbox" / "1795_menu.pdf"
    sent.parent.mkdir()
    sent.write_bytes(b"%PDF")
    rec = tmp_path / "last.json"
    monkeypatch.setattr(d, "_LAST_ATTACHMENT_PATH", rec)
    order = "make a post from the file I sent"
    none = d.ground_order("ceram_quartz", _brand(tmp_path), "weekly", order)
    assert not none["ok"] and "no recent file" in none["error"]
    rec.write_text(json.dumps({"path": str(sent), "name": "menu.pdf", "saved_at": time.time()}))
    g = d.ground_order("ceram_quartz", _brand(tmp_path), "weekly", order)
    assert g["ok"] and "1795_menu.pdf" in g["note"]


def test_a_telegram_file_is_saved_and_its_caption_runs_with_the_path(tmp_path, monkeypatch):
    import era_telegram as tg

    monkeypatch.setattr(tg, "_INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(tg, "_LAST_ATTACHMENT", tmp_path / "last.json")

    def fake_download(file_id, dest_dir=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / "1795_file_17.pdf"
        p.write_bytes(b"%PDF")
        return p

    monkeypatch.setattr(tg, "download_file", fake_download)
    sent, orders = [], []
    monkeypatch.setattr(tg, "send_message", lambda t, **k: sent.append(t) or True)
    msg = {"document": {"file_id": "F", "file_name": "Menu Delta.pdf"}, "caption": "post this"}
    tg._handle_attachment(msg, orders.append)
    assert orders and orders[0].startswith("post this (file I sent: ")
    assert "Menu Delta.pdf" in orders[0]
    rec = json.loads((tmp_path / "last.json").read_text())
    assert rec["name"] == "Menu Delta.pdf"
    orders.clear()
    tg._handle_attachment({"photo": [{"file_id": "a", "file_size": 1}]}, orders.append)
    assert not orders and "Tell me what to do with it" in sent[-1]


def test_one_off_orders_are_never_lessons_and_old_ones_are_not_rendered(tmp_path, monkeypatch):
    from core import cm_learning as c

    monkeypatch.setattr(c, "_LESSONS_PATH", tmp_path / "lessons.json")
    monkeypatch.setattr(c, "_EVENTS_PATH", tmp_path / "events.jsonl")
    for order in (
        "CONNECTIVITY TEST ONLY. Do not generate content.",
        "Abort all tasks related to the Meta Ads campaign.",
        "Resume ad 52580530744840 so it delivers again, do not change the budget",
        "x" * 301,
    ):
        assert c.is_one_off_order(order)
        assert c.record_feedback("ceram_quartz", order, "ads") is None
    assert not c.is_one_off_order("stop using the clinical white kitchens")
    # An old one-off already in the store is not rendered as a rule, and can be forgotten.
    tmp = tmp_path / "lessons.json"
    tmp.write_text(
        json.dumps(
            {
                "ceram_quartz": [
                    {"category": "general", "polarity": "avoid", "text": "Abort all tasks now"},
                    {"category": "material", "polarity": "avoid", "text": "never use Polaris"},
                ]
            }
        ),
        encoding="utf-8",
    )
    rendered = c.render_lessons("ceram_quartz")
    assert "never use Polaris" in rendered and "Abort" not in rendered
    assert c.forget_lessons("ceram_quartz", "never use polaris") == 1
    assert c.forget_lessons("ceram_quartz", "ab") == 0  # too short to be a safe match


def test_a_campaign_named_by_its_id_reaches_the_ads_manage_lane():
    from app.cm_intents import _infer_cm_task_type, ads_mode

    for order in (
        "Activate the Meta campaign 52593521115440 for Delta Cuisine",
        "Reactivate campaign 52593521115440 for Delta Cuisine",
        "Activate ad 52593521115440 for Delta Cuisine",
    ):
        assert _infer_cm_task_type(order) == "ads" and ads_mode(order) == "manage", order
    assert _infer_cm_task_type("plan a content campaign for Ceram") == "campaign"
    assert _infer_cm_task_type("Approve the ad campaign") == "approve"


def test_a_do_it_now_action_order_is_never_a_lesson():
    """PF-075. Live 2026-11-20 23:23/23:28: two of tonight's orders were stored as lessons."""
    from core.cm_learning import is_one_off_order

    assert is_one_off_order(
        "pause appel and electro move their budget to the two winners. And fix delta cuisine "
        "compaign to became performant and winners."
    )
    assert is_one_off_order(
        "I approve the drafts, publish them in the Meta Ads Manager, and keep an eye on them."
    )
    # Standing preferences still count.
    assert not is_one_off_order("pause any ad that costs over 2 EUR per conversation")
    assert not is_one_off_order("found an ad running at $100 per day, this is too much spend")
    assert not is_one_off_order("lower the music in reels, it covers the voice")
