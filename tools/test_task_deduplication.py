"""Exact active requests share a job; terminal jobs can be requested again."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.task_queue import TaskQueue
from agent.task_store import TaskStore
from agent.types import TaskStatus


def submit_or_existing(queue, goal):
    from agent.task_queue import DuplicateTaskError

    try:
        return queue.submit(goal)
    except DuplicateTaskError as exc:
        return exc.task_id


def test_concurrent_identical_submissions_create_one_job():
    queue = TaskQueue()
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: submit_or_existing(queue, "review project"), range(32)))
    assert len(set(ids)) == 1
    assert queue.pending_count() == 1


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL])
def test_active_job_reports_existing_id(status):
    from agent.task_queue import DuplicateTaskError

    queue = TaskQueue()
    task_id = queue.submit("review project")
    queue._tasks[task_id].status = status
    with pytest.raises(DuplicateTaskError) as caught:
        queue.submit(" review project ")
    assert caught.value.task_id == task_id
    assert status.value.replace("_", " ") in str(caught.value)


def test_cancelled_but_still_executing_job_blocks_duplicate():
    from agent.task_queue import DuplicateTaskError

    queue = TaskQueue()
    task_id = queue.submit("review project")
    queue._tasks[task_id].execution_active = True
    assert queue.cancel(task_id)
    with pytest.raises(DuplicateTaskError):
        queue.submit("review project")
    queue._tasks[task_id].execution_active = False
    assert queue.submit("review project") != task_id


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_terminal_jobs_can_be_requested_again(status):
    queue = TaskQueue()
    task_id = queue.submit("review project")
    queue._tasks[task_id].status = status
    assert queue.submit("review project") != task_id


def test_goal_identity_preserves_case_and_internal_whitespace():
    queue = TaskQueue()
    ids = [
        queue.submit(goal) for goal in ['print("A")', 'print("a")', 'print("a  b")', 'print("a b")']
    ]
    assert len(set(ids)) == 4


def test_recovered_pending_job_prevents_duplicate(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    try:
        first = TaskQueue(store=store).submit("review project")
        recovered = TaskQueue(store=store)
        assert submit_or_existing(recovered, "review project") == first
        assert len(store.list_tasks()) == 1
    finally:
        store.close()


@pytest.mark.parametrize("channel", ["live", "executor"])
def test_duplicate_is_reported_to_the_requesting_channel(monkeypatch, channel):
    import asyncio
    from types import SimpleNamespace

    queue = TaskQueue()
    first = queue.submit("review project")
    monkeypatch.setattr("agent.task_queue.get_queue", lambda: queue)
    if channel == "executor":
        from agent.executor import _call_tool_result

        result = _call_tool_result("agent_task", {"goal": "review project"}, None)
        text = result.message
    else:
        from app.era_live.tool_dispatch import ToolDispatchMixin

        era = SimpleNamespace(
            session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
            _pending_cm_ask_active=lambda: False,
            _cm_handoff_active=lambda: False,
            speak=lambda text: None,
        )
        text = asyncio.run(
            ToolDispatchMixin._execute_tool_raw(era, "agent_task", {"goal": "review project"})
        )
    assert first in text
    assert "already pending" in text
    assert "No duplicate was started" in text
    assert queue.pending_count() == 1


def test_cancellation_keeps_duplicate_guard_until_executor_returns():
    import threading

    from agent.task_queue import DuplicateTaskError
    from core.tool_result import ToolResult

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Executor:
        async def execute(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return ToolResult.ok("finished")

    queue = TaskQueue(executor_factory=Executor)
    first = queue.submit("review project", on_complete=lambda *_: finished.set())
    queue.start()
    try:
        assert entered.wait(5)
        assert queue.cancel(first)
        with pytest.raises(DuplicateTaskError, match="stopping"):
            queue.submit("review project")
        release.set()
        assert finished.wait(5)
        assert queue.get_status(first)["status"] == "cancelled"
        queue.stop()
        assert queue.submit("review project") != first
    finally:
        release.set()
        queue.stop()
        queue._worker_thread.join(timeout=5)


def test_stop_waking_idle_worker_does_not_launch_a_concurrent_submission(monkeypatch):
    import threading
    import time

    from agent.task_queue import Task

    queue = TaskQueue()
    waiting = threading.Event()
    original_wait = queue._condition.wait

    def wait(timeout=None):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(queue._condition, "wait", wait)
    queue.start()
    try:
        assert waiting.wait(5)
        # Reproduce stop followed by submission before the idle worker gets
        # its lock back. No side-effecting executor is configured.
        with queue._condition:
            queue._running = False
            task = Task(priority=2, created_at=time.time(), task_id="after-stop", goal="later")
            queue._tasks[task.task_id] = task
            queue._queue.append(task)
            queue._condition.notify_all()
        queue._worker_thread.join(timeout=5)
        assert not queue._worker_thread.is_alive()
        assert queue.get_status(task.task_id)["status"] == "pending"
    finally:
        queue.stop()
        queue._worker_thread.join(timeout=5)
