import asyncio
import json
from types import SimpleNamespace

from agent.task_queue import TaskQueue
from agent.task_store import TaskStore
from app.hud.api import HudApi


def test_hud_task_endpoints_serve_persisted_status_and_ordered_events(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task("persisted", "prepare the brief", 2, 10.0)
    store.update_task("persisted", "running", updated_at=11.0)
    store.update_task("persisted", "completed", updated_at=12.0, result="Ready")
    queue = TaskQueue(store=store)
    monkeypatch.setattr("agent.task_queue.get_queue", lambda: queue)
    api = HudApi(SimpleNamespace())

    tasks_response = asyncio.run(api.tasks(SimpleNamespace(query={"limit": "10"})))
    events_response = asyncio.run(api.task_events(SimpleNamespace(query={"limit": "10"})))
    task_response = asyncio.run(api.task(SimpleNamespace(match_info={"task_id": "persisted"})))

    assert json.loads(tasks_response.text)["tasks"][0]["status"] == "completed"
    assert [event["status"] for event in json.loads(events_response.text)["events"]] == [
        "pending",
        "running",
        "completed",
    ]
    assert json.loads(task_response.text)["task"]["result"] == "Ready"
    store.close()
