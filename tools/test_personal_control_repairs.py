"""Regressions for owner calls, personal media, bounded UIA and HUD assent."""

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.hud.approval import submit_decision
from app.telegram_call_runtime import handle_tool
from core.pending_confirmation import PendingConfirmationStore
from core.personal_media import resolve_song
from core.tool_result import ToolResult


def test_saved_song_reference_uses_original_script_and_artist(monkeypatch):
    monkeypatch.setattr(
        "memory.memory_manager.load_memory",
        lambda: {"preferences": {"favorite_song": {"value": "سواد الغيم by Mikasa Music"}}},
    )
    assert resolve_song("computer_settings", {"action": "spotify_play"}, "play my song") == (
        "spotify_control",
        {"action": "search", "value": "سواد الغيم by Mikasa Music"},
    )
    assert resolve_song("spotify_control", {"action": "pause"}, "pause my song")[1] == {
        "action": "pause"
    }
    assert resolve_song("spotify_control", {"action": "play"}, "play my son's song")[1] == {
        "action": "play"
    }


def test_play_my_track_never_detours_into_a_lookup():
    from core.personal_media import divert_personal_song

    play = ("spotify_control", {"action": "play"})
    for tool, args in (
        ("retrieve_memory_context", {"query": "favorite_song"}),
        ("spotify_control", {"action": "liked_songs"}),
    ):
        assert divert_personal_song(tool, args, "Play my track, era.") == play
    # Questions, liked-songs requests and the son's song keep their own path.
    lookup = ("retrieve_memory_context", {"query": "favorite_song"})
    assert divert_personal_song(*lookup, "What is my favorite song?") == lookup
    assert divert_personal_song(
        "spotify_control", {"action": "liked_songs"}, "play my liked songs"
    ) == ("spotify_control", {"action": "liked_songs"})
    assert divert_personal_song(*lookup, "play my son's favorite song") == lookup


def test_recall_returns_facts_not_standing_rules():
    from memory.memory_manager import compact_memory_preview

    memory = {
        "operational_instruction": {
            "memory_before_personal_clarification": {"value": "Check saved memory first"}
        },
        "preferences": {"favorite_song": {"value": "Some Song"}},
    }
    preview = compact_memory_preview(memory, "favorite_song")
    assert "operational_instruction" not in preview
    assert preview.startswith("preferences/favorite_song: ")


def test_favorite_song_survives_crowded_prompt():
    from memory.memory_manager import format_memory_for_prompt_ranked

    memory = {
        "preferences": {
            "favorite_song": {"value": "Original song by Artist", "updated": "2020-01-01"}
        },
        "operational_instruction": {f"rule_{i}": {"value": "x" * 500} for i in range(30)},
    }
    assert "favorite_song = Original song by Artist" in format_memory_for_prompt_ranked(
        memory, top_k=1
    )


def test_call_tool_reports_request_separately_from_connection():
    service = SimpleNamespace(
        request_call_owner=Mock(return_value={"accepted": True}),
        snapshot=lambda: {"state": "dialing"},
    )
    result = asyncio.run(handle_tool(SimpleNamespace(_telegram_calls=service), {"action": "dial"}))
    assert result.success and result.data["state"] == "dialing"
    assert "not connected yet" in result.message
    service.request_call_owner.assert_called_once_with()


def test_call_tool_unavailable_and_busy():
    result = asyncio.run(handle_tool(SimpleNamespace(), {"action": "dial"}))
    assert not result.success and result.error_type == "CallUnavailable"
    service = SimpleNamespace(request_call_owner=Mock(side_effect=RuntimeError))
    result = asyncio.run(handle_tool(SimpleNamespace(_telegram_calls=service), {"action": "dial"}))
    assert not result.success and result.error_type == "CallBusy"


def test_hud_exact_approval_executes_without_user_speech(monkeypatch):
    from tools.test_telegram_approvals import result_engine

    engine, dispatch = result_engine(monkeypatch)
    monkeypatch.setattr(dispatch, "verify_tool_result", lambda result, **kwargs: result)
    monkeypatch.setattr("core.approval_execution.publish_approved_result", lambda *args: None)
    engine._pending_store.store(
        "file_controller", {"action": "delete", "path": "C:/fixture-only.txt"}, "HIGH", "delete"
    )
    pending = engine._pending_store.peek()

    async def run():
        engine._loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        engine._deliver_tool_outcome = Mock(side_effect=lambda *args: finished.set())
        assert not submit_decision(
            engine, pending.confirmation_id, pending.created_at - 1, "approve"
        )
        assert submit_decision(engine, pending.confirmation_id, pending.created_at, "approve")
        # Simultaneous double-clicks may both be admitted; atomic consumption
        # must still permit only one action to execute.
        submit_decision(engine, pending.confirmation_id, pending.created_at, "approve")
        await asyncio.wait_for(finished.wait(), timeout=3.0)
        assert len(engine.ran) == 1
        engine._deliver_tool_outcome.assert_called_once()
        assert engine._deliver_tool_outcome.call_args.args[1].success
        assert not engine._pending_store.is_pending()
        assert not submit_decision(engine, pending.confirmation_id, pending.created_at, "approve")

    asyncio.run(run())


