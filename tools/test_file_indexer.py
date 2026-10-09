import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _safe_path_mock(*args, **kwargs):
    return True


class TestFileIndexSchema:
    """Tests for SQLite FTS5 schema creation and basic operations."""

    def test_creates_db_file(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()
            assert db_path.exists()
            idx.close()

    def test_schema_has_all_tables(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            conn = idx._get_conn()
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "files" in tables
            assert "files_fts" in tables
            assert "opened_paths" in tables
            assert "config" in tables
            idx.close()

    def test_wal_mode_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            conn = idx._get_conn()
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
            idx.close()

    def test_schema_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()
            # Second call should not raise
            idx._init_schema()
            idx.close()


class TestIndexBuild:
    """Tests for background index building."""

    def test_build_populates_files_table(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "test.txt").write_text("hello")
            (root / "sub").mkdir()
            (root / "sub" / "nested.txt").write_text("world")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            count = idx.file_count()
            assert count >= 2
            idx.close()

    def test_build_populates_fts_table(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            conn = idx._get_conn()
            fts_count = conn.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0]
            assert fts_count >= 1
            idx.close()

    def test_build_skips_dirs_in_SKIP_DIRS(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            (root / ".git" / "config.txt").write_text("skip me")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "pkg.txt").write_text("skip me")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "cache.txt").write_text("skip me")
            (root / "visible.txt").write_text("find me")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            conn = idx._get_conn()
            names = [r[0] for r in conn.execute("SELECT name FROM files").fetchall()]
            assert "visible.txt" in names
            assert "config.txt" not in names
            assert "pkg.txt" not in names
            assert "cache.txt" not in names
            idx.close()

    def test_build_respects_max_depth(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Create deeply nested structure
            deep = root
            for i in range(10):
                deep = deep / f"level{i}"
                deep.mkdir()
            (deep / "deep.txt").write_text("too deep")

            (root / "shallow.txt").write_text("visible")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            conn = idx._get_conn()
            paths = [r[0] for r in conn.execute("SELECT path FROM files").fetchall()]
            assert any("shallow.txt" in p for p in paths)
            idx.close()

    def test_build_records_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "file.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            last = idx.last_indexed_at()
            assert last > 0
            assert time.time() - last < 10
            idx.close()

    def test_build_clears_old_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "old.txt").write_text("old")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)
            assert idx.file_count() >= 1

            # Rebuild with different files
            (root / "old.txt").unlink()
            (root / "new.txt").write_text("new")
            idx.rebuild(roots=[root], wait=True)

            conn = idx._get_conn()
            names = [r[0] for r in conn.execute("SELECT name FROM files").fetchall()]
            assert "new.txt" in names
            assert "old.txt" not in names
            idx.close()

    def test_double_rebuild_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)
            # Second rebuild should be ignored (already building or done)
            idx.rebuild(roots=[root], wait=True)  # Should not hang or error
            idx.close()

    def test_empty_roots_builds_empty_index(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            # Pass empty list (not None) so _get_search_roots is not called
            idx._roots = []
            idx.rebuild(roots=[], wait=True)
            assert idx.file_count() == 0
            idx.close()


class TestFTSSearch:
    """Tests for FTS5 full-text search."""

    def test_exact_name_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report.txt")
            assert len(result["candidates"]) >= 1
            assert result["candidates"][0]["name"] == "report.txt"
            idx.close()

    def test_prefix_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "annual_report_2026.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("annual")
            assert len(result["candidates"]) >= 1
            assert any("annual" in c["name"].lower() for c in result["candidates"])
            idx.close()

    def test_partial_name_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "my_report_final.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report")
            assert len(result["candidates"]) >= 1
            idx.close()

    def test_search_empty_query(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[Path(d)], wait=True)

            result = idx.search("")
            assert result["candidates"] == []
            idx.close()

    def test_search_no_results(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("zzzzzzz_nonexistent_xyz")
            assert result["candidates"] == []
            idx.close()

    def test_search_not_ready_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            # Don't build — index is not ready
            result = idx.search("anything")
            assert result["candidates"] == []
            idx.close()

    def test_max_results_limit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(50):
                (root / f"report_{i:02d}.txt").write_text(str(i))

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report", max_results=5)
            assert len(result["candidates"]) <= 5
            idx.close()


class TestFileTypeFilter:
    """Tests for item_type and extension filters."""

    def test_file_type_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")
            (root / "reports").mkdir()

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report", item_type="file")
            types = {c["type"] for c in result["candidates"]}
            assert "folder" not in types
            idx.close()

    def test_folder_type_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")
            (root / "reports").mkdir()

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report", item_type="folder")
            types = {c["type"] for c in result["candidates"]}
            assert "file" not in types
            idx.close()

    def test_extension_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.pdf").write_text("pdf")
            (root / "report.txt").write_text("txt")
            (root / "report.docx").write_text("docx")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report", extension=".pdf")
            exts = {Path(c["path"]).suffix for c in result["candidates"]}
            assert exts == {".pdf"}
            idx.close()


class TestScoring:
    """Tests for scoring and ranking."""

    def test_results_sorted_by_score(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("exact")
            (root / "my_report_final.txt").write_text("contains")
            (root / "report_archive.zip").write_text("also contains")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("report")
            scores = [c["score"] for c in result["candidates"]]
            assert scores == sorted(scores, reverse=True)
            idx.close()

    def test_candidate_has_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "test.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            result = idx.search("test")
            assert len(result["candidates"]) >= 1
            c = result["candidates"][0]
            assert "name" in c
            assert "path" in c
            assert "type" in c
            assert "score" in c
            assert "match_type" in c
            assert "modified" in c
            assert c["type"] in ("file", "folder")
            idx.close()


class TestRecordOpen:
    """Tests for opened path tracking."""

    def test_record_open(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()  # init

            idx.record_open("/some/path.txt")
            count = idx.get_open_count("/some/path.txt")
            assert count == 1
            idx.close()

    def test_record_open_increments(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()

            idx.record_open("/some/path.txt")
            idx.record_open("/some/path.txt")
            idx.record_open("/some/path.txt")
            count = idx.get_open_count("/some/path.txt")
            assert count == 3
            idx.close()

    def test_get_open_count_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()

            count = idx.get_open_count("/nonexistent.txt")
            assert count == 0
            idx.close()


class TestIndexStatus:
    """Tests for index status reporting."""

    def test_file_count(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.txt").write_text("a")
            (root / "b.txt").write_text("b")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            assert idx.file_count() >= 2
            idx.close()

    def test_is_ready_after_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            assert not idx.is_ready
            idx.rebuild(roots=[root], wait=True)
            assert idx.is_ready
            idx.close()

    def test_is_building_during_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Create enough files to slow down the build
            for i in range(200):
                (root / f"file_{i:03d}.txt").write_text(str(i))

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            # Start build without waiting
            idx.rebuild(roots=[root], wait=False)
            # Should be building (or finished if very fast)
            # Just verify it doesn't hang
            time.sleep(0.5)
            idx.close()

    def test_close_during_build_waits_for_the_worker(self, tmp_path, monkeypatch):
        """close() must not close the shared connection under a live build.

        The connection is check_same_thread=False, so conn.close() from the
        test thread while _build_worker is mid-execute is a native access
        violation, not an exception — it once took the whole pytest process
        down. close() has to wait for the build instead of racing it.
        """
        import threading

        import actions.file_indexer as fi

        gate = threading.Event()
        entered = threading.Event()
        real_flush = fi.FileIndex._flush_batch

        def slow_flush(self, conn, batch):
            if not entered.is_set():
                entered.set()
                gate.wait(timeout=10)
            real_flush(self, conn, batch)

        monkeypatch.setattr(fi.FileIndex, "_flush_batch", slow_flush)
        (tmp_path / "a.txt").write_text("a")
        idx = fi.FileIndex(db_path=tmp_path / "index.db")
        idx.rebuild(roots=[tmp_path], wait=False)
        assert entered.wait(timeout=10), "build never reached a flush"

        closer = threading.Thread(target=idx.close)
        closer.start()
        closer.join(timeout=0.3)
        assert closer.is_alive(), "close() returned while the build was mid-flush"

        gate.set()
        closer.join(timeout=10)
        assert not closer.is_alive(), "close() hung on the in-flight build"
        # Everything closed: the database file is deletable (no handle left).
        (tmp_path / "index.db").unlink()

    def test_a_late_build_cannot_reopen_the_database(self, tmp_path, monkeypatch):
        """A worker that starts late must not re-open the DB after close().

        _get_conn() lazily re-creates self._conn, so a build still starting
        when close() ran used to open a fresh handle nobody ever closed — the
        file stayed locked and temp-dir cleanup died with WinError 32.
        """
        import threading

        import actions.file_indexer as fi

        gate = threading.Event()
        started = threading.Event()
        real_connect = fi.sqlite3.connect

        def gated_connect(*a, **k):
            conn = real_connect(*a, **k)
            if threading.current_thread() is not threading.main_thread():
                started.set()
                gate.wait(timeout=10)
            return conn

        monkeypatch.setattr(fi.sqlite3, "connect", gated_connect)
        (tmp_path / "a.txt").write_text("a")
        idx = fi.FileIndex(db_path=tmp_path / "index.db")
        idx.rebuild(roots=[tmp_path], wait=False)
        assert started.wait(timeout=10), "build worker never reached connect"

        closer = threading.Thread(target=idx.close)
        closer.start()
        gate.set()  # let the worker proceed — close() is already committed
        closer.join(timeout=10)
        assert not closer.is_alive()
        idx._build_thread.join(timeout=10)

        db = tmp_path / "index.db"
        if db.exists():
            db.unlink()  # PermissionError if a connection survived close()


class TestParentHintFiltering:
    """Tests for parent_hint scoping in search results."""

    def test_parent_hint_filters_results(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p1 = root / "ProjectA"
            p1.mkdir()
            (p1 / "config.txt").write_text("a")
            p2 = root / "ProjectB"
            p2.mkdir()
            (p2 / "config.txt").write_text("b")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Direct search — should find both
            result = idx.search("config.txt")
            assert len(result["candidates"]) >= 2

            # Parent hint filtering happens in file_controller, not in FileIndex
            # But we can verify the search works
            idx.close()


class TestFileControllerIntegration:
    """Tests for integration with file_controller.py."""

    def test_find_candidates_uses_index_when_ready(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Patch the singleton to use our test index
            import actions.file_indexer as indexer_mod

            old_instance = indexer_mod._index_instance
            indexer_mod._index_instance = idx

            try:
                from actions.file_controller import find_file_folder_candidates

                # No explicit roots → the FTS index fast-path is used (explicit
                # roots would instead force a scoped os.walk).
                result = find_file_folder_candidates("report.txt")
                assert len(result["candidates"]) >= 1
                assert result["status"] == "complete"
            finally:
                indexer_mod._index_instance = old_instance
                idx.close()

    def test_fallback_to_walk_when_index_not_ready(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            # Don't build — index not ready

            import actions.file_indexer as indexer_mod

            old_instance = indexer_mod._index_instance
            indexer_mod._index_instance = idx

            try:
                from actions.file_controller import find_file_folder_candidates

                result = find_file_folder_candidates("report.txt", roots=[root])
                # Should fall back to os.walk and still find the file
                assert len(result["candidates"]) >= 1
            finally:
                indexer_mod._index_instance = old_instance
                idx.close()


class TestRebuildIndexAction:
    """Tests for the file_controller rebuild_index action."""

    def test_rebuild_index_action(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)
        from actions.file_controller import file_controller

        result = file_controller({"action": "rebuild_index"})
        assert "rebuild" in result.lower() or "index" in result.lower()

    def test_index_status_action(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)
        from actions.file_controller import file_controller

        result = file_controller({"action": "index_status"})
        assert (
            "status" in result.lower() or "index" in result.lower() or "entries" in result.lower()
        )


class TestWatcher:
    """Tests for watchdog file watcher initialization."""

    def test_watcher_does_not_crash_without_watchdog(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()
            # start_watcher should not crash even if watchdog import fails
            # (it will just log and return)
            idx.start_watcher(roots=[Path(d)])
            idx.close()

    def test_watcher_starts_with_valid_roots(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()
            idx.start_watcher(roots=[Path(d)])
            # Just verify it doesn't crash
            idx.close()


class TestConcurrentAccess:
    """Tests for thread safety with concurrent reads/writes."""

    def test_concurrent_reads_after_build(self):
        import threading

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(100):
                (root / f"file_{i:03d}.txt").write_text(str(i))

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            results = []
            errors = []

            def search_worker(query):
                try:
                    r = idx.search(query)
                    results.append(r)
                except Exception as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=search_worker, args=(f"file_{i:03d}",))
                for i in range(0, 100, 10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0, f"Errors: {errors}"
            assert len(results) == 10
            idx.close()


class TestOpenedPathsCache:
    """Tests for Fix 1: opened_paths instant lookup."""

    def test_opened_paths_returned_in_search(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Record an open
            idx.record_open(str(root / "report.txt"))

            # Search — should find it via opened_paths cache
            result = idx.search("report")
            assert len(result["candidates"]) >= 1
            # The opened_paths match should be among results
            paths = [c["path"] for c in result["candidates"]]
            assert str(root / "report.txt") in paths
            idx.close()

    def test_opened_paths_boost_score(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "alpha.txt").write_text("a")
            (root / "beta_report.txt").write_text("b")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Open beta_report many times
            for _ in range(10):
                idx.record_open(str(root / "beta_report.txt"))

            # Search for "report" — beta_report should rank higher due to frecency
            result = idx.search("report")
            if len(result["candidates"]) >= 2:
                names = [c["name"] for c in result["candidates"]]
                # beta_report should be first or among top results
                assert "beta_report.txt" in names[:2]
            idx.close()

    def test_opened_paths_respects_item_type_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")
            (root / "reports").mkdir()

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Open both
            idx.record_open(str(root / "report.txt"))
            idx.record_open(str(root / "reports"))

            # Search for files only
            result = idx.search("report", item_type="file")
            types = {c["type"] for c in result["candidates"]}
            assert "folder" not in types

            # Search for folders only
            result = idx.search("report", item_type="folder")
            types = {c["type"] for c in result["candidates"]}
            assert "file" not in types
            idx.close()

    def test_opened_paths_deduplicates_with_fts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "report.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Record open AND it's in the index
            idx.record_open(str(root / "report.txt"))

            result = idx.search("report")
            # Should appear only once (deduplicated)
            paths = [c["path"] for c in result["candidates"]]
            assert paths.count(str(root / "report.txt")) == 1
            idx.close()


class TestIncrementalWatcher:
    """Tests for Fix 2: incremental watcher updates."""

    def test_incremental_add(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)
            count_before = idx.file_count()

            # Create file on disk, then add to index
            (root / "new_file.txt").write_text("hello")
            idx._incremental_add(str(root / "new_file.txt"))
            assert idx.file_count() == count_before + 1

            # Verify it's searchable
            result = idx.search("new_file")
            assert len(result["candidates"]) >= 1
            idx.close()

    def test_incremental_remove(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "file.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)
            count_before = idx.file_count()
            assert count_before >= 1

            # Simulate file deletion
            idx._incremental_remove(str(root / "file.txt"))
            assert idx.file_count() == count_before - 1
            idx.close()

    def test_incremental_update(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "file.txt").write_text("content")

            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx.rebuild(roots=[root], wait=True)

            # Get initial size
            conn = idx._get_conn()
            row = conn.execute(
                "SELECT size FROM files WHERE path = ?", (str(root / "file.txt"),)
            ).fetchone()
            initial_size = row[0]

            # Modify the file
            (root / "file.txt").write_text("content with more data")
            idx._incremental_update(str(root / "file.txt"))

            # Check size updated
            row = conn.execute(
                "SELECT size FROM files WHERE path = ?", (str(root / "file.txt"),)
            ).fetchone()
            assert row[0] > initial_size
            idx.close()

    def test_incremental_remove_nonexistent_no_error(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()
            # Should not raise
            idx._incremental_remove("/nonexistent/path.txt")
            idx.close()

    def test_incremental_add_nonexistent_no_error(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test_index.db"
            from actions.file_indexer import FileIndex

            idx = FileIndex(db_path=db_path)
            idx._get_conn()
            # Should not raise
            idx._incremental_add("/nonexistent/path.txt")
            idx.close()
