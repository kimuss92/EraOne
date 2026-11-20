"""State observations and approval previews must not become write handles."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from core.pending_confirmation import PendingConfirmationBusy, PendingConfirmationStore
from core.state_store import BrowserTabInfo, ForegroundInfo, MediaSessionInfo, StateStore


async def test_state_ingress_and_snapshot_are_detached():
    store = StateStore()
    foreground = ForegroundInfo(window_title="original")
    apps = {"editor": {"windows": [1]}}
    tabs = {"tab": BrowserTabInfo(title="original")}
    media = MediaSessionInfo(session_id="music", is_playing=True)
    flag = {"details": {"expected": [1]}}
    await store.update_foreground(foreground)
    await store.update_open_apps(apps)
    await store.update_browser_tabs(tabs)
    await store.update_media_session(media)
    await store.add_drift_flag(flag)
    foreground.window_title = "changed"
    apps["editor"]["windows"].append(2)
    tabs["tab"].title = "changed"
    media.is_playing = False
    flag["details"]["expected"].append(2)
    snapshot = await store.snapshot()
    assert snapshot.foreground.window_title == "original"
    assert snapshot.open_apps["editor"]["windows"] == [1]
    assert snapshot.browser_tabs["tab"].title == "original"
    assert snapshot.media_sessions["music"].is_playing is True
    assert snapshot.drift_flags == [{"details": {"expected": [1]}}]
    snapshot.foreground.window_title = "injected"
    snapshot.open_apps["editor"]["windows"].append(3)
    snapshot.browser_tabs.clear()
    snapshot.media_sessions["music"].is_playing = False
    snapshot.drift_flags[0]["details"]["expected"].append(3)
    fresh = await store.snapshot()
    assert fresh.foreground.window_title == "original"
    assert fresh.open_apps["editor"]["windows"] == [1]
    assert fresh.browser_tabs["tab"].title == "original"
    assert fresh.media_sessions["music"].is_playing is True
    assert fresh.drift_flags == [{"details": {"expected": [1]}}]


@pytest.mark.parametrize("reader", ["get", "peek", "busy"])
def test_approval_previews_cannot_change_armed_action(reader):
    store = PendingConfirmationStore()
    parameters = {"files": [{"path": "reviewed.txt"}]}
    cid = store.store("delete_file", parameters, "HIGH", "reviewed reason")
    parameters["files"][0]["path"] = "input-mutation.txt"
    if reader == "busy":
        with pytest.raises(PendingConfirmationBusy) as caught:
            store.store("other", {}, "HIGH", "other")
        preview = caught.value.pending
    else:
        preview = getattr(store, reader)(cid)
    assert preview.parameters == {"files": [{"path": "reviewed.txt"}]}
    preview.parameters["files"][0]["path"] = "preview-mutation.txt"
    preview.tool = "different_tool"
    preview.expires_at = 0
    approved = store.confirm(cid)
    assert approved is not None
    assert approved.tool == "delete_file"
    assert approved.parameters == {"files": [{"path": "reviewed.txt"}]}


def test_concurrent_confirmation_is_consumed_exactly_once():
    store = PendingConfirmationStore()
    cid = store.store("tool", {"nested": [1]}, "HIGH", "reason")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.confirm(cid), range(32)))
    consumed = [result for result in results if result is not None]
    assert len(consumed) == 1
    assert consumed[0].parameters == {"nested": [1]}
    assert not store.is_pending()
