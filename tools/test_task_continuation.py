import asyncio
import json

import pytest

from agent import executor as ex
from agent.task_queue import TaskQueue
from agent.types import TaskStatus
from core.pending_confirmation import PendingConfirmationStore
from core.tool_result import ToolResult


@pytest.fixture
def workflow(monkeypatch):
    steps = [
        {"step": 1, "tool": "read", "description": "inspect", "parameters": {}},
        {"step": 2, "tool": "risky", "description": "approved change", "parameters": {}},
        {"step": 3, "tool": "tail", "description": "finish", "parameters": {}},
    ]
    planned, called = [], []

    def plan(goal):
        planned.append(goal)
        return {"steps": steps}

    async def inject(parameters, tool, observations, **kwargs):
        if tool == "tail":
            assert observations[1] == "read finished"
            assert observations[2] == "approved change finished"
        return parameters

    monkeypatch.setattr(ex, "create_plan", plan)
    monkeypatch.setattr(ex, "_inject_context", inject)
    monkeypatch.setattr(
        ex.AgentExecutor,
        "_summarize",
        lambda self, goal, completed, speak: f"{len(completed)} steps complete",
    )
    return planned, called


def test_resume_runs_only_remaining_steps_and_keeps_result_serializable(monkeypatch, workflow):
    planned, called = workflow

    def tool(name, parameters, speak):
        called.append(name)
        if name == "risky":
            return ToolResult.fail(
                "Approve?", error_type="ConfirmationRequired", confirmation_id="cid"
            )
        return ToolResult.ok(f"{name} finished")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    executor = ex.AgentExecutor()
    paused = asyncio.run(executor.execute("goal"))
    checkpoint = paused._execution_checkpoint
    json.dumps(paused.to_dict())  # private checkpoint cannot leak into protocol data
    resumed = asyncio.run(
        executor.execute(
            "goal", checkpoint=checkpoint, approved_result=ToolResult.ok("approved change finished")
        )
    )
    assert resumed.success
    assert resumed.message == "3 steps complete"
    assert called == ["read", "risky", "tail"]
    assert planned == ["goal"]


def test_failed_approved_step_runs_no_remaining_actions(monkeypatch, workflow):
    _, called = workflow

    def tool(name, parameters, speak):
        called.append(name)
        return (
            ToolResult.fail("Approve?", error_type="ConfirmationRequired")
            if name == "risky"
            else ToolResult.ok(f"{name} finished")
        )

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    executor = ex.AgentExecutor()
    paused = asyncio.run(executor.execute("goal"))
    resumed = asyncio.run(
        executor.execute(
            "goal",
            checkpoint=paused._execution_checkpoint,
            approved_result=ToolResult.fail("Change failed", error_type="DeviceError"),
        )
    )
    assert not resumed.success
    assert resumed.error_type == "DeviceError"
    assert called == ["read", "risky"]


def run_queued(queue, task_id):
    task = queue._tasks[task_id]
    queue._queue.remove(task)
    task.status = TaskStatus.RUNNING
    task.execution_active = True
    queue._active_count = 1
    queue._run_task(task)


@pytest.mark.parametrize("channel", ["executor", "live"])
def test_real_confirmation_handler_queues_continuation_under_same_id(
    monkeypatch, workflow, channel
):
    planned, called = workflow
    store = PendingConfirmationStore()
    queue = TaskQueue(pending_store=store, executor_factory=ex.AgentExecutor)
    monkeypatch.setattr(ex, "_pending_store", store)
    monkeypatch.setattr("agent.task_queue.get_queue", lambda: queue)
    monkeypatch.setattr("core.pending_confirmation._user_approval_probe", lambda pending: True)
    actual_confirm_handler = ex._call_tool_result

    def tool(name, parameters, speak, **kwargs):
        if kwargs.get("_confirmed_once"):
            return ToolResult.ok("approved change finished")
        called.append(name)
        if name == "risky":
            cid = store.store("risky", {}, "HIGH", "change")
            return ToolResult.fail(
                "Approve?", error_type="ConfirmationRequired", confirmation_id=cid
            )
        return ToolResult.ok(f"{name} finished")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    tid = queue.submit("goal")
    run_queued(queue, tid)
    assert queue.get_status(tid)["status"] == "awaiting_approval"
    if channel == "executor":
        approved = actual_confirm_handler("confirm_pending_confirmation", {}, None)
    else:
        from types import SimpleNamespace

        from app.era_live.tool_dispatch import ToolDispatchMixin

        era = SimpleNamespace(
            _last_user_text="",
            _inflight_user_text="",
            _inflight_started_at=0.0,
            _recent_user_texts=[(store.peek().created_at + 1, "yes")],
            _pending_store=store,
            speak=lambda *args: None,
            session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
            _pending_cm_ask_active=lambda: False,
            _cm_handoff_active=lambda: False,
        )

        async def raw(name, parameters, fc=None):
            if name == "confirm_pending_confirmation":
                return await ToolDispatchMixin._execute_tool_raw(era, name, parameters, fc)
            return ToolResult.ok("approved change finished")

        era._execute_tool_raw = raw
        approved = asyncio.run(
            ToolDispatchMixin._execute_tool_result(
                era, SimpleNamespace(name="confirm_pending_confirmation", args={})
            )
        )
    assert approved.success
    assert queue.get_status(tid)["status"] == "pending"
    run_queued(queue, tid)
    assert queue.get_status(tid)["status"] == "completed"
    assert queue.get_status(tid)["result"] == "3 steps complete"
    assert len(queue.get_all_statuses()) == 1
    assert planned == ["goal"]
    assert called == ["read", "risky", "tail"]