def test_hud_cancel_cannot_consume_replacement():
    store = PendingConfirmationStore()
    engine = SimpleNamespace(_pending_store=store)
    store.store(
        "file_controller", {"action": "delete", "path": "C:/fixture-old.txt"}, "HIGH", "delete"
    )
    old = store.peek()
    store.store(
        "file_controller",
        {"action": "delete", "path": "C:/fixture-new.txt"},
        "HIGH",
        "delete",
        replace=True,
    )
    assert not submit_decision(engine, old.confirmation_id, old.created_at, "cancel")
    current = store.peek()
    assert submit_decision(engine, current.confirmation_id, current.created_at, "cancel")


def test_ui_worker_timeout_is_killed_and_unicode_query_is_not_shell_code(monkeypatch):
    from actions import spotify_search_worker as worker

    clock = iter([0.0, 0.0, 2.0])
    process = Mock()
    process.poll.return_value = None
    process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 0.1), ("", "")]
    launch = Mock(return_value=process)
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    result = worker.search_ui("سواد الغيم $(literal)", cold=False, timeout_s=1.0)
    assert not result[0] and "timed out" in result[1]
    process.kill.assert_called_once()
    payload = json.loads(process.communicate.call_args_list[0].args[0])
    assert payload["query"] == "سواد الغيم $(literal)"
    assert "shell" not in launch.call_args.kwargs


def test_unrelated_track_change_is_not_search_success(monkeypatch):
    from actions import spotify_control as control

    monkeypatch.setattr(control, "_now_playing", lambda: ("Unrelated next track", "Other Artist"))
    monkeypatch.setattr(control, "_spotify_is_playing", lambda: True)
    monkeypatch.setattr(control, "_wait_until", lambda predicate, *a, **kw: predicate())
    assert (
        control._wait_for_new_track(("Old track", "Old Artist"), "سواد الغيم by Mikasa Music")
        is None
    )
    assert control._track_matches("سواد الغيم by Mikasa Music", ("سواد الغيم", "Mikasa Music"))


def test_equivalent_spotify_searches_share_one_claim():
    from core.cancel_registry import CancelRegistry

    registry = CancelRegistry()
    first, _ = registry.claim_unique(
        "one", "spotify_control", {"action": "search", "value": "My Song"}
    )
    second, existing = registry.claim_unique(
        "two", "computer_settings", {"action": "spotify_search", "query": "  my   song  "}
    )
    assert second is None and existing is first
    different, _ = registry.claim_unique(
        "three", "spotify_control", {"action": "search", "value": "Different Song"}
    )
    assert different is not None


def test_cancelled_ui_worker_is_reaped(monkeypatch):
    from actions import spotify_search_worker as worker
    from core.cancel_registry import CancelledTask

    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(worker.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(worker, "raise_if_cancelled", Mock(side_effect=CancelledTask("fixture")))
    with pytest.raises(CancelledTask):
        worker.search_ui("Fixture song", cold=False)
    process.kill.assert_called_once()
    process.communicate.assert_called_once()


def test_explicit_song_artist_rejects_partial_music_hits():
    from actions.spotify_ui import _best_search_match
    from tools.test_spotify_search import _Btn

    rows = [_Btn("Play Other Music"), _Btn("Play سواد الغيم by Other Artist")]
    assert _best_search_match(rows, "سواد الغيم by Mikasa Music")[0] is None
    exact = _Btn("Play سواد الغيم by Mikasa Music")
    assert _best_search_match([*rows, exact], "سواد الغيم by Mikasa Music")[0] is exact


def test_hud_approval_http_validates_exact_decision():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from app.hud.api import HudApi

    callback = Mock(return_value=True)

    async def run():
        app = web.Application()
        HudApi(SimpleNamespace(_on_approval=callback)).register(app)
        async with TestClient(TestServer(app)) as client:
            invalid = await client.post("/api/approval", json={"decision": "approve"})
            assert invalid.status == 400 and not callback.called
            valid = await client.post(
                "/api/approval", json={"id": "fixture", "createdAt": 10.0, "decision": "approve"}
            )
            assert valid.status == 200 and (await valid.json())["accepted"]
            callback.assert_called_once_with("fixture", 10.0, "approve")
            callback.return_value = False
            stale = await client.post(
                "/api/approval", json={"id": "fixture", "createdAt": 10.0, "decision": "approve"}
            )
            assert stale.status == 409

    asyncio.run(run())
