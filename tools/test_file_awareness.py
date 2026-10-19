"""File events reuse the index watcher and never become content or instructions."""

import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from actions.file_indexer import FileIndex
from core.file_awareness import MAX_PENDING, MAX_RECENT, FileAwareness


class Source:
    def __init__(self, root):
        self.status = {"running": True, "roots": [str(root)], "failedRoots": []}
        self.listeners = set()

    def watcher_status(self):
        return deepcopy(self.status)

    def subscribe_changes(self, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)


def test_reasoning_summary_bounds_large_scope_without_mutating_hud(monkeypatch):
    import core.file_awareness as module

    long_path = "C:/" + "x" * 4000
    original = {
        "roots": [long_path] * 128,
        "failedRoots": [long_path] * 128,
        "changes": [{"path": long_path, "destination": long_path}] * MAX_RECENT,
    }
    monkeypatch.setattr(
        module, "get_file_awareness", lambda: SimpleNamespace(snapshot=lambda: deepcopy(original))
    )
    result = module.file_summary()
    assert len(result["roots"]) == len(result["failedRoots"]) == 4
    assert len(result["changes"]) == 6 and result["unlistedChanges"] == 58
    assert result["unlistedFailedRoots"] == result["unlistedRoots"] == 124
    assert result["pathHintsTruncated"] and len(json.dumps(result)) < 6000
    assert len(original["changes"][0]["path"]) == 4003


def event(path, kind="created", destination=None):
    return {
        "kind": kind,
        "path": str(path),
        "destination": str(destination) if destination else None,
        "isDirectory": False,
        "observedAt": 0.0,
    }


def test_create_move_delete_observed_without_reading_contents(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("private content", encoding="utf-8")
    service = FileAwareness(source=Source(tmp_path))
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("no content read")))
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("no content read")))
    service.enqueue(event(a))
    service.refresh()
    assert service.snapshot()["changes"][0]["source"]["sizeBytes"] == 15
    assert "private content" not in json.dumps(service.snapshot())
    a.rename(b)
    service.enqueue(event(a, "moved", b))
    service.refresh()
    row = service.snapshot()["changes"][0]
    assert row["source"]["state"] == "missing" and row["target"]["state"] == "exists"
    b.unlink()
    service.enqueue(event(b, "deleted"))
    service.refresh()
    assert service.snapshot()["changes"][-1]["source"]["state"] == "missing"


def test_reconciles_recent_metadata_when_an_event_was_missed(tmp_path):
    a = tmp_path / "a.txt"
    a.write_bytes(b"a")
    service = FileAwareness(source=Source(tmp_path))
    service.enqueue(event(a))
    service.refresh()
    a.write_bytes(b"abcdef")
    service.refresh()
    row = service.snapshot()["changes"][0]
    assert row["source"]["sizeBytes"] == 6 and row["kind"] == "reconciled"


def test_heartbeat_has_no_fake_file_change_and_views_are_detached(tmp_path):
    published, now = [], [100.0]
    service = FileAwareness(
        source=Source(tmp_path),
        clock=lambda: now[0],
        publisher=lambda view, **kw: published.append(kw["changed"]),
    )
    service.enqueue(event(tmp_path / "missing.txt"))
    service.refresh()
    now[0] += 15
    service.refresh()
    assert published == [True, False]
    service.snapshot()["changes"].clear()
    assert service.snapshot()["changes"]
    now[0] += 40
    assert service.snapshot()["health"] == "stale" and not service.snapshot()["changes"]


def test_queue_overflow_and_retention_are_bounded_and_explicit(tmp_path):
    service = FileAwareness(source=Source(tmp_path))
    for i in range(MAX_PENDING + 3):
        service.enqueue(event(tmp_path / f"{i}.txt"))
    assert len(service._pending) == MAX_PENDING
    service.refresh()
    view = service.snapshot()
    assert view["droppedEvents"] == 3 and view["health"] == "degraded"
    assert len(view["changes"]) == MAX_RECENT


def test_coalescing_retains_latest_evidence_without_hiding_count(tmp_path):
    service = FileAwareness(source=Source(tmp_path))
    service.enqueue(event(tmp_path / "a.txt"))
    service.enqueue(event(tmp_path / "a.txt", "deleted"))
    service.refresh()
    view = service.snapshot()
    assert view["coalescedEvents"] == 1 and view["changes"][0]["kind"] == "deleted"


def test_background_churn_cannot_evict_preferred_user_folder_events(tmp_path):
    preferred = tmp_path / "documents"
    preferred.mkdir()
    source = Source(preferred)
    source.status["roots"].append(tmp_path.anchor)
    service = FileAwareness(source=source)
    service.refresh()
    for i in range(MAX_PENDING):
        service.enqueue(event(preferred / f"{i}.txt"))
    service.enqueue(event(tmp_path / "background.txt"))
    service.refresh()
    view = service.snapshot()
    assert view["droppedEvents"] == 1
    assert all(Path(c["path"]).parent == preferred for c in view["changes"])


@pytest.mark.parametrize("mutation", ["nul", "content", "kind"])
def test_malformed_or_content_carrying_events_are_rejected(tmp_path, mutation):
    service = FileAwareness(source=Source(tmp_path))
    data = event(tmp_path / "a.txt")
    if mutation == "nul":
        data["path"] += "\0"
    elif mutation == "content":
        data["content"] = "must never enter awareness"
    else:
        data["kind"] = "execute"
    with pytest.raises(ValueError):
        service.enqueue(data)
    assert not service._pending


