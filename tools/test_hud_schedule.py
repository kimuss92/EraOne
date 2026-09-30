"""HUD scheduled-tasks view (app/hud/schedule_view.py): a truthful, READ-ONLY
mirror of core/task_scheduler. Every test gets its own task store and no
running scheduler, so nothing here can register or fire a real job."""

from datetime import datetime, timedelta

import pytest

from app.hud import schedule_view
from core import task_scheduler as ts
from core import task_store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_SCHEDULED_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("ERA_HIVE_DIR", str(tmp_path / "hive"))
    monkeypatch.setattr(ts, "_scheduler", None)
    monkeypatch.setattr(ts, "_reporter", lambda text, urgent: None)


def remind(name, when="tomorrow at 9", message="drink water"):
    return ts.create_task(name, when, kind="reminder", message=message)


def edit(name, **fields):
    data = task_store.load()
    task = next(t for t in data.values() if t["name"] == name)
    task.update(fields)
    task_store.save(data)


def test_lists_active_tasks_with_their_stored_next_run():
    remind("water")
    snap = schedule_view.snapshot()
    assert snap["ok"] and snap["running"] is False
    (row,) = snap["tasks"]
    assert row["name"] == "water" and row["kind"] == "reminder"
    assert row["summary"] == "remind you: drink water"
    assert (
        row["nextRun"] and row["nextRunLive"] is False
    )  # scheduler not running: fallback, flagged
    assert row["recurring"] is False and row["awaiting"] is None


def test_finished_tasks_are_left_out_and_paused_ones_have_no_next_run():
    remind("done")
    remind("paused")
    remind("active")
    edit("done", finished=True, enabled=False)
    edit("paused", enabled=False)
    rows = {r["name"]: r for r in schedule_view.snapshot()["tasks"]}
    assert set(rows) == {"paused", "active"}
    assert rows["paused"]["nextRun"] is None and rows["paused"]["enabled"] is False


def test_awaiting_approval_comes_first_then_soonest():
    remind("later", when="in 3 hours")
    remind("sooner", when="in 1 hour")
    remind("asks", when="in 5 hours")
    since = datetime.now().isoformat()
    edit(
        "asks",
        pending={"since": since, "reason": "Deleting files needs your OK.", "announced_at": None},
    )
    names = [r["name"] for r in schedule_view.snapshot()["tasks"]]
    assert names == ["asks", "sooner", "later"]
    row = schedule_view.snapshot()["tasks"][0]
    assert row["awaiting"]["reason"] == "Deleting files needs your OK."


def test_reading_never_expires_an_old_approval():
    # pending_approvals() would expire this (>24 h) as a side effect; the view must not.
    remind("stale")
    old = (datetime.now() - timedelta(days=3)).isoformat()
    edit("stale", pending={"since": old, "reason": "old ask", "announced_at": None})
    schedule_view.snapshot()
    task = next(t for t in task_store.load().values() if t["name"] == "stale")
    assert task["pending"] is not None and not task.get("history")


def test_the_live_scheduler_time_wins_over_the_stored_one(monkeypatch):
    remind("water")
    live = datetime.now().astimezone() + timedelta(minutes=7)
    monkeypatch.setattr(ts, "is_running", lambda: True)
    monkeypatch.setattr(ts, "live_next_run", lambda task_id: live)
    (row,) = schedule_view.snapshot()["tasks"]
    assert row["nextRunLive"] is True and row["nextRun"] == pytest.approx(live.timestamp())


def test_a_broken_store_degrades_instead_of_raising(monkeypatch):
    def boom(include_finished=False):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(ts, "list_tasks", boom)
    snap = schedule_view.snapshot()
    assert snap["ok"] is False and snap["tasks"] == [] and "unreadable" in snap["error"]
