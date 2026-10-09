"""Scheduled tasks: WHO may approve, waive or authorise — the tool handler's Authority
and the dispatcher's negation-aware, time-bound reading of the user's own words.

Every test gets its own task store and hive (conftest already points both away
from the real runtime/; these fixtures also keep tests from sharing state).
The APScheduler instance is only started in TestStartup, and shut down after.
"""

import asyncio
import contextlib
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


def YES(task, focused):  # an authorize check that stands for "the user clearly approved"
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


def _asked_minutes_ago(task_id, minutes):
    """Backdate when the user was asked about a waiting task."""
    data = ts._load()
    data[task_id]["pending"]["announced_at"] = time.time() - minutes * 60
    ts._save(data)


def _delete_task(**kw):
    return ts.create_task(
        "clean temp", "every day at 3", tool="file_controller", args=DELETE_TEMP, **kw
    )


# ── the tool handler: authority flags come from the user's words ─────────────
class TestToolHandler:
    def test_model_cannot_waive_on_its_own(self):
        out = tool.scheduled_task(
            {
                "action": "create",
                "name": "clean",
                "when": "every day at 3",
                "tool": "file_controller",
                "tool_args": '{"action": "delete", "path": "D:\\\\scratch\\\\a.tmp"}',
                "waive_confirmation": True,
            }
        )
        assert out["success"] and "NOT waived" in out["message"]
        assert not ts.waiver_valid(ts.get_task("clean"))

    def test_user_waiver_is_honoured(self):
        out = tool.scheduled_task(
            {
                "action": "create",
                "name": "clean",
                "when": "every day at 3",
                "tool": "file_controller",
                "tool_args": {"action": "delete", "path": "D:\\scratch\\a.tmp"},
                "waive_confirmation": True,
            },
            tool.Authority(waives_new=lambda name: name == "clean"),
        )
        assert out["success"] and ts.waiver_valid(ts.get_task("clean"))

    def test_approve_requires_user_words(self, isolated):
        task = _delete_task()
        ts.fire(task["id"])
        assert not tool.scheduled_task({"action": "approve", "task": "clean temp"})["success"]
        assert isolated.calls == []
        assert tool.scheduled_task({"action": "approve"}, tool.Authority(approves=YES))["success"]
        assert len(isolated.calls) == 1

    def test_bare_yes_answers_the_task_announced_last(self, isolated):
        """2026-09-18: four tasks were waiting, so a bare "approved" was refused for
        evening_movie_search although it was the one Era had just announced."""
        older = ts.create_task("check claude", "every 5 minutes", tool="agent_task", args={})
        ts.fire(older["id"])
        _asked_minutes_ago(older["id"], 12)
        latest = _delete_task()
        ts.fire(latest["id"])
        seen = {}

        def only_focused(task, focused):
            seen[task["name"]] = focused
            return focused

        auth = tool.Authority(approves=only_focused)
        assert not ts.is_focused(ts.get_task("check claude"))
        assert tool.scheduled_task({"action": "approve"}, auth)["success"]  # no name needed
        assert seen == {"clean temp": True}
        assert isolated.calls == [("file_controller", DELETE_TEMP)]

    def test_two_asks_together_make_a_bare_yes_ambiguous(self, isolated):
        for name in ("check claude", "check mail"):
            ts.fire(ts.create_task(name, "every 5 minutes", tool="agent_task", args={})["id"])
        out = tool.scheduled_task({"action": "approve"}, tool.Authority(approves=YES))
        assert not out["success"] and isolated.calls == []

    def test_skip_with_two_pending_asks_requires_a_task_name(self, isolated):
        for name in ("check claude", "check mail"):
            ts.fire(ts.create_task(name, "every 5 minutes", tool="agent_task", args={})["id"])

        out = tool.scheduled_task({"action": "skip"})

        assert not out["success"]
        assert all(ts.get_task(name)["pending"] for name in ("check claude", "check mail"))

    def test_an_unheard_ask_is_not_what_a_bare_yes_answers(self, isolated, monkeypatch):
        """Era was busy, so the ask only reached the log: the user never heard it."""
        heard = _delete_task()
        ts.fire(heard["id"])
        _asked_minutes_ago(heard["id"], 5)
        monkeypatch.setattr(ts, "_reporter", lambda text, urgent: False)
        unheard = ts.create_task("check claude", "every 5 minutes", tool="agent_task", args={})
        ts.fire(unheard["id"])
        assert ts.is_focused(ts.get_task("clean temp"))
        assert not ts.is_focused(ts.get_task("check claude"))

    def test_run_now_is_asked_through_the_model(self, isolated):
        _delete_task()
        out = tool.scheduled_task({"action": "run_now", "task": "clean temp"})
        assert "needs approval" in out["message"]
        assert ts.get_task("clean temp")["pending"]["announced_at"]

    def test_always_while_waiting_waives_and_runs_it(self, isolated):
        task = _delete_task()
        ts.fire(task["id"])
        auth = tool.Authority(waives=lambda name, focused=False, since=0.0: focused)
        out = tool.scheduled_task({"action": "always"}, auth)
        assert out["success"], out
        assert isolated.calls == [("file_controller", DELETE_TEMP)]
        after = ts.get_task("clean temp")
        assert ts.waiver_valid(after) and not after.get("pending")
        ts.fire(task["id"])  # the next due run goes straight through
        assert len(isolated.calls) == 2

    def test_yes_always_on_approve_also_waives(self, isolated):
        task = _delete_task()
        ts.fire(task["id"])
        auth = tool.Authority(approves=YES, waives=lambda name, focused=False, since=0.0: focused)
        assert tool.scheduled_task({"action": "approve"}, auth)["success"]
        assert ts.waiver_valid(ts.get_task("clean temp"))

    def test_waive_action_requires_user_words_about_that_task(self):
        _delete_task()
        waive = {"action": "waive", "task": "clean temp"}
        assert not tool.scheduled_task(waive)["success"]
        other = tool.Authority(waives=lambda name, *_: name == "something else")
        assert not tool.scheduled_task(waive, other)["success"]
        this = tool.Authority(waives=lambda name, *_: name == "clean temp")
        assert tool.scheduled_task(waive, this)["success"]

    def test_protocol_authority_is_asked_about_the_scheduled_tool(self):
        asked = []
        auth = tool.Authority(names_protocol=lambda t: asked.append(t) or t == "protocol_zero")
        out = tool.scheduled_task(
            {
                "action": "create",
                "name": "p1",
                "when": "every day at 3",
                "tool": "protocol_one",
                "tool_args": '{"action": "launch"}',
            },
            auth,
        )
        assert not out["success"] and asked == ["protocol_one"]

    def test_bad_json_and_unknown_action_fail_cleanly(self):
        bad = tool.scheduled_task(
            {
                "action": "create",
                "name": "x",
                "when": "at 23:59",
                "tool": "weather_report",
                "tool_args": "{nope",
            }
        )
        assert not bad["success"] and "JSON" in bad["message"]
        assert not tool.scheduled_task({"action": "explode"})["success"]

    def test_list_and_results(self):
        tool.scheduled_task(
            {
                "action": "create",
                "name": "w",
                "when": "every day at 8",
                "tool": "weather_report",
                "tool_args": '{"city": "Montreal"}',
            }
        )
        assert "weather_report" in tool.scheduled_task({"action": "list"})["message"]
        tool.scheduled_task({"action": "run_now", "task": "w"})
        assert "ok" in tool.scheduled_task({"action": "results"})["message"]


