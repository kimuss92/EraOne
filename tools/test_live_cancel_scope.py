"""Generic cancellation targets Live Activity, never persisted schedules."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.cancel_intent import is_cancel_all_live_order, is_cancel_task_order
from core.cancel_registry import CancelRegistry


@pytest.mark.parametrize(
    "text", ["Cancel task", "Cancel the current task", "Cancel live activity task"]
)
def test_singular_live_cancel_forms(text):
    assert is_cancel_task_order(text)


@pytest.mark.parametrize(
    "text", ["Cancel every task", "Cancel all tasks", "Era, stop all running tasks"]
)
def test_all_live_cancel_forms(text):
    assert is_cancel_all_live_order(text)


@pytest.mark.parametrize(
    "text",
    [
        "Cancel all scheduled tasks",
        "Cancel medication reminder",
        "Do not cancel every task",
        'Explain "cancel every task"',
    ],
)
def test_schedule_qualifiers_and_negation_are_not_live_cancels(text):
    assert not is_cancel_all_live_order(text) and not is_cancel_task_order(text)


def test_cancel_every_task_stops_all_live_jobs_once(monkeypatch):
    from app.era_live import cancel

    registry = CancelRegistry()
    registry.register("first", "spotify_control")
    registry.register("second", "system_performance")
    monkeypatch.setattr(cancel, "REGISTRY", registry)
    applied, spoken = [], []
    era = SimpleNamespace(
        _wake_words=lambda: [],
        _apply_cancel=applied.append,
        speak=spoken.append,
        _inflight_user_text="Cancel every task",
    )
    assert cancel.CancelMixin._cancel_or_correct(
        era, "Cancel every task", source="voice", partial=True
    )
    assert len(applied) == 2 and not registry.active() and len(spoken) == 1
    assert cancel.CancelMixin._cancel_or_correct(era, "Cancel every task. Era.", source="voice")
    assert len(spoken) == 1


def test_singular_cancel_targets_only_newest_live_job(monkeypatch):
    from app.era_live import cancel

    registry = CancelRegistry()
    registry.register("first", "spotify_control")
    registry.register("second", "system_performance")
    monkeypatch.setattr(cancel, "REGISTRY", registry)
    era = SimpleNamespace(_wake_words=lambda: [], _apply_cancel=Mock(), speak=Mock())
    assert cancel.CancelMixin._cancel_or_correct(era, "Cancel task", source="typed")
    assert [t.cid for t in registry.active()] == ["first"]


def test_no_live_job_consumes_cancel_task_without_schedule_dispatch(monkeypatch):
    from app.era_live import cancel

    monkeypatch.setattr(cancel, "REGISTRY", CancelRegistry())
    spoken = []
    era = SimpleNamespace(_wake_words=lambda: [], speak=spoken.append)
    assert cancel.CancelMixin._cancel_or_correct(era, "Cancel task", source="typed")
    assert "no live task" in spoken[0]


@pytest.mark.parametrize(
    "text", ["Cancel every task", "Cancel task", "Era. Cancel the live activity task."]
)
@pytest.mark.parametrize("action", ["cancel", "create"])
def test_model_schedule_delete_is_blocked_for_generic_live_cancel(monkeypatch, text, action):
    from actions import scheduled_task
    from app.era_live.tool_dispatch import ToolDispatchMixin

    mutate = Mock(side_effect=AssertionError("scheduled jobs must not change"))
    monkeypatch.setattr(scheduled_task, "scheduled_task", mutate)
    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        _inflight_user_text=text,
        _last_user_text=text,
    )
    result = asyncio.run(
        ToolDispatchMixin._execute_tool_raw(
            era, "scheduled_task", {"action": action, "task": "persisted-job"}
        )
    )
    assert not result.success and result.error_type == "BlockedAction"
    mutate.assert_not_called()


def test_exact_duplicate_claim_is_atomic_under_concurrent_requests():
    registry = CancelRegistry()
    with ThreadPoolExecutor(max_workers=10) as pool:
        claims = list(
            pool.map(
                lambda i: registry.claim_unique(
                    str(i), "spotify_control", {"action": "liked_songs"}
                ),
                range(20),
            )
        )
    assert sum(token is not None for token, owner in claims) == 1
    assert len(registry.active()) == 1
    first = registry.active()[0]
    registry.finish(first.cid)
    assert (
        registry.claim_unique("fresh", "spotify_control", {"action": "liked_songs"})[0] is not None
    )


def test_different_arguments_are_not_deduplicated():
    registry = CancelRegistry()
    assert registry.claim_unique("one", "spotify_control", {"action": "play"})[0]
    assert registry.claim_unique("two", "spotify_control", {"action": "pause"})[0]


def test_duplicate_live_dispatch_returns_existing_task_without_new_hud_row(monkeypatch):
    from app.era_live import tool_dispatch as dispatch

    registry = CancelRegistry()
    registry.claim_unique("already-live", "spotify_control", {"action": "liked_songs"})
    monkeypatch.setattr(dispatch, "REGISTRY", registry)
    monkeypatch.setattr(
        dispatch.hud, "tool_started", Mock(side_effect=AssertionError("duplicate HUD row"))
    )
    fc = SimpleNamespace(
        name="spotify_control", id="new-provider-call", args={"action": "liked_songs"}
    )
    response = asyncio.run(dispatch.ToolDispatchMixin._execute_tool(SimpleNamespace(), fc))
    assert response.response["data"]["liveTaskId"] == "already-live"
    assert response.response["data"]["alreadyRunning"] is True


def test_spotify_worker_receives_cancel_token(monkeypatch):
    from actions import spotify_control
    from app.era_live import tool_dispatch as dispatch
    from core.cancel_registry import CURRENT_TOKEN, cancelled

    token = CancelRegistry().register("worker", "spotify_control")
    token.event.set()
    observed = []
    monkeypatch.setattr(
        spotify_control, "spotify_control", lambda **kw: observed.append(cancelled()) or "Stopped"
    )
    era = SimpleNamespace(
        session_manager=SimpleNamespace(get_state_snapshot=lambda: {}),
        _pending_cm_ask_active=lambda: False,
        _cm_handoff_active=lambda: False,
        ui=None,
    )

    async def scenario():
        reset = CURRENT_TOKEN.set(token)
        try:
            await dispatch.ToolDispatchMixin._execute_tool_raw(
                era, "spotify_control", {"action": "liked_songs"}
            )
        finally:
            CURRENT_TOKEN.reset(reset)

    asyncio.run(scenario())
    assert observed == [True]


def test_cancel_during_liked_songs_settle_never_presses_play(monkeypatch):
    from actions import spotify_control as spotify
    from actions import spotify_ui
    from core.cancel_registry import CancelledTask, bind

    token = CancelRegistry().register("liked-cancel", "spotify_control")
    monkeypatch.setattr(spotify.threading.Thread, "start", lambda *a: None)
    monkeypatch.setattr(spotify, "_spotify_window_exists", lambda: True)
    monkeypatch.setattr(spotify, "_open_spotify_uri", lambda *a: True)

    def ready_then_cancel(*args):
        token.event.set()
        return 1, "Spotify"

    monkeypatch.setattr(spotify, "_wait_for_spotify_ui_ready", ready_then_cancel)
    play = Mock(side_effect=AssertionError("cancelled work must not play"))
    monkeypatch.setattr(spotify_ui, "invoke_primary_play", play)
    with bind(token), pytest.raises(CancelledTask):
        spotify._spotify_play_liked_songs()
    play.assert_not_called()


def test_cancelled_uia_read_stops_before_next_native_discovery():
    from actions import spotify_ui
    from core.cancel_registry import CancelledTask, bind

    token = CancelRegistry().register("tree-cancel", "spotify_control")
    token.event.set()
    window = Mock()
    with bind(token), pytest.raises(CancelledTask):
        spotify_ui._wait_for_tree(window)
    window.descendants.assert_not_called()
