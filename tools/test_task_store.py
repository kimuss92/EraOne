from agent.task_queue import TaskQueue
from agent.task_store import TaskStore
from agent.types import TaskStatus


def test_task_and_ordered_status_events_survive_store_reopen(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = TaskStore(path)
    store.create_task("task-1", "prepare a meeting brief", 2, 10.0)
    store.update_task("task-1", "running", updated_at=11.0)
    store.update_task("task-1", "completed", updated_at=12.0, result="Brief ready")
    store.close()

    reopened = TaskStore(path)
    task = reopened.get_task("task-1")
    events = reopened.get_events("task-1")

    assert task["status"] == "completed"
    assert task["result"] == "Brief ready"
    assert [event["status"] for event in events] == ["pending", "running", "completed"]
    assert [event["seq"] for event in events] == [1, 2, 3]
    reopened.close()


def test_recovery_requeues_never_started_tasks_but_never_replays_running_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task("queued", "safe to start", 2, 10.0)
    store.create_task("running", "might have side effects", 2, 11.0)
    store.update_task("running", "running", updated_at=12.0)
    store.create_task("approval", "approval was pending", 2, 13.0)
    store.update_task(
        "approval", "awaiting_approval", updated_at=14.0, confirmation_id="old-confirmation"
    )

    recovered = store.recover_after_restart()

    assert [row["task_id"] for row in recovered] == ["queued"]
    assert store.get_task("running")["status"] == "failed"
    assert store.get_task("running")["error_type"] == "InterruptedByRestart"
    assert store.get_task("approval")["status"] == "failed"
    assert store.get_task("approval")["error_type"] == "ApprovalLostOnRestart"
    store.close()


def test_task_queue_does_not_replay_work_interrupted_during_execution(tmp_path):
    import threading

    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task("unsafe-replay", "might send an email", 2, 10.0)
    store.update_task("unsafe-replay", "running", updated_at=11.0)
    calls = []

    class Executor:
        async def execute(self, **kwargs):
            calls.append(kwargs)

    queue = TaskQueue(executor_factory=lambda: Executor(), store=store)
    queue.start()
    try:
        threading.Event().wait(0.05)
        status = queue.get_status("unsafe-replay")
        assert calls == []
        assert status["status"] == TaskStatus.FAILED.value
        assert status["error_type"] == "InterruptedByRestart"
        assert "not replayed" in status["error"]
    finally:
        queue.stop()
        if queue._worker_thread:
            queue._worker_thread.join(timeout=2)
        store.close()


def test_cancelled_pending_task_is_persisted_and_removed_from_queue(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    queue = TaskQueue(store=store)
    task_id = queue.submit("do not run this")

    assert queue.cancel(task_id)
    assert not queue.cancel(task_id)
    assert queue.pending_count() == 0
    assert store.get_task(task_id)["status"] == TaskStatus.CANCELLED.value
    assert [event["status"] for event in store.get_events(task_id)] == ["pending", "cancelled"]
    store.close()


def test_cancelled_approval_task_disarms_the_pending_confirmation(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    queue = TaskQueue(store=store)
    task_id = queue.submit("shutdown")
    task = queue._tasks[task_id]
    task.status = TaskStatus.AWAITING_APPROVAL
    task.confirmation_id = "confirm-me"
    store.update_task(task_id, "awaiting_approval", confirmation_id="confirm-me")
    cancelled = []

    class PendingStore:
        def cancel(self, confirmation_id):
            cancelled.append(confirmation_id)
            return True

    monkeypatch.setattr("core.pending_confirmation.get_shared_store", lambda: PendingStore())

    assert queue.cancel(task_id)
    assert cancelled == ["confirm-me"]
    assert store.get_task(task_id)["status"] == TaskStatus.CANCELLED.value
    store.close()


def test_task_queue_restores_pending_task_with_original_identity(tmp_path):
    import threading

    from core.tool_result import ToolResult

    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task("persisted-id", "prepare report", 2, 10.0)
    done = threading.Event()

    class Executor:
        async def execute(self, **kwargs):
            done.set()
            return ToolResult.ok("Resumed")

    queue = TaskQueue(executor_factory=lambda: Executor(), store=store)
    assert queue.pending_count() == 1
    assert queue.get_status("persisted-id")["status"] == TaskStatus.PENDING.value
    assert queue.get_status("persisted-id")["goal"] == "prepare report"
    queue.start()
    try:
        assert done.wait(3), "persisted pending task did not resume"
        for _ in range(30):
            if queue.get_status("persisted-id")["status"] == TaskStatus.COMPLETED.value:
                break
            threading.Event().wait(0.01)
        assert queue.get_status("persisted-id")["status"] == TaskStatus.COMPLETED.value
        assert [event["status"] for event in store.get_events("persisted-id")] == [
            "pending",
            "running",
            "completed",
        ]
    finally:
        queue.stop()
        if queue._worker_thread:
            queue._worker_thread.join(timeout=2)
        store.close()


def test_task_queue_persists_and_publishes_each_lifecycle_transition(tmp_path):
    import threading

    from core.tool_result import ToolResult

    store = TaskStore(tmp_path / "tasks.sqlite3")
    done = threading.Event()
    published = []

    class Executor:
        async def execute(self, **kwargs):
            return ToolResult.ok("Brief ready")

    def publish(event):
        published.append(event)
        if event["status"] == "completed":
            done.set()

    queue = TaskQueue(executor_factory=lambda: Executor(), store=store, event_callback=publish)
    queue.start()
    try:
        task_id = queue.submit("prepare report")
        assert done.wait(3), "task worker did not finish in time"
        events = store.get_events(task_id)
        assert [event["status"] for event in events] == ["pending", "running", "completed"]
        assert [event["seq"] for event in published] == [1, 2, 3]
        assert queue.get_status(task_id)["status"] == "completed"
        assert store.get_task(task_id)["result"] == "Brief ready"
    finally:
        queue.stop()
        if queue._worker_thread:
            queue._worker_thread.join(timeout=2)
        store.close()
