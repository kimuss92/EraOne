"""The user's scheduled tasks: parser, engine safety gate, waivers, reporting, hive feed.

Every test gets its own task store and hive (conftest already points both away
from the real runtime/; these fixtures also keep tests from sharing state).
The APScheduler instance is only started in TestStartup, and shut down after.
"""

import asyncio
import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import scheduled_task as tool
from core import hive
from core import task_scheduler as ts
from core.schedule_parser import ScheduleParseError, parse_when

THURSDAY_3PM = datetime(2026, 9, 17, 15, 0)
DELETE_TEMP = {"action": "delete", "path": r"D:\scratch\old.tmp"}


def YES(task, sole):  # an authorize check that stands for "the user clearly approved"
    return True


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_SCHEDULED_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("ERA_HIVE_DIR", str(tmp_path / "hive"))
    monkeypatch.setattr(ts, "_scheduler", None)
    calls, reports = [], []

    def executor(tool_name, args, task):
        calls.append((tool_name, args))
        return {"success": True, "message": f"{tool_name} done"}

    monkeypatch.setattr(ts, "_executor", executor)
    monkeypatch.setattr(ts, "_reporter", lambda text, urgent: reports.append((text, urgent)))
    return types.SimpleNamespace(calls=calls, reports=reports)


def _delete_task(**kw):
    return ts.create_task(
        "clean temp", "every day at 3", tool="file_controller", args=DELETE_TEMP, **kw
    )


# ── parser ───────────────────────────────────────────────────────────────────
class TestParser:
    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("at 16:00", {"type": "date", "run_at": "2026-09-17T16:00:00"}),
            ("at 14:30", {"type": "date", "run_at": "2026-09-18T14:30:00"}),  # passed -> tomorrow
            ("tomorrow at 7pm", {"type": "date", "run_at": "2026-09-18T19:00:00"}),
            ("in 20 minutes", {"type": "date", "run_at": "2026-09-17T15:20:00"}),
            ("demain à 7h", {"type": "date", "run_at": "2026-09-18T07:00:00"}),
            ("friday at 5pm", {"type": "date", "run_at": "2026-09-18T17:00:00"}),
            ("every day at 8", {"type": "cron", "hour": 8, "minute": 0, "day_of_week": "*"}),
            (
                "every monday and friday at 9:30",
                {"hour": 9, "minute": 30, "day_of_week": "mon,fri"},
            ),
            ("every weekday at 8:15", {"day_of_week": "mon-fri", "hour": 8, "minute": 15}),
            ("chaque lundi à 9h30", {"day_of_week": "mon", "hour": 9, "minute": 30}),
            ("every month on the 1st at 9", {"type": "cron", "day": "1", "hour": 9}),
            ("every 2 hours", {"type": "interval", "seconds": 7200}),
            ("cron: 0 9 * * mon-fri", {"type": "cron", "day_of_week": "mon-fri", "hour": "9"}),
        ],
    )
    def test_phrases(self, phrase, expected):
        spec = parse_when(phrase, THURSDAY_3PM)
        assert {k: spec[k] for k in expected} == expected
        assert spec["description"]

    def test_five_word_sentence_is_not_cron(self):
        assert parse_when("every monday at 10 am", THURSDAY_3PM)["day_of_week"] == "mon"

    @pytest.mark.parametrize("phrase", ["later", "", "every 10 seconds", "2026-01-01 10:00"])
    def test_refuses_instead_of_guessing(self, phrase):
        with pytest.raises(ScheduleParseError):
            parse_when(phrase, THURSDAY_3PM)


