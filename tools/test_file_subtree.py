"""Nested folder metadata follows move/delete events without filesystem scans."""

import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from actions.file_indexer import FileIndex
from tools.file_fixture import rename_fixture


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    old = root / "folder_%_old"
    nested = old / "nested"
    nested.mkdir(parents=True)
    note = nested / "report.txt"
    note.write_text("fixture")
    sibling = root / "folder_%_older"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("fixture")
    index = FileIndex(db_path=tmp_path / "index.db")
    index.rebuild(roots=[root], wait=True)
    index.record_open(str(note))
    yield index, old, sibling
    index.close()


def event(kind, path, destination=""):
    return SimpleNamespace(
        event_type=kind, src_path=str(path), dest_path=str(destination), is_directory=True
    )


def paths(index, table="files"):
    return {r["path"] for r in index._get_conn().execute(f"SELECT path FROM {table}")}


def test_nested_rename_updates_search_fts_and_open_history(indexed, monkeypatch):
    index, old, sibling = indexed
    new = old.with_name("renamed")
    rename_fixture(old, new)
    monkeypatch.setattr(os, "walk", lambda *a, **kw: pytest.fail("unexpected recursive scan"))
    index._handle_watch_event(event("moved", old, new))
    assert str(new / "nested" / "report.txt") in paths(index)
    assert not any(p == str(old) or p.startswith(str(old) + os.sep) for p in paths(index))
    assert str(sibling / "keep.txt") in paths(index)
    assert paths(index, "opened_paths") == {str(new / "nested" / "report.txt")}
    assert index.get_open_count(str(new / "nested" / "report.txt")) == 1
    found = index.search("report.txt")["candidates"]
    assert found and all(c["path"] == str(new / "nested" / "report.txt") for c in found)
    assert (
        index._get_conn()
        .execute("SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH 'renamed'")
        .fetchone()[0]
        > 0
    )
    assert (
        index._get_conn()
        .execute("SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH 'old'")
        .fetchone()[0]
        == 0
    )
    index._get_conn().execute("INSERT INTO files_fts(files_fts, rank) VALUES('integrity-check', 1)")
    index._get_conn().commit()


def test_delete_prunes_only_exact_subtree_and_history_without_touching_disk(indexed):
    index, old, sibling = indexed
    index._handle_watch_event(event("deleted", old))
    assert paths(index) == {str(sibling), str(sibling / "keep.txt")}
    assert paths(index, "opened_paths") == set()
    assert (old / "nested" / "report.txt").read_text() == "fixture"
    assert not index.search("report.txt")["candidates"]


def test_move_into_ignored_directory_prunes_all_descendants(indexed):
    index, old, sibling = indexed
    index._handle_watch_event(event("moved", old, old.parent / "node_modules" / "ignored"))
    assert paths(index) == {str(sibling), str(sibling / "keep.txt")}
    assert not paths(index, "opened_paths")


def test_windows_deleted_directory_file_flag_uses_known_index_type(indexed):
    index, old, sibling = indexed
    observed = []
    index.subscribe_changes(observed.append)
    index._handle_watch_event(
        SimpleNamespace(event_type="deleted", src_path=str(old), dest_path="", is_directory=False)
    )
    assert paths(index) == {str(sibling), str(sibling / "keep.txt")}
    assert not paths(index, "opened_paths")
    assert observed[0]["isDirectory"] is True


def test_duplicate_directory_move_and_child_events_are_idempotent(indexed):
    index, old, sibling = indexed
    new = old.with_name("renamed")
    rename_fixture(old, new)
    for _ in range(2):
        index._handle_watch_event(event("moved", old, new))
    index._handle_watch_event(
        SimpleNamespace(
            event_type="moved",
            is_directory=False,
            src_path=str(old / "nested" / "report.txt"),
            dest_path=str(new / "nested" / "report.txt"),
        )
    )
    assert len(paths(index)) == 5
    assert paths(index, "opened_paths") == {str(new / "nested" / "report.txt")}


def test_directory_creation_adds_folder_entry(tmp_path):
    index = FileIndex(db_path=tmp_path / "index.db")
    folder = tmp_path / "created"
    folder.mkdir()
    try:
        index._handle_watch_event(event("created", folder))
        assert str(folder) in paths(index)
    finally:
        index.close()


def test_case_only_folder_move_preserves_descendants_and_history(indexed):
    index, old, _ = indexed
    new = old.with_name(old.name.upper())
    index._incremental_subtree(str(old), str(new))
    assert str(new / "nested" / "report.txt") in paths(index)
    assert paths(index, "opened_paths") == {str(new / "nested" / "report.txt")}


def test_move_collision_keeps_fts_consistent(indexed):
    index, old, _ = indexed
    new = old.with_name("new")
    (new / "nested").mkdir(parents=True)
    (new / "nested" / "report.txt").write_text("fixture")
    index._incremental_add(str(new / "nested" / "report.txt"))
    index._incremental_subtree(str(old), str(new))
    assert len(index.search("report.txt")["candidates"]) == 1
    index._get_conn().execute("INSERT INTO files_fts(files_fts, rank) VALUES('integrity-check', 1)")
    index._get_conn().commit()


def test_failed_subtree_repair_rolls_back_both_tables(indexed):
    index, old, _ = indexed
    before, history = paths(index), paths(index, "opened_paths")
    conn = index._get_conn()
    conn.execute(
        "CREATE TRIGGER deny_cache_delete BEFORE DELETE ON opened_paths BEGIN SELECT RAISE(ABORT, 'fixture'); END"
    )
    conn.commit()
    index._incremental_subtree(str(old))
    assert paths(index) == before and paths(index, "opened_paths") == history


def test_overlapping_build_roots_keep_full_text_index_consistent(tmp_path):
    root = tmp_path / "watched"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "report.txt").write_text("fixture")
    index = FileIndex(db_path=tmp_path / "index.db")
    try:
        index.rebuild(roots=[root, nested], wait=True)
        index._get_conn().execute(
            "INSERT INTO files_fts(files_fts, rank) VALUES('integrity-check', 1)"
        )
        index._get_conn().commit()
    finally:
        index.close()


def test_native_watcher_nested_rename_and_delete_in_disposable_temp(indexed):
    index, old, sibling = indexed
    root = old.parent
    # Every actual filesystem mutation is confined to pytest's TEMP fixture.
    assert root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    observed = []
    index.subscribe_changes(observed.append)

    def wait_for(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        pytest.fail(
            f"native watcher timeout: files={paths(index)}, history={paths(index, 'opened_paths')}, events={observed}"
        )

    index.start_watcher(roots=[root])
    assert index.watcher_status()["running"]
    marker = root / "observer-ready.txt"
    marker.write_text("fixture")
    wait_for(lambda: str(marker) in paths(index))
    new = root / "renamed"
    rename_fixture(old, new)
    wait_for(
        lambda: (
            str(new / "nested" / "report.txt") in paths(index)
            and paths(index, "opened_paths") == {str(new / "nested" / "report.txt")}
        )
    )
    assert not any(p.startswith(str(old) + os.sep) for p in paths(index))
    assert new.resolve().is_relative_to(root.resolve()) and new != root
    shutil.rmtree(new)
    wait_for(
        lambda: (
            not any(p == str(new) or p.startswith(str(new) + os.sep) for p in paths(index))
            and not paths(index, "opened_paths")
        )
    )
    assert (sibling / "keep.txt").read_text() == "fixture"
    index._get_conn().execute("INSERT INTO files_fts(files_fts, rank) VALUES('integrity-check', 1)")
    index._get_conn().commit()
    index.close()
    assert not index.watcher_status()["running"]
