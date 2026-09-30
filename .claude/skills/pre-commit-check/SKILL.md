---
name: pre-commit-check
description: Run Era's pre-commit gate — ruff format check, ruff lint, byte-compile, and the pytest suites — over changed files before committing. Use when the user says "pre-commit check", "run the checks", "is this ready to commit", or before committing Python changes.
disable-model-invocation: true
---

# pre-commit-check

Run EraOne's standard verification gate and report a clear pass/fail. This packages
the ad-hoc `py_compile` + `pytest` steps into one repeatable pass.

## Steps

Run from the project root (`C:\Users\nesri\OneDrive\Documents\EraOne`). Use the
Bash tool. If a step fails, stop and report the failure with its output — do not
continue to the next step.

1. **Format check** (non-mutating — reports files that need formatting):
   ```
   python -m ruff format --check .
   ```
   If it reports files, offer to run `python -m ruff format .` to fix them.

2. **Lint**:
   ```
   python -m ruff check .
   ```
   Most issues are autofixable: `python -m ruff check --fix .`

3. **Byte-compile** the changed Python files (syntax gate). Determine changed
   files from the current work; if unknown, compile the core entrypoints:
   ```
   python -m py_compile main.py ui.py agent/executor.py core/safety_guard.py
   ```

4. **Tests** — run the suites (fast, quiet):
   ```
   python -m pytest tools/ -q
   python -m pytest test_hermes_router.py test_memory_guard.py test_pending_ask_suppression.py test_task_queue.py -q
   ```

## Report

Summarize each step as ✅ pass / ❌ fail with the relevant output for any failure.
End with a single verdict line: **READY TO COMMIT** or **NOT READY — <reason>**.
Never run `git commit` yourself; the user commits.