# ── engine: running + safety gate ────────────────────────────────────────────
class TestSafetyGate:
    def test_low_risk_tool_runs_and_reports(self, isolated):
        task = ts.create_task(
            "weather", "every day at 8", tool="weather_report", args={"city": "Montreal"}
        )
        run = ts.fire(task["id"])
        assert run["status"] == "ok"
        assert isolated.calls == [("weather_report", {"city": "Montreal"})]
        assert "finished" in isolated.reports[-1][0]
        assert any(m["kind"] == "task_run" for m in hive.inbox("protocol_one"))

    def test_destructive_task_parks_for_approval_and_never_runs_by_itself(self, isolated):
        task = _delete_task()
        run = ts.fire(task["id"])
        assert run["status"] == "awaiting_confirmation"
        assert isolated.calls == []
        assert "Say 'yes' to run it, 'always'" in isolated.reports[-1][0]
        assert [i["category"] for i in hive.open_insights()] == ["needs_approval"]
        # Falling due again while still parked neither runs nor re-asks.
        ts.fire(task["id"])
        assert isolated.calls == [] and len(isolated.reports) == 1

    def test_approve_runs_exactly_once_and_clears_the_insight(self, isolated):
        task = _delete_task()
        ts.fire(task["id"])
        with pytest.raises(ts.TaskError):
            ts.approve("clean temp")  # no authorize check supplied: nothing is approved
        with pytest.raises(ts.TaskError):
            ts.approve("clean temp", authorize=lambda t, sole: False)
        assert isolated.calls == []
        out = ts.approve("clean temp", authorize=YES)
        assert out["run"]["status"] == "ok"
        assert len(isolated.calls) == 1
        assert hive.open_insights() == []
        with pytest.raises(ts.TaskError):
            ts.approve("clean temp", authorize=YES)  # nothing pending: cannot re-run it
        assert len(isolated.calls) == 1

    def test_approval_expires_after_24h_even_when_approve_is_called(self, isolated):
        task = _delete_task()
        ts.fire(task["id"])
        data = ts._load()
        data[task["id"]]["pending"]["since"] = (datetime.now() - timedelta(days=3)).isoformat()
        ts._save(data)
        with pytest.raises(ts.TaskError, match="expired"):
            ts.approve(authorize=YES)
        assert isolated.calls == [] and not ts.get_task("clean temp")["pending"]

    def test_task_edited_after_approval_claim_is_not_run(self, isolated):
        task = _delete_task()
        stale_fp = ts.fingerprint(task)
        ts.update_task("clean temp", args={"action": "delete", "path": r"D:\Documents"})
        run = ts.fire(task["id"], trigger="approved", approved=True, approved_fingerprint=stale_fp)
        assert run["status"] == "skipped" and isolated.calls == []

    def test_open_ended_tools_park_when_unattended_but_reads_do_not(self, isolated):
        agent = ts.create_task("research", "every day at 9", tool="agent_task", args={"goal": "x"})
        assert ts.fire(agent["id"])["status"] == "awaiting_confirmation"
        reader = ts.create_task(
            "disk", "every day at 9", tool="file_controller", args={"action": "disk_usage"}
        )
        assert ts.fire(reader["id"])["status"] == "ok"
        assert isolated.calls == [("file_controller", {"action": "disk_usage"})]

    def test_paused_task_cannot_be_run_now(self, isolated):
        _delete_task(waive=True)
        ts.update_task("clean temp", enabled=False)
        with pytest.raises(ts.TaskError, match="paused"):
            ts.run_now("clean temp")
        assert isolated.calls == []

    def test_skip_does_not_run(self, isolated):
        task = _delete_task()
        ts.fire(task["id"])
        assert ts.skip()["run"]["status"] == "skipped"
        assert isolated.calls == []

    def test_waiver_runs_without_asking(self, isolated):
        task = _delete_task(waive=True)
        assert ts.fire(task["id"])["status"] == "ok"
        assert len(isolated.calls) == 1

    def test_editing_the_action_drops_the_waiver(self, isolated):
        _delete_task(waive=True)
        edited = ts.update_task("clean temp", args={"action": "delete", "path": r"D:\Documents"})
        assert edited["waiver_dropped"] and not ts.waiver_valid(edited)
        assert ts.fire(edited["id"])["status"] == "awaiting_confirmation"
        assert isolated.calls == []

    @pytest.mark.parametrize(
        "change", [{"when": "every 2 hours"}, {"notify": "failures"}, {"name": "renamed"}]
    )
    def test_waiver_covers_timing_and_notify_but_not_the_name(self, change):
        _delete_task(waive=True)
        edited = ts.update_task("clean temp", **change)
        assert edited["waiver_dropped"] == ("name" not in change)
        assert ts.waiver_valid(edited) == ("name" in change)

    def test_blocked_action_refused_at_creation_and_unwaivable(self, monkeypatch):
        with pytest.raises(ts.TaskError, match="blocked"):
            ts.create_task(
                "wreck",
                "at 16:00",
                tool="file_controller",
                args={"action": "delete", "path": r"C:\Windows\System32"},
            )
        # If it became blocked after creation, the run is refused even with a waiver.
        task = _delete_task(waive=True)
        monkeypatch.setattr(
            ts,
            "assess",
            lambda t: {
                "risk_level": "BLOCKED",
                "requires_confirmation": False,
                "reason": "protected",
            },
        )
        assert ts.fire(task["id"])["status"] == "blocked"
        with pytest.raises(ts.TaskError):
            ts.set_waiver("clean temp", True)

    def test_preconfirmation_arguments_are_stripped(self):
        task = ts.create_task(
            "sneaky",
            "every day at 3",
            tool="file_controller",
            args={**DELETE_TEMP, "confirmed": True, "confirmation_id": "abcd"},
        )
        assert "confirmed" not in task["args"] and "confirmation_id" not in task["args"]

    @pytest.mark.parametrize(
        "name",
        ["scheduled_task", "confirm_pending_confirmation", "voice_lock", "manage_preference"],
    )
    def test_unschedulable_tools(self, name):
        with pytest.raises(ts.TaskError):
            ts.create_task("x", "every day at 3", tool=name, args={"action": "anything"})

    def test_protocol_launch_needs_the_user_to_have_named_it(self):
        with pytest.raises(ts.TaskError):
            ts.create_task("p1", "every day at 3", tool="protocol_one", args={"action": "launch"})
        task = ts.create_task(
            "p1",
            "every day at 3",
            tool="protocol_one",
            args={"action": "launch"},
            voice_authorized=True,
        )
        assert ts.voice_authorized(task)

    def test_protocol_authorization_does_not_survive_an_edit_to_launch(self):
        # Authorized while it was a harmless status check...
        ts.create_task(
            "p1 brief",
            "every day at 3",
            tool="protocol_one",
            args={"action": "status"},
            voice_authorized=True,
        )
        # ...then edited to launch with no fresh spoken command: refused.
        with pytest.raises(ts.TaskError):
            ts.update_task("p1 brief", args={"action": "launch"})
        edited = ts.update_task("p1 brief", args={"action": "launch"}, voice_authorized=True)
        assert ts.voice_authorized(edited)


