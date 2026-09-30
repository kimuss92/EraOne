"""How a finished community-management result reaches the user.

Regression tests for 2026-09-19/20: an ads report finished 90 seconds after Era
went to standby and was never heard. Standby keeps the live session OPEN but
gates its audio, so "is there a session?" is true exactly when Era cannot speak.
Also pins that ads results are read aloud as plain sentences, and that several
reports finishing while Era was away no longer overwrite each other.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import era_dispatch as ed  # noqa: E402
from app.era_live import cm_flow  # noqa: E402
from app.era_live.cm_flow import CMFlowMixin, _speakable_ads_result  # noqa: E402


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, line):
        self.logs.append(line)


class _Era(CMFlowMixin):
    """The smallest thing CMFlowMixin's delivery methods can run on."""

    def __init__(self, *, connected=True, standby=False):
        self._loop = object() if connected else None
        self.session = object() if connected else None
        self._standby_active = standby
        self._pending_cm_announcement = None
        self._tg_reply_pending = True
        self.ui = _UI()
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


# ---- delivery -------------------------------------------------------------


def test_a_connected_awake_session_speaks_the_report_now():
    era = _Era(connected=True, standby=False)
    era._deliver_or_queue_cm_report("Two campaigns are on track.")
    assert era.spoken == ["Two campaigns are on track."]
    assert era._pending_cm_announcement is None


def test_a_report_finishing_in_standby_is_held_not_spoken_into_a_muted_session():
    """The bug: session is truthy in standby, so the old code called speak() and the
    reply was discarded. It must be HELD for the resume ack instead."""
    era = _Era(connected=True, standby=True)
    era._deliver_or_queue_cm_report("Two campaigns are on track.")
    assert era.spoken == []
    assert era._pending_cm_announcement == "Two campaigns are on track."


def test_a_report_with_no_session_is_still_held():
    era = _Era(connected=False, standby=False)
    era._deliver_or_queue_cm_report("Done.")
    assert era.spoken == [] and era._pending_cm_announcement == "Done."


def test_reports_finishing_while_away_accumulate_instead_of_overwriting():
    """Five ads runs finished while Era was in standby; only the last survived."""
    era = _Era(connected=True, standby=True)
    for line in ("First report.", "Second report.", "Third report."):
        era._deliver_or_queue_cm_report(line)
    for line in ("First report.", "Second report.", "Third report."):
        assert line in era._pending_cm_announcement
    assert era._pending_cm_announcement.index("First") < era._pending_cm_announcement.index("Third")


# ---- what gets said -------------------------------------------------------


def test_speakable_result_strips_markdown_paths_and_bullets():
    raw = (
        "**Ad Account Lock (re-stated):** `act_1698581228193609` — never default.\n\n"
        "- Report saved (`D:\\telechargements\\ceramequartz\\ads_reports\\r.md`).\n"
        "- **SAVANA**: ON TRACK, see [details](https://x.example/y).\n"
    )
    out = _speakable_ads_result(raw)
    for junk in ("**", "`", "D:\\", "https://", "](", "\n"):
        assert junk not in out, junk
    assert "SAVANA" in out and "ON TRACK" in out and "details" in out


def test_speakable_result_is_capped_at_a_sentence_boundary():
    text = ("This is a sentence about campaign performance. " * 60).strip()
    out = _speakable_ads_result(text, limit=200)
    assert len(out) <= 200 and out.endswith(".")


def test_speakable_result_handles_empty():
    assert _speakable_ads_result("") == "Done."
    assert _speakable_ads_result(None) == "Done."


# ---- _run_cm_flow: the ads lane ------------------------------------------


def _run_flow(monkeypatch, *, task_type, wrote_files, result_text):
    era = _Era()
    notes = []
    monkeypatch.setattr(cm_flow, "_tg_notify", lambda msg: notes.append(msg))
    monkeypatch.setattr(ed, "current_job", lambda: None)
    monkeypatch.setattr(
        ed,
        "run_task",
        lambda text, task_type, brand_key: {
            "ok": True,
            "brand": brand_key,
            "result": result_text,
            "wrote_files": wrote_files,
        },
    )
    payload = {
        "text": "how are my campaigns doing",
        "task_type": task_type,
        "brand_key": "ceram_quartz",
    }
    asyncio.run(era._run_cm_flow(payload, announce=False))
    return era, notes


def test_ads_result_is_read_aloud_short_and_without_the_wrote_nothing_caveat(monkeypatch):
    """An ads run's work happens on Meta, not on disk: a pause or a budget change
    legitimately writes 0 files. The caveat told Karim it 'may not have actually
    produced anything' about runs that had."""
    long_md = "**Report** " + ("Detail line about spend. " * 80)
    era, notes = _run_flow(monkeypatch, task_type="ads", wrote_files=0, result_text=long_md)
    assert len(era.spoken) == 1
    spoken = era.spoken[0]
    assert "caveat" not in spoken and "may not have actually produced" not in spoken
    assert "**" not in spoken and len(spoken) <= 650
    assert notes and "**" not in notes[0]  # the Telegram mirror gets the same short form


def test_other_lanes_keep_the_wrote_nothing_caveat(monkeypatch):
    era, _ = _run_flow(monkeypatch, task_type="weekly", wrote_files=0, result_text="Plan ready.")
    assert "may not have actually produced anything" in era.spoken[0]


def test_suite_never_touches_the_live_status_file():
    """run_task() writes and unlinks this file; pointed at ~/.era it rewrote the
    LIVE Era's job marker and flaked with WinError 32 (2026-09-26)."""
    assert ed._STATUS_PATH != Path.home() / ".era" / "era_status.json"


def test_clear_status_survives_a_locked_status_file(monkeypatch):
    """Windows refuses the unlink while another process holds the file open. A
    finished job must not raise over that bookkeeping (degrade, never raise)."""

    class _Locked:
        def unlink(self):
            raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(ed, "_STATUS_PATH", _Locked())
    ed._clear_status()  # must not raise
