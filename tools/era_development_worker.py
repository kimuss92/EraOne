"""Isolated development verifier. Invoked only on a copied source workspace."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _progress(value: int, phase: str) -> None:
    target = ROOT / "runtime/development_progress.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"progress": value, "phase": phase}), encoding="utf-8")
    os.replace(temporary, target)


def verify(candidate: dict) -> dict:
    from actions import self_improve as si
    from actions.self_diagnosis import run_self_diagnosis

    if os.environ.get("ERA_DEVELOPMENT_STAGE") != str(ROOT):
        raise ValueError("Worker requires an isolated workspace")
    files = candidate["files"]
    # This process owns this copied module. No global state of the live engine
    # is changed. The mandatory combined tests below replace per-file nested tests.
    si._generate_updated_code = lambda target, original, instruction: files[
        target.relative_to(ROOT).as_posix()
    ]
    si._find_test_files = lambda target: []
    si._MAX_MODEL_REPAIRS = 0
    _progress(25, "Applying staged source")
    for rel, content in files.items():
        target = ROOT / rel
        if target.suffix == ".py":
            outcome = {}
            message = si.self_improve(
                {
                    "action": "apply" if target.exists() else "create",
                    "file_path": rel,
                    "description": "Apply this staged candidate",
                    "content": content,
                    "reload": candidate.get("reload", True),
                },
                out=outcome,
            )
            print(message)
            if outcome.get("verdict") not in {"skipped", "pass", "staged"}:
                raise ValueError(f"self_improve refused {rel}: {message}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    for rel in files:
        if rel.endswith(".py"):
            ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    from tools.era_manifest import main as refresh_manifest

    refresh_manifest([])
    result = {"syntax": True, "passed": False, "frontend": False}
    _progress(35, "Checking syntax and lint")
    python_files = [rel for rel in files if rel.endswith(".py")]
    if python_files:
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--fix", *python_files],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", *python_files],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lint = subprocess.run(
            [sys.executable, "-m", "ruff", "check", *python_files],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(lint.stdout, lint.stderr)
        result["lint"] = lint.returncode == 0
    else:
        result["lint"] = True
    # Generated tests supplement fixed regression gates; the generator cannot
    # replace or omit these checks to validate its own broken patch.
    baseline = [
        "tools/test_stability_regressions.py",
        "tools/test_autopilot.py",
        "tools/test_task_scheduler_authority.py",
        "tools/test_visual_engine.py",
        "tools/test_development.py",
        "tools/test_autonomy_recovery.py",
    ]
    test_paths = list(dict.fromkeys(candidate["tests"] + baseline))
    _progress(45, "Running candidate and fixed regression tests")
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *test_paths, "--timeout=40"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=240,
    )
    print(tests.stdout, tests.stderr)
    result["tests"] = tests.returncode == 0
    result["test_summary"] = (tests.stdout + tests.stderr)[-5000:]
    _progress(70, "Checking feature and frontend wiring")
    if any(rel.startswith("ui_src/") for rel in files):
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if npm is None:
            raise ValueError("Node/npm is required for frontend verification")
        for command in (
            [npm, "ci", "--ignore-scripts"],
            [npm, "exec", "tsc", "--", "--noEmit"],
            [npm, "run", "build"],
        ):
            process = subprocess.run(
                command, cwd=ROOT / "ui_src", capture_output=True, text=True, timeout=180
            )
            print(process.stdout, process.stderr)
            if process.returncode:
                raise ValueError("Frontend install/typecheck/build failed")
        result["frontend"] = True
    _progress(90, "Validating feature contract")
    from core import feature_engine

    feature_engine.install(
        [ROOT / rel for rel in files if rel.startswith("features/") and rel.endswith(".py")]
    )
    result["feature_contract"] = True
    # Diagnostics are real and reported separately: a copied workspace has no
    # user credentials/devices, so Doctor may report configuration limitations.
    _progress(95, "Running self diagnosis")
    result["diagnosis"] = run_self_diagnosis({"deep": False})
    result["passed"] = bool(result["syntax"] and result["lint"] and result["tests"])
    return result


if __name__ == "__main__":
    report = {"passed": False}
    try:
        report = verify(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"])
    path = ROOT / "runtime" / "development_verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    sys.exit(0 if report["passed"] else 1)
