"""Stage Claude-generated changes, verify them, and activate an approved revision.

Live source is never modified while generation or verification is running.
Candidate tests run in a separate source copy; this is not an OS sandbox.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "runtime" / "development"
_lock = threading.RLock()
_active = None
_ALLOWED = {"app", "actions", "core", "features", "tools", "ui_src"}
_SUFFIXES = {".py", ".tsx", ".ts", ".css"}


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _path(rel: str, root: Path | None = None) -> Path:
    from actions.self_improve import SAFETY_CRITICAL

    root = ROOT if root is None else root
    if not isinstance(rel, str):
        raise ValueError("Source path must be a string")
    parsed = PurePosixPath(rel)
    if (
        not isinstance(rel, str)
        or "\\" in rel
        or parsed.is_absolute()
        or any(part in {"..", "."} or part.startswith(".") for part in parsed.parts)
        or len(parsed.parts) < 2
        or parsed.parts[0] not in _ALLOWED
    ):
        raise ValueError("Change path must be an allowed repository source path")
    if parsed.suffix not in _SUFFIXES or rel in SAFETY_CRITICAL:
        raise ValueError("Protected source or unsupported file type")
    if parsed.parts[0] == "ui_src" and parsed.parts[1] != "src":
        raise ValueError("Only ui_src/src presentation code may be generated")
    target = root / rel
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes repository")
    return target


def _read(job_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{16}", str(job_id)):
        raise ValueError("Invalid development id")
    return json.loads((JOBS / job_id / "job.json").read_text(encoding="utf-8"))


def _save(job: dict) -> None:
    folder = JOBS / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "job.tmp"
    temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, folder / "job.json")
    from app.hud.events import hud

    hud._emit("development.changed", message=job["state"], developmentId=job["id"])


def _rollback_files(job: dict) -> None:
    """Recover an interrupted deployment without overwriting external edits."""
    backup = JOBS / job["id"] / "backup"
    for item in reversed(job["files"]):
        rel = item["path"]
        target = ROOT / rel if rel == "ui/dist/index.html" else _path(rel)
        current = _hash(target)
        if current == item["base"]:
            continue
        if current != item["candidate"]:
            raise ValueError(f"External edit at {rel}; manual recovery required")
        if item["base"] is None:
            target.unlink(missing_ok=True)
        else:
            if _hash(backup / rel) != item["base"]:
                raise ValueError(f"Backup changed at {rel}; manual recovery required")
            temporary = target.with_suffix(target.suffix + ".development-tmp")
            shutil.copy2(backup / rel, temporary)
            os.replace(temporary, target)


def list_jobs() -> list[dict]:
    with _lock:
        jobs = []
        for path in sorted(JOBS.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)[
            :10
        ]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(record, dict):
                    if record.get("state") == "activating":
                        try:
                            _rollback_files(record)
                            record.update(
                                state="failed",
                                error="Interrupted deployment rolled back; generate and verify again.",
                            )
                        except (OSError, ValueError) as exc:
                            record.update(state="recovery_required", error=str(exc))
                        _save(record)
                    if (
                        record.get("state") in {"planning", "verifying"}
                        and record.get("id") != _active
                    ):
                        record.update(
                            state="failed",
                            progress=100,
                            error="Development was interrupted by engine restart; live source was not activated. Generate again.",
                        )
                        _save(record)
                    jobs.append(record)
            except (OSError, ValueError):
                continue
        return jobs


def _generate(request: str, paths: list[str]) -> dict:
    from core.llm.adapters.claude_cli import ClaudeCliProvider
    from core.llm.spend import from_registry
    from core.llm.types import LlmMessage, LlmRequest, PromptBlock

    context = []
    context_bytes = 0
    for rel in paths[:5]:
        target = _path(rel)
        if (
            target.exists()
            and target.stat().st_size <= 60000
            and context_bytes + target.stat().st_size <= 80000
        ):
            context_bytes += target.stat().st_size
            context.append(f"FILE {rel}\n{target.read_text(encoding='utf-8')}")
    ledger = from_registry()
    if not ledger.can_afford(1.0)[0]:
        raise ValueError("Development budget exhausted")
    prompt = (
        "Implement a small, complete Era One feature. Return strict JSON: "
        '{"plan":"...","files":[{"path":"...","content":"complete source"}],"tests":["tools/test_feature.py"]}. '
        "At most 6 files, 200KB total. Include meaningful tests in tools/test_*.py. "
        "No secrets, network/device activity in tests, dependency changes, or weakened safety gates. "
        "Use features/name.py for reloadable Python features: FEATURE={id,description}; run(arguments)->dict. "
        "Use ui_src/src for React presentation, or existing noncritical Python modules. "
        "Never use code_helper. Existing Python edits will run through self_improve(action='apply') in staging. "
        "No markdown fences. Request:\n" + request + "\n\n" + "\n\n".join(context)
    )
    response = ClaudeCliProvider().generate(
        LlmRequest(
            alias="era.deep",
            messages=(LlmMessage.text("user", prompt),),
            system=(
                PromptBlock(
                    text="Generate verified candidate source, not live edits. Output JSON only."
                ),
            ),
            max_output_tokens=32000,
            timeout_s=300,
            request_id=f"development-{uuid.uuid4().hex}",
        )
    )
    ledger.record(response.usage.est_cost_usd, alias="era.deep")
    result = json.loads(response.text)
    if not isinstance(result, dict):
        raise ValueError("Claude did not return a plan object")
    return result


def propose(request: str, paths: list[str] | None = None, *, reload: bool = True) -> dict:
    global _active
    if not isinstance(request, str) or not 1 <= len(request.strip()) <= 8000:
        raise ValueError("Feature request must be 1–8000 characters")
    if paths is not None and (
        not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)
    ):
        raise ValueError("Source paths must be an array of strings")
    if paths:
        for path in paths:
            _path(path)
    selected = paths or []
    if not selected:
        from actions.self_improve_targets import find_target_files

        selected = [rel for score, rel in find_target_files(request)[:5]]
    # Unsupported/critical matches can be used by a human later; don't supply
    # them to a generator allowed to rewrite complete files.
    filtered = []
    for rel in selected:
        try:
            _path(rel)
            filtered.append(rel)
        except ValueError:
            continue
    with _lock:
        if _active is not None:
            raise ValueError("A development job is already running")
        job = {
            "id": uuid.uuid4().hex[:16],
            "request": request,
            "state": "planning",
            "progress": 0,
            "plan": "",
            "files": [],
            "checks": {},
            "reload": bool(reload),
            "approval": "",
        }
        _active = job["id"]
        try:
            _save(job)
            threading.Thread(
                target=_worker, args=(job, filtered), daemon=True, name="EraDevelopment"
            ).start()
        except Exception:
            _active = None
            raise
        return dict(job)


def _copy_source(destination: Path) -> None:
    # Include tests and frontend lockfile, not user credentials/state or dependencies.
    allowed_roots = _ALLOWED | {
        "utils",
        "agent",
        "windows",
        "memory",
        "config",
        "ui",
        "docs",
        "assets",
        "scripts",
        "src",
    }
    for source in ROOT.rglob("*"):
        if not source.is_file() or not source.resolve().is_relative_to(ROOT.resolve()):
            continue
        rel = source.relative_to(ROOT)
        if any(
            part.startswith(".") or part in {"node_modules", "__pycache__", "runtime", "logs"}
            for part in rel.parts
        ):
            continue
        if len(rel.parts) > 1 and rel.parts[0] not in allowed_roots:
            continue
        if source.suffix not in {
            ".py",
            ".tsx",
            ".ts",
            ".css",
            ".json",
            ".yaml",
            ".txt",
            ".toml",
            ".ini",
            ".html",
            ".md",
        }:
            continue
        if rel.parts[0] == "memory" and source.suffix != ".py":
            continue
        if (
            rel.parts[0] == "config"
            and source.suffix != ".py"
            and rel.as_posix()
            not in {"config/llm.yaml", "config/api_keys.example.json", "config/autopilot.json"}
        ):
            continue
        if source.stat().st_size > 2_000_000:
            continue
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _worker(job: dict, paths: list[str]) -> None:
    global _active
    try:
        plan = _generate(job["request"], paths)
        prepare(job, plan)
    except Exception as exc:
        job.update(state="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")
        _save(job)
    finally:
        with _lock:
            _active = None


def prepare(job: dict, plan: dict) -> dict:
    files, tests = plan.get("files"), plan.get("tests")
    if (
        not isinstance(files, list)
        or not 1 <= len(files) <= 6
        or not isinstance(tests, list)
        or not 1 <= len(tests) <= 6
    ):
        raise ValueError("Plan requires 1–6 source files and 1–6 focused tests")
    contents, total = {}, 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ValueError("Each candidate requires a path and complete source")
        rel = item.get("path", "")
        _path(rel)
        if rel in contents:
            raise ValueError("Duplicate candidate path")
        total += len(item["content"].encode())
        contents[rel] = item["content"]
    if total > 200000:
        raise ValueError("Candidate exceeds 200KB")
    for test in tests:
        _path(test)
        if not re.fullmatch(r"tools/test_[a-zA-Z0-9_]+\.py", test):
            raise ValueError("Tests must be repository tools/test_*.py files")
    if not any(test in contents for test in tests):
        raise ValueError("A new feature requires at least one new or updated test")
    folder = JOBS / job["id"]
    stage = folder / "workspace"
    _copy_source(stage)
    # Save candidate inputs outside the clone so the worker cannot confuse them
    # with already-applied source. Capture originals for stale-source protection.
    job["files"] = []
    for rel, content in contents.items():
        live = _path(rel)
        before = live.read_text(encoding="utf-8") if live.exists() else ""
        job["files"].append(
            {
                "path": rel,
                "base": _hash(live),
                "candidate": hashlib.sha256(content.encode()).hexdigest(),
                "new": not live.exists(),
                "diff": "\n".join(
                    difflib.unified_diff(
                        before.splitlines(), content.splitlines(), fromfile=rel, tofile=rel
                    )
                )[:40000],
            }
        )
    job.update(state="verifying", progress=25, plan=str(plan.get("plan", ""))[:4000])
    _save(job)
    payload = {"files": contents, "tests": tests, "reload": job["reload"]}
    (folder / "candidate.json").write_text(json.dumps(payload), encoding="utf-8")
    env = dict(os.environ)
    env["ERA_DEVELOPMENT_STAGE"] = str(stage)
    env["ERA_SELF_IMPROVE_VERIFYING"] = "1"
    completed = subprocess.run(
        [sys.executable, "tools/era_development_worker.py", str(folder / "candidate.json")],
        cwd=stage,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    (folder / "verification.txt").write_text(
        completed.stdout + "\n" + completed.stderr, encoding="utf-8"
    )
    result_file = stage / "runtime" / "development_verification.json"
    checks = (
        json.loads(result_file.read_text(encoding="utf-8"))
        if result_file.exists()
        else {"error": "Worker returned no verification report"}
    )
    job["checks"] = checks
    if completed.returncode != 0 or not checks.get("passed"):
        job.update(
            state="failed",
            progress=100,
            error="Candidate verification failed; live source is unchanged",
        )
    else:
        for item in job["files"]:
            # Ruff/self_improve may normalize the source; approval binds the
            # actually verified bytes, not the generator's original answer.
            candidate = stage / item["path"]
            item["candidate"] = _hash(candidate)
            before = _path(item["path"]).read_text(encoding="utf-8") if not item["new"] else ""
            item["diff"] = "\n".join(
                difflib.unified_diff(
                    before.splitlines(),
                    candidate.read_text(encoding="utf-8").splitlines(),
                    fromfile=item["path"],
                    tofile=item["path"],
                )
            )[:40000]
        if checks.get("frontend"):
            job["files"].append(
                {
                    "path": "ui/dist/index.html",
                    "base": _hash(ROOT / "ui/dist/index.html"),
                    "candidate": _hash(stage / "ui/dist/index.html"),
                    "new": False,
                    "diff": "Rebuilt frontend bundle; review source changes above.",
                }
            )
        job["approval"] = hashlib.sha256(
            json.dumps(job["files"], sort_keys=True).encode()
        ).hexdigest()
        job.update(state="awaiting_approval", progress=100)
    _save(job)
    return job


def activate(job_id: str, approval: str, *, confirmed: bool = False) -> dict:
    if confirmed is not True:
        raise ValueError("Explicit final approval required")
    with _lock:
        job = _read(job_id)
        fingerprint = hashlib.sha256(json.dumps(job["files"], sort_keys=True).encode()).hexdigest()
        if (
            job["state"] != "awaiting_approval"
            or not approval
            or approval != job["approval"]
            or approval != fingerprint
            or not job.get("checks", {}).get("passed")
        ):
            raise ValueError("Only the verified pending revision can be activated")
        stage = JOBS / job_id / "workspace"
        backup = JOBS / job_id / "backup"
        for item in job["files"]:
            rel = item["path"]
            target = ROOT / rel if rel == "ui/dist/index.html" else _path(rel)
            if _hash(target) != item["base"] or _hash(stage / rel) != item["candidate"]:
                raise ValueError("Source or candidate changed; generate and verify again")
            if target.exists():
                saved = backup / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
        from core import feature_engine
        from tools.era_manifest import main as refresh_manifest

        with feature_engine._lock:
            prior_features = dict(feature_engine._features)
        # Persist the recovery journal BEFORE writing the first live file.
        job["requires_restart"] = any(
            item["path"].endswith(".py")
            and not item["path"].startswith(("features/", "tools/test_"))
            for item in job["files"]
        )
        job["engine_session"] = feature_engine.SESSION_ID
        job["state"] = "activating"
        _save(job)
        try:
            for item in job["files"]:
                target = ROOT / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".development-tmp")
                shutil.copy2(stage / item["path"], temporary)
                os.replace(temporary, target)
            if not job["requires_restart"]:
                feature_engine.install(
                    [
                        ROOT / item["path"]
                        for item in job["files"]
                        if re.fullmatch(r"features/[a-zA-Z0-9_]+\.py", item["path"])
                    ]
                )
            refresh_manifest([])
            job["requires_restart"] = any(
                item["path"].endswith(".py")
                and not item["path"].startswith(("features/", "tools/test_"))
                for item in job["files"]
            )
            job["state"] = "activated"
            _save(job)
        except Exception as exc:
            _rollback_files(job)
            with feature_engine._lock:
                feature_engine._features.clear()
                feature_engine._features.update(prior_features)
            job.update(state="failed", error=f"Activation rolled back: {type(exc).__name__}: {exc}")
            _save(job)
            raise
        from app.hud.events import hud

        hud._emit("visual.changed", message="Approved frontend/features activated")
        return job


def reject(job_id: str) -> dict:
    with _lock:
        job = _read(job_id)
        if job["state"] != "awaiting_approval":
            raise ValueError("Only a pending candidate can be rejected")
        job["state"] = "rejected"
        _save(job)
        return job
