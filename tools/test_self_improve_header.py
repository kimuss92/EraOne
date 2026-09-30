"""A self-edit must not delete code it was not asked to touch. (Board item 27.)

WHAT HAPPENED
  On 2026-09-29 Era was asked to ADD a function to `actions/open_app.py`. The
  rewrite added it — and silently deleted the file's 9-line module header:

      # actions/open_app.py
      \"\"\"
      High-performance Windows Application Launcher for Era One.
      Features:
      - Ultra-fast Start Menu Shortcut (.lnk) indexing ...
      \"\"\"

  Every existing check passed. The file compiled, imported, linted and its
  tests were green; the removed lines matched no `_SAFETY_MARKERS`; and 9 lines
  out of 434 is a 2% shrink against `_SHRINK_LIMIT`'s 30%. Verdict: `pass`.
  Nobody found it until the backup was diffed by hand, days later.

WHY IT IS A ROLLBACK AND NOT A WARNING
  The generation prompt already says "Preserve all existing behavior that the
  change does not touch: comments, public function signatures, imports used
  elsewhere." An unverified instruction to a model is a preference, not a
  guarantee. And a model that discarded the header has already broken the
  non-destructive contract, so the rest of its rewrite is no more trustworthy.
  Rejecting costs little: `_keep_rejected` preserves the rejected version under
  `runtime/self_improve/rejected/`, so the work is recoverable in a way the
  deleted header was not.

THE OTHER HALF OF THE JOB
  A check this strict earns its place only if it cannot fire on an ordinary
  edit. Most of the tests below are therefore about what must still be allowed:
  rewording a docstring, extending it, reformatting it, adding one where there
  was none, and editing a file that never had a header at all.
"""

from __future__ import annotations

import pytest

from actions.self_improve_verifier import header_loss

REAL_BACKUP = "runtime/self_improve/backups/20260929-154441/actions/open_app.py"

WITH_HEADER = '''# actions/demo.py
"""
Demo module.
Features:
- one
"""

import os


def f():
    return os.sep
'''

NO_HEADER = """import os


def f():
    return os.sep
"""


# ── the deletion that shipped ───────────────────────────────────────────────


def test_the_real_2026_09_29_regression(tmp_path):
    """Not a synthetic case: the actual backup, and the actual edit made to it.

    The live self-edit is reconstructed exactly — drop the 9-line header, append
    the function that was asked for — so this test fails if the check ever stops
    recognising the one deletion that is known to have got through.
    """
    from pathlib import Path

    backup = Path(REAL_BACKUP)
    if not backup.is_file():
        pytest.skip(f"{REAL_BACKUP} has been cleaned up")
    before = backup.read_text(encoding="utf-8")
    assert before.startswith("# actions/open_app.py"), "the backup is not the one described"

    # What the model did: header gone, a function appended.
    after = before.split('"""', 2)[2].lstrip("\n") + "\n\ndef added():\n    return 1\n"

    assert (len(before.splitlines()) - len(after.splitlines())) / len(before.splitlines()) < 0.30, (
        "this must remain BELOW the truncation limit — that is why it got through"
    )
    lost = header_loss(before, after)
    assert lost, "the deletion that actually shipped is still invisible"
    assert "docstring" in lost


def test_a_deleted_docstring_is_reported_with_its_size():
    lost = header_loss(WITH_HEADER, NO_HEADER)
    assert lost and "docstring" in lost and "3 lines" in lost


def test_a_deleted_header_comment_block_is_caught_on_its_own():
    """The `# actions/open_app.py` provenance line was deleted too. A file whose
    header is comments and no docstring must be protected the same way."""
    before = "# module: demo\n# owner: era\n\nimport os\n\n\ndef f():\n    return os\n"
    after = "import os\n\n\ndef f():\n    return os\n"
    lost = header_loss(before, after)
    assert lost and "2-line header comment block" in lost


# ── what must still be allowed ─────────────────────────────────────────────


def test_rewording_a_docstring_is_allowed():
    after = WITH_HEADER.replace("Demo module.", "A launcher for Windows apps.")
    assert header_loss(WITH_HEADER, after) is None


def test_extending_a_docstring_is_allowed():
    after = WITH_HEADER.replace("- one\n", "- one\n- two\n- three\n")
    assert header_loss(WITH_HEADER, after) is None


