"""Regression tests for the 2026-09-18 morning_routine failure.

The scheduled chain hit `Gmail request failed (SSLError)` because a live turn was
reading mail on the same shared httplib2 connection; the chain then stopped before
Spotify; weather_report had nothing to say aloud; and runs Era was off for vanished.
"""

import ssl
import sys
import threading
import types
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import weather_report as wr
from core import gmail_auth as ga
from core import gmail_service as gs
from core import task_catchup as tc
from core import task_scheduler as ts
from core.action_verifiers import verify_weather


# ── Gmail: one client per thread + one retry on a connection fault ──────────
class TestGmailThreadSafety:
    @pytest.fixture
    def fake_build(self, monkeypatch):
        import googleapiclient.discovery as disc

        monkeypatch.setattr(ga, "_load_credentials", lambda: types.SimpleNamespace(token="t"))
        monkeypatch.setattr(disc, "build", lambda *a, **k: object())
        ga._invalidate_service()
        ga.drop_thread_service()
        yield
        ga._invalidate_service()

    def test_each_thread_gets_its_own_client(self, fake_build):
        main = ga.get_service()
        assert ga.get_service() is main  # cached within a thread
        other = []
        t = threading.Thread(target=lambda: other.append(ga.get_service()))
        t.start()
        t.join()
        assert other[0] is not None and other[0] is not main

    def test_invalidate_retires_every_threads_client(self, fake_build):
        before = ga.get_service()
        ga._invalidate_service()
        assert ga.get_service() is not before


class _Req:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def execute(self):
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class TestGmailReadRetry:
    def _wire(self, monkeypatch, outcomes):
        dropped = []
        monkeypatch.setattr(gs, "_require_service", lambda: object())
        monkeypatch.setattr(gs, "drop_thread_service", lambda: dropped.append(1))
        req = _Req(outcomes)
        return (lambda svc: req), dropped

    def test_ssl_error_is_retried_once_on_a_fresh_client(self, monkeypatch):
        build, dropped = self._wire(monkeypatch, [ssl.SSLError("bad record mac"), {"ok": 1}])
        assert gs._execute_read(build) == {"ok": 1}
        assert dropped == [1]

    def test_second_fault_is_not_retried_again(self, monkeypatch):
        build, _ = self._wire(monkeypatch, [ConnectionResetError(), TimeoutError(), {"x": 1}])
        with pytest.raises(TimeoutError):
            gs._execute_read(build)

    def test_an_http_answer_is_not_retried(self, monkeypatch):
        http_error = OSError("403")
        http_error.resp = types.SimpleNamespace(status=403)
        build, dropped = self._wire(monkeypatch, [http_error, {"x": 1}])
        with pytest.raises(OSError):
            gs._execute_read(build)
        assert dropped == []

    def test_list_messages_survives_one_ssl_error(self, monkeypatch):
        calls = []

        class Svc:
            def users(self):
                return self

            def messages(self):
                return self

            def list(self, **kw):
                calls.append("list")
                return _Req([ssl.SSLError("x")] if calls.count("list") == 1 else [{}])

        monkeypatch.setattr(gs, "_require_service", lambda: Svc())
        monkeypatch.setattr(gs, "drop_thread_service", lambda: None)
        result = gs.list_messages()
        assert result["ok"] is True and calls == ["list", "list"]


# ── weather_report returns a sentence to read aloud ──────────────────────────
FORECAST = {
    "current": {"temperature_2m": 28.6, "weather_code": 2},
    "daily": {
        "weather_code": [2, 80],
        "temperature_2m_max": [33.2, 30.4],
        "temperature_2m_min": [22.1, 23.0],
        "precipitation_probability_max": [30, 38],
    },
}


class TestWeather:
    def test_today_is_spoken_text(self):
        text = wr.format_forecast("Tunis", "today", FORECAST)
        assert text == (
            "Tunis today: 29 degrees and partly cloudy right now, high 33 degrees, "
            "low 22 degrees, 30% chance of rain, sir."
        )

    @pytest.mark.parametrize("when", ["tomorrow", "Demain matin"])
    def test_tomorrow(self, when):
        text = wr.format_forecast("Tunis", when, FORECAST)
        assert text.startswith("Tunis tomorrow: light showers, high 30 degrees")

    def test_action_returns_forecast(self, monkeypatch):
        monkeypatch.setattr(wr, "fetch_forecast", lambda city, when: f"{city} today: sunny")
        assert wr.weather_action({"city": " Tunis "}) == "Tunis today: sunny"

    def test_offline_falls_back_to_browser_and_still_verifies(self, monkeypatch):
        def offline(city, when):
            raise ConnectionError

        opened = []
        monkeypatch.setattr(wr, "fetch_forecast", offline)
        monkeypatch.setattr(wr.webbrowser, "open", lambda url: opened.append(url) or True)
        msg = wr.weather_action({"city": "Tunis"})
        assert opened and "browser" in msg
        from core.tool_result import ToolResult

        result = verify_weather(ToolResult(success=True, message=msg), {"city": "Tunis"})
        assert result.success is True

    def test_missing_city(self):
        assert "missing" in wr.weather_action({"city": ""})


