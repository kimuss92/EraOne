"""Real executor/queue outcome contract; planner, tools and speech are isolated."""

import pytest

from agent import executor as ex
from agent.task_queue import TaskQueue
from agent.types import TaskStatus
from core.tool_result import ToolResult


def run_task(executor, spoken=None, on_complete=None):
    queue = TaskQueue(executor_factory=lambda: executor)
    task_id = queue.submit("prepare a brief", speak=spoken, on_complete=on_complete)
    task = queue._tasks[task_id]
    queue._queue.remove(task)
    task.status = TaskStatus.RUNNING
    queue._active_count = 1
    queue._run_task(task)
    assert queue._active_count == 0
    return queue, task_id


def test_invalid_plan_is_failed_not_completed(monkeypatch):
    monkeypatch.setattr(ex, "create_plan", lambda goal: {"steps": []})
    spoken = []
    queue, task_id = run_task(ex.AgentExecutor(), spoken.append)
    status = queue.get_status(task_id)
    assert status["status"] == "failed"
    assert "valid plan" in status["error"]
    assert len(spoken) == 1
    assert "failed" in spoken[0].lower()
    assert "valid plan" in spoken[0]


def test_agent_task_planning_timeout_is_reported_without_hanging(monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr(ex, "PLAN_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(ex, "create_plan", lambda goal: time.sleep(0.2) or {"steps": []})

    started = time.monotonic()
    result = asyncio.run(ex.AgentExecutor().execute("brief"))

    assert time.monotonic() - started < 0.1
    assert isinstance(result, ToolResult)
    assert not result.success
    assert result.error_type == "PlanningTimeout"


@pytest.mark.parametrize(
    "result, expected",
    [
        (ToolResult.ok("Brief saved."), "completed"),
        (ToolResult.fail("Tool unavailable."), "failed"),
        (ToolResult.fail("Task cancelled.", error_type="Cancelled"), "cancelled"),
        (
            ToolResult.fail("Approve the edit?", error_type="ConfirmationRequired"),
            "awaiting_approval",
        ),
        ("All done!", "failed"),
        (None, "failed"),
    ],
)
def test_queue_records_and_reports_only_explicit_outcomes(result, expected):
    class Executor:
        async def execute(self, **kwargs):
            return result

    spoken, callbacks = [], []
    queue, task_id = run_task(
        Executor(), spoken.append, lambda tid, outcome: callbacks.append((tid, outcome))
    )
    status = queue.get_status(task_id)
    assert status["status"] == expected
    assert isinstance(status["result"], str)
    assert len(spoken) == 1
    assert expected.replace("_", " ") in spoken[0].lower()
    assert task_id in spoken[0]
    assert len(callbacks) == 1
    assert isinstance(callbacks[0][1], ToolResult)
    assert callbacks[0][1].success == (expected == "completed")


@pytest.mark.parametrize("cancelled", [False, True])
def test_executor_exception_reports_outcome_without_overwriting_cancellation(cancelled):
    class Executor:
        async def execute(self, cancel_flag, **kwargs):
            if cancelled:
                cancel_flag.set()
            raise RuntimeError("provider unavailable")

    spoken = []
    queue, task_id = run_task(Executor(), spoken.append)
    expected = "cancelled" if cancelled else "failed"
    assert queue.get_status(task_id)["status"] == expected
    assert len(spoken) == 1 and expected in spoken[0]


def test_notification_failure_does_not_corrupt_task_or_worker_count():
    class Executor:
        async def execute(self, **kwargs):
            return ToolResult.fail("Tool failed.")

    def broken_speech(text):
        raise RuntimeError("speaker offline")

    callbacks = []
    queue, task_id = run_task(
        Executor(), broken_speech, lambda tid, result: callbacks.append(result)
    )
    assert queue.get_status(task_id)["error"] == "Tool failed."
    assert len(callbacks) == 1


@pytest.fixture
def plan(monkeypatch):
    step = {"step": 1, "tool": "web_search", "description": "Find sources", "parameters": {}}
    plan = {"steps": [step]}
    monkeypatch.setattr(ex, "create_plan", lambda goal: plan)
    monkeypatch.setattr(ex, "replan", lambda *args: plan)
    monkeypatch.setattr(ex, "generate_text", lambda *args, **kwargs: "Found sources.")
    return plan


@pytest.mark.parametrize("decision", [ex.ErrorDecision.ABORT, ex.ErrorDecision.SKIP, "replan"])
def test_failed_steps_never_become_completed(monkeypatch, plan, decision):
    monkeypatch.setattr(ex, "_call_tool_result", lambda *a: ToolResult.fail("Search unavailable."))
    monkeypatch.setattr(
        ex, "analyze_error", lambda *a, **k: {"decision": decision, "reason": "Search unavailable."}
    )
    outcomes = []
    queue, task_id = run_task(
        ex.AgentExecutor(), on_complete=lambda tid, result: outcomes.append(result)
    )
    assert queue.get_status(task_id)["status"] == "failed"
    assert outcomes[0].error_type != "InvalidOutcome"
    assert "Search unavailable" in outcomes[0].error


def test_success_is_structured_and_reported_once(monkeypatch, plan):
    monkeypatch.setattr(ex, "_call_tool_result", lambda *a: ToolResult.ok("Sources found."))
    spoken = []
    queue, task_id = run_task(ex.AgentExecutor(), spoken.append)
    assert queue.get_status(task_id)["status"] == "completed"
    assert len(spoken) == 1 and "completed" in spoken[0]


@pytest.mark.parametrize("alternative", [False, True])
def test_approval_stops_execution_and_preserves_confirmation(monkeypatch, plan, alternative):
    approval = ToolResult.fail(
        "Approve this edit?", error_type="ConfirmationRequired", confirmation_id="approval-1"
    )
    calls = []

    def tool(*args):
        calls.append(args)
        if alternative and len(calls) == 1:
            return ToolResult.fail("Search unavailable.")
        return approval

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    monkeypatch.setattr(
        ex, "analyze_error", lambda *a, **k: {"decision": "fix", "fix_suggestion": "try another"}
    )
    monkeypatch.setattr(ex, "generate_fix", lambda *a: plan["steps"][0])
    outcomes = []
    queue, task_id = run_task(
        ex.AgentExecutor(), on_complete=lambda tid, result: outcomes.append(result)
    )
    assert queue.get_status(task_id)["status"] == "awaiting_approval"
    assert outcomes == [approval]
    assert len(calls) == (2 if alternative else 1)


def test_cancellation_during_last_tool_does_not_speak_success(monkeypatch, plan):
    import asyncio
    import threading

    flag = threading.Event()

    def tool(*args):
        flag.set()
        return ToolResult.ok("Sources found.")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    spoken = []
    result = asyncio.run(ex.AgentExecutor().execute("brief", spoken.append, flag))
    assert isinstance(result, ToolResult)
    assert result.error_type == "Cancelled"
    assert not any("Found sources" in text or "done" in text.lower() for text in spoken)


def test_live_agent_task_uses_literal_logged_reporting(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.era_live.tool_dispatch import ToolDispatchMixin

    submitted = []

    def submit(**kwargs):
        submitted.append(kwargs)
        return "job-1"

    monkeypatch.setattr("agent.task_queue.get_queue", lambda: SimpleNamespace(submit=submit))

    def literal(text):
        pass

    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        speak=lambda text: None,
        speak_dispatched=literal,
    )
    result = asyncio.run(ToolDispatchMixin._execute_tool_raw(era, "agent_task", {"goal": "brief"}))
    assert "job-1" in result
    assert submitted[0]["speak"] is literal


def test_cancellation_during_recovery_runs_no_alternative(monkeypatch, plan):
    import asyncio
    import threading

    flag = threading.Event()
    calls = []

    def tool(*args):
        calls.append(args)
        return ToolResult.fail("Search unavailable.")

    def analyze(*a, **k):
        flag.set()  # cancelled while the recovery decision was being made
        return {"decision": "fix", "fix_suggestion": "try another", "user_message": "retrying"}

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    monkeypatch.setattr(ex, "analyze_error", analyze)
    monkeypatch.setattr(ex, "generate_fix", lambda *a: plan["steps"][0])
    spoken = []
    result = asyncio.run(ex.AgentExecutor().execute("brief", spoken.append, flag))
    assert isinstance(result, ToolResult)
    assert result.error_type == "Cancelled"
    assert len(calls) == 1, "the alternative tool ran after the task was cancelled"


def test_broken_speaker_cannot_kill_recovery(monkeypatch, plan):
    import asyncio

    def broken_speak(text):
        raise RuntimeError("speaker offline")

    monkeypatch.setattr(ex, "_call_tool_result", lambda *a: ToolResult.fail("Search unavailable."))
    monkeypatch.setattr(
        ex,
        "analyze_error",
        lambda *a, **k: {"decision": "fix", "fix_suggestion": "try", "user_message": "trouble"},
    )
    monkeypatch.setattr(ex, "generate_fix", lambda *a: plan["steps"][0])
    result = asyncio.run(ex.AgentExecutor().execute("brief", broken_speak))
    assert isinstance(result, ToolResult)
    assert result.error_type == "ReplanLimit", "recovery must finish despite a broken speaker"


def test_executor_coroutine_cancelled_reports_cancelled():
    import asyncio

    class Executor:
        async def execute(self, **kwargs):
            raise asyncio.CancelledError()

    spoken = []
    queue, task_id = run_task(Executor(), spoken.append)
    assert queue.get_status(task_id)["status"] == "cancelled"
    assert queue._active_count == 0


def test_throwing_on_complete_does_not_corrupt_outcome():
    class Executor:
        async def execute(self, **kwargs):
            return ToolResult.ok("Done.")

    def boom(tid, result):
        raise RuntimeError("callback exploded")

    spoken = []
    queue, task_id = run_task(Executor(), spoken.append, boom)
    status = queue.get_status(task_id)
    assert status["status"] == "completed" and status["result"] == "Done."
    assert queue._active_count == 0


def test_awaiting_approval_keeps_confirmation_id_in_status(monkeypatch, plan):
    import asyncio

    approval = ToolResult.fail(
        "Approve the edit?",
        error_type="ConfirmationRequired",
        confirmation_id="conf-7",
        data={"plan": "edit"},
    )

    class Executor:
        async def execute(self, **kwargs):
            return approval

    queue, task_id = run_task(Executor())
    status = queue.get_status(task_id)
    assert status["status"] == "awaiting_approval"
    assert status["confirmation_id"] == "conf-7"
    assert status["error_type"] == "ConfirmationRequired"
    assert status["result"] == "Approve the edit?"


def test_agent_task_replanning_timeout_is_reported_without_hanging(monkeypatch, plan):
    import asyncio
    import time

    monkeypatch.setattr(ex, "PLAN_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(ex, "_call_tool_result", lambda *a: ToolResult.fail("Search unavailable."))
    monkeypatch.setattr(ex, "analyze_error", lambda *a, **k: {"decision": "replan"})
    monkeypatch.setattr(ex, "replan", lambda *args: time.sleep(0.2) or plan)

    started = time.monotonic()
    result = asyncio.run(ex.AgentExecutor().execute("brief"))

    assert time.monotonic() - started < 0.1
    assert isinstance(result, ToolResult)
    assert not result.success
    assert result.error_type == "PlanningTimeout"
    assert "replanning timed out" in result.message
