"""Candidate approval, deployment, reload and scheduler authority regressions."""

import hashlib
import json
from pathlib import Path

import pytest

from app.hud import schedule_control as controls
from core import development as dev
from core import feature_engine as features
from core import task_scheduler as scheduler


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "JOBS", tmp_path / "runtime/development")
    monkeypatch.setattr(features, "ROOT", tmp_path)
    monkeypatch.setattr(features, "_features", {})
    monkeypatch.setattr(features, "_restored", {})
    from tools import era_manifest

    monkeypatch.setattr(era_manifest, "main", lambda _: 0)
    return tmp_path


def pending(root, code=None):
    job_id = "a" * 16
    rel = "features/sample.py"
    code = (
        code
        or 'FEATURE = {"id": "sample", "description": "Example"}\ndef run(arguments):\n    return {"result": arguments["value"] * 2}\n'
    )
    path = dev.JOBS / job_id / "workspace" / rel
    path.parent.mkdir(parents=True)
    path.write_text(code)
    job = {
        "id": job_id,
        "state": "awaiting_approval",
        "approval": "verified",
        "files": [
            {
                "path": rel,
                "base": None,
                "candidate": dev._hash(path),
                "new": True,
                "diff": "+feature",
            }
        ],
    }
    job["approval"] = hashlib.sha256(json.dumps(job["files"], sort_keys=True).encode()).hexdigest()
    job["checks"] = {"passed": True}
    dev._save(job)
    return job


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/tmp/x.py",
        "core/../../outside.py",
        "core/development.py",
        "core/safety_guard.py",
        "app/hud/api.py",
        "ui_src/package.json",
        "ui_src/src/.secret.py",
        "core\\utils.py",
        None,
    ],
)
def test_change_scope_is_enforced(repository, path):
    with pytest.raises(ValueError):
        dev._path(path)


def test_symlink_escape(repository):
    outside = repository.parent / "outside"
    outside.mkdir(exist_ok=True)
    (repository / "core").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        dev._path("core/file.py")


@pytest.mark.asyncio
async def test_final_approval_required_and_feature_runs_live(repository):
    job = pending(repository)
    for kwargs in ({}, {"confirmed": True, "approval": "wrong"}):
        with pytest.raises(ValueError):
            dev.activate(job["id"], kwargs.pop("approval", job["approval"]), **kwargs)
        assert not (repository / "features/sample.py").exists()
    activated = dev.activate(job["id"], job["approval"], confirmed=True)
    assert not activated["requires_restart"]
    assert await features.run("sample", {"value": 6}) == {"result": 12}
    with pytest.raises(ValueError):
        dev.activate(job["id"], job["approval"], confirmed=True)
    features._features.clear()
    features._restored.clear()
    features.restore()
    assert await features.run("sample", {"value": 4}) == {"result": 8}


@pytest.mark.parametrize("changed", ["live", "candidate"])
def test_stale_approval_cannot_deploy(repository, changed):
    job = pending(repository)
    path = (
        repository / "features/sample.py"
        if changed == "live"
        else dev.JOBS / job["id"] / "workspace/features/sample.py"
    )
    path.parent.mkdir(exist_ok=True)
    path.write_text("externally changed")
    with pytest.raises(ValueError, match="changed"):
        dev.activate(job["id"], job["approval"], confirmed=True)
    assert not features.catalog()


def test_bad_feature_rolls_back_files(repository):
    job = pending(repository, 'FEATURE = {"id": "sample", "description": "Example"}\n')
    with pytest.raises(ValueError, match="run"):
        dev.activate(job["id"], job["approval"], confirmed=True)
    assert not (repository / "features/sample.py").exists()
    assert not features.catalog()


def test_rejection_keeps_live_source_unchanged(repository):
    job = pending(repository)
    assert dev.reject(job["id"])["state"] == "rejected"
    with pytest.raises(ValueError):
        dev.activate(job["id"], job["approval"], confirmed=True)
    assert not (repository / "features/sample.py").exists()


def test_corrupt_job_does_not_break_hud(repository):
    folder = dev.JOBS / ("b" * 16)
    folder.mkdir(parents=True)
    (folder / "job.json").write_text("{broken")
    assert dev.list_jobs() == []


