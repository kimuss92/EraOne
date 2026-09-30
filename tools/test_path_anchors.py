"""Bare names resolve through known paths (favorites / recently opened).

Regression: "open ABC" / "find ABC" failed although a saved favorite was
D:\\...\\IG\\ABC\\claude automation — the parent was never considered.
"""

from pathlib import Path
from unittest import mock

import pytest

from actions import file_controller as fc
from actions import path_anchors as pa


@pytest.fixture
def tree(tmp_path):
    abc = tmp_path / "Ctrl Z" / "IG" / "ABC"
    fav = abc / "claude automation"
    (fav / "output juillet").mkdir(parents=True)
    (fav / "output juillet" / "plan.md").write_text("x", encoding="utf-8")
    (abc.parent / "Other").mkdir()
    with mock.patch.object(pa, "anchor_paths", return_value=[fav]):
        yield {"root": tmp_path, "abc": abc, "fav": fav}


def test_parent_of_favorite_resolves(tree):
    assert pa.resolve_known("ABC") == [tree["abc"]]


def test_match_is_case_and_punctuation_insensitive(tree):
    assert pa.resolve_known("abc") == [tree["abc"]]
    assert pa.resolve_known("Claude-Automation") == [tree["fav"]]


def test_nested_relative_name_joins_below_anchor(tree):
    got = pa.resolve_known("ABC/claude automation/output juillet/plan.md")
    assert got == [tree["fav"] / "output juillet" / "plan.md"]


def test_direct_child_of_anchor(tree):
    assert pa.resolve_known("output juillet") == [tree["fav"] / "output juillet"]


def test_item_type_filter(tree):
    assert pa.resolve_known("ABC", item_type="file") == []


def test_unknown_and_generic_names_do_not_match(tree):
    assert pa.resolve_known("Nope") == []
    assert pa.resolve_known("Users") == []
    assert pa.resolve_known(str(tree["abc"])) == []  # absolute paths are not names


def test_ambiguous_one_returns_none(tmp_path):
    a = tmp_path / "x" / "ABC"
    b = tmp_path / "y" / "ABC"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    with mock.patch.object(pa, "anchor_paths", return_value=[a, b]):
        assert len(pa.resolve_known("ABC")) == 2
        assert pa.resolve_known_one("ABC") is None


def test_resolve_path_uses_known_anchor(tree):
    assert fc._resolve_path("ABC/claude automation") == tree["fav"]


def test_open_uses_known_anchor_before_machine_scan(tree):
    with (
        mock.patch.object(fc, "_open_path", side_effect=lambda p: f"OPENED {p}"),
        mock.patch.object(fc, "is_safe_path", return_value=True),
        mock.patch.object(fc, "find_file_folder_candidates") as scan,
    ):
        assert fc.open_file_or_folder("desktop", name="ABC") == f"OPENED {tree['abc']}"
        scan.assert_not_called()


def test_open_favorite_falls_back_to_path_components(tree):
    with (
        mock.patch("memory.memory_manager.load_memory", return_value={"favorite_paths": {}}),
        mock.patch.object(fc, "_open_path", side_effect=lambda p: f"OPENED {p}"),
        mock.patch.object(fc, "is_safe_path", return_value=True),
    ):
        assert fc._open_favorite("ABC") == f"OPENED {tree['abc']}"


def test_unscoped_find_reports_known_path_without_scanning(tree):
    with (
        mock.patch.object(fc, "is_safe_path", return_value=True),
        mock.patch.object(fc, "find_file_folder_candidates") as scan,
    ):
        out = fc.file_controller({"action": "find", "name": "ABC", "item_type": "folder"})
    scan.assert_not_called()
    assert "ABC" in out and str(Path(tree["abc"]).parent) in out