# ── live dispatcher integration ──────────────────────────────────────────────
class TestDispatcher:
    @pytest.fixture
    def td(self):
        return pytest.importorskip("app.era_live.tool_dispatch")

    @pytest.mark.parametrize("said", ["yes", "approve task backup", "go ahead", "oui vas-y"])
    def test_approval_words(self, td, said):
        assert td._SCHED_APPROVE_COMMAND.search(said)

    @pytest.mark.parametrize("said", ["what's the weather", "schedule a backup", "no, skip it"])
    def test_non_approval_words(self, td, said):
        assert not td._SCHED_APPROVE_COMMAND.search(said)

    @pytest.mark.parametrize(
        "said",
        [
            "run it without asking",
            "waive confirmation for backup",
            "don't ask me for that one",
            "sans confirmation",
        ],
    )
    def test_waiver_words(self, td, said):
        assert td._SCHED_WAIVE_COMMAND.search(said)

    @pytest.mark.parametrize("said", ["ask me first", "schedule backup every day", "confirm it"])
    def test_non_waiver_words(self, td, said):
        assert not td._SCHED_WAIVE_COMMAND.search(said)

    def _stub(self, td, snapshot):
        era = types.SimpleNamespace()
        era.session_manager = types.SimpleNamespace(get_state_snapshot=lambda: snapshot)
        era._pending_cm_ask_active = lambda: False
        era._cm_handoff_active = lambda: False
        era.enter_standby = lambda: None
        era._execute_tool_raw = td.ToolDispatchMixin._execute_tool_raw.__get__(era)
        return era

    def test_standby_blocks_the_model_but_not_a_scheduled_run(self, td):
        era = self._stub(td, {"standby_active": True})
        blocked = asyncio.run(era._execute_tool_raw("enter_standby", {}))
        assert "standby" in blocked
        token = ts.current_task.set({"id": "t", "tool": "enter_standby"})
        try:
            ran = asyncio.run(era._execute_tool_raw("enter_standby", {}))
        finally:
            ts.current_task.reset(token)
        assert "blocked" not in str(ran).lower()

    @staticmethod
    @contextlib.contextmanager
    def _running(task, step):
        """Stand in for the executor, which sets BOTH contextvars for every step."""
        tokens = (ts.current_task.set(task), ts.current_step.set(step))
        try:
            yield
        finally:
            ts.current_step.reset(tokens[1])
            ts.current_task.reset(tokens[0])

    def test_protocol_authority_only_inside_its_own_voice_authorized_task(self, td):
        check = td._scheduled_by_voice
        assert not check("protocol_one")  # nothing running at all
        launch = {
            "kind": "tool",
            "tool": "protocol_one",
            "args": {"action": "launch"},
            "message": "",
        }
        step = {"tool": "protocol_one", "args": {"action": "launch"}}
        authorized = {**launch, "voice_authorization": ts.fingerprint(launch)}
        for task, expected in (
            (authorized, True),
            ({**launch, "voice_authorization": None}, False),
            # authorization recorded for a different action (e.g. "status") no longer matches
            ({**launch, "voice_authorization": "0123456789abcdef"}, False),
        ):
            with self._running(task, step):
                assert check("protocol_one") is expected and not check("protocol_zero")
        # The grant covers the gated ACTION, not the tool: a status step is not a launch.
        with self._running(authorized, {"tool": "protocol_one", "args": {"action": "status"}}):
            assert not check("protocol_one")

    def test_a_chain_grant_covers_only_the_protocol_the_user_named(self, td):
        """The user said only "launch Protocol One". A Protocol Zero link beside it in
        the same chain must not ride on that grant — including when its action carries
        stray whitespace, which the dispatcher strips but a naive gate would not."""
        check = td._scheduled_by_voice
        one = {"tool": "protocol_one", "args": {"action": "launch"}, "label": ""}
        zero = {"tool": "protocol_zero", "args": {"action": "execute "}, "label": ""}
        chain = {"kind": "chain", "tool": "", "args": {}, "message": "", "steps": [one, zero]}
        chain["voice_authorization"] = ts.fingerprint(chain)
        chain["voice_authorized_tools"] = ["protocol_one"]
        with self._running(chain, one):
            assert check("protocol_one") and not check("protocol_zero")
        # The step the user never named gets nothing, though the chain IS authorized.
        with self._running(chain, zero):
            assert not check("protocol_zero") and not check("protocol_one")
        # A chain carrying no per-tool record at all authorizes nothing: the boolean it
        # would have to fall back on cannot say WHICH protocol was named.
        untagged = {**chain, "voice_authorized_tools": None}
        with self._running(untagged, one):
            assert not check("protocol_one") and not check("protocol_zero")

    def test_such_a_chain_cannot_be_created_in_the_first_place(self, td):
        from actions.scheduled_task import Authority, _voice_authorized

        steps = ts.normalize_steps(
            [
                {"tool": "protocol_one", "args": {"action": "launch"}},
                {"tool": "protocol_zero", "args": {"action": "execute "}},
            ]
        )
        # Naming one Protocol grants neither: a partial grant is no grant.
        said_one_only = Authority(names_protocol=lambda tool: tool == "protocol_one")
        assert _voice_authorized(steps, said_one_only) == []
        with pytest.raises(ts.TaskError, match="Protocol One can only be scheduled"):
            ts.create_task("nightly", "every day at 3", steps=steps, voice_authorized=[])
        # Even handed a grant for Protocol One only, the store refuses the Zero step.
        with pytest.raises(ts.TaskError, match="Step 2: Protocol Zero"):
            ts.create_task(
                "nightly", "every day at 3", steps=steps, voice_authorized=["protocol_one"]
            )
        # Naming both is what the user must actually have said.
        said_both = Authority(names_protocol=lambda tool: True)
        granted = _voice_authorized(steps, said_both)
        assert granted == ["protocol_one", "protocol_zero"]
        task = ts.create_task("nightly", "every day at 3", steps=steps, voice_authorized=granted)
        assert task["voice_authorized_tools"] == ["protocol_one", "protocol_zero"]
        assert ts.voice_authorized_for(task, "protocol_zero")

    # ── word-level authority: negation, timing, naming ──
    def _speaker(self, td, utterances, in_progress="", in_progress_started_ago=2):
        """utterances: [(seconds_ago, text)] recorded; in_progress: current partial turn."""
        now = time.time()
        era = types.SimpleNamespace(
            _recent_user_texts=[(now - ago, text) for ago, text in utterances],
            _last_user_text=in_progress or (utterances[-1][1] if utterances else ""),
            _inflight_user_text=in_progress,
            _inflight_started_at=now - in_progress_started_ago,
        )
        era._user_words_match = td.ToolDispatchMixin._user_words_match.__get__(era)
        return td.ToolDispatchMixin._scheduled_task_authority(era)

    @staticmethod
    def _parked(name="clean temp", seconds_ago=20):
        since = datetime.now().replace(microsecond=0) - timedelta(seconds=seconds_ago)
        return {"name": name, "pending": {"since": since.isoformat()}}

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("yes", True),
            ("go ahead", True),
            ("oui vas-y", True),
            ("don't do it", False),
            ("no, don't run it", False),
            ("not ok", False),
            ("je ne suis pas d'accord", False),
            ("yes, but don't run it", False),
        ],
    )
    def test_bare_approval_after_the_announcement(self, td, said, expected):
        assert self._speaker(td, [(5, said)]).approves(self._parked(), True) is expected

    def test_words_said_before_the_task_parked_do_not_approve(self, td):
        auth = self._speaker(td, [(50, "okay thanks")])
        assert not auth.approves(self._parked(seconds_ago=20), True)

    def test_bare_yes_is_the_latest_reply_not_the_first(self, td):
        # 2026-09-18: the user chatted after the announcement, then said "approved" —
        # refused, because only the FIRST reply after parking counted.
        auth = self._speaker(td, [(15, "what's the weather"), (5, "approved")])
        assert auth.approves(self._parked(), True)

    def test_a_stale_yes_followed_by_anything_else_does_not_approve(self, td):
        auth = self._speaker(td, [(15, "yes"), (5, "what's the weather")])
        assert not auth.approves(self._parked(), True)

    @pytest.mark.parametrize(
        "said", ["approved", "confirm", "sure", "no, I said approve", "yes always", "always"]
    )
    def test_simple_approvals(self, td, said):
        assert self._speaker(td, [(5, said)]).approves(self._parked(), True)

    def test_streamed_voice_chunks_are_read_whole(self, td):
        """Speech-to-text streams a turn in chunks; _last_user_text held only the last
        one ("movie search"), so the waiver phrase was never seen during the turn."""
        now = time.time()
        era = types.SimpleNamespace(
            _recent_user_texts=[(now - 40, "what's pending")],
            _last_user_text="movie search",
            _inflight_user_text="waive confirmation for evening movie search",
            _inflight_started_at=now - 3,
        )
        era._user_words_match = td.ToolDispatchMixin._user_words_match.__get__(era)
        auth = td.ToolDispatchMixin._scheduled_task_authority(era)
        assert auth.waives("evening_movie_search")

    def test_a_sentence_begun_before_the_ask_does_not_answer_it(self, td):
        """The in-progress turn is stamped with when it STARTED, never "now"."""
        auth = self._speaker(td, [], in_progress="okay", in_progress_started_ago=40)
        assert not auth.approves(self._parked(seconds_ago=20), True)
        asked_later = {**self._parked(seconds_ago=60)}
        asked_later["pending"]["announced_at"] = time.time() - 10
        early = self._speaker(td, [(30, "yes")])  # said after parking, before the ask
        assert not early.approves(asked_later, True)

    @pytest.mark.parametrize(
        "said",
        [
            "don't ask me now",
            "stop asking me questions and play the music",
            "you never ask before doing things",
            "you scheduled that without my approval",
            "I got no confirmation email from Amazon",
            "okay thanks",
            "yeah, what time is it",
            "can I approve it later?",
            "what happens if I say yes",
            "maybe tomorrow",
        ],
    )
    def test_ordinary_speech_neither_approves_nor_waives(self, td, said):
        auth = self._speaker(td, [(5, said)])
        assert not auth.approves(self._parked(), True)
        assert not auth.waives("clean temp", True)

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("search movies every night at 9 without asking", True),
            ("summarise this page without asking me questions", False),
            ("don't run it without asking, every day at 9", False),
        ],
    )
    def test_waiver_while_creating_is_about_scheduling(self, td, said, expected):
        assert self._speaker(td, [(5, said)]).waives_new("evening_movie_search") is expected

    @pytest.mark.parametrize(
        ("said", "name"),
        [
            ("yes, search for pizza places", "evening_movie_search"),
            ("okay, what's the temperature", "clean temp"),
            ("approve evening", "evening_movie_search"),  # one word of three is not a name
        ],
    )
    def test_a_stray_word_does_not_name_a_task(self, td, said, name):
        assert not self._speaker(td, [(5, said)]).approves(self._parked(name), False)

    @pytest.mark.parametrize(
        "said",
        [
            "is evening movie search waived?",
            "remove the waiver for evening movie search",
            "waive evening movie search? no, don't",
        ],
    )
    def test_questions_about_a_waiver_are_not_a_waiver(self, td, said):
        assert not self._speaker(td, [(5, said)]).waives("evening_movie_search")

    def test_typed_input_ends_a_stale_voice_fragment(self, td):
        now = time.time()
        era = types.SimpleNamespace(
            _recent_user_texts=[],
            _last_user_text="",
            _inflight_user_text="yes",  # left over by a dropped session
            _inflight_started_at=now - 5,
        )
        td.ToolDispatchMixin._note_user_utterance(era, "no, skip it")
        era._user_words_match = td.ToolDispatchMixin._user_words_match.__get__(era)
        auth = td.ToolDispatchMixin._scheduled_task_authority(era)
        assert era._inflight_user_text == ""
        assert not auth.approves(self._parked(seconds_ago=10), True)

    def test_several_waiting_needs_the_task_name(self, td):
        assert not self._speaker(td, [(5, "yes")]).approves(self._parked(), False)
        named = self._speaker(td, [(5, "approve the temp cleanup")])
        assert named.approves(self._parked(), False)
        movie = self._speaker(td, [(5, "approve the movie search")])
        assert movie.approves(self._parked("evening_movie_search"), False)

    def test_in_progress_voice_turn_counts_as_now(self, td):
        auth = self._speaker(td, [(50, "clean temp every day")], in_progress="yes go ahead")
        assert auth.approves(self._parked(), True)

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("run clean temp without asking", True),
            ("waive confirmation for the temp cleanup", True),
            ("never run clean temp without asking", False),
            ("please don't waive anything for clean temp", False),
            ("read my emails, no approval needed for reading", False),  # not this task
        ],
    )
    def test_waiver_words(self, td, said, expected):
        assert self._speaker(td, [(5, said)]).waives("clean temp") is expected

    def test_stale_waiver_words_do_not_count(self, td):
        assert not self._speaker(td, [(150, "run clean temp without asking")]).waives("clean temp")

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("always", True),
            ("yes, always", True),
            ("don't ask again", True),
            ("no need to ask", True),
            ("stop asking me", True),
            ("waived", True),
            ("wave the confirmation", True),  # speech-to-text spelling of "waive"
            ("you always get this wrong", False),
            ("no, don't run it, and don't ask again", False),
            ("what's the weather", False),
        ],
    )
    def test_short_waiver_for_the_task_being_answered(self, td, said, expected):
        assert self._speaker(td, [(5, said)]).waives("evening_movie_search", True) is expected

    def test_short_waiver_needs_the_name_for_any_other_task(self, td):
        assert not self._speaker(td, [(5, "always")]).waives("evening_movie_search", False)
        named = self._speaker(td, [(5, "wave confirmation for evening movie search")])
        assert named.waives("evening_movie_search", False)

    @pytest.mark.parametrize(
        ("said", "tool_name", "expected"),
        [
            ("launch protocol one every night at 3", "protocol_one", True),
            ("what did protocol one find", "protocol_one", False),
            ("don't launch protocol one", "protocol_one", False),
            ("launch protocol one every night", "protocol_zero", False),
            ("launch protocol zero every monday", "protocol_zero", True),
            ("execute protocol zero every monday", "protocol_zero", False),
        ],
    )
    def test_protocol_command_words(self, td, said, tool_name, expected):
        assert self._speaker(td, [(5, said)]).names_protocol(tool_name) is expected
