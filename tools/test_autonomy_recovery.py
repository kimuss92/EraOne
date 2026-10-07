"""Discovery, bounded repair and durable development history regressions."""

import json
from pathlib import Path

import pytest

from core import development as dev
from core import source_discovery as discovery
from memory import safe_store


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "JOBS", tmp_path / "runtime/development")
    monkeypatch.setattr(dev, "_active", None)
    monkeypatch.setattr(discovery, "ROOT", tmp_path)
    monkeypatch.setattr(discovery, "_cache", {})
    return tmp_path


def put(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def job():
    return {
        "id": "f" * 16,
        "request": "Fix memory recall",
        "state": "planning",
        "progress": 0,
        "plan": "",
        "files": [],
        "checks": {},
        "approval": "",
        "reload": True,
        "attempts": [],
    }


def test_source_discovery_finds_react_and_python_and_skills(repo):
    put(
        repo,
        "ui_src/src/components/ActivityPanel.tsx",
        "export function ActivityPanel() { return null; }",
    )
    put(repo, "memory/recall.py", '"""Persistent memory retrieval"""\ndef recall(): pass\n')
    put(repo, "skills/hud/SKILL.md", "# HUD\nUse actual activity events for interface state.")
    hud = discovery.discover("Improve interface activity animation")
    assert any(r["path"].endswith("ActivityPanel.tsx") for r in hud["candidates"])
    assert hud["skills"][0]["path"] == "skills/hud/SKILL.md"
    assert (
        discovery.discover("persistent memory recall")["candidates"][0]["path"]
        == "memory/recall.py"
    )


def test_index_refreshes_new_and_changed_source(repo):
    put(repo, "features/first.py", "def first(): pass\n")
    assert len(discovery.index()) == 1
    put(repo, "features/second.py", "def second(): pass\n")
    assert len(discovery.index()) == 2
    put(repo, "features/first.py", "def replacement_symbol(): pass\n")
    assert discovery.index()[0]["symbols"] == ["replacement_symbol"]


def test_discovery_rejects_symlink_and_prunes_dependencies(repo, tmp_path):
    put(repo, "ui_src/src/node_modules/huge.ts", "export const leak=1")
    outside = repo.parent / "secret.py"
    outside.write_text('TOKEN = "do-not-read"')
    (repo / "features").mkdir()
    try:
        (repo / "features/link.py").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert not discovery.index()


def test_dependencies_are_planning_context_and_protected_files_not_editable(repo):
    put(
        repo,
        "features/recall.py",
        "from core.safety_guard import classify_action\ndef recall(): pass\n",
    )
    put(repo, "core/safety_guard.py", "def classify_action(): pass\n")
    report = discovery.discover("recall")
    assert report["dependencies"][0]["path"] == "core/safety_guard.py"
    assert not discovery.discover("safety guard")["candidates"][0]["editable"]


def test_worker_repairs_once_with_real_failure_evidence(repo, monkeypatch):
    calls = []

    def generate(request, paths):
        calls.append(request)
        return {"attempt": len(calls)}

    def prepare(j, plan):
        if plan["attempt"] == 1:
            j.update(
                state="failed",
                error="test_recall failed",
                checks={"passed": False, "test_summary": "expected 2, got 1"},
            )
        else:
            j.update(state="awaiting_approval", checks={"passed": True}, approval="new-fingerprint")
        return j

    monkeypatch.setattr(dev, "_generate", generate)
    monkeypatch.setattr(dev, "prepare", prepare)
    j = job()
    dev._active = j["id"]
    dev._worker(j, [])
    assert len(calls) == 2 and "expected 2, got 1" in calls[1]
    assert j["state"] == "awaiting_approval" and dev._active is None
    assert len(j["attempts"]) == 1
    assert not (repo / "features").exists()


def test_worker_stops_after_two_failures(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(dev, "_generate", lambda *args: calls.append(1) or {})

    def fail(j, plan):
        j.update(state="failed", checks={"passed": False}, error="still fails")

    monkeypatch.setattr(dev, "prepare", fail)
    j = job()
    dev._worker(j, [])
    assert len(calls) == 2 and j["state"] == "failed"
    assert dev._read(j["id"])["attempts"][-1]["attempt"] == 2


def test_provider_auth_failure_is_not_blindly_retried(repo, monkeypatch):
    calls = []

    def auth(*args):
        calls.append(1)
        raise RuntimeError("401 authentication failed")

    monkeypatch.setattr(dev, "_generate", auth)
    j = job()
    dev._worker(j, [])
    assert len(calls) == 1 and j["state"] == "failed"
    assert "Sign in" in j["error"]


def test_history_survives_reloading_and_ignores_corrupt_job(repo):
    j = job()
    j.update(state="failed", error="test recall failed")
    dev._save(j)
    put(repo, "runtime/development/aaaaaaaaaaaaaaaa/job.json", "{bad")
    assert dev.inspect_request("memory recall")["history"][0]["error"] == "test recall failed"
    assert len(dev.list_jobs()) == 1


def test_restart_clears_stuck_repair_without_writing_source(repo):
    j = job()
    j["state"] = "repairing"
    dev._save(j)
    assert dev.list_jobs()[0]["state"] == "failed"
    assert not list(repo.rglob("*.py"))


@pytest.mark.parametrize(
    "rel", ["core//test.py", "core/./test.py", "core/../features/test.py", "/core/test.py"]
)
def test_paths_do_not_normalize_away_traversal(repo, rel):
    with pytest.raises(ValueError):
        dev._path(rel)


def test_write_flushes_before_atomic_rename(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(safe_store.os, "fsync", lambda fd: calls.append("sync"))
    replace = safe_store.os.replace

    def tracked(src, dst):
        calls.append("replace")
        replace(src, dst)

    monkeypatch.setattr(safe_store.os, "replace", tracked)
    target = repo / "state.json"
    safe_store.safe_write_json(target, {"persisted": True})
    assert calls == ["sync", "replace"]
    assert json.loads(target.read_text()) == {"persisted": True}


def test_memory_recovers_older_backup_when_newest_is_damaged(repo):
    target = repo / "facts.json"
    safe_store.safe_write_json(target, {"name": "persistent"})
    safe_store.safe_write_json(target, {"name": "persistent", "new": True})
    backup = safe_store._versioned_backup(target)
    backup.write_text("{broken newest backup")
    target.write_text("{broken current memory")
    report = safe_store.validate_json_file(target)
    assert report["status"] == "CORRUPTED_RECOVERED"
    assert json.loads(target.read_text())["name"] == "persistent"


def test_corrupt_memory_is_not_silently_replaced(repo, monkeypatch):
    from memory import memory_manager

    target = repo / "long_term.json"
    target.write_text("{corrupt facts")
    monkeypatch.setattr(memory_manager, "MEMORY_PATH", target)
    with pytest.raises(OSError, match="preserved"):
        memory_manager.save_memory({"notes": {}})
    assert target.read_text() == "{corrupt facts"


def test_malformed_generation_gets_one_schema_repair(repo, monkeypatch):
    calls = []

    def generate(request, paths):
        calls.append(request)
        if len(calls) == 1:
            raise json.JSONDecodeError("No JSON", "not JSON", 0)
        return {}

    monkeypatch.setattr(dev, "_generate", generate)
    monkeypatch.setattr(
        dev, "prepare", lambda j, p: j.update(state="awaiting_approval", approval="fresh")
    )
    j = job()
    dev._worker(j, [])
    assert len(calls) == 2 and "plan/files/tests" in calls[1]
    assert j["state"] == "awaiting_approval"