def test_restore_refuses_modified_approved_feature(repository):
    job = pending(repository)
    dev.activate(job["id"], job["approval"], confirmed=True)
    (repository / "features/sample.py").write_text('raise RuntimeError("must not import")')
    features.restore()
    assert features.catalog() == []


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("ERA_SCHEDULED_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("ERA_HIVE_DIR", str(tmp_path / "hive"))
    monkeypatch.setattr(scheduler, "_scheduler", None)
    calls = []
    monkeypatch.setattr(
        scheduler,
        "_executor",
        lambda name, args, task: calls.append((name, args)) or {"success": True, "message": "done"},
    )
    monkeypatch.setattr(scheduler, "_reporter", None)
    return calls


def test_visual_reminder_controls(tasks):
    created = controls.control(
        {
            "action": "create_reminder",
            "name": "Water",
            "message": "Water break",
            "when": "in 20 minutes",
        }
    )
    task = scheduler.get_task(created["id"])
    controls.control({"id": task["id"], "version": controls.version(task), "action": "pause"})
    assert not scheduler.get_task(task["id"])["enabled"]
    with pytest.raises(ValueError, match="changed"):
        controls.control({"id": task["id"], "version": controls.version(task), "action": "resume"})


def test_visual_run_retains_delete_gate_and_version_bound_approval(tasks):
    task = scheduler.create_task(
        "Clean",
        "in 20 minutes",
        tool="file_controller",
        args={"action": "delete", "path": "/tmp/test.tmp"},
    )
    controls.control({"id": task["id"], "version": controls.version(task), "action": "run_now"})
    assert tasks == []
    parked = scheduler.get_task(task["id"])
    assert parked["pending"]
    body = {"id": task["id"], "version": controls.version(parked), "action": "approve"}
    with pytest.raises(ValueError, match="approval"):
        controls.control(body)
    scheduler.update_task(task["id"], name="Changed preview")
    with pytest.raises(ValueError, match="changed"):
        controls.control({**body, "confirmed": True})
    current = scheduler.get_task(task["id"])
    controls.control({**body, "version": controls.version(current), "confirmed": True})
    assert len(tasks) == 1


def test_interrupted_activation_is_recovered(repository):
    job = pending(repository)
    source = dev.JOBS / job["id"] / "workspace/features/sample.py"
    target = repository / "features/sample.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    job["state"] = "activating"
    dev._save(job)
    assert dev.list_jobs()[0]["state"] == "failed"
    assert not target.exists()


def test_interrupted_activation_preserves_external_edit(repository):
    job = pending(repository)
    target = repository / "features/sample.py"
    target.parent.mkdir(parents=True)
    target.write_text("external edit")
    job["state"] = "activating"
    dev._save(job)
    assert dev.list_jobs()[0]["state"] == "recovery_required"
    assert target.read_text() == "external edit"


def test_interrupted_verification_is_reported(repository):
    job = pending(repository)
    job["state"] = "verifying"
    dev._save(job)
    assert dev.list_jobs()[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_same_size_rapid_feature_replacement_uses_new_bytes(repository):
    job = pending(repository)
    dev.activate(job["id"], job["approval"], confirmed=True)
    path = repository / "features/sample.py"
    before = path.read_text()
    path.write_text(before.replace("* 2", "* 3"))
    features.install([path])
    assert await features.run("sample", {"value": 2}) == {"result": 6}


def test_unverified_candidate_cannot_activate(repository):
    job = pending(repository)
    job["checks"]["passed"] = False
    dev._save(job)
    with pytest.raises(ValueError, match="verified"):
        dev.activate(job["id"], job["approval"], confirmed=True)
    assert not (repository / "features/sample.py").exists()


def test_mutated_plan_cannot_reuse_approval(repository):
    job = pending(repository)
    job["files"][0]["diff"] = "changed preview"
    dev._save(job)
    with pytest.raises(ValueError, match="verified"):
        dev.activate(job["id"], job["approval"], confirmed=True)


def test_protected_explicit_request_is_not_silently_retargeted(repository):
    with pytest.raises(ValueError, match="Protected"):
        dev.propose("Fix my approval gates", ["core/development.py"])


def test_mixed_engine_feature_change_waits_for_new_session(repository):
    job = pending(repository)
    extra = dev.JOBS / job["id"] / "workspace/actions/demo.py"
    extra.parent.mkdir(parents=True)
    extra.write_text("VALUE = 1\n")
    job["files"].append(
        {
            "path": "actions/demo.py",
            "base": None,
            "candidate": dev._hash(extra),
            "new": True,
            "diff": "+VALUE = 1",
        }
    )
    job["approval"] = hashlib.sha256(json.dumps(job["files"], sort_keys=True).encode()).hexdigest()
    dev._save(job)
    activated = dev.activate(job["id"], job["approval"], confirmed=True)
    assert activated["requires_restart"]
    features.restore()
    assert features.catalog() == []
    features.SESSION_ID = "replacement-session"
    try:
        features.restore()
        assert features.catalog()[0]["id"] == "sample"
    finally:
        features.SESSION_ID = activated["engine_session"]


def test_failed_job_creation_releases_busy_slot(repository, monkeypatch):
    def fail(job):
        raise OSError("disk full")

    monkeypatch.setattr(dev, "_save", fail)
    monkeypatch.setattr(dev, "_active", None)
    with pytest.raises(OSError):
        dev.propose("Add a test feature", ["features/sample.py"])
    assert dev._active is None


def test_feature_import_exit_cannot_terminate_hud(repository):
    path = repository / "features/exit.py"
    path.parent.mkdir(parents=True)
    path.write_text("raise SystemExit(1)\n")
    with pytest.raises(ValueError, match="terminate"):
        features.install([path])
    assert features.catalog() == []


@pytest.mark.asyncio
async def test_feature_run_exit_is_contained(repository):
    job = pending(
        repository,
        'FEATURE = {"id": "sample", "description": "Example"}\ndef run(arguments):\n    raise SystemExit(1)\n',
    )
    dev.activate(job["id"], job["approval"], confirmed=True)
    with pytest.raises(ValueError, match="terminate"):
        await features.run("sample", {})
