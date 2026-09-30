"""Tests for the deterministic lint-repair layer.

The regression these guard: self-improve used to fail `ruff check`, roll back,
and retry from the SAME prompt — so I001 (import order) and F401 (unused
import) came back every time. Two properties keep that fixed:

  * ruff repairs what ruff can repair, with no model in the loop;
  * whatever survives reaches the model as a named rule with a named fix.

The autofix tests run the real ruff against a real temp file. That is the
point: a mocked subprocess would have passed happily while ruff's actual JSON
schema drifted underneath.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from actions import self_improve
from core import lint_feedback
from core.lint_feedback import Finding

REPO_ROOT = Path(__file__).resolve().parent.parent

_RUFF_OK = (
    subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True).returncode == 0
)
needs_ruff = pytest.mark.skipif(not _RUFF_OK, reason="ruff not installed")


def _write(tmp_path: Path, body: str) -> Path:
    """A target ruff will lint with THIS repo's ruff.toml (E,F,I,W,UP,B).

    Named like a normal module on purpose: `test_*.py` carries a per-file
    ignore for F401 in ruff.toml, which would silently void half these tests.
    """
    p = tmp_path / "sample_module.py"
    p.write_text(body, encoding="utf-8")
    return p


# ── Parsing ──────────────────────────────────────────────────────────────────


class TestParseDiagnostics:
    def test_extracts_code_line_and_safe_fixability(self):
        raw = json.dumps(
            [
                {
                    "code": "F401",
                    "name": "unused-import",
                    "message": "`os` imported but unused",
                    "location": {"row": 3, "column": 8},
                    "fix": {
                        "applicability": "safe",
                        "message": "Remove unused import: `os`",
                    },
                    "url": "https://docs.astral.sh/ruff/rules/unused-import",
                }
            ]
        )
        (f,) = lint_feedback.parse_diagnostics(raw)
        assert (f.code, f.line, f.rule) == ("F401", 3, "unused-import")
        assert f.fixable is True
        assert f.fix_message == "Remove unused import: `os`"

    def test_unsafe_fix_is_not_treated_as_fixable(self):
        """An unsafe fix can change behavior — it stays a model decision.

        F401 in a package __init__ is the case that matters: ruff's fix deletes
        a deliberate re-export, so it must never be applied unattended.
        """
        raw = json.dumps(
            [
                {
                    "code": "F401",
                    "name": "unused-import",
                    "message": "`x` imported but unused",
                    "location": {"row": 1, "column": 1},
                    "fix": {"applicability": "unsafe", "message": "Remove"},
                }
            ]
        )
        (f,) = lint_feedback.parse_diagnostics(raw)
        assert f.fixable is False
        assert f.fix_message == ""

    def test_no_fix_key_means_not_fixable(self):
        raw = json.dumps(
            [
                {
                    "code": "F821",
                    "name": "undefined-name",
                    "message": "Undefined name `foo`",
                    "location": {"row": 9, "column": 1},
                    "fix": None,
                }
            ]
        )
        assert lint_feedback.parse_diagnostics(raw)[0].fixable is False

    @pytest.mark.parametrize("raw", ["", "not json", "{}", "null", "[1, 2]"])
    def test_garbage_never_raises(self, raw):
        """Degrade, never raise: a broken linter costs lint coverage, not the job."""
        assert lint_feedback.parse_diagnostics(raw) == []


# ── Remediation lookup ───────────────────────────────────────────────────────


class TestRemediation:
    def test_i001_describes_ruffs_own_isort_ordering(self):
        text = lint_feedback.remediation_for("I001")
        assert "isort" in text
        assert "__future__" in text and "third-party" in text
        # Must not invite the model to add or drop imports while re-sorting.
        assert "Do not add, remove" in text

    def test_f401_names_the_deletion_and_the_noqa_escape(self):
        text = lint_feedback.remediation_for("F401")
        assert "Delete the whole import statement" in text
        assert "noqa: F401" in text
        assert "invent a use" in text

    def test_unknown_code_falls_back_to_its_family(self):
        assert lint_feedback.remediation_for("F999") == lint_feedback.remediation_for("F998")
        assert "pyflakes" in lint_feedback.remediation_for("F999")
        assert "pyupgrade" in lint_feedback.remediation_for("UP045")

    def test_wholly_unknown_family_still_returns_guidance(self):
        assert lint_feedback.remediation_for("ZZ1").strip()


# ── The repair brief ─────────────────────────────────────────────────────────


def _finding(code: str, line: int = 1, msg: str = "boom", fix: str = "") -> Finding:
    return Finding(
        code=code,
        line=line,
        column=1,
        message=msg,
        rule="some-rule",
        fixable=bool(fix),
        fix_message=fix,
    )


class TestRepairBrief:
    def test_lists_every_finding_with_its_line(self):
        brief = lint_feedback.repair_brief(
            [_finding("F401", 3, "`os` imported but unused"), _finding("I001", 1)]
        )
        assert "line 3" in brief and "`os` imported but unused" in brief
        assert "F401" in brief and "I001" in brief

    def test_remediation_is_deduplicated_per_rule(self):
        """Twelve F401s need the instruction once, not twelve times."""
        brief = lint_feedback.repair_brief([_finding("F401", i) for i in range(1, 13)])
        assert brief.count("Delete the whole import statement") == 1
        assert brief.count("- F401") == 12 or brief.count("F401 (") == 12

    def test_long_finding_lists_are_capped_but_counted(self):
        brief = lint_feedback.repair_brief([_finding("E711", i) for i in range(1, 60)])
        assert "more of the same kinds" in brief
        assert len(brief.splitlines()) < 40

    def test_empty_findings_produce_no_brief(self):
        assert lint_feedback.repair_brief([]) == ""


class TestRepairPrompt:
    def test_carries_current_code_diagnostics_and_rules(self):
        prompt = lint_feedback.repair_prompt(
            "core/thing.py",
            "import os\n",
            "add a docstring",
            ["ruff: F401 ..."],
            [_finding("F401", 1, "`os` imported but unused")],
        )
        assert "core/thing.py" in prompt
        assert "add a docstring" in prompt  # the original ask is context, not the retry
        assert "import os" in prompt
        assert "Delete the whole import statement" in prompt
        assert "Fix ONLY the problems listed" in prompt

    def test_non_lint_failures_are_passed_through_verbatim(self):
        prompt = lint_feedback.repair_prompt(
            "core/thing.py",
            "x = 1\n",
            "do a thing",
            ["ruff: I001 ...", "tests (test_thing.py): assert 1 == 2"],
            [_finding("I001")],
        )
        assert "assert 1 == 2" in prompt
        assert prompt.count("ruff: I001") == 0  # the brief covers lint, not this list


# ── Running the real ruff ────────────────────────────────────────────────────


@needs_ruff
class TestRunRuff:
    def test_clean_file_reports_nothing(self, tmp_path):
        target = _write(tmp_path, "X = 1\n")
        findings, err = lint_feedback.run_ruff(target, REPO_ROOT)
        assert (findings, err) == ([], "")

    def test_i001_and_f401_are_both_detected_and_fixable(self, tmp_path):
        target = _write(tmp_path, "import sys\nimport os\nimport json\n\nX = 1\n")
        findings, err = lint_feedback.run_ruff(target, REPO_ROOT)
        assert err == ""
        codes = {f.code for f in findings}
        assert "I001" in codes and "F401" in codes
        assert all(f.fixable for f in findings if f.code in {"I001", "F401"})

    def test_undefined_name_is_reported_but_not_fixable(self, tmp_path):
        target = _write(tmp_path, "X = undefined_thing\n")
        findings, _ = lint_feedback.run_ruff(target, REPO_ROOT)
        f821 = [f for f in findings if f.code == "F821"]
        assert f821 and not f821[0].fixable

    def test_missing_ruff_is_unavailable_not_a_failure(self, tmp_path, monkeypatch):
        """No ruff must cost lint coverage, never the job."""
        monkeypatch.setattr(lint_feedback, "_run", lambda *a, **k: None)
        findings, err = lint_feedback.run_ruff(_write(tmp_path, "X = 1\n"), REPO_ROOT)
        assert findings == [] and err == lint_feedback.UNAVAILABLE


@needs_ruff
class TestAutofix:
    def test_sorts_imports_and_removes_unused_ones_without_a_model(self, tmp_path):
        target = _write(tmp_path, "import sys\nimport os\n\nprint(os.sep)\n")
        notes = lint_feedback.autofix(target, REPO_ROOT)
        after = target.read_text(encoding="utf-8")
        assert "import sys" not in after  # F401 gone
        assert "import os" in after  # the used one survives
        assert notes and any("auto-fixed" in n for n in notes)
        assert lint_feedback.run_ruff(target, REPO_ROOT)[0] == []

    def test_clean_file_is_left_alone_and_reports_no_repair(self, tmp_path):
        """Empty notes is the caller's signal to stop rather than spin."""
        target = _write(tmp_path, "X = 1\n")
        before = target.read_text(encoding="utf-8")
        assert lint_feedback.autofix(target, REPO_ROOT) == []
        assert target.read_text(encoding="utf-8") == before

    def test_unfixable_violation_survives_for_the_model(self, tmp_path):
        target = _write(tmp_path, "import os\n\nX = undefined_thing\n")
        lint_feedback.autofix(target, REPO_ROOT)
        codes = {f.code for f in lint_feedback.run_ruff(target, REPO_ROOT)[0]}
        assert "F821" in codes  # never silenced
        assert "F401" not in codes  # but the mechanical one is gone


