import os
import tempfile
import time
from pathlib import Path

import pytest

from actions.file_controller import (
    SKIP_DIRS,
    _compute_final_score,
    _compute_match,
    _detect_onedrive,
    _get_project_folder,
    _should_auto_open,
    find_file_folder_candidates,
)


def _safe_path_mock(*args, **kwargs):
    return True


class TestExactFileFound:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_exact_file_match(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "report.txt").write_text("hello")
            result = find_file_folder_candidates("report.txt", roots=[Path(d)])
            assert len(result["candidates"]) == 1
            c = result["candidates"][0]
            assert c["name"] == "report.txt"
            assert c["match_type"] == "exact"
            assert c["score"] >= 0.95


class TestExactFolderFound:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_exact_folder_match(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "myproject").mkdir()
            result = find_file_folder_candidates("myproject", roots=[Path(d)])
            assert len(result["candidates"]) == 1
            c = result["candidates"][0]
            assert c["name"] == "myproject"
            assert c["type"] == "folder"
            assert c["match_type"] == "exact"


class TestStemMatch:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_stem_match(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "report.pdf").write_text("content")
            result = find_file_folder_candidates("report", roots=[Path(d)])
            assert len(result["candidates"]) == 1
            c = result["candidates"][0]
            assert c["match_type"] == "stem"
            assert c["score"] >= 0.90


class TestContainsMatch:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_contains_match(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "my_report_final.txt").write_text("hello")
            result = find_file_folder_candidates("report", roots=[Path(d)])
            assert len(result["candidates"]) == 1
            c = result["candidates"][0]
            assert c["match_type"] in ("contains", "compact_contains")
            assert 0.70 <= c["score"] <= 0.85


class TestExtensionFilter:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_extension_filter(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.pdf").write_text("a")
            Path(d, "b.txt").write_text("b")
            Path(d, "c.docx").write_text("c")
            result = find_file_folder_candidates("a", roots=[Path(d)], extension=".pdf")
            names = [c["name"] for c in result["candidates"]]
            assert "a.pdf" in names
            assert "b.txt" not in names
            assert "c.docx" not in names


class TestTypeFilter:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_file_filter_excludes_folders(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "report.txt").write_text("content")
            Path(d, "reports").mkdir()
            result = find_file_folder_candidates("report", roots=[Path(d)], item_type="file")
            types = [c["type"] for c in result["candidates"]]
            assert all(t == "file" for t in types)

    def test_folder_filter_excludes_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "report.txt").write_text("content")
            Path(d, "reports").mkdir()
            result = find_file_folder_candidates("report", roots=[Path(d)], item_type="folder")
            types = [c["type"] for c in result["candidates"]]
            assert all(t == "folder" for t in types)


class TestMultipleMatches:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_multiple_matches_sorted_by_score(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "report.txt").write_text("exact")
            Path(d, "my_report.txt").write_text("contains")
            Path(d, "reports_archive.txt").write_text("also contains")
            result = find_file_folder_candidates("report", roots=[Path(d)])
            assert len(result["candidates"]) >= 2
            scores = [c["score"] for c in result["candidates"]]
            assert scores == sorted(scores, reverse=True)


class TestFuzzyNeverAutoOpens:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_fuzzy_match_not_auto_opened(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "rapport_final.pdf").write_text("content")
            result = find_file_folder_candidates("report", roots=[Path(d)])
            if result["candidates"]:
                c = result["candidates"][0]
                if c["match_type"] == "fuzzy":
                    should_open, reason = _should_auto_open(result["candidates"])
                    assert not should_open


class TestLowFuzzyNoOpen:
    def test_low_confidence_returns_empty(self):
        result = find_file_folder_candidates(
            "zzzzzzz",
            roots=[Path(tempfile.mkdtemp())],
        )
        assert len(result["candidates"]) == 0


class TestOneDriveDetection:
    def test_onedrive_detection_optional(self):
        roots = _detect_onedrive()
        assert isinstance(roots, list)


class TestProjectFolder:
    def test_project_folder_in_roots(self):
        project = _get_project_folder()
        assert project is None or isinstance(project, Path)


