"""The gate for the script-style `check()` diagnostics under tools/.

WHY THIS EXISTS
  Ten files under tools/ are diagnostic SCRIPTS, not pytest modules. Each
  defines its own `check(name, condition)` that increments a counter and
  PRINTS `PASS:` / `FAIL:` — and that is all it does. It never raises, and the
  non-zero exit lives behind `if __name__ == "__main__"`, which pytest never
  takes. Seven of them additionally set `__test__ = False`.

  So under `python -m pytest tools/ -q` those files are imported, their checks
  execute as import side effects, their failures are printed into captured
  stdout nobody reads, and the file reports green. The project's real gate
  (ruff + pytest) could not see them at all.

  That is not theoretical. Two assertions in `test_memory_safety` sat green for
  months while asserting the OPPOSITE of correct behaviour — they encoded the
  same-key dedup bug that was silently discarding small memory corrections. And
  `test_main_tool_bridge` asserted a `[COMPACT MEMORY` header that no version of
  `compact_memory_preview()` has ever emitted; it had been failing since the day
  it was written. Both were invisible to CI. See [[era-source-text-tooling]].

WHY A SEPARATE FILE RATHER THAN MAKING check() RAISE
  Raising inside `check()` would abort each script at its FIRST failure, which
  throws away the property these scripts were built for: run everything, report
  every failure in one pass. It would also turn an import-time failure into a
  pytest COLLECTION ERROR — which fails the build, but reports as a broken file
  rather than a named failing assertion, and would break their standalone use as
  diagnostics. Reading the counters afterwards keeps both behaviours: the
  scripts still run in full and print every failure, and the gate still goes red.

WHY THE `zz` IN THE FILENAME
  pytest imports and runs test files in collection (alphabetical) order, and
  three of these scripts accumulate their counters while pytest RUNS them
  (`test_logging`, `test_domain_validation`, `test_memory_safety` define real
  test functions) rather than at import. Sorting last is what guarantees every
  counter is final before it is read. `_module()` below still imports anything
  not already loaded, so the gate is correct even if that ordering changes —
  the name is belt, the import is braces.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent

# module name -> attribute holding its failure count.
#
# The three spellings are historical, not meaningful: `failed` (most),
# `fail_count` (the smoke/registry pair), `_failures` (test_logging). They are
# listed explicitly rather than sniffed so that RENAMING a counter breaks this
# gate loudly instead of silently reverting the file to unguarded.
_SCRIPTS: dict[str, str] = {
    "test_browser_intelligence": "failed",
    "test_domain_validation": "failed",
    "test_era_doctor": "failed",
    "test_execution_layer": "failed",
    "test_logging": "_failures",
    "test_main_tool_bridge": "failed",
    "test_memory_safety": "failed",
    "test_tool_registry_validator": "fail_count",
    "test_verification_layer": "failed",
}

# test_smoke_e2e is gated by SUBPROCESS instead (see below), so it is
# deliberately absent from _SCRIPTS.
_SUBPROCESS_SCRIPTS = ("test_smoke_e2e",)

# These two keep their checks inside `run()` instead of at module level, so
# importing them proves nothing — under pytest their diagnostics have never
# executed at all. The gate calls run() itself, which is a net GAIN in coverage,
# not just a report of it.
_NEEDS_RUN = frozenset({"test_execution_layer", "test_verification_layer"})


def _module(name: str):
    """The already-imported script module, importing it if pytest has not yet.

    pytest puts tools/ on sys.path (there is no tools/__init__.py), so these
    import as top-level modules and sys.modules is shared — this reuses the
    instance pytest already ran rather than re-executing it. The sys.path
    insert is only for running this file directly.
    """
    if name in sys.modules:
        return sys.modules[name]
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    return importlib.import_module(name)


@pytest.mark.parametrize("script", sorted(_SCRIPTS))
def test_script_checks_all_passed(script: str) -> None:
    """Fail the gate when a script-style diagnostic reported any FAIL."""
    mod = _module(script)

    if script in _NEEDS_RUN and getattr(mod, "_gate_ran", False) is False:
        mod.run()
        mod._gate_ran = True

    attr = _SCRIPTS[script]
    assert hasattr(mod, attr), (
        f"{script} no longer has a '{attr}' counter — its check() helper was "
        f"renamed or removed. Update _SCRIPTS in this file, or the script has "
        f"silently stopped being gated."
    )
    failures = getattr(mod, attr)
    passes = getattr(mod, "passed", getattr(mod, "pass_count", None))

    # Zero failures because zero checks RAN is not a pass — it is precisely the
    # vacuous green this whole file exists to abolish. It happens when the gate
    # is run on its own: the three scripts that accumulate their counters from
    # pytest test functions (test_logging, test_domain_validation,
    # test_memory_safety) have only been imported, not run. Report that as
    # inconclusive rather than success.
    #
    # test_logging keeps no module-level pass counter, so it cannot be checked
    # this way; it is covered under a full `pytest tools/` run, which is the
    # only context in which this gate is authoritative.
    if passes is not None and passes == 0 and failures == 0:
        pytest.skip(
            f"{script} reported no checks at all — it was imported but not run. "
            f"This gate is authoritative only under a full `pytest tools/`."
        )

    assert failures == 0, (
        f"{script}.py reported {failures} failing check(s) "
        f"({passes} passed). Its check() only prints, so the detail is in that "
        f"file's captured stdout — run it directly to see which:\n"
        f"    PYTHONPATH=. python tools/{script}.py"
    )


# Above the 600s subprocess ceiling below, so the ceiling stays the thing that
# bounds this test. Without the override, pytest.ini's 120s default (board item
# 18) would kill the run here — this file alone can exceed all the others
# combined — and report the suite's slowest legitimate test as a hang.
@pytest.mark.timeout(660)
@pytest.mark.parametrize("script", _SUBPROCESS_SCRIPTS)
def test_script_passes_in_its_own_process(script: str) -> None:
    """Gate the scripts that must OWN the interpreter they run in.

    test_smoke_e2e is a true end-to-end script, not a module: it mkdtemps a log
    directory, points ERA_LOG_DIR at it, and only THEN imports era_one so the
    logger binds to it. Inside a shared pytest process that premise is already
    broken — conftest has imported and bound the logger long before, so the
    executor writes to conftest's log dir while the script reads its own, and
    all fifteen of its log-reading checks fail for purely environmental
    reasons. (Standalone it is 72/72.)

    It is also not side-effect free in-process: at module end it rmtree's its
    log dir and calls clear_pending(), which mutates the shared pending store
    other tests rely on.

    So it is run the way it is built to run — its own interpreter, its own
    environment — and gated on its exit code. That is a stronger gate than
    reading a counter, and it matches what test_era_doctor already does with
    the doctor.
    """
    # encoding/errors rather than text=True: on Windows text=True decodes with
    # the console codepage (cp1252 here), and Era's own output carries
    # characters it cannot represent. The reader thread then dies mid-decode
    # and subprocess hands back stdout=None with a perfectly normal exit code —
    # a failure that looks like the script produced nothing at all.
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / f"{script}.py")],
        cwd=str(_TOOLS_DIR.parent),
        env={**os.environ, "PYTHONPATH": str(_TOOLS_DIR.parent)},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-25:])
    assert proc.returncode == 0, (
        f"{script}.py exited {proc.returncode} — its checks failed.\n"
        f"Reproduce with:\n    PYTHONPATH=. python tools/{script}.py\n\n{tail}"
    )


def test_every_check_script_is_listed() -> None:
    """A new script-style diagnostic must be added to _SCRIPTS.

    Without this, someone copies the pattern into an eleventh file and it is
    unguarded again — which is exactly how this class of bug persisted.
    """
    on_disk = {
        p.stem
        for p in _TOOLS_DIR.glob("test_*.py")
        if "def check(" in p.read_text(encoding="utf-8", errors="replace")
    }
    on_disk.discard(Path(__file__).stem)
    missing = sorted(on_disk - set(_SCRIPTS) - set(_SUBPROCESS_SCRIPTS))
    assert not missing, (
        f"These tools/ scripts define their own check() but are not gated: "
        f"{missing}. Add each to _SCRIPTS with the name of its failure counter."
    )
