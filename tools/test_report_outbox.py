"""Reports Era owes the owner must never be dropped: failed or successful, across a
session drop, a standby or a relaunch (core/report_outbox + app/era_live/report_pump)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.era_live import report_pump
from core import development, report_outbox


@pytest.fixture(autouse=True)
def outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(report_outbox, "PATH", tmp_path / "report_outbox.json")
    monkeypatch.setattr(development, "_notifier", None)
    return report_outbox


def _job(state="awaiting_approval", **extra):
    job = {
        "id": "a" * 16,
        "request": "Fix the glasses reconnecting. More detail follows here.",
        "state": state,
        "files": ["core/a.py", "tools/test_a.py"],
        "checks": {"passed": True},
        "plan": "Add hysteresis so a missed enumeration cannot flip the route. Second sentence.",
        "updated_at": time.time(),
    }
    job.update(extra)
    return job


def test_enqueue_is_idempotent_and_survives_reload(outbox):
    assert outbox.enqueue("k1", "first", "t") is True
    assert outbox.enqueue("k1", "again", "t") is False
    assert [i["text"] for i in outbox.pending()] == ["first"]


def test_a_report_stays_until_it_is_delivered(outbox):
    outbox.enqueue("k1", "first")
    item = outbox.pending()[0]
    outbox.mark_delivered(item["id"])
    assert outbox.pending() == []
    outbox.mark_undelivered(item["id"])
    assert len(outbox.pending()) == 1
    assert outbox.enqueue("k1", "first") is False  # still remembered: never repeated twice over


def test_success_report_says_what_to_approve_and_where():
    text = development.report_text(_job())
    assert "finished and verified" in text and "every check passed" in text
    assert "Fix the glasses reconnecting." in text and "More detail" not in text
    assert "2 files" in text and "approve or reject" in text and "Development tab" in text
    assert "core/a.py" not in text and "a" * 16 not in text  # plain speech: no paths or ids


def test_modules_and_paths_are_not_read_aloud():
    text = development.report_text(
        _job(plan="Add hysteresis to core.audio_devices and app/era_live/audio.py so it holds.")
    )
    assert "core.audio_devices" not in text and "audio.py" not in text
    assert "hysteresis to the code and the code so it holds" in text


def test_failure_report_says_why_and_that_live_code_is_unchanged():
    text = development.report_text(_job("failed", error="Claude authentication unavailable."))
    assert "failed" in text and "authentication unavailable" in text
    assert "live code was not changed" in text and "retry" in text


def test_finishing_a_job_queues_it_and_wakes_the_speaker(outbox):
    woke = []
    development.set_notifier(lambda: woke.append(1))
    development.report_finished(_job())
    development.report_finished(_job("failed", id="b" * 16, error="boom"))
    development.report_finished(_job("planning", id="c" * 16))  # not terminal: no report
    assert len(outbox.pending()) == 2 and len(woke) == 2


def test_reconcile_reports_jobs_that_ended_while_era_was_down(outbox, monkeypatch):
    old = _job("failed", id="d" * 16, updated_at=time.time() - 10 * 86400)
    monkeypatch.setattr(development, "list_jobs", lambda: [_job(), old])
    assert development.reconcile_reports() == 1  # the stale one is not dredged up
    assert development.reconcile_reports() == 0  # and nothing repeats


class _Turn:
    def __init__(self, idle=True):
        self.idle = idle

    def is_idle_for_renewal(self, tool_running=False):
        return self.idle


def _era(**extra):
    spoken, logs = [], []
    era = SimpleNamespace(
        _loop=SimpleNamespace(is_running=lambda: True),
        session=object(),
        _is_speaking=False,
        _standby_active=False,
        _shutdown_requested=False,
        _conn_generation=1,
        _go_away_event=SimpleNamespace(is_set=lambda: False),
        live_turn=_Turn(),
        _tool_is_running=lambda: False,
        speak=spoken.append,
        ui=SimpleNamespace(write_log=logs.append),
        spoken=spoken,
        logs=logs,
    )
    era.__dict__.update(extra)
    return era


def test_pump_speaks_a_queued_report_once_and_logs_it(outbox):
    outbox.enqueue("k1", "The task failed.")
    era = _era()
    report_pump._deliver_one(era)
    assert len(era.spoken) == 1 and "The task failed." in era.spoken[0]
    assert "Report: The task failed." in era.logs[0]
    assert outbox.pending() == []


def test_the_lead_in_does_not_double_sir_and_keeps_proper_nouns(outbox):
    outbox.enqueue("k1", "Sir, the development task failed: Fix Star Glasses on Windows.")
    era = _era()
    report_pump._deliver_one(era)
    spoken = era.spoken[0]
    assert ", sir. The development task failed" in spoken
    assert "Sir, the" not in spoken and "Fix Star Glasses on Windows" in spoken


def test_pump_holds_the_report_until_there_is_a_live_quiet_moment(outbox):
    outbox.enqueue("k1", "The task failed.")
    for blocked in (
        {"session": None},
        {"_is_speaking": True},
        {"live_turn": _Turn(idle=False)},
        {"_go_away_event": SimpleNamespace(is_set=lambda: True)},
    ):
        era = _era(**blocked)
        report_pump._deliver_one(era)
        assert era.spoken == [], blocked
        assert len(outbox.pending()) == 1, blocked


def test_standby_wakes_era_and_keeps_the_report(outbox):
    outbox.enqueue("k1", "The task failed.")
    woke = []
    era = _era(_standby_active=True, _request_completion_wake=lambda: woke.append(1))
    report_pump._deliver_one(era)
    assert woke == [1] and era.spoken == [] and len(outbox.pending()) == 1


def test_a_drop_right_after_speaking_puts_the_report_back(outbox):
    outbox.enqueue("k1", "The task failed.")
    era = _era()
    report_pump._deliver_one(era)
    assert outbox.pending() == []
    era._conn_generation = 2  # the connection dropped while it was being said
    report_pump._confirm_or_requeue(era)
    assert len(outbox.pending()) == 1


def test_reports_are_spoken_one_at_a_time(outbox):
    outbox.enqueue("k1", "one")
    outbox.enqueue("k2", "two")
    era = _era()
    report_pump._deliver_one(era)
    report_pump._deliver_one(era)
    assert len(era.spoken) == 1 and len(outbox.pending()) == 1
