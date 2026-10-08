import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.task_store import TaskStore
from agent.types import TaskPriority, TaskStatus
from core.tool_result import ToolResult


@dataclass(eq=False)
class Task:
    priority: int
    created_at: float = field(compare=False)
    task_id: str = field(compare=False)
    goal: str = field(compare=False)
    status: TaskStatus = field(compare=False, default=TaskStatus.PENDING)
    result: Any = field(compare=False, default=None)
    error: str = field(compare=False, default="")
    speak: Any = field(compare=False, default=None)
    on_complete: Any = field(compare=False, default=None)
    # The structured ToolResult the executor ended on. `result` keeps the
    # plain message for existing readers; this keeps the correlation an
    # awaiting-approval task needs (confirmation_id, error_type, data).
    outcome: Any = field(compare=False, default=None)
    error_type: str | None = field(compare=False, default=None)
    confirmation_id: str | None = field(compare=False, default=None)
    cancel_flag: threading.Event = field(compare=False, default_factory=threading.Event)


class TaskQueue:
    def __init__(
        self,
        max_concurrent: int = 1,
        executor_factory: Callable[[], Any] | None = None,
        store: TaskStore | None = None,
        event_callback: Callable[[dict], None] | None = None,
    ):
        self._queue: list[Task] = []
        self._lock: threading.Lock = threading.Lock()
        self._condition: threading.Condition = threading.Condition(self._lock)
        self._tasks: dict[str, Task] = {}
        self._running: bool = False
        self._worker_thread: threading.Thread | None = None
        self._max_concurrent = max_concurrent
        self._active_count = 0
        self._executor_factory = executor_factory
        self._executor = None
        self._store = store
        self._event_callback = event_callback
        if self._store:
            for row in self._store.recover_after_restart():
                task = Task(
                    priority=int(row["priority"]),
                    created_at=float(row["created_at"]),
                    task_id=row["task_id"],
                    goal=row["goal"],
                )
                self._tasks[task.task_id] = task
                self._queue.append(task)
            self._queue.sort(key=lambda t: (t.priority, t.created_at))

    def set_executor_factory(self, factory: Callable[[], Any]) -> None:
        self._executor_factory = factory

    def set_event_callback(self, callback: Callable[[dict], None] | None) -> None:
        self._event_callback = callback

    def _record_transition(
        self, task: Task, message: str = "", *, persist: bool = True, event_seq: int | None = None
    ) -> None:
        if self._store and persist:
            event_seq = self._store.update_task(
                task.task_id,
                task.status.value,
                result=task.result if isinstance(task.result, str) else None,
                error=task.error or None,
                error_type=task.error_type,
                confirmation_id=task.confirmation_id,
                message=message,
            )
        if self._event_callback:
            payload = {
                "task_id": task.task_id,
                "seq": event_seq,
                "goal": task.goal[:240],
                "status": task.status.value,
                "result": str(task.result or "")[:1000],
                "error": task.error[:1000],
                "error_type": task.error_type,
                "confirmation_id": task.confirmation_id,
                "updated_at": time.time(),
            }
            try:
                self._event_callback(payload)
            except Exception as e:
                print(f"[TaskQueue] ⚠️ status event callback error: {e}")

    def _get_executor(self):
        if self._executor is None:
            if self._executor_factory is None:
                raise RuntimeError(
                    "Executor factory not configured. "
                    "Call set_executor_factory() before submitting tasks."
                )
            self._executor = self._executor_factory()
        return self._executor

    def start(self) -> None:
        with self._condition:
            if self._running and self._worker_thread and self._worker_thread.is_alive():
                return
            self._running = True
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True, name="AgentTaskQueue"
            )
            self._worker_thread.start()
        print("[TaskQueue] ✅ Started")

    def stop(self) -> None:
        self._running = False
        with self._condition:
            self._condition.notify_all()
        print("[TaskQueue] 🔴 Stopped")

    def submit(
        self,
        goal: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        speak: Callable | None = None,
        on_complete: Callable | None = None,
    ) -> str:

        task_id = uuid.uuid4().hex
        task = Task(
            priority=priority.value,
            created_at=time.time(),
            task_id=task_id,
            goal=goal,
            speak=speak,
            on_complete=on_complete,
        )

        event_seq = None
        with self._condition:
            if self._store:
                event_seq = self._store.create_task(task_id, goal, priority.value, task.created_at)
            self._queue.append(task)
            self._queue.sort(key=lambda t: (t.priority, t.created_at))
            self._tasks[task_id] = task
            # Publish the pending transition before waking the worker, keeping
            # the live event order aligned with the persisted sequence numbers.
            self._record_transition(task, "Task queued", persist=False, event_seq=event_seq)
            self._condition.notify()

        print(f"[TaskQueue] 📥 Task queued: [{task_id}]")
        return task_id

    def cancel(self, task_id: str) -> bool:
        with self._condition:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return False
            cancel_confirmation = (
                task.confirmation_id if task.status == TaskStatus.AWAITING_APPROVAL else None
            )
            task.cancel_flag.set()
            task.status = TaskStatus.CANCELLED
            task.error = "Task cancelled by user."
            task.error_type = "Cancelled"
            try:
                self._queue.remove(task)
            except ValueError:
                pass
            self._condition.notify_all()
            print(f"[TaskQueue] 🚫 Task cancelled: [{task_id}]")
        if cancel_confirmation:
            try:
                from core.pending_confirmation import get_shared_store

                get_shared_store().cancel(cancel_confirmation)
            except Exception as e:
                print(f"[TaskQueue] ⚠️ pending confirmation cancellation failed: {e}")
        self._record_transition(task, task.error)
        return True

    def get_status(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return self._store.get_task(task_id) if self._store else None
            out = {
                "task_id": task.task_id,
                "goal": task.goal,
                "status": task.status.value,
                "result": task.result,
                "error": task.error,
                "error_type": task.error_type,
                "confirmation_id": task.confirmation_id,
            }
            return out

    def get_all_statuses(self) -> list[dict]:
        if self._store:
            return self._store.list_tasks()
        with self._lock:
            return [
                {
                    "task_id": t.task_id,
                    "goal": t.goal[:50],
                    "status": t.status.value,
                }
                for t in self._tasks.values()
            ]

    def get_task_events(self, task_id: str, limit: int = 100) -> list[dict]:
        return self._store.get_events(task_id, limit) if self._store else []

    def get_recent_task_events(self, limit: int = 100) -> list[dict]:
        return self._store.get_recent_events(limit) if self._store else []

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._queue if t.status == TaskStatus.PENDING)

    def _worker_loop(self) -> None:
        while self._running:
            task = None

            with self._condition:
                while self._running and not self._next_task():
                    self._condition.wait(timeout=1.0)
                task = self._next_task()
                if task:
                    task.status = TaskStatus.RUNNING
                    self._active_count += 1
                    try:
                        self._queue.remove(task)
                    except ValueError:
                        pass

            if task:
                self._record_transition(task, "Task started")
                threading.Thread(
                    target=self._run_task,
                    args=(task,),
                    daemon=True,
                    name=f"AgentTask-{task.task_id}",
                ).start()

    def _next_task(self) -> Task | None:
        if self._active_count >= self._max_concurrent:
            return None
        for task in self._queue:
            if task.status == TaskStatus.PENDING and not task.cancel_flag.is_set():
                return task
        return None

    def _run_task(self, task: Task) -> None:
        print(f"[TaskQueue] ▶️ Running: [{task.task_id}] {task.goal[:60]}")
        try:
            executor = self._get_executor()
            result = asyncio.run(
                executor.execute(
                    goal=task.goal,
                    speak=task.speak,
                    cancel_flag=task.cancel_flag,
                )
            )

            # A normal return is not evidence of success. Never infer the
            # outcome from prose (or treat None as "Done").
            if not isinstance(result, ToolResult):
                result = ToolResult.fail(
                    "Executor returned no structured outcome; completion could not be verified.",
                    error_type="InvalidOutcome",
                )
        except asyncio.CancelledError:
            result = ToolResult.fail("Task cancelled.", error_type="Cancelled")
        except Exception as e:
            result = ToolResult.fail(str(e) or type(e).__name__, error_type=type(e).__name__)

        with self._condition:
            if task.cancel_flag.is_set() or result.error_type == "Cancelled":
                result = ToolResult.fail("Task cancelled.", error_type="Cancelled")
                task.status = TaskStatus.CANCELLED
            elif result.error_type == "ConfirmationRequired":
                task.status = TaskStatus.AWAITING_APPROVAL
            elif result.success:
                task.status = TaskStatus.COMPLETED
            else:
                task.status = TaskStatus.FAILED
            task.result = result.message
            task.outcome = result
            task.error = (
                (result.error or result.message) if task.status == TaskStatus.FAILED else ""
            )
            task.error_type = result.error_type
            task.confirmation_id = result.confirmation_id
            self._active_count -= 1
            self._condition.notify()

        self._record_transition(task, result.message)
        # Persist the outcome before invoking fallible user-facing callbacks.
        # A broken speaker/callback must not change it or release the slot twice.
        report = f"Task {task.task_id} {task.status.value.replace('_', ' ')}: {result.message}"
        print(f"[TaskQueue] {report}")
        if task.speak:
            try:
                task.speak(report)
            except Exception as e:
                print(f"[TaskQueue] ⚠️ outcome notification error: {e}")
        if task.on_complete:
            try:
                task.on_complete(task.task_id, result)
            except Exception as e:
                print(f"[TaskQueue] ⚠️ on_complete callback error: {e}")


_queue = TaskQueue(store=TaskStore())


def get_queue() -> TaskQueue:
    return _queue
