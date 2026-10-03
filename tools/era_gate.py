#!/usr/bin/env python3
"""The gate that runs after a change — fast enough to actually be run.

WHY NOT JUST `pytest`
  The full suite is 5,083 tests and takes over twenty minutes (one script is
  allowed a 600s subprocess of its own — tools/test_zz_check_gate.py). A gate
  that costs twenty minutes is not run after a three-line fix, and a gate that
  is not run is not a gate. The regression it was meant to catch ships anyway.

  So there are two tiers, and the fast one is the default:

    era_gate.py --changed core/llm/registry.py
        Runs only the tests that actually reach that module, found through the
        import graph in runtime/manifest.json. Typically seconds.

    era_gate.py
        The whole suite minus the files measured to be slow. Minutes.

    era_gate.py --all
        Everything. Before finishing a piece of work, not during it.

HOW "THE TESTS THAT REACH IT" ARE FOUND
  runtime/manifest.json records, for every module, which first-party modules it
  imports — including imports written inside functions, which this repo uses
  heavily to break cycles. Closing that relation transitively gives, for each
  test module, the full set of modules it can execute. A test is selected when
  a changed module is in that set.

  This is derived from the code, not from a naming convention: renaming
  test_foo.py does not hide it, and a test that reaches a module only through
  three layers of imports is still selected.

  It is a REACHABILITY relation, not a coverage measurement. It over-selects
  (importing a module is not the same as exercising it) and it cannot see
  runtime-only coupling such as importlib or a subprocess. Over-selecting is
  the safe direction; the subprocess blind spot is why --all exists and why
  the slow tier still runs before work is called done.

EVERY TIER ALSO RUNS THE CHEAP INVARIANTS
  ruff, the manifest freshness check, and era_doctor together cost a few
  seconds and catch a different class of breakage than any test does.

USAGE
  python tools/era_gate.py                      # fast tier
  python tools/era_gate.py --changed a.py b.py  # only what those reach
  python tools/era_gate.py --all                # everything
  python tools/era_gate.py --list               # what would run, run nothing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "runtime" / "manifest.json"

# Measured, not guessed: these are the files whose runtime dominates the suite.
# Populate from `pytest --durations=40`; an entry here is a claim that the file
# was TIMED, so a guess would make the fast tier quietly skip working tests.
SLOW_FILES: tuple[str, ...] = (
    # tools/test_zz_check_gate.py runs tools/test_smoke_e2e.py in its own
    # interpreter with a 600s ceiling (see that file's docstring for why it
    # cannot share the pytest process). It alone can exceed the rest combined.
    "tools/test_zz_check_gate.py",
)


def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise FileNotFoundError(
            f"{MANIFEST.relative_to(REPO)} is missing. Run: python tools/era_manifest.py"
        )
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _module_for(path: str, manifest: dict) -> str | None:
    """Repo-relative path -> dotted module name, as the manifest spells it."""
    wanted = path.replace("\\", "/").lstrip("./")
    for mod in manifest["modules"]:
        if mod["path"] == wanted:
            return mod.get("module")
    return None


def reachable_from(
    test_module: str, imports: dict[str, set[str]], max_depth: int | None = None
) -> set[str]:
    """Modules a test can execute, following imports up to `max_depth` hops.

    WHY DEPTH IS BOUNDED BY DEFAULT
      Unbounded closure is the honest relation but a poor filter here, because
      the repo is tightly coupled — nearly every module reaches core/utils.py,
      so nearly every test reaches nearly everything.

      MEASURED, changing one leaf module (core/llm/claude_accounts.py), out of
      159 test files:

          --depth 1     1 file    tests that import it directly
          --depth 2     2 files   + one module removed   <- default
          --depth 3     9 files
          --depth 0    99 files   unbounded: 62% of the suite

      Depth 2 is the default because a break caused by a change shows up in
      the tests that touch it or its immediate callers. It is a heuristic, and
      a heuristic can miss a deep path — which is exactly why --all exists and
      why it still runs before a piece of work is called done.
    """
    seen: set[str] = set()
    frontier = set(imports.get(test_module, ()))
    depth = 0
    while frontier:
        depth += 1
        seen |= frontier
        if max_depth and depth >= max_depth:
            break
        nxt: set[str] = set()
        for module in frontier:
            nxt |= imports.get(module, set()) - seen
        frontier = nxt
    return seen


def select_tests(
    changed: list[str], manifest: dict, max_depth: int | None = 2
) -> tuple[list[str], list[str]]:
    """(test files to run, changed paths nothing covers)."""
    imports: dict[str, set[str]] = {
        m["module"]: set(m.get("imports", ()))
        for m in manifest["modules"]
        if m.get("module") and not m.get("error")
    }
    tests = [m for m in manifest["modules"] if m.get("is_test") and not m.get("error")]

    targets: dict[str, str] = {}  # dotted module -> the path the user named
    uncovered: list[str] = []
    for path in changed:
        normalized = path.replace("\\", "/").lstrip("./")
        # A changed test file always runs itself.
        if Path(normalized).name.startswith("test_"):
            targets[normalized] = normalized
            continue
        module = _module_for(normalized, manifest)
        if module:
            targets[module] = normalized
        else:
            uncovered.append(normalized)

    selected: set[str] = set()
    hit: set[str] = set()
    for test in tests:
        if test["path"] in targets:  # the changed file IS a test
            selected.add(test["path"])
            hit.add(test["path"])
            continue
        reach = reachable_from(test["module"], imports, max_depth)
        for module in targets:
            if module in reach:
                selected.add(test["path"])
                hit.add(module)

    for module, original in targets.items():
        if module not in hit:
            uncovered.append(original)

    return sorted(selected), sorted(set(uncovered))


def run(label: str, cmd: list[str]) -> tuple[bool, float]:
    print(f"\n=== {label} " + "=" * max(0, 60 - len(label)))
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(REPO))
    elapsed = time.perf_counter() - started
    ok = proc.returncode == 0
    print(f"--- {label}: {'PASS' if ok else 'FAIL'} in {elapsed:.1f}s")
    return ok, elapsed


def invariants(scope: list[str] | None = None) -> list[tuple[str, bool, float]]:
    """The checks that cost seconds and catch what tests do not.

    LINT SCOPE FOLLOWS THE TIER, and that is a deliberate choice.
      The repo carries pre-existing lint debt (measured 2026-09-29: 18 errors
      and 12 files needing reformat, none of it in code changed since). A gate
      that runs `ruff check .` therefore fails on every run, for reasons that
      have nothing to do with the change being gated — and a gate that always
      fails teaches people to ignore it, which is worse than not having one.

      So `--changed` lints exactly the changed files: you are answerable for
      what you touched. The untargeted tiers still lint everything, so the
      debt stays visible rather than being defined away — see TODO.md.

    THE MANIFEST IS REGENERATED, NOT CHECKED.
      `--check` was the first design and it was wrong in practice: the manifest
      is a DERIVED artifact, so any source edit makes it stale, and the gate
      then failed on the one problem it could have fixed itself. Measured over
      one session that cost four wasted cycles, each ending in the same
      mechanical command. Regenerating is idempotent and takes ~3s.

      The strict check still exists where it earns its keep — as a pytest
      assertion (tools/test_era_manifest.py), so anyone running the suite
      directly is still told the map has drifted rather than reading a stale
      one. The gate fixes it; the suite reports it.
    """
    py = sys.executable
    targets = list(scope) if scope else ["."]
    steps = [
        ("ruff format", [py, "-m", "ruff", "format", "--check", *targets]),
        ("ruff lint", [py, "-m", "ruff", "check", *targets]),
        ("manifest", [py, "tools/era_manifest.py"]),
        ("era_doctor", [py, "tools/era_doctor.py"]),
    ]
    return [(label, *run(label, cmd)) for label, cmd in steps]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", nargs="*", default=None, help="files that were edited")
    parser.add_argument("--all", action="store_true", help="run the entire suite")
    parser.add_argument("--list", action="store_true", help="print the plan, run nothing")
    parser.add_argument("--no-invariants", action="store_true", help="tests only")
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="import hops to follow from a test (0 = unbounded; default 2)",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest()
    py = sys.executable
    pytest_cmd = [py, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    uncovered: list[str] = []

    if args.all:
        tier = "FULL — every test"
    elif args.changed:
        selected, uncovered = select_tests(args.changed, manifest, args.depth or None)
        if not selected:
            tier = "NOTHING REACHES THE CHANGED FILES"
        else:
            reach = f"depth {args.depth}" if args.depth else "unbounded"
            tier = f"TARGETED — {len(selected)} test file(s) reach the changed code ({reach})"
            pytest_cmd += selected
    else:
        tier = f"FAST — everything except {len(SLOW_FILES)} measured-slow file(s)"
        for path in SLOW_FILES:
            pytest_cmd += ["--ignore", path]

    print(f"Tier: {tier}")
    if args.changed:
        print("Changed: " + ", ".join(args.changed))
        if uncovered:
            # Worth saying out loud: a changed file no test reaches is a gap in
            # the safety net, and staying quiet about it is how it stays a gap.
            print("\nNO TEST REACHES THESE — the gate cannot vouch for them:")
            for path in uncovered:
                print(f"  ! {path}")
    if args.list:
        print("\nWould run: " + " ".join(pytest_cmd[3:]))
        return 0

    results: list[tuple[str, bool, float]] = []
    if not args.no_invariants:
        lint_scope = [p for p in (args.changed or []) if p.endswith(".py")] or None
        results += invariants(lint_scope)

    if args.changed and not uncovered and "NOTHING REACHES" in tier:
        pass
    elif "NOTHING REACHES" not in tier:
        results.append(("tests", *run("tests", pytest_cmd)))

    total = sum(elapsed for _, _, elapsed in results)
    failed = [label for label, ok, _ in results if not ok]

    print("\n" + "=" * 64)
    for label, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<22} {elapsed:6.1f}s")
    print("=" * 64)
    if failed:
        print(f"  GATE FAILED — {', '.join(failed)}   ({total:.1f}s)")
        return 1
    print(f"  GATE PASSED   ({total:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