# ── engine: chained tasks (core/task_chain.py) ───────────────────────────────
MORNING = [
    {"tool": "weather_report", "args": {"city": "Tunis"}},
    {"tool": "gmail_control", "args": {"action": "important"}},
    {"tool": "spotify_control", "args": {"action": "search", "value": "calm music"}},
]
LOW = {"risk_level": "LOW", "requires_confirmation": False, "reason": "fine"}
BLOCKED = {"risk_level": "BLOCKED", "requires_confirmation": False, "reason": "protected"}


def _morning(**kw):
    return ts.create_task("morning routine", "every weekday at 9", steps=MORNING, **kw)


def _failing_on(tool_name, calls):
    """An executor where one link of the chain fails and the others succeed."""

    def executor(tool, args, task):
        calls.append((tool, args))
        if tool == tool_name:
            return {"success": False, "message": "not signed in"}
        return {"success": True, "message": f"{tool} done"}

    return executor


class TestChains:
    def test_one_task_runs_every_step_in_order_and_reports_once(self, isolated):
        task = _morning()
        assert task["kind"] == "chain"
        run = ts.fire(task["id"])
        assert run["status"] == "ok"
        assert [t for t, _a in isolated.calls] == [s["tool"] for s in MORNING]
        assert isolated.calls[0][1] == {"city": "Tunis"}
        assert [(e["n"], e["status"]) for e in run["steps"]] == [(1, "ok"), (2, "ok"), (3, "ok")]
        assert len(isolated.reports) == 1  # one routine, one report

    def test_a_failed_link_stops_the_rest_and_the_run_is_not_called_ok(self, isolated, monkeypatch):
        monkeypatch.setattr(ts, "_executor", _failing_on("gmail_control", isolated.calls))
        run = ts.fire(_morning()["id"])
        assert run["status"] == "partial"  # NOT "ok": part of the routine did not happen
        assert [t for t, _a in isolated.calls] == ["weather_report", "gmail_control"]
        assert [e["status"] for e in run["steps"]] == ["ok", "failed", "skipped"]
        assert "step 2 of 3" in run["message"] and "not signed in" in run["message"]
        assert "only partly ran" in isolated.reports[-1][0]

    def test_continue_reports_the_failure_before_the_next_step_runs(self, monkeypatch):
        timeline = []

        def executor(tool, args, task):
            timeline.append(f"ran {tool}")
            return {"success": tool != "gmail_control", "message": "not signed in"}

        monkeypatch.setattr(ts, "_executor", executor)
        monkeypatch.setattr(ts, "_reporter", lambda text, urgent: timeline.append(f"said {text}"))
        run = ts.fire(_morning(on_error="continue")["id"])
        assert run["status"] == "partial"
        # The user hears about link 2 BEFORE link 3 acts — the whole point of the policy.
        assert timeline[0] == "ran weather_report"
        assert timeline[1] == "ran gmail_control"
        assert timeline[2].startswith("said ") and "step 2 of 3" in timeline[2]
        assert "continuing" in timeline[2]
        assert timeline[3] == "ran spotify_control"

    def test_one_risky_step_gates_the_whole_chain(self, isolated):
        task = ts.create_task(
            "mixed",
            "every day at 9",
            steps=[MORNING[0], {"tool": "file_controller", "args": DELETE_TEMP}],
        )
        run = ts.fire(task["id"])
        # Not even the harmless first link runs: the routine is approved as one unit.
        assert run["status"] == "awaiting_confirmation" and isolated.calls == []
        assert "step 2 of 2" in run["message"]
        assert ts.approve("mixed", authorize=YES)["run"]["status"] == "ok"
        assert [t for t, _a in isolated.calls] == ["weather_report", "file_controller"]

    def test_a_blocked_step_refuses_the_whole_chain_at_creation(self):
        wreck = {"action": "delete", "path": r"C:\Windows\System32"}
        with pytest.raises(ts.TaskError, match="blocked"):
            ts.create_task(
                "wreck",
                "every day at 9",
                steps=[MORNING[0], {"tool": "file_controller", "args": wreck}],
            )

    def test_a_step_blocked_since_the_approval_stops_the_chain_even_on_continue(
        self, isolated, monkeypatch
    ):
        task = _morning(on_error="continue")
        monkeypatch.setattr(
            ts, "assess_step", lambda tool, args: BLOCKED if tool == "gmail_control" else LOW
        )
        run = ts.fire(task["id"])
        # on_error="continue" never carries on past a BLOCK: the ground moved after the yes.
        assert run["status"] == "partial"
        assert [e["status"] for e in run["steps"]] == ["ok", "blocked", "skipped"]
        assert [t for t, _a in isolated.calls] == ["weather_report"]

    def test_editing_any_step_drops_the_waiver(self):
        assert ts.waiver_valid(_morning(waive=True))
        louder = {"tool": "spotify_control", "args": {"action": "search", "value": "loud"}}
        edited = ts.update_task("morning routine", steps=[*MORNING[:2], louder])
        assert edited["waiver_dropped"] and not ts.waiver_valid(edited)

    def test_switching_to_continue_drops_the_waiver(self):
        _morning(waive=True)
        edited = ts.update_task("morning routine", on_error="continue")
        assert edited["waiver_dropped"] and not ts.waiver_valid(edited)

    def test_every_step_is_validated_and_named_when_refused(self):
        with pytest.raises(ts.TaskError, match="Step 2"):
            ts.create_task(
                "sneaky", "every day at 9", steps=[MORNING[0], {"tool": "voice_lock", "args": {}}]
            )
        with pytest.raises(ts.TaskError, match="Step 2"):
            ts.create_task("headless", "every day at 9", steps=[MORNING[0], {"args": {}}])

    def test_preconfirmation_arguments_are_stripped_from_every_step(self):
        armed = {"tool": "file_controller", "args": {**DELETE_TEMP, "confirmed": True}}
        task = ts.create_task("sneaky", "every day at 9", steps=[MORNING[0], armed])
        assert all("confirmed" not in s["args"] for s in task["steps"])

    def test_a_chain_is_bounded_and_needs_more_than_one_step(self):
        with pytest.raises(ts.TaskError, match="at least two"):
            ts.create_task("solo", "every day at 9", steps=[MORNING[0]])
        with pytest.raises(ts.TaskError, match="at most"):
            ts.create_task("epic", "every day at 9", steps=MORNING * 3)

    def test_a_protocol_inside_a_chain_still_needs_the_spoken_command(self):
        launch = {"tool": "protocol_one", "args": {"action": "launch"}}
        with pytest.raises(ts.TaskError, match="Step 2"):
            ts.create_task("wake up", "every day at 9", steps=[MORNING[0], launch])
        task = ts.create_task(
            "wake up", "every day at 9", steps=[MORNING[0], launch], voice_authorized=True
        )
        assert ts.voice_authorized(task)
        # ...and it covers THAT chain only: re-ordering the routine revokes it, so the
        # edit is refused unless the user names the Protocol again for the new chain.
        with pytest.raises(ts.TaskError, match="Step 2"):
            ts.update_task("wake up", steps=[MORNING[1], launch])
        redone = ts.update_task("wake up", steps=[MORNING[1], launch], voice_authorized=True)
        assert ts.voice_authorized(redone)

    def test_a_crash_mid_chain_names_the_link_and_is_never_repeated(self, isolated, monkeypatch):
        task = ts.create_task("once", "in 10 minutes", steps=MORNING)

        def crash(tool, args, _task):
            isolated.calls.append((tool, args))
            if tool == "gmail_control":
                raise SystemExit  # Era dying mid-chain, before the result is saved
            return {"success": True, "message": "done"}

        monkeypatch.setattr(ts, "_executor", crash)
        with pytest.raises(SystemExit):
            ts.fire(task["id"])
        assert ts._load()[task["id"]]["running_step"] == 2
        isolated.calls.clear()
        try:
            ts.start()
            last = ts._load()[task["id"]]["last_run"]
            assert last["status"] == "failed" and "step 2 of 3" in last["message"]
            assert "not repeated" in last["message"] and isolated.calls == []
        finally:
            if ts._scheduler is not None:
                ts._scheduler.shutdown(wait=False)

    def test_the_decisive_argument_is_never_truncated_out_of_the_prompt(self, isolated):
        """The approval prompt is built from describe_step, and dict order is whatever
        the model emitted. Padding a delete with filler args must not push `path` out
        of the four shown, or the user approves a deletion without hearing its target."""
        padded = {
            "tool": "file_controller",
            "args": {"action": "delete", "a": 1, "b": 2, "c": 3, "d": 4, "path": r"D:\Documents"},
        }
        shown = ts.describe_step(padded)
        assert r"path=D:\Documents" in shown and "+1 more" in shown
        task = ts.create_task("padded", "every day at 9", steps=[MORNING[0], padded])
        ts.fire(task["id"])
        assert r"path=D:\Documents" in isolated.reports[-1][0]  # the parked-run prompt

    def test_a_reminder_cannot_smuggle_unvalidated_steps(self):
        """validate_action returns early for a reminder, so steps sent beside a message
        are never checked against UNSCHEDULABLE_TOOLS or the Protocol gate. They must
        not be stored at all — inert today is one missing guard from executable."""
        task = ts.create_task(
            "nudge",
            "in 10 minutes",
            kind="reminder",
            message="stand up",
            steps=[{"tool": "voice_lock", "args": {"action": "off"}}, MORNING[0]],
        )
        assert task["steps"] == [] and ts.steps_of(task) == []

    def test_changing_the_failure_policy_re_asks_a_parked_chain(self, isolated):
        """on_error is outside the action fingerprint, but a yes for "stop at the first
        failure" is not a yes for "run all of them regardless"."""
        task = ts.create_task(
            "mixed",
            "every day at 9",
            steps=[MORNING[0], {"tool": "file_controller", "args": DELETE_TEMP}],
        )
        assert ts.fire(task["id"])["status"] == "awaiting_confirmation"
        assert ts.get_task("mixed")["pending"]
        ts.update_task("mixed", on_error="continue")
        assert not ts.get_task("mixed")["pending"]  # the old yes cannot land on the new policy
        with pytest.raises(ts.TaskError):
            ts.approve("mixed", authorize=YES)
        assert isolated.calls == []

    def test_a_scheduled_protocol_launch_parks_even_though_it_was_voice_authorized(self, isolated):
        """classify_action rates a Protocol launch as needing no confirmation because the
        user's spoken command IS the confirmation. That holds in a live turn; unattended,
        weeks later, the only thing left is a stored fingerprint — so it gets the same
        unattended review as agent_task and self_improve. The user can still waive it."""
        task = ts.create_task(
            "nightly",
            "every day at 3",
            tool="protocol_one",
            args={"action": "launch"},
            voice_authorized=["protocol_one"],
        )
        assert ts.fire(task["id"])["status"] == "awaiting_confirmation"
        assert isolated.calls == []
        # A read-only Protocol call is not open-ended and still runs on its own.
        status = ts.create_task(
            "brief", "every day at 8", tool="protocol_one", args={"action": "status"}
        )
        assert ts.fire(status["id"])["status"] == "ok"

    def test_single_task_results_are_unchanged_by_the_chain_runner(self, isolated):
        task = ts.create_task("w", "every day at 8", tool="weather_report", args={"city": "X"})
        run = ts.fire(task["id"])
        assert run["status"] == "ok" and run["message"] == "weather_report done"
        assert "steps" not in run  # a single call is never dressed up as a chain