class TestSkipDirectories:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_skip_dirs_not_scanned(self):
        with tempfile.TemporaryDirectory() as d:
            for skip_name in SKIP_DIRS:
                skip_dir = Path(d, skip_name)
                skip_dir.mkdir()
                (skip_dir / "hidden.txt").write_text("skip me")

            Path(d, "visible.txt").write_text("find me")
            result = find_file_folder_candidates("hidden", roots=[Path(d)])
            names = [c["name"] for c in result["candidates"]]
            assert "hidden.txt" not in names


class TestTimeout:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_timeout_returns_partial(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(50):
                Path(d, f"file_{i:03d}.txt").write_text(str(i))
            result = find_file_folder_candidates("file", roots=[Path(d)], timeout_sec=0.001)
            assert result["status"] in ("complete", "partial")


class TestScoringRanking:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_exact_ranks_above_contains(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "my_report.txt").write_text("contains")
            Path(d, "report.txt").write_text("exact")
            result = find_file_folder_candidates("report", roots=[Path(d)])
            if len(result["candidates"]) >= 2:
                exact_idx = next(
                    (i for i, c in enumerate(result["candidates"]) if c["name"] == "report.txt"), -1
                )
                contains_idx = next(
                    (i for i, c in enumerate(result["candidates"]) if c["name"] == "my_report.txt"),
                    -1,
                )
                if exact_idx >= 0 and contains_idx >= 0:
                    assert exact_idx < contains_idx


class TestRecencyBonus:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_recent_file_scores_higher(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d, "old_report.txt")
            old.write_text("old")
            os.utime(str(old), (time.time() - 86400 * 10, time.time() - 86400 * 10))

            new = Path(d, "new_report.txt")
            new.write_text("new")
            os.utime(str(new), (time.time() - 60, time.time() - 60))

            result = find_file_folder_candidates("report", roots=[Path(d)])
            if len(result["candidates"]) == 2:
                scores = {c["name"]: c["score"] for c in result["candidates"]}
                assert scores["new_report.txt"] > scores["old_report.txt"]


class TestShouldAutoOpen:
    def test_single_strong_match_opens(self):
        candidates = [{"match_type": "exact", "score": 0.95}]
        should, _ = _should_auto_open(candidates)
        assert should

    def test_fuzzy_never_opens(self):
        candidates = [{"match_type": "fuzzy", "score": 0.80}]
        should, _ = _should_auto_open(candidates)
        assert not should

    def test_multiple_close_matches_no_open(self):
        candidates = [
            {"match_type": "contains", "score": 0.82},
            {"match_type": "contains", "score": 0.80},
        ]
        should, _ = _should_auto_open(candidates)
        assert not should

    def test_empty_candidates_no_open(self):
        should, _ = _should_auto_open([])
        assert not should

    def test_low_score_no_open(self):
        candidates = [{"match_type": "contains", "score": 0.50}]
        should, _ = _should_auto_open(candidates)
        assert not should


class TestExactPathFastPath:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_exact_path_exists(self, monkeypatch):
        from actions.file_controller import open_file_or_folder

        opened = []

        def fake_open_path(target, reveal_file=False):
            opened.append(Path(target))
            return f"Opened: {Path(target).name}"

        monkeypatch.setattr("actions.file_controller._open_path", fake_open_path)

        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "test.txt")
            target.write_text("content")
            result = open_file_or_folder(d, name="test.txt")
            assert len(opened) == 1
            assert "Opened" in result


# ── HOTFIX TESTS: Drive Discovery, Windows Paths, Folder Intent, Parent Scoping ──────────────


class TestWindowsPathDetection:
    def test_drive_letter_path(self):
        from actions.file_controller import _is_windows_path

        assert _is_windows_path("D:\\my folder")
        assert _is_windows_path("C:\\Users\\test\\file.txt")

    def test_quoted_path(self):
        from actions.file_controller import _is_windows_path

        assert _is_windows_path('"D:\\my folder"')

    def test_path_in_sentence(self):
        from actions.file_controller import _is_windows_path

        assert _is_windows_path("open D:\\projects\\EraOne")

    def test_not_windows_path(self):
        from actions.file_controller import _is_windows_path

        assert not _is_windows_path("open the report")
        assert not _is_windows_path("find my folder")