def test_approval_result_arriving_before_executor_return_is_not_lost(monkeypatch, workflow):
    from core.approval_execution import CURRENT_TASK_ID

    _, called = workflow
    store = PendingConfirmationStore()
    queue = TaskQueue(pending_store=store, executor_factory=ex.AgentExecutor)

    def tool(name, parameters, speak):
        called.append(name)
        if name == "risky":
            cid = store.store("risky", {}, "HIGH", "change")
            token = CURRENT_TASK_ID.set(None)  # user-facing channel has a separate context
            try:
                assert store.confirm(cid)
                assert queue.resolve_approval(cid, ToolResult.ok("approved change finished"))
            finally:
                CURRENT_TASK_ID.reset(token)
            return ToolResult.fail(
                "Approve?", error_type="ConfirmationRequired", confirmation_id=cid
            )
        return ToolResult.ok(f"{name} finished")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    tid = queue.submit("goal")
    run_queued(queue, tid)
    assert queue.get_status(tid)["status"] == "pending"
    run_queued(queue, tid)
    assert queue.get_status(tid)["status"] == "completed"
    assert called == ["read", "risky", "tail"]


def test_later_step_can_pause_again_without_replaying_earlier_steps(monkeypatch, workflow):
    planned, called = workflow

    def tool(name, parameters, speak):
        called.append(name)
        if name in ("risky", "tail"):
            return ToolResult.fail(
                "Approve?", error_type="ConfirmationRequired", confirmation_id=name
            )
        return ToolResult.ok("read finished")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    executor = ex.AgentExecutor()
    first = asyncio.run(executor.execute("goal"))
    second = asyncio.run(
        executor.execute(
            "goal",
            checkpoint=first._execution_checkpoint,
            approved_result=ToolResult.ok("approved change finished"),
        )
    )
    assert second.confirmation_id == "tail"
    completed = asyncio.run(
        executor.execute(
            "goal",
            checkpoint=second._execution_checkpoint,
            approved_result=ToolResult.ok("tail finished"),
        )
    )
    assert completed.success and completed.message == "3 steps complete"
    assert called == ["read", "risky", "tail"]
    assert planned == ["goal"]


def test_cancelled_continuation_runs_no_tools(monkeypatch, workflow):
    import threading

    _, called = workflow

    def tool(name, parameters, speak):
        called.append(name)
        return ToolResult.fail("Approve?", error_type="ConfirmationRequired")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    executor = ex.AgentExecutor()
    paused = asyncio.run(executor.execute("goal"))
    flag = threading.Event()
    flag.set()
    cancelled = asyncio.run(
        executor.execute(
            "goal",
            checkpoint=paused._execution_checkpoint,
            approved_result=ToolResult.ok("approved change finished"),
            cancel_flag=flag,
        )
    )
    assert cancelled.error_type == "Cancelled"
    assert called == ["read"]


def test_threaded_continuation_persists_ordered_events_without_replay(
    monkeypatch, workflow, tmp_path
):
    import threading

    from agent.task_store import TaskStore

    planned, called = workflow
    pending = PendingConfirmationStore()
    history = TaskStore(tmp_path / "tasks.sqlite3")
    awaiting, finished = threading.Event(), threading.Event()

    def event(payload):
        if payload["status"] == "awaiting_approval":
            awaiting.set()
        if payload["status"] == "completed":
            finished.set()

    queue = TaskQueue(
        pending_store=pending,
        store=history,
        executor_factory=ex.AgentExecutor,
        event_callback=event,
    )
    monkeypatch.setattr(ex, "_pending_store", pending)
    monkeypatch.setattr("agent.task_queue.get_queue", lambda: queue)
    monkeypatch.setattr("core.pending_confirmation._user_approval_probe", lambda record: True)
    confirm = ex._call_tool_result

    def tool(name, parameters, speak, **kwargs):
        if kwargs.get("_confirmed_once"):
            return ToolResult.ok("approved change finished")
        called.append(name)
        if name == "risky":
            cid = pending.store("risky", {}, "HIGH", "change")
            return ToolResult.fail(
                "Approve?", error_type="ConfirmationRequired", confirmation_id=cid
            )
        return ToolResult.ok(f"{name} finished")

    monkeypatch.setattr(ex, "_call_tool_result", tool)
    tid = queue.submit("goal")
    queue.start()
    try:
        assert awaiting.wait(5)
        assert confirm("confirm_pending_confirmation", {}, None).success
        assert finished.wait(5)
        events = history.get_events(tid)
        assert [row["status"] for row in events] == [
            "pending",
            "running",
            "awaiting_approval",
            "pending",
            "running",
            "completed",
        ]
        assert [row["seq"] for row in events] == list(range(1, 7))
        assert history.get_task(tid)["confirmation_id"] is None
        assert planned == ["goal"]
        assert called == ["read", "risky", "tail"]
    finally:
        pending.clear()
        queue.stop()
        queue._worker_thread.join(timeout=5)
        history.close()
