import os
import tempfile
from pathlib import Path

import pytest

from actions.file_controller import (
    _backup_file,
    _cloud_present,
    _ensure_openable_extension,
    _recover_source,
    copy_file,
    create_file,
    move_file,
    write_file,
)


def _safe_path_mock(*args, **kwargs):
    return True


class TestCreateFileOverwrite:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_create_new_file_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            result = create_file(d, name="test.txt", content="hello")
            assert "created" in result.lower()
            assert Path(d, "test.txt").read_text() == "hello"

    def test_create_existing_file_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("existing")
            result = create_file(d, name="test.txt", content="new")
            assert "already exists" in result.lower()

    def test_create_existing_file_overwrite_creates_backup(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("original content")
            result = create_file(d, name="test.txt", content="new", overwrite=True)
            assert "created" in result.lower()
            assert "backup" in result.lower()
            assert Path(d, "test.txt").read_text() == "new"
            backups = list(Path(d).glob("test.txt.bak-*"))
            assert len(backups) == 1
            assert backups[0].read_text() == "original content"

    def test_create_existing_file_backup_failure_blocks(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("original")

            def failing_backup(*args, **kwargs):
                return None

            monkeypatch.setattr("actions.file_controller._backup_file", failing_backup)
            result = create_file(d, name="test.txt", content="new", overwrite=True)
            assert "backup failed" in result.lower()
            assert Path(d, "test.txt").read_text() == "original"


class TestOpenableExtension:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_extensionless_name_gets_txt(self):
        assert _ensure_openable_extension(Path("dir") / "Report").name == "Report.txt"

    def test_existing_suffix_untouched(self):
        assert _ensure_openable_extension(Path("dir") / "notes.md").name == "notes.md"

    def test_dotfile_untouched(self):
        assert _ensure_openable_extension(Path("dir") / ".gitignore").name == ".gitignore"

    def test_create_file_without_extension_writes_txt(self):
        with tempfile.TemporaryDirectory() as d:
            result = create_file(d, name="VideoUploadIssueReport", content="hello")
            assert "created" in result.lower()
            assert Path(d, "VideoUploadIssueReport.txt").read_text() == "hello"
            assert not Path(d, "VideoUploadIssueReport").exists()

    def test_write_file_without_extension_writes_txt(self):
        with tempfile.TemporaryDirectory() as d:
            result = write_file(d, name="Notes", content="hello")
            assert "written" in result.lower()
            assert Path(d, "Notes.txt").read_text() == "hello"


class TestWriteFileOverwrite:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_write_new_file_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            result = write_file(d, name="test.txt", content="hello")
            assert "written" in result.lower()

    def test_write_existing_file_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("existing")
            result = write_file(d, name="test.txt", content="new")
            assert "already exists" in result.lower()

    def test_write_append_no_block(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("line1\n")
            result = write_file(d, name="test.txt", content="line2\n", append=True)
            assert "appended" in result.lower()
            assert "backup" not in result.lower()
            assert Path(d, "test.txt").read_text() == "line1\nline2\n"

    def test_write_overwrite_creates_backup(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("original content")
            result = write_file(d, name="test.txt", content="new", overwrite=True)
            assert "written" in result.lower()
            assert "backup" in result.lower()
            assert Path(d, "test.txt").read_text() == "new"
            backups = list(Path(d).glob("test.txt.bak-*"))
            assert len(backups) == 1
            assert backups[0].read_text() == "original content"

    def test_write_overwrite_backup_failure_blocks(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.txt").write_text("original")

            def failing_backup(*args, **kwargs):
                return None

            monkeypatch.setattr("actions.file_controller._backup_file", failing_backup)
            result = write_file(d, name="test.txt", content="new", overwrite=True)
            assert "backup failed" in result.lower()
            assert Path(d, "test.txt").read_text() == "original"


class TestMoveFileOverwrite:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_move_to_new_location_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            result = move_file(str(src_dir), name="file.txt", destination=str(dst_dir))
            assert "moved" in result.lower()
            assert not (src_dir / "file.txt").exists()
            assert (dst_dir / "file.txt").read_text() == "data"

    def test_move_to_existing_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            (dst_dir / "file.txt").write_text("existing")
            result = move_file(str(src_dir), name="file.txt", destination=str(dst_dir))
            assert "already exists" in result.lower()
            assert (dst_dir / "file.txt").read_text() == "existing"

    def test_move_overwrite_creates_backup(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("new data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            (dst_dir / "file.txt").write_text("existing data")
            result = move_file(
                str(src_dir), name="file.txt", destination=str(dst_dir), overwrite=True
            )
            assert "moved" in result.lower()
            assert "backup" in result.lower()
            assert (dst_dir / "file.txt").read_text() == "new data"
            backups = list(dst_dir.glob("file.txt.bak-*"))
            assert len(backups) == 1
            assert backups[0].read_text() == "existing data"

    def test_move_overwrite_backup_failure_blocks(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("new data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            (dst_dir / "file.txt").write_text("existing data")

            def failing_backup(*args, **kwargs):
                return None

            monkeypatch.setattr("actions.file_controller._backup_file", failing_backup)
            result = move_file(
                str(src_dir), name="file.txt", destination=str(dst_dir), overwrite=True
            )
            assert "backup failed" in result.lower()
            assert (dst_dir / "file.txt").read_text() == "existing data"
            assert (src_dir / "file.txt").read_text() == "new data"


class TestCopyFileOverwrite:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_copy_to_new_location_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            result = copy_file(str(src_dir), name="file.txt", destination=str(dst_dir))
            assert "copied" in result.lower()
            assert (src_dir / "file.txt").read_text() == "data"
            assert (dst_dir / "file.txt").read_text() == "data"

    def test_copy_to_existing_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            (dst_dir / "file.txt").write_text("existing")
            result = copy_file(str(src_dir), name="file.txt", destination=str(dst_dir))
            assert "already exists" in result.lower()
            assert (dst_dir / "file.txt").read_text() == "existing"

    def test_copy_overwrite_creates_backup(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("new data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            (dst_dir / "file.txt").write_text("existing data")
            result = copy_file(
                str(src_dir), name="file.txt", destination=str(dst_dir), overwrite=True
            )
            assert "copied" in result.lower()
            assert "backup" in result.lower()
            assert (dst_dir / "file.txt").read_text() == "new data"
            backups = list(dst_dir.glob("file.txt.bak-*"))
            assert len(backups) == 1
            assert backups[0].read_text() == "existing data"

    def test_copy_overwrite_backup_failure_blocks(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d, "src")
            src_dir.mkdir()
            (src_dir / "file.txt").write_text("new data")
            dst_dir = Path(d, "dst")
            dst_dir.mkdir()
            (dst_dir / "file.txt").write_text("existing data")

            def failing_backup(*args, **kwargs):
                return None

            monkeypatch.setattr("actions.file_controller._backup_file", failing_backup)
            result = copy_file(
                str(src_dir), name="file.txt", destination=str(dst_dir), overwrite=True
            )
            assert "backup failed" in result.lower()
            assert (dst_dir / "file.txt").read_text() == "existing data"
            assert (src_dir / "file.txt").read_text() == "new data"


class TestCloudPresenceAndRecovery:
    """Regression cover for the OneDrive move failure: a file successfully
    opened via fuzzy search was then handed to move under a *wrong* absolute
    path, and a single exists() check dead-ended with 'Source not found' even
    though the file was present at its real location."""

    def test_cloud_present_true_for_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "here.txt")
            f.write_text("x")
            assert _cloud_present(f) is True

    def test_cloud_present_false_for_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            # retries=1 keeps the negative case fast (no hydration wait)
            assert _cloud_present(Path(d, "nope.txt"), retries=1) is False

    def test_recover_source_finds_file_in_known_folder(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d, "wanted.txt")
            real.write_text("data")
            monkeypatch.setattr("actions.file_controller.get_desktop", lambda: Path(d))
            found = _recover_source("wanted.txt")
            assert found is not None
            assert found.name == "wanted.txt"
            assert found.read_text() == "data"

    def test_move_recovers_when_given_wrong_source_folder(self, monkeypatch):
        # File lives in src_dir, but move is called with a bogus source folder
        # (mimicking a path guessed from an open that reported only a basename).
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            (Path(src_dir) / "doc.txt").write_text("payload")
            monkeypatch.setattr("actions.file_controller.get_desktop", lambda: Path(src_dir))
            wrong = str(Path(dst_dir) / "doc.txt")  # not where the file is
            result = move_file(path=wrong, name="", destination=dst_dir, overwrite=True)
            assert "moved" in result.lower()
            assert (Path(dst_dir) / "doc.txt").read_text() == "payload"
            assert not (Path(src_dir) / "doc.txt").exists()

    def test_move_still_reports_when_truly_absent(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)
        with tempfile.TemporaryDirectory() as d:
            # Point every recovery folder at an empty dir so nothing is found.
            for fn in (
                "get_desktop",
                "get_downloads",
                "get_documents",
                "get_pictures",
                "get_music",
                "get_videos",
            ):
                monkeypatch.setattr(f"actions.file_controller.{fn}", lambda: Path(d))
            result = move_file(
                path=str(Path(d, "ghost.txt")), name="", destination=d, overwrite=True
            )
            assert "source not found" in result.lower()


class TestBackupFile:
    def test_backup_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            result = _backup_file(Path(d, "does_not_exist.txt"))
            assert result is None

    def test_backup_existing_creates_timestamped_copy(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "test.txt")
            target.write_text("original")
            result = _backup_file(target)
            assert result is not None
            assert ".bak-" in result
            backup = Path(result)
            assert backup.read_text() == "original"
            assert target.read_text() == "original"


class TestDiagnosisReportRead:
    """2026-11-05 live: read parent="diagnosis" name="diag_..." answered File not found."""

    @pytest.fixture
    def reports(self, monkeypatch, tmp_path):
        import actions.self_diagnosis as sd

        monkeypatch.setattr(sd, "REPORT_DIR", tmp_path)
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)
        (tmp_path / "diag_20261105_234618.md").write_text("# report body", encoding="utf-8")
        return tmp_path

    @pytest.mark.parametrize(
        "params",
        [
            {"action": "read", "parent": "diagnosis", "name": "diag_20261105_234618.md"},
            {"action": "read", "path": "diagnosis", "name": "diag_20261105_234618"},
            {"action": "read", "path": "diag_20261105_234618.md"},
        ],
    )
    def test_a_report_id_reads_the_real_report(self, reports, params):
        from actions.file_controller import file_controller

        assert file_controller(params) == "# report body"

    def test_an_unknown_report_id_is_still_not_found(self, reports):
        from actions.file_controller import file_controller

        out = file_controller({"action": "read", "name": "diag_20991231_000000.md"})
        assert out.startswith("File not found")

    def test_a_lookalike_name_never_resolves_into_the_report_dir(self, reports):
        from actions.file_controller import _diagnosis_report

        for bad in ("diag_20261105_234618.md.txt", "../diag_20261105_234618.md", "diag_x.md"):
            assert _diagnosis_report("desktop", bad) is None

    def test_write_never_resolves_into_the_report_dir(self, reports, monkeypatch, tmp_path):
        out_dir = tmp_path / "elsewhere"
        out_dir.mkdir()
        write_file(str(out_dir), name="diag_20261105_234618.md", content="x", overwrite=True)
        assert (reports / "diag_20261105_234618.md").read_text(encoding="utf-8") == "# report body"


class TestAppendFindsTheExistingFile:
    """2026-11-06 live: append to 'system_limitation_log.txt' silently created a second
    file next to 'System Limitations Log.txt' and still said 'Appended to'."""

    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_a_differently_spelled_name_appends_to_the_one_existing_file(self, tmp_path):
        real = tmp_path / "System Limitations Log.txt"
        real.write_text("first\n", encoding="utf-8")
        out = write_file(str(tmp_path), "system_limitation_log.txt", "second\n", append=True)
        assert real.read_text(encoding="utf-8") == "first\nsecond\n"
        assert not (tmp_path / "system_limitation_log.txt").exists()
        assert "Appended to: System Limitations Log.txt" in out
        assert "system_limitation_log.txt" in out and str(real) in out

    def test_a_truly_new_file_is_reported_as_created_not_appended(self, tmp_path):
        out = write_file(str(tmp_path), "notes.txt", "x", append=True)
        assert out.startswith("Created new file") and "Appended" not in out
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "x"

    def test_two_possible_twins_never_guess(self, tmp_path):
        (tmp_path / "System Limitations Log.txt").write_text("a", encoding="utf-8")
        (tmp_path / "system-limitation-log.txt").write_text("b", encoding="utf-8")
        out = write_file(str(tmp_path), "system_limitations_log.txt", "c", append=True)
        assert out.startswith("Created new file")

    def test_an_unrelated_name_or_other_extension_is_not_a_twin(self, tmp_path):
        (tmp_path / "System Limitations Log.md").write_text("a", encoding="utf-8")
        (tmp_path / "shopping list.txt").write_text("b", encoding="utf-8")
        out = write_file(str(tmp_path), "system_limitation_log.txt", "c", append=True)
        assert out.startswith("Created new file")

    def test_overwrite_never_redirects_to_a_twin(self, tmp_path):
        real = tmp_path / "System Limitations Log.txt"
        real.write_text("keep", encoding="utf-8")
        write_file(str(tmp_path), "system_limitation_log.txt", "new", overwrite=True)
        assert real.read_text(encoding="utf-8") == "keep"