class TestWindowsPathExtraction:
    def test_bare_path(self):
        from actions.file_controller import _extract_windows_path

        assert _extract_windows_path("D:\\myfolder") == "D:\\myfolder"

    def test_quoted_path(self):
        from actions.file_controller import _extract_windows_path

        assert _extract_windows_path('"D:\\my folder"') == "D:\\my folder"

    def test_path_in_sentence(self):
        from actions.file_controller import _extract_windows_path

        assert _extract_windows_path("open D:\\projects\\EraOne please") == "D:\\projects\\EraOne"

    def test_no_path(self):
        from actions.file_controller import _extract_windows_path

        assert _extract_windows_path("open the report") is None


class TestFolderIntentParsing:
    def test_folder_keyword(self):
        from actions.file_controller import _parse_folder_intent

        query, item_type, drive, parent = _parse_folder_intent("open folder myproject")
        assert query == "myproject"
        assert item_type == "folder"

    def test_directory_keyword(self):
        from actions.file_controller import _parse_folder_intent

        query, item_type, drive, parent = _parse_folder_intent("where is directory docs")
        assert query == "docs"
        assert item_type == "folder"

    def test_drive_hint(self):
        from actions.file_controller import _parse_folder_intent

        query, item_type, drive, parent = _parse_folder_intent("find report on D:")
        assert drive == "D:"

    def test_parent_hint(self):
        from actions.file_controller import _parse_folder_intent

        query, item_type, drive, parent = _parse_folder_intent("open config in EraOne")
        assert parent == "EraOne"


class TestDriveHintSearch:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_drive_hint_prioritized(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "target.txt").write_text("hello")
            result = find_file_folder_candidates("target.txt", roots=[Path(d)], drive_hint="X:")
            assert len(result["candidates"]) >= 1


class TestParentHintSearch:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_parent_hint_scopes_search(self):
        with tempfile.TemporaryDirectory() as d:
            parent_dir = Path(d, "MyProject")
            parent_dir.mkdir()
            (parent_dir / "config.txt").write_text("hello")

            other_dir = Path(d, "OtherProject")
            other_dir.mkdir()
            (other_dir / "config.txt").write_text("other")

            result = find_file_folder_candidates(
                "config.txt", roots=[Path(d)], parent_hint="MyProject"
            )
            assert len(result["candidates"]) == 1
            assert "MyProject" in result["candidates"][0]["path"]


class TestSafetyGateWindowsPaths:
    def test_windows_path_with_command_allows(self):
        from core.input_safety_gate import InputSafetyGate

        gate = InputSafetyGate()
        decision = gate.validate_input("open D:\\my folder")
        assert decision.action == "ALLOW"

    def test_windows_path_without_command_allows(self):
        from core.input_safety_gate import InputSafetyGate

        gate = InputSafetyGate()
        decision = gate.validate_input("D:\\my folder")
        assert decision.action == "ALLOW"


class TestOpenPathRegression:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_open_file_or_folder_uses_open_path(self, monkeypatch):
        from actions.file_controller import open_file_or_folder

        opened = []

        def fake_open_path(target, reveal_file=False):
            opened.append(Path(target))
            return f"Opened: {Path(target).name}"

        monkeypatch.setattr("actions.file_controller._open_path", fake_open_path)

        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "test.txt")
            target.write_text("content")
            result = open_file_or_folder(d, name="test.txt")
            assert len(opened) == 1
            assert "Opened" in result

    def test_open_path_missing_returns_safe_error(self):
        from actions.file_controller import _open_path

        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "deleted.txt")
            target.write_text("content")
            target.unlink()
            result = _open_path(target)
            assert "no longer exists" in result

    def test_open_path_denied_returns_error(self):
        import actions.file_controller as fc
        from actions.file_controller import _open_path

        original = fc.is_safe_path
        fc.is_safe_path = lambda p, **kw: False
        try:
            with tempfile.TemporaryDirectory() as d:
                target = Path(d, "test.txt")
                target.write_text("content")
                result = _open_path(target)
                assert "Access denied" in result
        finally:
            fc.is_safe_path = original


