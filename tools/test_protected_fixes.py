"""The protected-fixes registry (docs/PROTECTED_FIXES.md) must stay enforceable.

If this file fails, someone deleted, renamed or switched off a guard test, or
an entry points at a file that moved. Do NOT fix it by editing the registry to
drop the guard: restore the guard, or update the entry ONLY if the owner asked
for that behaviour to change (and record it in handoff.md).
"""

from __future__ import annotations

import pytest

from tools import era_gate
from tools import protected_fixes as pf


def test_the_registry_is_enforceable():
    assert pf.problems() == []


def test_every_entry_has_at_least_one_guard_and_one_file():
    for entry in pf.load():
        assert entry.guards and entry.files, entry.id


def _registry(tmp_path, guards: str, files: str = "`tools/protected_fixes.py`"):
    path = tmp_path / "PF.md"
    path.write_text(
        "## Entries\n\n"
        "### PF-900 · Example\n"
        f"- **Files:** {files}\n"
        "- **Rule:** keep it.\n"
        "- **Why:** it broke.\n"
        f"- **Guards:** {guards}\n"
        "- **Since:** 2026-11-12 · test\n",
        encoding="utf-8",
    )
    return pf.load(path)


def test_a_deleted_guard_is_reported(tmp_path):
    entries = _registry(tmp_path, "`tools/test_protected_fixes.py::test_that_does_not_exist`")
    assert any("not found" in p for p in pf.problems(entries))


def test_a_moved_file_is_reported(tmp_path):
    entries = _registry(
        tmp_path, "`tools/test_protected_fixes.py::test_the_registry_is_enforceable`", "`nope.py`"
    )
    assert any("file does not exist" in p for p in pf.problems(entries))


def test_class_and_method_node_ids_resolve():
    assert pf.guard_exists("tools/test_browser_media_routing.py::TestYoutubeVideoVerification")
    assert pf.guard_exists(
        "tools/test_browser_media_routing.py::TestYoutubeVideoVerification"
        "::test_verify_watch_url_returns_true"
    )
    assert not pf.guard_exists("tools/test_browser_media_routing.py::TestNoSuchClass")


def test_a_skipped_guard_is_reported(tmp_path, monkeypatch):
    test_file = tmp_path / "tools" / "test_fake_guard.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "import shutil\nimport pytest\n\n"
        "@pytest.mark.skip(reason='x')\ndef test_guard():\n    pass\n\n"
        "@pytest.mark.xfail\ndef test_xfailed():\n    pass\n\n"
        "@pytest.mark.skipif(True, reason='x')\ndef test_always_skipped():\n    pass\n\n"
        "@pytest.mark.skipif(shutil.which('node') is None, reason='no node')\n"
        "def test_needs_node():\n    pass\n\n"
        "def test_live():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "f.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(pf, "REPO", tmp_path)

    def problems_for(name):
        return pf.problems(_registry(tmp_path, f"`tools/test_fake_guard.py::{name}`", "`f.py`"))

    for switched_off in ("test_guard", "test_xfailed", "test_always_skipped"):
        assert any("skip/xfail" in p for p in problems_for(switched_off)), switched_off
    # An environmental skipif (tooling missing) is a legitimate guard.
    assert problems_for("test_needs_node") == []
    assert problems_for("test_live") == []


@pytest.mark.parametrize(
    "path",
    [
        "actions/spotify_control.py",
        "./actions/spotify_control.py",
        "actions\\spotify_control.py",
        str(pf.REPO / "actions" / "spotify_control.py"),
    ],
)
def test_a_protected_file_is_found_however_the_path_is_spelled(path):
    ids = {e.id for e in pf.for_files([path])}
    assert {"PF-001", "PF-002"} <= ids


def test_an_unprotected_file_has_no_entries():
    assert pf.for_files(["tools/era_doctor.py"]) == []


def test_the_gate_always_runs_every_guard():
    """A change anywhere re-proves every protected fix (the whole point)."""
    cmd = era_gate.guard_command()
    assert set(pf.all_guards()) <= set(cmd)