def test_shortening_a_docstring_is_allowed_as_long_as_it_survives():
    """Narrow on purpose: this check is about REMOVAL, not about length. A
    stricter rule would start refusing the tidying edits Era is asked to make."""
    after = WITH_HEADER.replace("Demo module.\nFeatures:\n- one\n", "Demo module.\n")
    assert header_loss(WITH_HEADER, after) is None


def test_adding_a_docstring_where_there_was_none_is_allowed():
    after = '"""Now documented."""\n\n' + NO_HEADER
    assert header_loss(NO_HEADER, after) is None


def test_a_file_that_never_had_a_header_is_not_held_to_one():
    after = NO_HEADER + "\n\ndef g():\n    return 2\n"
    assert header_loss(NO_HEADER, after) is None


def test_changing_the_quote_style_is_not_a_deletion():
    """`ast` is used rather than a regex precisely so this reads as the same
    docstring, not as one deleted and another added."""
    before = '"""Doc."""\n\nimport os\n'
    after = "'''Doc.'''\n\nimport os\n"
    assert header_loss(before, after) is None


def test_a_docstring_that_follows_comments_is_still_found():
    before = "# lead-in\n\n" + '"""Doc."""\n\nimport os\n'
    after = "# lead-in\n\nimport os\n"
    lost = header_loss(before, after)
    assert lost and "docstring" in lost


def test_a_broken_rewrite_is_left_to_the_syntax_check():
    """Step 1 has already failed by then; this must not raise on top of it and
    must not invent a second, misleading reason for the same rollback."""
    assert header_loss(WITH_HEADER, "def f(:\n  pass\n") is None


# ── it must actually reach the verdict, not just compute a string ──────────


def test_the_verifier_rolls_back_on_a_deleted_header(tmp_path, monkeypatch):
    """The end-to-end contract: a lost header is a FAILURE, so the change does
    not stay on disk — not a `review` that leaves it there.
    """
    from actions import self_improve_verifier as v

    repo = tmp_path / "repo"
    (repo / "actions").mkdir(parents=True)
    target = repo / "actions" / "demo.py"
    backup = tmp_path / "backup.py"
    backup.write_text(WITH_HEADER, encoding="utf-8")
    target.write_text(NO_HEADER, encoding="utf-8")

    # Everything except step 7 passes, so the verdict can only come from it.
    monkeypatch.setattr(
        v, "_run", lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    )
    monkeypatch.setattr(v, "select_tests", lambda rel, root: [])
    monkeypatch.delenv("ERA_SELF_IMPROVE_VERIFYING", raising=False)

    rolled: list[str] = []

    def _rollback():
        # Worded as actions/self_improve._rollback words it: _restore_ok checks
        # the message AND the bytes, so a fake that only copies is not enough.
        target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        rolled.append("done")
        return "Rolled actions/demo.py back to the backup."

    verdict = v.verify(target, backup, "actions/demo.py", repo, _rollback)

    assert verdict.status == "rolled_back", f"a deleted header produced {verdict.status!r}"
    assert rolled, "the change was kept"
    assert "Header:   FAIL" in verdict.report
    assert target.read_text(encoding="utf-8") == WITH_HEADER, "the header did not come back"


def test_an_intact_header_still_passes_cleanly(tmp_path, monkeypatch):
    """The check must not make every self-edit unreloadable — `reload_safe` is
    granted only on a clean `pass`, so a false FAIL here would quietly disable
    acceptance point 1."""
    from actions import self_improve_verifier as v

    repo = tmp_path / "repo"
    (repo / "actions").mkdir(parents=True)
    target = repo / "actions" / "demo.py"
    backup = tmp_path / "backup.py"
    backup.write_text(WITH_HEADER, encoding="utf-8")
    target.write_text(WITH_HEADER.replace("return os.sep", "return os.sep + ''"), encoding="utf-8")

    monkeypatch.setattr(
        v, "_run", lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    )
    monkeypatch.setattr(v, "select_tests", lambda rel, root: [])
    monkeypatch.delenv("ERA_SELF_IMPROVE_VERIFYING", raising=False)

    verdict = v.verify(target, backup, "actions/demo.py", repo, lambda: "never")
    assert verdict.status == "pass", verdict.report
    assert "Header:   OK" in verdict.report