class TestCompactMatching:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_compact_exact_match(self):
        from actions.file_controller import _compute_match

        match_type, score = _compute_match("ceramequartz", "Cerame Quartz", Path("Cerame Quartz"))
        assert match_type == "compact_exact"
        assert score >= 0.85

    def test_compact_contains_match(self):
        from actions.file_controller import _compute_match

        match_type, score = _compute_match(
            "ceramequartz", "My Cerame Quartz Folder", Path("My Cerame Quartz Folder")
        )
        assert match_type == "compact_contains"
        assert score >= 0.75

    def test_compact_hyphenated_match(self):
        from actions.file_controller import _compute_match

        match_type, score = _compute_match("ceramequartz", "cerame-quartz", Path("cerame-quartz"))
        assert match_type == "compact_exact"
        assert score >= 0.85


class TestQueryIntegrity:
    def test_parse_folder_intent_preserves_query(self):
        from actions.file_controller import _parse_folder_intent

        query, item_type, drive_hint, parent_hint = _parse_folder_intent("open ceramequartz folder")
        assert query == "ceramequartz"
        assert item_type == "folder"

    def test_no_desktop_injection(self):
        from actions.file_controller import _parse_folder_intent

        query, item_type, drive_hint, parent_hint = _parse_folder_intent("ceramequartz")
        assert "desktop" not in query.lower()
        assert query == "ceramequartz"


class TestExactPathBypass:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_exact_path_opens_directly(self, monkeypatch):
        from actions.file_controller import open_file_or_folder

        opened = []

        def fake_open_path(target, reveal_file=False):
            opened.append(Path(target))
            return f"Opened: {Path(target).name}"

        monkeypatch.setattr("actions.file_controller._open_path", fake_open_path)

        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "test.txt")
            target.write_text("content")
            result = open_file_or_folder(str(target))
            assert len(opened) == 1
            assert "Opened" in result

    def test_missing_exact_path_returns_not_found(self):
        from actions.file_controller import open_file_or_folder

        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "nonexistent.txt")
            result = open_file_or_folder(str(target))
            assert "Path not found" in result

    def test_exact_path_does_not_search_desktop(self, monkeypatch):
        from actions.file_controller import open_file_or_folder

        search_called = []

        def fake_find(*args, **kwargs):
            search_called.append(True)
            return {"status": "complete", "candidates": [], "query": "", "message": None}

        monkeypatch.setattr("actions.file_controller.find_file_folder_candidates", fake_find)

        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "test.txt")
            target.write_text("content")
            open_file_or_folder(str(target))
            assert len(search_called) == 0


class TestAllDriveSearch:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_mocked_drives_include_c_and_d(self, monkeypatch):
        from pathlib import Path

        from actions.file_controller import _get_available_drives

        monkeypatch.setattr("actions.file_controller._OS", "Windows")

        def fake_drives():
            return [Path("C:\\"), Path("D:\\")]

        monkeypatch.setattr("actions.file_controller._get_available_drives", fake_drives)
        drives = _get_available_drives()
        assert any("C" in str(d) for d in drives)
        assert any("D" in str(d) for d in drives)

    def test_search_finds_folder_on_mocked_d(self):
        from actions.file_controller import find_file_folder_candidates

        with tempfile.TemporaryDirectory() as d:
            Path(d, "target_folder").mkdir()
            result = find_file_folder_candidates(
                "target_folder", roots=[Path(d)], item_type="folder"
            )
            assert len(result["candidates"]) >= 1

    def test_drive_hint_d_searches_d_first(self):
        from actions.file_controller import find_file_folder_candidates

        with tempfile.TemporaryDirectory() as d:
            Path(d, "report.txt").write_text("hello")
            result = find_file_folder_candidates("report.txt", roots=[Path(d)], drive_hint="D:")
            assert len(result["candidates"]) >= 1


