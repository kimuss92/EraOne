"""A crew candidate's tests never run before the user's own yes.

* source.stage parks the candidate and arms the shared confirmation store — no thread;
* only the store's ticket starts it, once, and only with APPROVED_CONFIRMATION set;
* a forged or replayed ticket, or no approval, starts nothing;
* the verifier environment carries no credential-shaped variable;
* a held candidate lost to a restart reads as failed, never as pending;
* only Cody and Drew hold source.stage.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import development as dev
from core import development_submission as submission
from core.approval_execution import APPROVED_CONFIRMATION
from core.pending_confirmation import get_shared_store
from core.safety_guard import classify_action
from crew import coordination, source
from crew.registry import load_specs
from crew.runtime import run_tool

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "JOBS", tmp_path / "runtime/development")
    monkeypatch.setattr(dev, "_active", None)
    monkeypatch.setattr(dev, "_held_ids", set())
    monkeypatch.setattr(submission, "_HELD", {})
    monkeypatch.setattr(source, "ROOT", tmp_path)
    (tmp_path / "core").mkdir()
    (tmp_path / "core/example.py").write_text("VALUE = 1\n", encoding="utf-8")
    get_shared_store().clear()
    yield tmp_path
    get_shared_store().clear()


@pytest.fixture
def threads(monkeypatch):
    started = []

    class Deferred:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs)

    import threading as real

    monkeypatch.setattr(submission, "threading", SimpleNamespace(Thread=Deferred, Event=real.Event))
    return started


def candidate():
    return {
        "plan": "Change the example",
        "files": [
            {
                "path": "core/example.py",
                "content": "VALUE = 2\n",
                "base_sha256": dev._hash(dev.ROOT / "core/example.py"),
            },
            {
                "path": "tools/test_example.py",
                "content": "def test_value():\n    assert 2 == 2\n",
                "base_sha256": None,
            },
        ],
        "tests": ["tools/test_example.py"],
    }


def stage_in_turn(plan):
    work = coordination.TeamWork("w-gate", "cody", "Fix the example", {"cody"})
    reset = coordination.CURRENT_WORK.set(work)
    try:
        return source.stage(None, plan)
    finally:
        coordination.CURRENT_WORK.reset(reset)


def armed():
    """The pending entry the user is asked about: (its id, the carried ticket)."""
    pending = get_shared_store().peek()
    return pending.confirmation_id, pending.parameters["_approved_plan"]


def yes(plan, confirmation_id):
    """What the dispatcher does after the user's yes: pass the consumed id explicitly."""
    call = {"action": "verify_candidate", "_approved_plan": plan}
    return run_tool(call, approved_confirmation=confirmation_id)


