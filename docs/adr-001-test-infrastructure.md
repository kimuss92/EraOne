# ADR-001: Centralized Test Infrastructure with conftest.py + pytest.ini

## Status
Accepted

## Context
The era one project has 40 test files (~306 KB, ~2,176 assertions) with three
significant infrastructure problems:

1. **No shared configuration** — All pytest settings are defaults. No pytest.ini exists.
   No markers are registered. Tests cannot be selectively run by category.
2. **Fragmented import path setup** — 30 of 35 tools/test_*.py files duplicate
   `sys.path.insert(0, ...)` to resolve project-root imports. 5 files have no path
   setup at all and rely on being invoked from the project root directory.
3. **Missing dependencies** — `pytest-asyncio` is absent from requirements.txt despite
   being required by `@pytest.mark.asyncio` usage in `test_scroll_control.py`.
   No coverage tooling exists.
4. **Non-test files pollute discovery** — `test_final.py` (Telegram notification script)
   and `test_aware.py` (audio detection script) carry the `test_` prefix but are not
   tests, causing spurious collection attempts.

## Decision
Introduce a three-part test infrastructure:

### Part 1 — conftest.py (project root)
A single `conftest.py` file that:
- Inserts the project root into `sys.path` so all test files can import project modules
  without per-file boilerplate
- Registers custom pytest markers (`slow`, `unit`, `integration`, `smoke`) for test
  categorization

### Part 2 — pytest.ini (project root)
Standard pytest configuration:
- Sets `testpaths = tools .` to discover tests in both `tools/` and the root directory
- Enables `asyncio_mode = auto` so async test functions work with pytest-asyncio
  without requiring `@pytest.mark.asyncio` on every async test
- Declares all custom markers to suppress pytest warnings

### Part 3 — Rename non-test files
- `test_final.py` → `final_standalone.py` (Telegram notification script)
- `test_aware.py` → `audio_detect.py` (audio detection script)

### Part 4 — Dependency additions
Add `pytest-asyncio` and `pytest-cov` to `requirements.txt`.

## Alternatives Considered

### Keep status quo (no conftest.py, no pytest.ini)
- **Pro**: Zero changes, zero risk.
- **Con**: Fragmented path setup remains tech debt. Async tests fail when run via
  pytest. No categorization. Discovery is fragile.

### Use pyproject.toml instead of pytest.ini
- **Pro**: Single config file for all Python tooling.
- **Con**: Presumes `pyproject.toml` already exists (it does not). Adds a new config
  format without buy-in from the team. `pytest.ini` is simpler and pytest-native.

### Create a setup.py / install in editable mode instead of conftest.py
- **Pro**: Proper Python package resolution without path hacks.
- **Con**: Requires packaging the entire project, which has complex Windows-native
  dependencies. More invasive change with higher risk of breakage. conftest.py is
  the standard pytest pattern.

## Consequences

### Positive
- All test files can import project modules regardless of invocation directory.
- `pytest tools/` from any working directory works correctly.
- Async tests run without errors under pytest.
- Non-test scripts no longer pollute test discovery.
- Test categorization is ready for selective execution (e.g., `pytest -m "not slow"`).
- Zero changes to existing test logic — purely additive infrastructure.

### Negative
- 30 files still have redundant `sys.path.insert(0, ...)` lines that are now dead
  code under pytest. These can be cleaned up in a follow-up phase but pose no
  functional issue (duplicate entries in `sys.path` are harmless).
- The existing `sys.path.insert` lines remain essential for the 11 files that use the
  custom `check()` pattern and run as standalone scripts (not via pytest).

## Trade-offs
Test infrastructure simplicity is prioritized over "perfect" package installation.
The conftest approach is the de facto standard for pytest projects and carries the
lowest risk of regressions.
