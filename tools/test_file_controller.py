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