class TestHeld:
    def test_stage_runs_nothing_and_asks_the_user(self, repository, threads):
        res = stage_in_turn(candidate())
        assert not res.success and res.error_type == "ConfirmationRequired"
        assert res.confirmation_id and threads == [] and dev._active is None
        job = dev._read(res.data["development_id"])
        assert job["state"] == submission.HELD_STATE
        pending = get_shared_store().peek()
        assert pending.tool == "crew" and pending.parameters["action"] == "verify_candidate"
        ticket = pending.parameters["_approved_plan"]["ticket"]
        assert ticket and ticket != job["id"]
        assert (repository / "core/example.py").read_text() == "VALUE = 1\n"

    def test_the_users_yes_starts_it_exactly_once(self, repository, threads):
        stage_in_turn(candidate())
        cid, plan = armed()
        first = yes(plan, cid)
        assert first.success and len(threads) == 1
        dev._active = None  # the deferred thread never ran to release the slot
        assert not yes(plan, cid).success and len(threads) == 1

    def test_yes_works_from_an_executor_thread(self, repository, threads):
        """The live dispatcher runs the crew tool via run_in_executor, where the
        APPROVED_CONFIRMATION ContextVar is NOT visible (2026-10-18 review, HIGH)."""
        import asyncio

        stage_in_turn(candidate())
        cid, plan = armed()

        async def dispatch():
            token = APPROVED_CONFIRMATION.set(cid)
            try:
                approved = APPROVED_CONFIRMATION.get()  # read on the loop, as dispatch does
                loop = asyncio.get_running_loop()
                seen = await loop.run_in_executor(None, APPROVED_CONFIRMATION.get)
                result = await loop.run_in_executor(None, lambda: yes(plan, approved))
                return seen, result
            finally:
                APPROVED_CONFIRMATION.reset(token)

        seen, result = asyncio.run(dispatch())
        assert seen is None  # the reason the id must be passed explicitly
        assert result.success and len(threads) == 1

    def test_dispatcher_passes_the_approval_to_the_crew_tool(self):
        src = (REPO / "app/era_live/tool_dispatch.py").read_text(encoding="utf-8")
        branch = src[src.index('elif tool_name == "crew":') :][:1200]
        assert "approved = APPROVED_CONFIRMATION.get()" in branch
        assert "approved_confirmation=approved" in branch

    def test_without_approval_nothing_starts(self, repository, threads):
        stage_in_turn(candidate())
        _, plan = armed()
        assert not yes(plan, None).success and threads == []

    def test_a_different_confirmation_does_not_count(self, repository, threads):
        stage_in_turn(candidate())
        _, plan = armed()
        assert not yes(plan, "some-other-approval").success and threads == []
        assert submission._HELD  # still waiting for its own yes

    @pytest.mark.parametrize("forged", [None, {}, {"ticket": "guess"}, {"ticket": 7}, "x"])
    def test_forged_ticket_starts_nothing(self, repository, threads, forged):
        stage_in_turn(candidate())
        cid, _ = armed()
        assert not yes(forged, cid).success and threads == []

    def test_development_id_alone_is_not_a_ticket(self, repository, threads):
        res = stage_in_turn(candidate())
        cid, _ = armed()
        assert not yes({"ticket": res.data["development_id"]}, cid).success and threads == []

    def test_cancelled_held_job_never_starts(self, repository, threads):
        res = stage_in_turn(candidate())
        cid, plan = armed()
        job = dev._read(res.data["development_id"])
        job["state"] = "cancelled"
        dev._save(job)
        assert not yes(plan, cid).success and threads == []

    def test_question_names_files_tests_and_what_actually_runs(self, repository, threads):
        res = stage_in_turn(candidate())
        for text in (res.message, get_shared_store().peek().reason):
            assert "core/example.py" in text and "tools/test_example.py" in text
            assert "cody" in text and "your permissions" in text

    def test_codemode_team_turn_keeps_codemode_flow(self, repository, threads):
        """CodeMode's consult drops the spoken question and _review_job polls the job,
        so a held candidate there would hang silently and block the confirmation slot."""
        authority = {
            "source": "codemode",
            "session_id": "s1",
            "turn_revision": 1,
            "input_id": "i1",
            "input_source": "hud",
        }
        work = coordination.TeamWork(
            "w-cm", "codemode", "cody, fix it", {"era"}, authority=authority
        )
        reset = coordination.CURRENT_WORK.set(work)
        try:
            res = source.stage(None, candidate())
        finally:
            coordination.CURRENT_WORK.reset(reset)
        assert res.success and len(threads) == 1
        assert not get_shared_store().is_pending() and submission._HELD == {}

    def test_busy_store_holds_nothing(self, repository, threads):
        get_shared_store().store("file_controller", {"x": 1}, "HIGH", "user question first")
        with pytest.raises(ValueError, match="already waiting"):
            stage_in_turn(candidate())
        assert submission._HELD == {} and threads == []
        assert get_shared_store().peek().tool == "file_controller"

    def test_verify_candidate_is_classified_as_needing_confirmation(self):
        verdict = classify_action("crew", {"action": "verify_candidate"})
        assert verdict["requires_confirmation"] is True
        assert classify_action("crew", {"action": "ask"})["requires_confirmation"] is False

    def test_held_job_lost_to_restart_reads_as_failed(self, repository, threads):
        res = stage_in_turn(candidate())
        dev._held_ids.clear()  # what a restart leaves behind
        submission._HELD.clear()
        job = next(j for j in dev.list_jobs() if j["id"] == res.data["development_id"])
        assert job["state"] == "failed" and "nothing ran" in job["error"]


class TestVerifierEnv:
    def test_no_credential_shaped_variable_reaches_the_verifier(self, monkeypatch, tmp_path):
        for name in (
            "GEMINI_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ERA_TELEGRAM_TOKEN",
            "ERA_FISH_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "OPENAI_API_KEY",
            "ERA_DB_PASSWORD",
        ):
            monkeypatch.setenv(name, "s3cret-value")
        monkeypatch.setenv("ERA_VAULT_DIR", str(tmp_path))
        monkeypatch.setenv("RANDOM_TOOL_HOME", "x")
        env = dev._verifier_env(tmp_path / "stage")
        assert "s3cret-value" not in env.values()
        assert env["ERA_VAULT_DIR"] == str(tmp_path) and "RANDOM_TOOL_HOME" not in env
        assert "PATH" in {k.upper() for k in env}
        assert env["ERA_DEVELOPMENT_STAGE"] == str(tmp_path / "stage")


class TestGrants:
    def test_only_cody_and_drew_may_stage(self):
        specs = load_specs(REPO / "config" / "crew").specs
        holders = sorted(b for b, s in specs.items() if "source.stage" in s.capabilities)
        assert holders == ["cody", "drew"]