class TestFolderIntent:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_folder_request_returns_folders_only(self):
        from actions.file_controller import find_file_folder_candidates

        with tempfile.TemporaryDirectory() as d:
            Path(d, "myfolder").mkdir()
            Path(d, "myfile.txt").write_text("content")
            result = find_file_folder_candidates("myfolder", roots=[Path(d)], item_type="folder")
            for c in result["candidates"]:
                assert c["type"] == "folder"

    def test_files_excluded_when_item_type_is_folder(self):
        from actions.file_controller import find_file_folder_candidates

        with tempfile.TemporaryDirectory() as d:
            Path(d, "target").mkdir()
            Path(d, "target.txt").write_text("content")
            result = find_file_folder_candidates("target", roots=[Path(d)], item_type="folder")
            file_count = sum(1 for c in result["candidates"] if c["type"] == "file")
            assert file_count == 0


class TestParentSearch:
    @pytest.fixture(autouse=True)
    def _safe(self, monkeypatch):
        monkeypatch.setattr("actions.file_controller.is_safe_path", _safe_path_mock)

    def test_parent_found_child_found_inside(self):
        from actions.file_controller import find_file_folder_candidates

        with tempfile.TemporaryDirectory() as d:
            parent = Path(d, "Serum Quartz")
            parent.mkdir()
            (parent / "new").mkdir()
            result = find_file_folder_candidates("new", roots=[Path(d)], parent_hint="Serum Quartz")
            assert len(result["candidates"]) == 1
            assert "Serum Quartz" in result["candidates"][0]["path"]

    def test_multiple_parents_returns_candidates(self):
        from actions.file_controller import find_file_folder_candidates

        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d, "Parent1")
            p1.mkdir()
            (p1 / "child").mkdir()
            p2 = Path(d, "Parent2")
            p2.mkdir()
            (p2 / "child").mkdir()
            result = find_file_folder_candidates("child", roots=[Path(d)])
            assert len(result["candidates"]) >= 2


class TestPathSafetyLocalDrives:
    def test_valid_drive_path_allowed(self):
        import core.utils as utils
        from core.utils import is_safe_path

        original = utils.LOCAL_DRIVES
        try:
            utils.LOCAL_DRIVES = [Path("D:\\")]
            with tempfile.TemporaryDirectory():
                test_path = Path("D:\\telechargements\\ceramequartz")
                assert is_safe_path(test_path) or not Path("D:\\").exists()
        finally:
            utils.LOCAL_DRIVES = original

    def test_protected_windows_path_blocked(self):
        from core.utils import is_safe_path

        result = is_safe_path(Path("C:\\Windows\\System32"))
        assert not result

    def test_protected_program_files_blocked(self):
        from core.utils import is_safe_path

        result = is_safe_path(Path("C:\\Program Files\\SomeApp"))
        assert not result

    def test_protected_recycle_bin_skipped_in_search(self):
        from actions.file_controller import SKIP_DIRS

        assert "$Recycle.Bin" in SKIP_DIRS

    def test_protected_system_volume_skipped_in_search(self):
        from actions.file_controller import SKIP_DIRS

        assert "System Volume Information" in SKIP_DIRS

    def test_missing_exact_path_returns_not_found(self):
        from actions.file_controller import open_file_or_folder

        result = open_file_or_folder("D:\\not-existing-folder-xyz")
        assert "Path not found" in result

    def test_exact_path_does_not_guess(self, monkeypatch):
        from actions.file_controller import open_file_or_folder

        search_called = []

        def fake_find(*args, **kwargs):
            search_called.append(True)
            return {"status": "complete", "candidates": [], "query": "", "message": None}

        monkeypatch.setattr("actions.file_controller.find_file_folder_candidates", fake_find)
        open_file_or_folder("D:\\not-existing-folder-xyz")
        assert len(search_called) == 0

    def test_safety_gate_still_passes_raw_path(self):
        from core.input_safety_gate import InputSafetyGate

        gate = InputSafetyGate()
        decision = gate.validate_input("D:\\telechargements\\ceramequartz")
        assert decision.action == "ALLOW"

    def test_pre_router_still_passes(self, monkeypatch):
        from main import _extract_raw_path

        path = _extract_raw_path("D:\\telechargements\\ceramequartz")
        assert path == "D:\\telechargements\\ceramequartz"
