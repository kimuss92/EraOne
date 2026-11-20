from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.task_queue import TaskQueue
from agent.types import TaskStatus
from core.pending_confirmation import PendingConfirmationStore
from core.tool_result import ToolResult


def test_lifecycle_callbacks_are_detached_and_can_read_store():
    store = PendingConfirmationStore()
    seen = []

    def first(event):
        store.peek()  # callbacks execute after releasing the store lock
        event["confirmation_id"] = "changed"

    store.subscribe(first)
    unsubscribe = store.subscribe(seen.append)
    cid = store.store("tool", {"path": "reviewed"}, "HIGH", "reason")
    assert store.confirm(cid).parameters == {"path": "reviewed"}
    assert [event["state"] for event in seen] == ["stored", "consumed"]
    assert [event["seq"] for event in seen] == [1, 2]
    assert all(event["confirmation_id"] == cid for event in seen)
    unsubscribe()
    store.store("tool", {}, "HIGH", "reason")
    assert len(seen) == 2


def test_throwing_listener_does_not_change_approval_decision():
    store = PendingConfirmationStore()

    def broken(event):
        raise RuntimeError("reporter unavailable")

    store.subscribe(broken)
    cid = store.store("tool", {}, "HIGH", "reason")
    assert store.confirm(cid).confirmation_id == cid
    assert not store.is_pending()


def awaiting_queue(store):
    queue = TaskQueue(pending_store=store)
    task_id = queue.submit("prepare report")
    cid = store.store("tool", {}, "HIGH", "reason")
    task = queue._tasks[task_id]
    task.status = TaskStatus.AWAITING_APPROVAL
    task.confirmation_id = cid
    return queue, task_id, cid


@pytest.mark.parametrize("state", ["cancelled", "expired", "replaced"])
def test_ended_approval_updates_job_and_releases_duplicate_guard(state):
    store = PendingConfirmationStore(ttl=-1 if state == "expired" else 60)
    queue, task_id, cid = awaiting_queue(store)
    if state == "expired":
        assert store.expire_all() == 1
    elif state == "cancelled":
        assert store.cancel(cid)
    else:
        store.store("other", {}, "HIGH", "other", replace=True)
    row = queue.get_status(task_id)
    assert row["status"] == ("cancelled" if state == "cancelled" else "failed")
    assert state in row["result"]
    assert queue.submit("prepare report") != task_id


def test_consumption_alone_does_not_mark_job_complete():
    store = PendingConfirmationStore()
    queue, task_id, cid = awaiting_queue(store)
    assert store.confirm(cid)
    assert queue.get_status(task_id)["status"] == "awaiting_approval"


@pytest.mark.parametrize("kind", ["Cancelled", "UserCancelled"])
def test_cancelled_approved_action_cancels_original_job(kind):
    store = PendingConfirmationStore()
    queue, task_id, cid = awaiting_queue(store)
    assert store.confirm(cid)
    assert queue.resolve_approval(cid, ToolResult.fail("Action cancelled.", error_type=kind))
    assert queue.get_status(task_id)["status"] == "cancelled"


def test_expiry_before_executor_returns_is_reconciled():
    store = PendingConfirmationStore(ttl=-1)

    class Executor:
        async def execute(self, **kwargs):
            cid = store.store("tool", {}, "HIGH", "reason")
            store.expire_all()  # job is still marked running here
            return ToolResult.fail(
                "Approve?", error_type="ConfirmationRequired", confirmation_id=cid
            )

    spoken = []
    queue = TaskQueue(pending_store=store, executor_factory=Executor)
    tid = queue.submit("prepare report", speak=spoken.append)
    task = queue._tasks[tid]
    queue._queue.remove(task)
    task.status = TaskStatus.RUNNING
    queue._active_count = 1
    queue._run_task(task)
    assert queue.get_status(tid)["error_type"] == "ApprovalExpired"
    assert len(spoken) == 1
    assert "expired" in spoken[0]


def test_concurrent_consumption_emits_one_consumed_event():
    store = PendingConfirmationStore()
    seen = []
    store.subscribe(seen.append)
    cid = store.store("tool", {}, "HIGH", "reason")
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: store.confirm(cid), range(32)))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert [event["state"] for event in seen] == ["stored", "consumed"]


def test_approval_deadline_updates_job_without_polling():
    import threading

    expired = threading.Event()
    store = PendingConfirmationStore(ttl=0.1, automatic_expiry=True)
    queue, task_id, _ = awaiting_queue(store)
    store.subscribe(lambda event: expired.set() if event["state"] == "expired" else None)
    try:
        assert expired.wait(3)
        assert queue.get_status(task_id)["error_type"] == "ApprovalExpired"
        assert queue.submit("prepare report") != task_id
    finally:
        store.clear()


def test_stale_expiry_timer_cannot_cancel_replacement():
    import threading

    expired = threading.Event()
    store = PendingConfirmationStore(ttl=0.05, automatic_expiry=True)
    first = store.store("first", {}, "HIGH", "reason")
    store._ttl = 60
    second = store.store("second", {}, "HIGH", "reason", replace=True)
    store.subscribe(lambda event: expired.set() if event["state"] == "expired" else None)
    try:
        assert not expired.wait(0.15)
        assert store.peek(second) is not None
        assert store.peek(first) is None
    finally:
        store.clear()


def test_reused_confirmation_id_still_resolves_only_its_owning_job(monkeypatch):
    from core.approval_execution import CURRENT_TASK_ID

    monkeypatch.setattr("core.pending_confirmation.secrets.token_hex", lambda size: "same-id")
    store = PendingConfirmationStore()
    queue = TaskQueue(pending_store=store)
    ids = []
    for goal in ("first goal", "second goal"):
        tid = queue.submit(goal)
        token = CURRENT_TASK_ID.set(tid)
        try:
            cid = store.store("tool", {}, "HIGH", "reason")
        finally:
            CURRENT_TASK_ID.reset(token)
        queue._tasks[tid].status = TaskStatus.AWAITING_APPROVAL
        queue._tasks[tid].confirmation_id = cid
        assert store.confirm(cid)
        ids.append(tid)
    assert queue.resolve_approval("same-id", ToolResult.fail("Second action failed"))
    assert queue.get_status(ids[0])["status"] == "awaiting_approval"
    assert queue.get_status(ids[1])["status"] == "failed"


def test_old_timer_cannot_reschedule_replacement_with_reused_id(monkeypatch):
    monkeypatch.setattr("core.pending_confirmation.secrets.token_hex", lambda size: "same-id")
    store = PendingConfirmationStore(ttl=60, automatic_expiry=True)
    store.store("first", {}, "HIGH", "reason")
    old_timer = store._expiry_timer
    store.store("second", {}, "HIGH", "reason", replace=True)
    replacement_timer = store._expiry_timer
    try:
        old_timer.function()  # reproduce an already-dispatched stale callback
        assert store._expiry_timer is replacement_timer
        assert store.peek().tool == "second"
    finally:
        store.clear()
        replacement_timer.cancel()
