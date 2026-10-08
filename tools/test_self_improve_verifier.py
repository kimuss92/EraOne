"""Tests for actions/self_improve_verifier.py — the post-apply second pass."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from actions import self_improve
from actions import self_improve_verifier as v

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _not_nested(monkeypatch):
    # When the verifier verifies ITSELF these tests run under the guard env —
    # they must still exercise the real path, not the nested-run skip.
    monkeypatch.delenv(v._GUARD_ENV, raising=False)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A repo-relative module self_improve may rewrite; backups stay in tmp_path.
    Leading underscore keeps pytest from collecting it."""
    target = REPO_ROOT / "tools" / "_verifier_selftest.py"
    original = target.read_bytes() if target.exists() else None
    target.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(self_improve, "BACKUP_ROOT", tmp_path / "backups")
    try:
        yield target
    finally:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(original)
            assert target.read_bytes() == original, "sandbox did not restore the tracked source"
        shutil.rmtree(tmp_path, ignore_errors=True)


def _landed(tmp_path: Path, rel: str, before: str, after: str) -> tuple[Path, Path]:
    target = tmp_path / "repo" / rel
    backup = tmp_path / "backups" / "20260917-120000" / rel
    for p, text in ((target, after), (backup, before)):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return target, backup


def _restorer(target: Path, backup: Path):
    def _rollback() -> str:
        shutil.copy2(backup, target)
        return "Rolled it back."

    return _rollback


class TestApplyIntegration:
    def test_import_time_crash_passes_apply_gate_but_is_rolled_back(self, sandbox, monkeypatch):
        """The gap the verifier exists for: compiles, lints clean, dies on import."""
        monkeypatch.setattr(
            self_improve,
            "_generate_updated_code",
            lambda *a, **k: 'X = 2\nraise RuntimeError("boom at import")\n',
        )
        result = self_improve._apply(sandbox, "bump X")

        assert not result.startswith("Done")  # Protocol One reads "Done" as success
        assert "rolled" in result.lower() and "import" in result
        assert sandbox.read_text(encoding="utf-8") == "X = 1\n"
        rejected = list(
            (self_improve.BACKUP_ROOT.parent / "rejected").rglob("_verifier_selftest.py")
        )
        assert rejected and "boom at import" in rejected[0].read_text(encoding="utf-8")

    def test_clean_change_is_kept_and_says_so(self, sandbox, monkeypatch):
        monkeypatch.setattr(self_improve, "_generate_updated_code", lambda *a, **k: "X = 2\n")
        result = self_improve._apply(sandbox, "bump X")

        assert result.startswith("Done")
        assert "independent second check also passed" in result
        assert sandbox.read_text(encoding="utf-8") == "X = 2\n"


class TestTestAttribution:
    def _stub_static(self, monkeypatch, runs: list[set[str]]):
        monkeypatch.setattr(v, "select_tests", lambda rel, root: [Path("test_fake.py")])
        monkeypatch.setattr(v.importlib.util, "find_spec", lambda name: object())
        calls = iter(runs)

        def _pytest(files, root):
            failed = next(calls)
            return (not failed), failed, "stub"

        monkeypatch.setattr(v, "_pytest", _pytest)

    def test_failures_that_also_happen_without_the_change_keep_it(self, tmp_path, monkeypatch):
        target, backup = _landed(tmp_path, "mod_a.py", "X = 1\n", "X = 2\n")
        self._stub_static(monkeypatch, [{"t::a"}, {"t::a"}])

        verdict = v.verify(target, backup, "mod_a.py", tmp_path / "repo", _restorer(target, backup))

        assert verdict.status == "pass"
        assert "PRE-EXISTING" in verdict.report
        assert target.read_text(encoding="utf-8") == "X = 2\n"

    def test_failures_the_change_introduced_roll_it_back(self, tmp_path, monkeypatch):
        target, backup = _landed(tmp_path, "mod_b.py", "X = 1\n", "X = 2\n")
        self._stub_static(monkeypatch, [{"t::a", "t::new"}, {"t::a"}])

        verdict = v.verify(target, backup, "mod_b.py", tmp_path / "repo", _restorer(target, backup))

        assert verdict.status == "rolled_back"
        assert "t::new" in verdict.summary and "t::a" not in verdict.summary
        assert target.read_text(encoding="utf-8") == "X = 1\n"


class TestSafetyNets:
    def test_a_rollback_that_does_not_restore_is_reported_not_hidden(self, tmp_path, monkeypatch):
        target, backup = _landed(tmp_path, "mod_c.py", "X = 1\n", "X = (\n")

        verdict = v.verify(target, backup, "mod_c.py", tmp_path / "repo", lambda: "Rolled (lie)")

        assert verdict.status == "rollback_failed"
        assert "MANUAL ACTION REQUIRED" in verdict.report

    def test_nested_runs_are_skipped(self, tmp_path, monkeypatch):
        target, backup = _landed(tmp_path, "mod_d.py", "X = 1\n", "X = (\n")
        monkeypatch.setenv(v._GUARD_ENV, "1")

        verdict = v.verify(target, backup, "mod_d.py", tmp_path, lambda: pytest.fail("rolled back"))

        assert verdict.status == "skipped"

    def test_removed_confirmation_gate_is_flagged(self):
        before = 'if critical:\n    return {"requires_confirmation": True}\nX = 1\n'
        assert any("requires_confirmation" in f for f in v.diff_flags(before, "X = 1\n"))

    def test_large_shrink_is_flagged_as_truncation(self):
        before = "\n".join(f"A{i} = {i}" for i in range(100))
        after = "\n".join(f"A{i} = {i}" for i in range(50))
        assert any("truncation" in f for f in v.diff_flags(before, after))

    def test_ordinary_edit_raises_no_flags(self):
        assert v.diff_flags("X = 1\nY = 2\n", "X = 1\nY = 3\n") == []


class TestSelectTests:
    def test_finds_every_import_style_and_name_match_first(self):
        found = [p.name for p in v.select_tests("actions/self_improve.py", REPO_ROOT)]
        # test_lint_feedback uses `from actions import self_improve`,
        # test_protocol_one uses `import actions.self_improve as si`.
        assert "test_lint_feedback.py" in found
        assert "test_protocol_one.py" in found

    def test_the_verifier_selects_its_own_tests(self):
        found = [p.name for p in v.select_tests("actions/self_improve_verifier.py", REPO_ROOT)]
        assert found[0] == "test_self_improve_verifier.py"