def test_dead_native_emitter_is_not_reported_as_live_coverage(tmp_path):
    index = FileIndex(db_path=tmp_path / "index.db")
    index._observer = SimpleNamespace(
        is_alive=lambda: True, emitters=[SimpleNamespace(is_alive=lambda: False)]
    )
    assert not index.watcher_status()["running"]
    index._observer = None
    index.close()


@pytest.mark.parametrize("kind", ["outside", "runtime", "generated", "relative"])
def test_out_of_scope_and_runtime_events_are_not_published(tmp_path, kind):
    root = tmp_path / "watched"
    root.mkdir()
    runtime = root / "logs"
    service = FileAwareness(source=Source(root), excluded_roots=[runtime])
    paths = {
        "outside": tmp_path / "outside.txt",
        "runtime": runtime / "era.log",
        "generated": root / "node_modules" / "a.js",
        "relative": Path("relative.txt"),
    }
    service.enqueue(event(paths[kind]))
    service.refresh()
    assert not service.snapshot()["changes"]


def test_move_across_scope_boundary_redacts_external_path(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    service = FileAwareness(source=Source(root))
    service.enqueue(event(root / "a.txt", "moved", tmp_path / "outside.txt"))
    service.refresh()
    row = service.snapshot()["changes"][0]
    assert row["kind"] == "deleted" and row["destination"] is None


def test_partial_roots_and_read_failures_are_never_ready(tmp_path, monkeypatch):
    source = Source(tmp_path)
    source.status["failedRoots"] = [str(tmp_path / "denied")]
    service = FileAwareness(source=source)
    service.refresh()
    assert service.snapshot()["health"] == "degraded"
    source.status["failedRoots"] = []
    service.enqueue(event(tmp_path / "a.txt"))
    monkeypatch.setattr(
        service, "_metadata", lambda p: {"state": "unknown", "sizeBytes": None, "modifiedAt": None}
    )
    service.refresh()
    assert service.snapshot()["health"] == "degraded"
    source.status["running"] = False
    service.refresh()
    assert service.snapshot()["health"] == "unavailable" and not service.snapshot()["changes"]


def test_backend_lifecycle_has_one_subscription_and_releases_it(tmp_path):
    source, published = Source(tmp_path), threading.Event()
    service = FileAwareness(source=source, publisher=lambda *a, **kw: published.set())
    assert service.start()
    assert published.wait(2)
    assert not service.start() and len(source.listeners) == 1
    assert service.stop() and not source.listeners
    assert service.snapshot()["health"] == "unavailable"


def test_unavailable_roots_can_recover_without_a_successful_root(tmp_path):
    source, restarted = Source(tmp_path), threading.Event()
    source.status.update(running=False, roots=[], failedRoots=[str(tmp_path)])

    def restart(roots):
        assert roots == [tmp_path]
        source.status.update(running=True, roots=[str(tmp_path)], failedRoots=[])
        restarted.set()

    source.start_watcher = restart
    service = FileAwareness(source=source)
    assert service.start()
    try:
        assert restarted.wait(2)
    finally:
        assert service.stop()


def test_close_stops_observer_after_partial_start_failure(tmp_path):
    index = FileIndex(db_path=tmp_path / "index.db")
    observer = Mock(ident=None)
    observer.is_alive.return_value = False
    index._observer = observer
    index._watcher_started = False
    index.close()
    observer.stop.assert_called_once()
    observer.join.assert_not_called()
    assert index._conn is None


def test_index_database_writes_cannot_feed_back_into_the_index(tmp_path, monkeypatch):
    index = FileIndex(db_path=tmp_path / "index.db")
    add, update = Mock(), Mock()
    monkeypatch.setattr(index, "_incremental_add", add)
    monkeypatch.setattr(index, "_incremental_update", update)
    events = []
    index.subscribe_changes(events.append)
    for suffix in ("", "-wal", "-shm", "-journal"):
        index._handle_watch_event(
            SimpleNamespace(
                event_type="modified", src_path=str(index._db_path) + suffix, is_directory=False
            )
        )
    add.assert_not_called()
    update.assert_not_called()
    assert not events
    index.close()


def test_index_events_still_update_search_and_detach_subscribers(tmp_path):
    index = FileIndex(db_path=tmp_path / "index.db")
    events = []
    unsubscribe = index.subscribe_changes(events.append)
    a = tmp_path / "report.txt"
    a.write_text("hello")
    index._handle_watch_event(
        SimpleNamespace(event_type="created", src_path=str(a), is_directory=False)
    )
    assert index.file_count() == 1 and events[0]["kind"] == "created"
    unsubscribe()
    a.unlink()
    index._handle_watch_event(
        SimpleNamespace(event_type="deleted", src_path=str(a), is_directory=False)
    )
    assert index.file_count() == 0 and len(events) == 1
    index.close()


def test_file_bus_observation_does_not_change_conversation_state():
    from app.hud.events import HudBus

    bus = HudBus()
    before = bus.state()
    observation = {"revision": 1, "health": "ready", "changes": [], "roots": []}
    bus.files_observed(observation)
    observation["health"] = "wrong"
    assert bus.snapshot()["fileObservation"]["health"] == "ready"
    assert bus.state() == before