# ── engine: reporting, results, persistence ──────────────────────────────────
class TestReporting:
    def test_failure_opens_an_insight_and_a_later_success_resolves_it(self, isolated, monkeypatch):
        task = ts.create_task(
            "weather", "every day at 8", tool="weather_report", args={"city": "X"}
        )
        monkeypatch.setattr(
            ts, "_executor", lambda *a: {"success": False, "message": "city unknown"}
        )
        assert ts.fire(task["id"])["status"] == "failed"
        assert hive.open_insights()[0]["category"] == "scheduled_task"
        assert "failed: city unknown" in isolated.reports[-1][0]
        monkeypatch.setattr(ts, "_executor", lambda *a: {"success": True, "message": "sunny"})
        assert ts.fire(task["id"])["status"] == "ok"
        assert hive.open_insights() == []

    def test_executor_crash_is_a_recorded_failure(self, monkeypatch):
        task = ts.create_task("weather", "every day at 8", tool="weather_report", args={})
        monkeypatch.setattr(ts, "_executor", lambda *a: 1 / 0)
        assert ts.fire(task["id"])["status"] == "failed"

    def test_missing_runtime_is_a_failure_not_a_silent_skip(self, monkeypatch):
        task = ts.create_task("weather", "every day at 8", tool="weather_report", args={})
        monkeypatch.setattr(ts, "_executor", None)
        run = ts.fire(task["id"])
        assert run["status"] == "failed" and "not ready" in run["message"]

    def test_notify_failures_only_suppresses_successes(self, isolated):
        task = ts.create_task(
            "w", "every day at 8", tool="weather_report", args={}, notify="failures"
        )
        ts.fire(task["id"])
        assert isolated.reports == []

    def test_approval_requests_are_announced_even_when_silent(self, isolated):
        task = _delete_task(notify="silent")
        ts.fire(task["id"])
        assert len(isolated.reports) == 1

    def test_manual_run_is_not_double_reported(self, isolated):
        ts.create_task("w", "every day at 8", tool="weather_report", args={})
        assert ts.run_now("w")["run"]["status"] == "ok"
        assert isolated.reports == []

    def test_reminder_one_shot_finishes(self, isolated):
        task = ts.create_task("stretch", "in 10 minutes", kind="reminder", message="Stand up")
        ts.fire(task["id"])
        assert isolated.reports[-1][0] == "Reminder: Stand up"
        assert ts.list_tasks() == [] and ts.list_tasks(include_finished=True)[0]["finished"]

    def test_tasks_persist_and_cancel_removes(self):
        ts.create_task("w", "every day at 8", tool="weather_report", args={})
        assert [t["name"] for t in ts.list_tasks()] == ["w"]  # re-read from disk
        ts.cancel_task("w")
        assert ts.list_tasks(include_finished=True) == []

    def test_duplicate_and_ambiguous_names(self):
        ts.create_task("weather am", "every day at 8", tool="weather_report", args={})
        ts.create_task("weather pm", "every day at 18", tool="weather_report", args={})
        with pytest.raises(ts.TaskError, match="already"):
            ts.create_task("Weather AM", "every day at 9", tool="weather_report", args={})
        with pytest.raises(ts.TaskError, match="several"):
            ts.get_task("weather")

    def test_hive_status_and_briefing_carry_the_scheduler(self):
        task = _delete_task()
        ts.fire(task["id"])
        assert "1 awaiting approval" in hive.get_status()["scheduler"]["detail"]
        assert "Scheduler" in hive.briefing()
        assert "clean temp" in ts.briefing_line()


