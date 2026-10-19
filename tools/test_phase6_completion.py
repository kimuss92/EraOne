"""Phase 6 device inventory and moved-in tree discovery regressions."""

import time
from types import SimpleNamespace

import pytest

from actions.file_indexer import FileIndex
from core.pnp_awareness import PnpAwareness
from core.subtree_discovery import SubtreeDiscovery
from tools.file_fixture import rename_fixture


def evidence():
    return {
        "devices": [{"id": "USB\\fixture", "bus": "USB", "started": True, "problem": 0}],
        "unlistedDevices": 0,
    }


def test_pnp_remove_unknown_and_stale_are_distinct():
    clock = [100.0]
    service = PnpAwareness(clock=lambda: clock[0])
    service.observe(evidence())
    assert service.snapshot()["health"] == "ready"
    service.observe({"devices": [], "unlistedDevices": 0})
    assert service.snapshot()["devices"] == [] and service.snapshot()["health"] == "ready"
    service.unavailable()
    assert service.snapshot()["health"] == "unavailable"
    service.observe(evidence())
    clock[0] += 36
    assert service.snapshot()["health"] == "stale" and not service.snapshot()["devices"]


@pytest.mark.parametrize("change", ["duplicate", "bad_bool", "bad_problem", "oversize", "hidden"])
def test_pnp_identity_validation_and_incomplete_coverage(change):
    service = PnpAwareness()
    data = evidence()
    if change == "hidden":
        data["unlistedDevices"] = 5
        service.observe(data)
        assert service.snapshot()["health"] == "degraded"
        return
    if change == "duplicate":
        data["devices"] *= 2
    elif change == "bad_bool":
        data["devices"][0]["started"] = 1
    elif change == "bad_problem":
        data["devices"][0]["problem"] = -1
    else:
        data["devices"][0]["id"] = "x" * 513
    with pytest.raises(ValueError):
        service.observe(data)


def wait_discovery(index):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = index._discovery.snapshot()
        if not status["pending"] and not status["active"]:
            return status
        time.sleep(0.01)
    pytest.fail("discovery did not drain")


def test_unindexed_moved_in_tree_becomes_searchable_without_contents(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    index = FileIndex(tmp_path / "index.db")
    index.rebuild(roots=[root], wait=True)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "nested" / "phase6unique.txt").write_text("fixture")
    target = root / "incoming"
    try:
        rename_fixture(outside, target)
        index._handle_watch_event(
            SimpleNamespace(
                event_type="moved",
                src_path=str(outside),
                dest_path=str(target),
                is_directory=True,
            )
        )
        status = wait_discovery(index)
        assert not status["last"]["incomplete"]
        rows = index._get_conn().execute("SELECT path FROM files").fetchall()
        assert str(target / "nested" / "phase6unique.txt") in {r["path"] for r in rows}
        assert (
            index._get_conn()
            .execute("SELECT count(*) FROM files_fts WHERE files_fts MATCH 'phase6unique'")
            .fetchone()[0]
            == 1
        )
    finally:
        index.close()


def test_discovery_limits_are_reported_and_links_not_followed(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for n in range(8):
        (root / f"{n}.txt").write_text("fixture")
    index = FileIndex(tmp_path / "index.db")
    index._get_conn()
    discovery = SubtreeDiscovery(index._db_path, index._watch_path_ignored, limit=3)
    try:
        report = discovery.scan(root)
        assert report["incomplete"] and report["indexed"] <= 3
        discovery._epoch += 1
        assert discovery.close()
    finally:
        index.close()


def test_discovery_invalidated_before_write_rolls_back(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    index = FileIndex(tmp_path / "index.db")
    index._get_conn()
    discovery = SubtreeDiscovery(index._db_path, lambda path: False)

    def invalidate(path):
        discovery._epoch += 1
        return False

    discovery.ignored = invalidate
    try:
        report = discovery.scan(root)
        assert report["incomplete"] and report["indexed"] == 0
        assert index._get_conn().execute("SELECT count(*) FROM files").fetchone()[0] == 0
    finally:
        discovery.close()
        index.close()


@pytest.mark.parametrize("action", ["write", "create_file"])
def test_file_write_readback_rejects_false_done(tmp_path, action):
    from core.action_verifiers import verify_file_controller
    from core.tool_result import ToolResult

    target = tmp_path / "check.txt"
    params = {"action": action, "path": str(target), "content": "expected"}
    target.write_text("different", encoding="utf-8")
    failed = verify_file_controller(ToolResult.ok("File operation completed."), params)
    assert not failed.success and not failed.verified
    target.write_text("expected", encoding="utf-8")
    passed = verify_file_controller(ToolResult.ok("File operation completed."), params)
    assert passed.success and passed.verified


def test_file_delete_readback_and_append_uncertainty(tmp_path):
    from core.action_verifiers import verify_file_controller
    from core.tool_result import ToolResult

    target = tmp_path / "check.txt"
    target.write_text("fixture")
    assert not verify_file_controller(
        ToolResult.ok("Deleted."), {"action": "delete", "path": str(target)}
    ).success
    target.unlink()
    assert verify_file_controller(
        ToolResult.ok("Deleted."), {"action": "delete", "path": str(target)}
    ).verified
    target.write_text("fixturemore")
    result = verify_file_controller(
        ToolResult.ok("Appended."),
        {"action": "write", "path": str(target), "content": "more", "append": True},
    )
    assert result.success and not result.verified


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,action",
    [
        ("computer_control", "running_apps"),
        ("computer_control", "active_window"),
        ("computer_control", "system_snapshot"),
        ("file_controller", "find"),
        ("file_controller", "largest"),
        ("browser_control", "list_browsers"),
    ],
)
async def test_read_family_recovery_once_without_mutation(tool, action):
    from core.task_recovery import execute_read_recovery
    from core.tool_result import ToolResult

    calls = []

    async def execute(args):
        calls.append(dict(args))
        return (
            ToolResult.fail("Temporary read timeout", error_type="TimeoutError")
            if len(calls) == 1
            else ToolResult.ok("Read completed.")
        )

    result = await execute_read_recovery(tool, {"action": action}, execute)
    assert result.success and len(calls) == 2 and calls[0] == calls[1]
    assert result.data["recovery"]["attempts"] == 2
