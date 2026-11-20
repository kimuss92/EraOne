import asyncio

import pytest

from core.state_store import ForegroundInfo, StateStore


async def test_update_between_snapshot_and_wait_is_not_lost():
    store = StateStore()
    before = await store.snapshot()
    await store.update_foreground(ForegroundInfo(window_title="editor"))
    changed = await store.wait_for_change(before.revision, timeout=1)
    assert changed.revision == before.revision + 1
    assert changed.foreground.window_title == "editor"
    assert before.foreground.window_title == ""


async def test_all_waiters_receive_detached_latest_state():
    store = StateStore()
    waiters = [asyncio.create_task(store.wait_for_change(0, timeout=1)) for _ in range(8)]
    await asyncio.sleep(0)
    await store.update_open_apps({"editor": {"pid": 1}})
    snapshots = await asyncio.gather(*waiters)
    assert {snapshot.revision for snapshot in snapshots} == {1}
    snapshots[0].open_apps["editor"]["pid"] = 99
    assert snapshots[1].open_apps["editor"]["pid"] == 1
    assert (await store.snapshot()).open_apps["editor"]["pid"] == 1


async def test_slow_consumer_gets_complete_state_after_coalesced_updates():
    store = StateStore()
    await store.update_open_apps({"editor": {"pid": 1}})
    await store.update_foreground(ForegroundInfo(window_title="editor"))
    await store.add_drift_flag({"field": "media"})
    await store.clear_drift_flags()
    latest = await store.wait_for_change(0, timeout=1)
    assert latest.revision == 4
    assert latest.foreground.window_title == "editor"
    assert latest.open_apps["editor"]["pid"] == 1
    assert latest.drift_flags == []


async def test_timeout_and_cancellation_leave_store_usable():
    store = StateStore()
    with pytest.raises(TimeoutError):
        await store.wait_for_change(0, timeout=0.01)
    waiter = asyncio.create_task(store.wait_for_change(0))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await store.update_open_apps({"editor": {"pid": 1}})
    assert (await store.wait_for_change(0, timeout=1)).revision == 1