class TestStartup:
    @pytest.fixture
    def running(self, monkeypatch):
        yield
        if ts._scheduler is not None:
            ts._scheduler.shutdown(wait=False)

    def test_registers_jobs_and_marks_long_missed_one_shots(self, running, isolated):
        ts.start()
        live = ts.create_task("w", "every day at 8", tool="weather_report", args={})
        assert live["next_run"] and live["next_run"].endswith("08:00")
        stale = ts.create_task("old", "in 10 minutes", kind="reminder", message="x")
        data = ts._load()
        data[stale["id"]]["schedule"]["run_at"] = (datetime.now() - timedelta(hours=10)).isoformat()
        ts._save(data)
        ts._scheduler.shutdown(wait=False)
        ts._scheduler = None
        ts.start()
        assert ts._load()[stale["id"]]["last_run"]["status"] == "missed"
        assert ts._scheduler.get_job(f"user:{live['id']}") is not None

    def test_run_interrupted_by_a_crash_is_not_repeated(self, running, isolated, monkeypatch):
        task = ts.create_task("send once", "in 10 minutes", tool="weather_report", args={})

        def crash(*a):
            raise SystemExit  # simulates Era dying mid-run, before the result is saved

        monkeypatch.setattr(ts, "_executor", crash)
        with pytest.raises(SystemExit):
            ts.fire(task["id"])
        data = ts._load()
        data[task["id"]]["schedule"]["run_at"] = (datetime.now() - timedelta(hours=1)).isoformat()
        ts._save(data)
        monkeypatch.setattr(
            ts, "_executor", lambda *a: isolated.calls.append(a) or {"success": True}
        )
        ts.start()
        record = ts._load()[task["id"]]
        assert (
            record["last_run"]["status"] == "failed"
            and "not repeated" in record["last_run"]["message"]
        )
        assert ts._scheduler.get_job(f"user:{task['id']}") is None and isolated.calls == []

    def test_parked_one_shot_survives_restart(self, running):
        task = ts.create_task("once", "in 10 minutes", tool="file_controller", args=DELETE_TEMP)
        ts.fire(task["id"])
        data = ts._load()
        data[task["id"]]["schedule"]["run_at"] = (datetime.now() - timedelta(hours=10)).isoformat()
        ts._save(data)
        ts.start()
        assert ts._load()[task["id"]]["pending"]  # still waiting, not "missed"


