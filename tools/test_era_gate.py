"""Test selection is the gate's brain — a wrong answer here is silent.

If selection quietly misses the test that covers a change, the gate reports
PASS and the regression ships with a green tick next to it. That failure mode
is worse than having no gate at all, because it manufactures confidence.

So the properties tested here are mostly about what must NOT be missed:

  a direct importer is always selected
  an indirect importer is selected within the depth bound
  a changed test file runs itself
  a changed file that NOTHING reaches is reported, never silently dropped

The synthetic manifest below is deliberately tiny. Selection is graph logic,
and graph logic is easier to get wrong than to read — a four-node graph where
the right answer is obvious by inspection is what makes a wrong answer visible.
"""

from __future__ import annotations

import pytest

from tools import era_gate

# core.leaf  <- core.middle  <- tools.test_indirect
#            <- tools.test_direct
# tools.test_unrelated imports nothing relevant.
MANIFEST = {
    "modules": [
        {"path": "core/leaf.py", "module": "core.leaf", "imports": [], "is_test": False},
        {
            "path": "core/middle.py",
            "module": "core.middle",
            "imports": ["core.leaf"],
            "is_test": False,
        },
        {
            "path": "core/far.py",
            "module": "core.far",
            "imports": ["core.middle", "core.deep"],
            "is_test": False,
        },
        # Reachable only at depth >= 2 (tools/test_distant -> core.far -> here).
        {"path": "core/deep.py", "module": "core.deep", "imports": [], "is_test": False},
        {
            "path": "tools/test_direct.py",
            "module": "tools.test_direct",
            "imports": ["core.leaf"],
            "is_test": True,
        },
        {
            "path": "tools/test_indirect.py",
            "module": "tools.test_indirect",
            "imports": ["core.middle"],
            "is_test": True,
        },
        {
            "path": "tools/test_distant.py",
            "module": "tools.test_distant",
            "imports": ["core.far"],
            "is_test": True,
        },
        {
            "path": "tools/test_unrelated.py",
            "module": "tools.test_unrelated",
            "imports": [],
            "is_test": True,
        },
        # Nothing imports this one. It exists so the "no test reaches it" path
        # is testable at all: every other module here is imported by some test,
        # so none of them can demonstrate an uncovered file.
        {"path": "core/orphan.py", "module": "core.orphan", "imports": [], "is_test": False},
    ]
}


def select(changed, depth=2):
    return era_gate.select_tests(changed, MANIFEST, depth)


def test_a_direct_importer_is_selected():
    selected, uncovered = select(["core/leaf.py"])
    assert "tools/test_direct.py" in selected
    assert not uncovered


def test_an_indirect_importer_is_selected_within_the_depth_bound():
    selected, _ = select(["core/leaf.py"], depth=2)
    assert "tools/test_indirect.py" in selected, "one hop away must not be missed"


def test_an_unrelated_test_is_not_selected():
    selected, _ = select(["core/leaf.py"])
    assert "tools/test_unrelated.py" not in selected


def test_depth_one_keeps_only_direct_importers():
    selected, _ = select(["core/leaf.py"], depth=1)
    assert selected == ["tools/test_direct.py"]


def test_depth_zero_is_unbounded_and_reaches_the_distant_test():
    selected, _ = select(["core/leaf.py"], depth=None)
    assert "tools/test_distant.py" in selected, "unbounded must follow the whole chain"


def test_the_depth_bound_really_does_exclude_things():
    """Guards the tradeoff itself.

    If this ever passes with depth 2, the bound has stopped bounding and the
    'fast' tier is quietly running the whole suite again.
    """
    selected, _ = select(["core/leaf.py"], depth=2)
    assert "tools/test_distant.py" not in selected


def test_a_changed_test_file_runs_itself():
    selected, uncovered = select(["tools/test_unrelated.py"])
    assert selected == ["tools/test_unrelated.py"]
    assert not uncovered


def test_a_file_no_test_reaches_is_reported_not_dropped():
    """The gate must never imply it vouched for something it did not run.

    A changed file with no covering test is a hole in the safety net. Saying
    nothing about it is how the gate would report PASS for code it never
    executed — the one failure mode that is worse than having no gate.
    """
    selected, uncovered = select(["core/orphan.py"])
    assert uncovered == ["core/orphan.py"]
    assert selected == []


def test_the_cost_of_the_depth_bound_is_reported_not_hidden():
    """The tradeoff must be visible at the moment it bites.

    core/deep.py IS covered — by tools/test_distant.py, two hops away. At
    depth 1 that test is not selected, and the gate must SAY so rather than
    report a clean pass over code it chose not to exercise. Raising the depth
    finds it, which is the whole point of the flag being adjustable.
    """
    selected, uncovered = select(["core/deep.py"], depth=1)
    assert uncovered == ["core/deep.py"]
    assert selected == []

    selected, uncovered = select(["core/deep.py"], depth=2)
    assert "tools/test_distant.py" in selected
    assert not uncovered


def test_an_unknown_path_is_reported_as_uncovered():
    _selected, uncovered = select(["core/does_not_exist.py"])
    assert uncovered == ["core/does_not_exist.py"]


def test_several_changed_files_union_their_tests():
    selected, _ = select(["core/leaf.py", "core/far.py"], depth=1)
    assert set(selected) == {"tools/test_direct.py", "tools/test_distant.py"}


def test_windows_path_separators_are_accepted():
    """The caller pastes paths from anywhere; both spellings name one file."""
    selected, uncovered = select(["core\\leaf.py"])
    assert "tools/test_direct.py" in selected
    assert not uncovered


@pytest.mark.parametrize("depth", [1, 2, 3, None])
def test_selection_never_includes_a_non_test_file(depth):
    selected, _ = select(["core/leaf.py"], depth)
    assert all("test_" in path for path in selected)


def test_the_real_manifest_loads_and_selects():
    """An end-to-end check against the committed manifest, not the fixture.

    The synthetic graph proves the logic; this proves the logic is wired to
    real data with the field names the generator actually writes.
    """
    manifest = era_gate.load_manifest()
    selected, uncovered = era_gate.select_tests(
        ["core/llm/claude_accounts.py"], manifest, max_depth=2
    )
    assert not uncovered, "a module with tests was reported as uncovered"
    assert "tools/test_claude_account_failover.py" in selected