# ── catch-up for recurring runs Era was not running for ──────────────────────
WEEKDAY_9 = {"type": "cron", "minute": "0", "hour": "9", "day": "*", "day_of_week": "*"}
FRI = datetime(2026, 9, 18, 10, 59)


def _task(**kw):
    base = {"enabled": True, "schedule": dict(WEEKDAY_9), "catch_up_until": "11:00"}
    return {**base, **kw}


class TestCatchUpDecision:
    @pytest.mark.parametrize(
        ("raw", "norm"),
        [("11", "11:00"), ("11:00", "11:00"), ("9h30", "09:30"), ("11h", "11:00"), ("off", "")],
    )
    def test_normalize(self, raw, norm):
        assert tc.normalize_until(raw) == norm

    @pytest.mark.parametrize("bad", ["25:00", "noon", "11:75"])
    def test_normalize_rejects(self, bad):
        with pytest.raises(ts.TaskError):
            tc.normalize_until(bad)

    def test_launch_inside_window_catches_up(self):
        assert tc.startup_decision(_task(), FRI) == "catch_up"

    def test_launch_after_window_is_missed(self):
        assert tc.startup_decision(_task(), FRI.replace(hour=14)) == "missed"

    def test_before_first_fire_does_nothing(self):
        assert tc.startup_decision(_task(), FRI.replace(hour=8)) is None

    def test_already_ran_today_does_nothing(self):
        task = _task(history=[{"at": "2026-09-18T09:00:01", "status": "ok"}])
        assert tc.startup_decision(task, FRI) is None

    def test_yesterdays_run_does_not_count(self):
        task = _task(history=[{"at": "2026-09-17T09:00:01", "status": "ok"}])
        assert tc.startup_decision(task, FRI) == "catch_up"

    def test_opt_in_only(self):
        assert tc.startup_decision(_task(catch_up_until=""), FRI) is None

    def test_not_a_scheduled_day(self):
        task = _task(schedule={**WEEKDAY_9, "day_of_week": "mon-fri"})
        assert tc.startup_decision(task, datetime(2026, 9, 19, 10, 0)) is None  # Saturday

    @pytest.mark.parametrize("state", [{"enabled": False}, {"pending": {"since": "x"}}])
    def test_paused_or_parked_is_left_alone(self, state):
        assert tc.startup_decision(_task(**state), FRI) is None


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_SCHEDULED_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("ERA_HIVE_DIR", str(tmp_path / "hive"))
    monkeypatch.setattr(ts, "_scheduler", None)
    calls, reports = [], []
    monkeypatch.setattr(
        ts, "_executor", lambda tool, args, task: calls.append(tool) or {"success": True}
    )
    monkeypatch.setattr(ts, "_reporter", lambda text, urgent: reports.append(text))
    yield types.SimpleNamespace(calls=calls, reports=reports)
    if ts._scheduler is not None:
        ts._scheduler.shutdown(wait=False)


class TestCatchUpWiring:
    def test_create_and_update(self, store):
        task = ts.create_task(
            "morning", "every weekday at 9", tool="weather_report", args={"city": "Tunis"},
            catch_up_until="11",
        )  # fmt: skip
        assert task["catch_up_until"] == "11:00"
        assert ts.update_task("morning", catch_up_until="off")["catch_up_until"] == ""

    def test_one_shot_rejects_catch_up(self, store):
        with pytest.raises(ts.TaskError):
            ts.create_task("once", "in 10 minutes", kind="reminder", message="x",
                           catch_up_until="11:00")  # fmt: skip

    def test_adding_a_catch_up_window_drops_a_waiver(self, store):
        ts.create_task(
            "clean", "every day at 3", tool="file_controller",
            args={"action": "delete", "path": r"D:\scratch\old.tmp"}, waive=True,
        )  # fmt: skip
        assert ts.update_task("clean", catch_up_until="05:00")["waiver_dropped"] is True

    def test_start_schedules_a_catch_up_beside_the_recurring_job(self, store):
        task = ts.create_task("m", "every day at 0:00", tool="weather_report", args={},
                              catch_up_until="23:59")  # fmt: skip
        if datetime.now().strftime("%H:%M") in ("00:00", "23:59"):
            pytest.skip("too close to the window edge")
        ts.start()
        assert ts._scheduler.get_job(f"user:{task['id']}:catchup") is not None
        assert ts._scheduler.get_job(f"user:{task['id']}") is not None

    def test_start_reports_a_missed_day_once(self, store):
        task = ts.create_task("m", "every day at 0:00", tool="weather_report", args={},
                              catch_up_until="00:00")  # fmt: skip
        if datetime.now().strftime("%H:%M") == "00:00":
            pytest.skip("too close to the window edge")
        ts.start()
        assert ts._load()[task["id"]]["last_run"]["status"] == "missed"
        ts._scheduler.shutdown(wait=False)
        ts._scheduler = None
        ts.start()  # a second launch the same day must not report it again
        assert len(ts._load()[task["id"]]["history"]) == 1
        assert store.calls == []