# ── engine: conditional tasks (until / if) ───────────────────────────────────
DL_CHECK = {"tool": "file_controller", "args": {"action": "last_download"}}


def _download_executor(states, calls):
    """A check that reports each string in `states` in turn (last one sticks), and a
    weather_report action that always succeeds. Lets a test walk a poll to completion."""
    box = {"i": 0}

    def executor(tool, args, task):
        calls.append((tool, args))
        if tool == "file_controller":
            msg = states[min(box["i"], len(states) - 1)]
            box["i"] += 1
            return {"success": True, "message": msg}
        return {"success": True, "message": f"{tool} done"}

    return executor


class TestConditionEvaluation:
    """The pure comparison, testable with no runtime (core/task_store.condition_met)."""

    @pytest.mark.parametrize(
        ("op", "value", "out", "prev", "expected"),
        [
            ("truthy", "", {"success": True}, None, True),
            ("truthy", "", {"success": False}, None, False),
            ("falsy", "", {"success": False}, None, True),
            ("contains", "complete", {"message": "Download complete"}, None, True),
            ("contains", "complete", {"message": "downloading 40%"}, None, False),
            ("not_contains", "downloading", {"message": "done"}, None, True),
            ("not_contains", "downloading", {"message": "still downloading"}, None, False),
            ("equals", "ready", {"message": "READY"}, None, True),
            ("equals", "ready", {"message": "not ready"}, None, False),
            ("changed", "", {"message": "b"}, "a", True),
            ("changed", "", {"message": "a"}, "a", False),
            ("changed", "", {"message": "a"}, None, False),  # no baseline yet
        ],
    )
    def test_operators(self, op, value, out, prev, expected):
        cond = {"operator": op, "value": value}
        assert ts.condition_met(out, cond, prev) is expected