# ── End-to-end through self_improve ──────────────────────────────────────────


@pytest.fixture
def sandbox_target(tmp_path, monkeypatch):
    """A real repo-relative file self_improve is allowed to rewrite.

    Deliberately NOT under runtime/ or another ruff.toml `extend-exclude` path:
    an excluded target only gets linted because ruff skips exclusions for paths
    named explicitly, and a test resting on that would go vacuously green the
    day that changes. The leading underscore keeps pytest from collecting it.
    Points BACKUP_ROOT at tmp_path so the suite never writes real backups.
    """
    target = REPO_ROOT / "tools" / "_lint_feedback_selftest.py"
    target.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(self_improve, "BACKUP_ROOT", tmp_path / "backups")
    yield target
    target.unlink(missing_ok=True)
    shutil.rmtree(tmp_path / "backups", ignore_errors=True)


@needs_ruff
class TestApplyHealsLintFailures:
    def test_i001_f401_output_is_healed_instead_of_rolled_back(self, sandbox_target, monkeypatch):
        """The exact regression: a rewrite whose ONLY sin is import hygiene.

        It used to fail verification and roll back; now ruff settles it and the
        change lands, with the repair reported rather than hidden.
        """
        sloppy = "import sys\nimport os\n\n\ndef go():\n    return os.sep\n"
        monkeypatch.setattr(self_improve, "_generate_updated_code", lambda *a, **k: sloppy)

        def _no_model(*a, **k):
            raise AssertionError("a model was consulted for a mechanical lint fix")

        monkeypatch.setattr(self_improve, "_generate_repair", _no_model)

        result = self_improve._apply(sandbox_target, "add a go() helper")

        assert "rolled" not in result.lower()
        assert "auto-fixed" in result
        healed = sandbox_target.read_text(encoding="utf-8")
        assert "import sys" not in healed and "def go():" in healed

    def test_unhealable_failure_still_rolls_back_and_names_the_rule(
        self, sandbox_target, monkeypatch
    ):
        """Self-healing must not become self-breaking: the gate still holds."""
        original = sandbox_target.read_text(encoding="utf-8")
        monkeypatch.setattr(
            self_improve,
            "_generate_updated_code",
            lambda *a, **k: "X = 1\nY = undefined_thing\n",
        )
        monkeypatch.setattr(
            self_improve, "_generate_repair", lambda *a, **k: "Z = still_undefined\n"
        )

        result = self_improve._apply(sandbox_target, "break it")

        assert "rolled" in result.lower()
        assert "F821" in result
        assert sandbox_target.read_text(encoding="utf-8") == original

    def test_repair_pass_receives_the_diagnostics_not_the_original_prompt(
        self, sandbox_target, monkeypatch
    ):
        """The heart of the fix — the second attempt must not be context-free."""
        seen: dict = {}
        monkeypatch.setattr(
            self_improve,
            "_generate_updated_code",
            lambda *a, **k: "X = 1\nY = undefined_thing\n",
        )

        def _capture(target, current, instruction, failures, findings):
            seen["findings"] = findings
            seen["prompt"] = lint_feedback.repair_prompt(
                "x.py", current, instruction, failures, findings
            )
            return "X = 1\n"

        monkeypatch.setattr(self_improve, "_generate_repair", _capture)
        self_improve._apply(sandbox_target, "add a Y")

        assert {f.code for f in seen["findings"]} == {"F821"}
        assert "undefined_thing" in seen["prompt"]
        assert "not defined anywhere in this file" in seen["prompt"]

    def test_a_clean_rewrite_triggers_no_repair_machinery(self, sandbox_target, monkeypatch):
        monkeypatch.setattr(self_improve, "_generate_updated_code", lambda *a, **k: "X = 2\n")
        monkeypatch.setattr(
            self_improve,
            "_generate_repair",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("repaired a clean file")),
        )
        result = self_improve._apply(sandbox_target, "bump X")
        assert "Done" in result and "auto-fixed" not in result
