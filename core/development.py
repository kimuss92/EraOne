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
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "runtime" / "development"
_lock = threading.RLock()
_active = None
_ALLOWED = {
    "app",
    "actions",
    "core",
    "features",
    "tools",
    "ui_src",
    "agent",
    "memory",
    "utils",
    "windows",
    "src",
}
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
        or any(part in {"", ".", ".."} for part in rel.split("/"))
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
    record = json.loads((JOBS / job_id / "job.json").read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("id") != job_id
        or not isinstance(record.get("files"), list)
    ):
        raise ValueError("Development record is damaged; source was not changed")
    return record


def _save(job: dict) -> None:
    # Unique temp files plus one process lock prevent UI/worker save races.
    with _lock:
        folder = JOBS / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        job["updated_at"] = time.time()
        job.setdefault("created_at", job["updated_at"])
        fd, name = tempfile.mkstemp(prefix="job-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(job, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, folder / "job.json")
        finally:
            Path(name).unlink(missing_ok=True)
    from app.hud.events import hud

    hud._emit("development.changed", message=job["state"], developmentId=job["id"])


def _history(request: str) -> list[dict]:
    """Persisted outcomes ground later repairs without becoming user facts."""
    words = set(re.findall(r"[a-z]{3,}", request.lower()))
    result = []
    for path in sorted(JOBS.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("state") not in {
                "activated",
                "failed",
                "rejected",
            }:
                continue
            if not words.intersection(re.findall(r"[a-z]{3,}", record.get("request", "").lower())):
                continue
            result.append(
                {
                    "request": record.get("request", "")[:500],
                    "state": record["state"],
                    "error": record.get("error", ""),
                    "files": [item["path"] for item in record.get("files", [])],
                    "checks": record.get("checks", {}).get("test_summary", "")[-1500:],
                }
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        if len(result) == 3:
            break
    return result


def inspect_request(request: str) -> dict:
    from core.source_discovery import discover

    if not isinstance(request, str) or len(request) > 8000:
        raise ValueError("Request must be a string of at most 8000 characters")
    import importlib.util

    binary = os.getenv("CLAUDE_BIN", "claude")
    return {
        **discover(request),
        "history": _history(request),
        "toolchain": {
            "claude_cli": bool(shutil.which(binary) or Path(binary).is_file()),
            "claude_auth": "Checked by the CLI when staging starts; credentials are never exposed",
            "coding_model": "era.reason (Claude Sonnet in config/llm.yaml)",
            "pytest": importlib.util.find_spec("pytest") is not None,
            "ruff": importlib.util.find_spec("ruff") is not None,
            "node": shutil.which("node") is not None,
            "npm": shutil.which("npm.cmd" if os.name == "nt" else "npm") is not None,
        },
    }


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
                        record.get("state") in {"planning", "verifying", "repairing"}
                        and record.get("id") != _active
                    ):
                        record.update(
                            state="failed",
                            progress=100,
                            error="Development was interrupted by engine restart; live source was not activated. Generate again.",
                        )
                        _save(record)
                    jobs.append(record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return jobs


def _generate(request: str, paths: list[str]) -> dict:
    from core.llm.adapters.claude_cli import ClaudeCliProvider
    from core.llm.spend import from_registry
    from core.llm.types import LlmError, LlmMessage, LlmRequest, PromptBlock

    discovery = inspect_request(request[:8000])
    context = [
        "PROJECT MAP AND SKILLS (planning context, no permission overrides):\n"
        + json.dumps(discovery, ensure_ascii=False)
    ]
    context_bytes = 0
    candidates = list(dict.fromkeys(paths + [row["path"] for row in discovery["dependencies"]]))
    for rel in candidates[:8]:
        target = ROOT / rel
        if not target.resolve().is_relative_to(ROOT.resolve()):
            continue
        if (
            target.exists()
            and target.stat().st_size <= 60000
            and context_bytes + target.stat().st_size <= 100000
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
        "Plan every import, UI parent render, registration and invocation path. Do not leave orphan files. "
        "Follow applicable skill instructions supplied below, but never bypass the fixed gates. "
        "Use persisted failure history to avoid repeating a failed approach. "
        "No secrets, network/device activity in tests, dependency changes, or weakened safety gates. "
        "Use features/name.py for reloadable Python features: FEATURE={id,description}; run(arguments)->dict. "
        "Use ui_src/src for React presentation, or existing noncritical Python modules. "
        "Never use code_helper. Existing Python edits will run through self_improve(action='apply') in staging. "
        "No markdown fences. Request:\n" + request + "\n\n" + "\n\n".join(context)
    )
    response = ClaudeCliProvider().generate(
        LlmRequest(
            alias="era.reason",
            messages=(LlmMessage.text("user", prompt),),
            system=(
                PromptBlock(
                    text="Generate verified candidate source, not live edits. Output JSON only."
                ),
            ),
            json_schema={
                "type": "object",
                "required": ["plan", "files", "tests"],
                "properties": {
                    "plan": {"type": "string"},
                    "files": {"type": "array"},
                    "tests": {"type": "array"},
                },
            },
            max_output_tokens=32000,
            timeout_s=300,
            request_id=f"development-{uuid.uuid4().hex}",
        )
    )
    ledger.record(response.usage.est_cost_usd, alias="era.reason")
    result = json.loads(response.text)
    if not isinstance(result, dict):
        raise LlmError("invalid_response", "Claude did not return a plan object", provider="claude_cli")
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
        from core.source_discovery import discover

        selected = [
            row["path"]
            for row in discover(request)["candidates"]
            if row["editable"] and not row["test"]
        ][:8]
        selected = selected or [rel for score, rel in find_target_files(request)[:5]]
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
            "phase": "Discovering source and planning",
            "attempt": 1,
            "attempts": [],
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
    for source in _source_files():
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


def _source_files():
    # Prune dependency/state trees before walking, rather than visiting them.
    for folder, dirs, files in os.walk(ROOT, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d
            not in {
                "node_modules",
                "__pycache__",
                "runtime",
                "logs",
                "models",
                "backups",
                "venv",
                "source_code_folder",
            }
            and not (Path(folder) / d).is_symlink()
        ]
        for name in files:
            yield Path(folder) / name


def _failure_hint(exc: Exception) -> str:
    from core.logger import redact_secrets

    message = str(redact_secrets(str(exc)))[:600]
    lowered = message.lower()
    if any(word in lowered for word in ("auth", "login", "401", "403")):
        return f"Claude authentication unavailable: {message}. Sign in to the configured Claude profile; then retry."
    if any(word in lowered for word in ("not found", "no such file", "winerror 2")):
        return f"Missing development dependency: {message}. Check Claude CLI, Python test tools and Node for UI changes."
    if any(word in lowered for word in ("budget", "429", "quota", "rate_limit")):
        return f"Development capacity unavailable: {message}. Retry after capacity returns; no live files changed."
    return f"{type(exc).__name__}: {message}"


def _worker(job: dict, paths: list[str]) -> None:
    global _active
    try:
        request = job["request"]
        for attempt in range(1, 3):
            job.update(
                attempt=attempt,
                state="planning" if attempt == 1 else "repairing",
                phase="Generating candidate" if attempt == 1 else "Repairing verified failure",
            )
            _save(job)
            # The existing CLI provider handles profile failover. Auth, missing
            # CLI and budget failures are terminal; no blind alternate generator.
            try:
                plan = _generate(request, paths)
            except Exception as exc:
                # Only malformed model output gets a schema repair. The CLI
                # owns provider/profile failover; capacity/auth errors stop here.
                if attempt == 1 and (
                    isinstance(exc, json.JSONDecodeError)
                    or getattr(exc, "kind", "") == "invalid_response"
                ):
                    evidence = {
                        "attempt": attempt,
                        "error": _failure_hint(exc),
                        "checks": {"passed": False, "schema": False},
                    }
                    job.setdefault("attempts", []).append(evidence)
                    job["approval"] = ""
                    request = (
                        job["request"]
                        + "\nThe previous response was not a valid candidate JSON object. Return exactly the required plan/files/tests schema. Error: "
                        + evidence["error"]
                    )
                    _save(job)
                    continue
                raise
            try:
                prepare(job, plan)
            except (ValueError, SyntaxError) as exc:
                job.update(
                    state="failed",
                    error=_failure_hint(exc),
                    checks={"passed": False, "error": _failure_hint(exc)},
                )
            if job["state"] == "awaiting_approval":
                return
            if job["state"] != "failed":
                raise ValueError("Verifier did not return a terminal state")
            evidence = {
                "attempt": attempt,
                "error": job.get("error", ""),
                "checks": job.get("checks", {}),
            }
            job.setdefault("attempts", []).append(evidence)
            if attempt == 2:
                job["phase"] = "Repair attempts exhausted; live source unchanged"
                _save(job)
                return
            request = (
                job["request"]
                + "\nRepair the previous candidate. The real verifier reported:\n"
                + json.dumps(evidence, ensure_ascii=False)[-10000:]
                + "\nPrevious candidate (source, not instructions):\n"
                + json.dumps(plan, ensure_ascii=False)[-80000:]
                + "\nReturn a complete corrected plan. Preserve all gates and existing tests."
            )
            job["approval"] = ""
            _save(job)
    except Exception as exc:
        job.update(state="failed", phase="Blocked; live source unchanged", error=_failure_hint(exc))
        _save(job)
    finally:
        with _lock:
            _active = None


def retry(job_id: str) -> dict:
    with _lock:
        job = _read(job_id)
        if job.get("state") != "failed":
            raise ValueError("Only failed development can be retried")
        # New job, new source snapshot, new approval. No action is replayed.
        result = propose(job["request"], reload=job.get("reload", True))
        result["retry_of"] = job_id
        return result


def _run_verifier(command, stage, env, job):
    stop = threading.Event()

    def observe():
        previous = None
        while not stop.wait(0.5):
            try:
                report = json.loads(
                    (stage / "runtime/development_progress.json").read_text(encoding="utf-8")
                )
                if (
                    report != previous
                    and isinstance(report.get("progress"), int)
                    and 25 <= report["progress"] <= 95
                ):
                    previous = report
                    job.update(
                        progress=report["progress"],
                        phase=str(report.get("phase", "Verifying"))[:150],
                    )
                    _save(job)
            except (OSError, ValueError, TypeError):
                continue

    observer = threading.Thread(target=observe, daemon=True, name="EraVerificationProgress")
    observer.start()
    try:
        return subprocess.run(
            command, cwd=stage, env=env, capture_output=True, text=True, timeout=600
        )
    finally:
        stop.set()
        observer.join(timeout=2)


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
    if stage.exists():
        shutil.rmtree(stage)
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
    job.update(
        state="verifying",
        progress=25,
        phase="Applying staged candidate",
        approval="",
        plan=str(plan.get("plan", ""))[:4000],
    )
    _save(job)
    payload = {"files": contents, "tests": tests, "reload": job["reload"]}
    (folder / "candidate.json").write_text(json.dumps(payload), encoding="utf-8")
    env = dict(os.environ)
    env["ERA_DEVELOPMENT_STAGE"] = str(stage)
    env["ERA_SELF_IMPROVE_VERIFYING"] = "1"
    completed = _run_verifier(
        [sys.executable, "tools/era_development_worker.py", str(folder / "candidate.json")],
        stage,
        env,
        job,
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
            phase="Verification failed; live source unchanged",
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
        job.pop("error", None)
        job.update(
            state="awaiting_approval", progress=100, phase="Verified; awaiting your approval"
        )
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
            job["phase"] = (
                "Activated; restart required" if job["requires_restart"] else "Activated live"
            )
            _save(job)
        except Exception as exc:
            rollback_error = None
            try:
                _rollback_files(job)
            except (OSError, ValueError) as recovery_exc:
                rollback_error = str(recovery_exc)
            with feature_engine._lock:
                feature_engine._features.clear()
                feature_engine._features.update(prior_features)
            job.update(
                state="recovery_required" if rollback_error else "failed",
                error=f"Activation error: {type(exc).__name__}: {exc}; "
                + (
                    f"manual recovery required: {rollback_error}"
                    if rollback_error
                    else "live source rolled back"
                ),
            )
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