class TestConditions:
    def _until(self, states, isolated, **kw):
        isolated.calls.clear()
        return ts.create_task(
            "await download",
            "every 5 minutes",
            kind="reminder",
            message="download is done",
            condition={**{"check": DL_CHECK, "operator": "contains", "value": "complete"}, **kw},
        )

    def test_unmet_poll_waits_silently_and_writes_no_history(self, isolated, monkeypatch):
        monkeypatch.setattr(
            ts, "_executor", _download_executor(["downloading 40%"], isolated.calls)
        )
        task = self._until([], isolated, on_met="stop")
        run = ts.fire(task["id"])
        assert run is None  # nothing happened: no run record
        # The check ran; the action (a reminder) did not; nothing was reported.
        assert isolated.calls == [("file_controller", {"action": "last_download"})]
        assert isolated.reports == []
        stored = ts.get_task(task["id"])
        assert stored["history"] == [] and stored["last_run"] is None
        assert stored["condition_state"]["met"] is False

    def test_until_runs_the_followup_once_met_then_retires(self, isolated, monkeypatch):
        monkeypatch.setattr(
            ts,
            "_executor",
            _download_executor(["downloading", "download complete"], isolated.calls),
        )
        task = self._until([], isolated, on_met="stop")
        assert ts.fire(task["id"]) is None  # first poll: not met
        run = ts.fire(task["id"])  # second poll: met
        assert run["status"] == "ok" and run["message"] == "download is done"
        # It retired itself: met + follow-up ran = the "until" is over.
        stored = ts.get_task(task["id"])
        assert stored["finished"] and not stored["enabled"]
        assert any("is complete" in r[0] for r in isolated.reports)
        # A later fire does nothing — it is finished and disabled.
        assert ts.fire(task["id"]) is None

    def test_continue_keeps_polling_and_acts_each_time_met(self, isolated, monkeypatch):
        # on_met defaults to continue: it acts whenever met, never retiring.
        monkeypatch.setattr(
            ts,
            "_executor",
            _download_executor(["download complete"], isolated.calls),
        )
        task = ts.create_task(
            "daily if ready",
            "every 5 minutes",
            tool="weather_report",
            args={"city": "X"},
            condition={"check": DL_CHECK, "operator": "contains", "value": "complete"},
        )
        assert ts.fire(task["id"])["status"] == "ok"
        assert ("weather_report", {"city": "X"}) in isolated.calls
        assert not ts.get_task(task["id"])["finished"]  # still scheduled
        assert ts.fire(task["id"])["status"] == "ok"  # runs again next time it holds

    def test_condition_gates_the_approval_until_it_is_met(self, isolated, monkeypatch):
        """The whole point: a risky follow-up never asks for approval until the
        condition is actually met, so a 5-minute poll is not a 5-minute nag."""
        monkeypatch.setattr(
            ts,
            "_executor",
            _download_executor(["downloading", "download complete"], isolated.calls),
        )
        task = ts.create_task(
            "clean when done",
            "every 5 minutes",
            tool="file_controller",
            args=DELETE_TEMP,
            condition={"check": DL_CHECK, "operator": "contains", "value": "complete"},
        )
        assert ts.fire(task["id"]) is None  # not met: no park, no ask
        assert not ts.get_task(task["id"])["pending"] and isolated.reports == []
        run = ts.fire(task["id"])  # met: NOW it parks for approval
        assert run["status"] == "awaiting_confirmation"
        assert ts.get_task(task["id"])["pending"]

    def test_a_date_task_with_an_unmet_condition_finishes_rather_than_stranding(
        self, isolated, monkeypatch
    ):
        monkeypatch.setattr(ts, "_executor", _download_executor(["downloading"], isolated.calls))
        task = ts.create_task(
            "check once",
            "in 10 minutes",
            kind="reminder",
            message="done",
            condition={"check": DL_CHECK, "operator": "contains", "value": "complete"},
        )
        run = ts.fire(task["id"])
        assert run["status"] == "skipped" and ts.get_task(task["id"])["finished"]

    def test_the_check_is_re_gated_before_it_runs(self, isolated, monkeypatch):
        task = self._until([], isolated, on_met="stop")
        # The check has become risky since creation: it does not run, the task is blocked.
        monkeypatch.setattr(ts, "assess_step", lambda tool, args: BLOCKED)
        run = ts.fire(task["id"])
        assert run["status"] == "blocked" and isolated.calls == []

    def test_editing_the_condition_drops_the_waiver_and_re_asks(self, isolated):
        task = ts.create_task(
            "clean when done",
            "every 5 minutes",
            tool="file_controller",
            args=DELETE_TEMP,
            condition={"check": DL_CHECK, "operator": "contains", "value": "complete"},
            waive=True,
        )
        assert ts.waiver_valid(task)
        edited = ts.update_task("clean when done", condition={**task["condition"], "value": "done"})
        assert edited["waiver_dropped"] and not ts.waiver_valid(edited)

    def test_a_read_only_check_is_required(self):
        with pytest.raises(ts.TaskError, match="read-only"):
            ts.create_task(
                "bad check",
                "every 5 minutes",
                kind="reminder",
                message="x",
                condition={"check": {"tool": "file_controller", "args": DELETE_TEMP}},
            )

    def test_the_tool_layer_creates_and_lists_a_conditional_task(self, isolated):
        out = tool.scheduled_task(
            {
                "action": "create",
                "name": "await dl",
                "when": "every 5 minutes",
                "kind": "reminder",
                "message": "the download finished",
                "condition": '{"check": {"tool": "file_controller", "args": '
                '{"action": "last_download"}}, "operator": "contains", "value": '
                '"complete", "on_met": "stop"}',
            }
        )
        assert out["success"] and "condition is met" in out["message"]
        listed = tool.scheduled_task({"action": "list"})
        assert "only when" in listed["message"]


# ── engine: per-step run_if conditions ───────────────────────────────────────
def _check_executor(check_message, calls):
    """A check tool that reports `check_message`; every other tool just succeeds."""

    def executor(tool, args, task):
        calls.append((tool, args))
        if tool == "file_controller" and args.get("action") == "last_download":
            return {"success": True, "message": check_message}
        return {"success": True, "message": f"{tool} done"}

    return executor


def _if_ready(tool, args, value="complete"):
    """A chain step gated on the download check containing `value`."""
    return {
        "tool": tool,
        "args": args,
        "run_if": {"check": DL_CHECK, "operator": "contains", "value": value},
    }


