"""
Regression tests for agent/task_queue.py.

Primary guard: the `Task` class lost its @dataclass decorator, so `Task(...)`
hit object.__init__ and raised `TypeError: Task() takes no arguments` — meaning
`agent_task` could not queue anything at all (it blew up inside submit()). These
tests reconstruct that exact call path without starting the worker thread or a
real executor, so they run anywhere.

Run:  pytest test_task_queue.py -v
"""

import threading

import pytest

from agent.task_queue import Task, TaskQueue
from agent.types import TaskPriority, TaskStatus


# --------------------------------------------------------------------------- #
# 1. The direct regression: Task must be constructible by keyword (dataclass).
# --------------------------------------------------------------------------- #
def test_task_constructs_with_kwargs():
    t = Task(
        priority=TaskPriority.NORMAL.value,
        created_at=123.0,
        task_id="abc123",
        goal="prepare this week's content plan",
    )
    assert t.priority == TaskPriority.NORMAL.value
    assert t.task_id == "abc123"
    assert t.goal == "prepare this week's content plan"
    # Defaults applied by the dataclass.
    assert t.status is TaskStatus.PENDING
    assert t.result is None
    assert t.error == ""
    assert isinstance(t.cancel_flag, threading.Event)
    # Each instance gets its OWN Event (default_factory, not a shared default).
    other = Task(priority=1, created_at=1.0, task_id="x", goal="g")
    assert other.cancel_flag is not t.cancel_flag


# --------------------------------------------------------------------------- #
# 2. The reported failure site: submit() builds a Task and queues it. This is
#    the line (task_queue.py submit -> Task(...)) that raised the TypeError.
# --------------------------------------------------------------------------- #
def test_submit_queues_task_without_error():
    q = TaskQueue()  # constructor does NOT start the worker thread
    task_id = q.submit("prepare this week's content plan")

    assert isinstance(task_id, str) and task_id
    assert task_id in q._tasks
    queued = q._tasks[task_id]
    assert queued.goal == "prepare this week's content plan"
    assert queued.priority == TaskPriority.NORMAL.value  # default priority mapped
    assert queued.status is TaskStatus.PENDING
    assert q.pending_count() == 1


# --------------------------------------------------------------------------- #
# 3. Priority ordering still holds (HIGH=1 sorts ahead of LOW=3).
# --------------------------------------------------------------------------- #
def test_submit_orders_by_priority():
    q = TaskQueue()
    low_id = q.submit("low job", priority=TaskPriority.LOW)
    high_id = q.submit("high job", priority=TaskPriority.HIGH)

    assert [t.task_id for t in q._queue] == [high_id, low_id]


# --------------------------------------------------------------------------- #
# 4. eq=False guard: two same-priority tasks are distinct by identity, so
#    removing one from the queue leaves the other (a value-equality dataclass
#    would treat them as equal and could remove the wrong task).
# --------------------------------------------------------------------------- #
def test_same_priority_tasks_are_identity_distinct():
    q = TaskQueue()
    a = q.submit("job A", priority=TaskPriority.NORMAL)
    b = q.submit("job B", priority=TaskPriority.NORMAL)

    task_a = q._tasks[a]
    task_b = q._tasks[b]
    assert task_a != task_b  # identity equality, not value equality
    q._queue.remove(task_a)
    assert task_a not in q._queue
    assert task_b in q._queue  # the correct one survived