class TestStepConditions:
    def test_a_met_step_runs_like_any_other(self, isolated, monkeypatch):
        monkeypatch.setattr(ts, "_executor", _check_executor("download complete", isolated.calls))
        step = _if_ready("spotify_control", {"action": "search", "value": "party"})
        task = ts.create_task("if ready play", "every day at 9", steps=[MORNING[0], step])
        run = ts.fire(task["id"])
        assert run["status"] == "ok"
        assert ("spotify_control", {"action": "search", "value": "party"}) in isolated.calls
        assert [e["status"] for e in run["steps"]] == ["ok", "ok"]

    def test_an_unmet_step_is_skipped_and_the_chain_goes_on(self, isolated, monkeypatch):
        monkeypatch.setattr(ts, "_executor", _check_executor("downloading 40%", isolated.calls))
        step = _if_ready("spotify_control", {"action": "search", "value": "party"})
        task = ts.create_task(
            "if ready play", "every day at 9", steps=[MORNING[0], step, MORNING[1]]
        )
        run = ts.fire(task["id"])
        # Weather and Gmail ran; the conditional music step did not.
        assert run["status"] == "ok"
        assert [e["status"] for e in run["steps"]] == ["ok", "skipped", "ok"]
        assert run["steps"][1].get("skipped_by") == "condition"
        assert "1 skipped by condition" in run["message"]
        ran = [t for t, _a in isolated.calls]
        assert "spotify_control" not in ran and ran.count("weather_report") == 1

    def test_a_conditional_skip_is_not_a_failure_and_never_stops_the_chain(
        self, isolated, monkeypatch
    ):
        # on_error defaults to "stop", yet a NOT-MET step must not stop the rest:
        # a skip is deliberate, not a failure.
        monkeypatch.setattr(ts, "_executor", _check_executor("still downloading", isolated.calls))
        conditional_first = _if_ready("weather_report", {"city": "X"})
        task = ts.create_task("guarded", "every day at 9", steps=[conditional_first, MORNING[1]])
        run = ts.fire(task["id"])
        assert run["status"] == "ok"
        assert [e["status"] for e in run["steps"]] == ["skipped", "ok"]
        assert ("gmail_control", {"action": "important"}) in isolated.calls

    def test_a_risky_conditional_step_still_gates_the_whole_chain(self, isolated):
        """run_if decides at run time whether a step acts; it never relaxes the
        up-front approval. A chain with a risky conditional step still parks."""
        step = _if_ready("file_controller", DELETE_TEMP)
        task = ts.create_task("clean if ready", "every day at 9", steps=[MORNING[0], step])
        run = ts.fire(task["id"])
        assert run["status"] == "awaiting_confirmation"
        assert isolated.calls == []  # nothing ran — not even the check
        # Once approved, the conditional step is evaluated in place.
        assert ts.approve("clean if ready", authorize=YES)["run"]["status"] in ("ok", "partial")

    def test_a_run_if_check_must_be_read_only(self):
        bad = {
            "tool": "weather_report",
            "args": {},
            "run_if": {"check": {"tool": "file_controller", "args": DELETE_TEMP}},
        }
        with pytest.raises(ts.TaskError, match="Step 2 run_if"):
            ts.create_task("bad", "every day at 9", steps=[MORNING[0], bad])

    def test_changed_is_refused_for_a_step_because_it_has_no_baseline(self):
        step = {
            "tool": "weather_report",
            "args": {},
            "run_if": {"check": DL_CHECK, "operator": "changed"},
        }
        with pytest.raises(ts.TaskError, match="baseline"):
            ts.create_task("nope", "every day at 9", steps=[MORNING[0], step])

    def test_the_listing_shows_which_step_is_conditional(self, isolated):
        step = _if_ready("spotify_control", {"action": "search", "value": "calm"})
        ts.create_task("if ready play", "every day at 9", steps=[MORNING[0], step])
        out = tool.scheduled_task({"action": "steps", "task": "if ready play"})
        assert "only when" in out["message"] and "last_download" in out["message"]

    def test_editing_a_step_run_if_drops_the_waiver(self, isolated):
        step = _if_ready("file_controller", DELETE_TEMP)
        task = ts.create_task(
            "clean if ready", "every day at 9", steps=[MORNING[0], step], waive=True
        )
        assert ts.waiver_valid(task)
        looser = _if_ready("file_controller", DELETE_TEMP, value="done")
        edited = ts.update_task("clean if ready", steps=[MORNING[0], looser])
        assert edited["waiver_dropped"] and not ts.waiver_valid(edited)

    def test_a_check_blocked_since_creation_skips_the_step_safely(self, isolated, monkeypatch):
        step = _if_ready("spotify_control", {"action": "search", "value": "x"})
        task = ts.create_task("guarded", "every day at 9", steps=[MORNING[0], step])
        # The check tool has become blocked; the action tools are still fine.
        monkeypatch.setattr(
            ts, "assess_step", lambda t, a: BLOCKED if t == "file_controller" else LOW
        )
        run = ts.fire(task["id"])
        assert run["status"] == "ok"  # the conditional step is skipped, not run
        assert [e["status"] for e in run["steps"]] == ["ok", "skipped"]
        assert "spotify_control" not in [t for t, _a in isolated.calls]


# ── routing + classification ─────────────────────────────────────────────────
class TestRouting:
    @pytest.mark.parametrize(
        "action", ["cancel", "list", "update", "schedule", "run_now", "approve"]
    )
    def test_registry_never_reroutes(self, action):
        from core.action_registry import canonicalize_tool_call

        assert canonicalize_tool_call("scheduled_task", {"action": action})[0] == "scheduled_task"

    def test_classification(self):
        from core.safety_guard import classify_action

        assert classify_action("scheduled_task", {"action": "create"})["risk_level"] == "LOW"
        waive = classify_action("scheduled_task", {"action": "waive_confirmation"})
        assert waive["risk_level"] == "MEDIUM" and not waive["requires_confirmation"]


class TestNameMatching:
    """Spoken names ("morning routine") must find snake/Camel-case tasks."""

    DATA = {
        "a1": {"id": "a1", "name": "morning_routine"},
        "b2": {"id": "b2", "name": "research_innovation"},
        "c3": {"id": "c3", "name": "CheckBrandAds"},
        "d4": {"id": "d4", "name": "netflix_check"},
        "e5": {"id": "e5", "name": "NetflixNextLimitation"},
    }

    @pytest.mark.parametrize(
        "ref,name",
        [
            ("morning routine", "morning_routine"),
            ("ResearchAndInnovation", "research_innovation"),
            ("research innovation", "research_innovation"),
            ("check brand ads", "CheckBrandAds"),
        ],
    )
    def test_spoken_forms_resolve(self, ref, name):
        from core import task_store

        assert task_store.find(self.DATA, ref)["name"] == name

    def test_ambiguity_is_asked_not_guessed(self):
        from core import task_store

        with pytest.raises(task_store.TaskError, match="several tasks"):
            task_store.find(self.DATA, "netflix")

    def test_no_match_lists_the_real_tasks(self):
        from core import task_store

        with pytest.raises(task_store.TaskError, match="Existing tasks: 'morning_routine'"):
            task_store.find(self.DATA, "failed_task_log")
