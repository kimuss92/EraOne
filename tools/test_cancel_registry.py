"""The cancel registry's contract: tokens, tombstones, and the executor trap.

Pure unit tests -- no EraLive, no event loop beyond asyncio.run(). A real Era
instance holds ~/.era/era.lock and would make this file look like a 30-minute
hang, so nothing here constructs one. Each test builds its own
`CancelRegistry()` rather than touching the module singleton, so the tests
cannot pollute each other.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core.cancel_registry import (
    CancelledTask,
    CancelRegistry,
    bind,
    cancelled,
    current,
    offload,
    raise_if_cancelled,
    run_with_grace,
    sleep_cancellable,
)


def _reg() -> CancelRegistry:
    return CancelRegistry()


# ── lifecycle ────────────────────────────────────────────────────────────


def test_register_shows_up_as_active():
    r = _reg()
    token = r.register("c1", "spotify_control", label="Searching Spotify")
    assert [t.cid for t in r.active()] == ["c1"]
    assert token.label == "Searching Spotify"
    assert token.cancelled() is False


def test_register_falls_back_to_the_tool_name_for_a_label():
    r = _reg()
    assert r.register("c1", "web_search").label == "web_search"


def test_finish_removes_it_and_is_idempotent():
    r = _reg()
    r.register("c1", "web_search")
    r.finish("c1")
    assert r.active() == []
    r.finish("c1")  # a cancel and a natural finish can race
    r.finish("never-existed")
    assert r.active() == []


def test_a_cancelled_task_is_no_longer_offered_as_cancellable():
    r = _reg()
    r.register("c1", "web_search")
    r.cancel("c1", source="hud")
    # Still live (its thread has not returned), but not worth cancelling again.
    assert r.active() == []
    assert r.get("c1") is not None


def test_cancelling_twice_returns_none_the_second_time():
    r = _reg()
    r.register("c1", "web_search")
    assert r.cancel("c1", source="hud") is not None
    assert r.cancel("c1", source="voice") is None


def test_cancelling_an_unknown_cid_is_none_not_a_crash():
    assert _reg().cancel("nope", source="hud") is None


def test_attach_task_records_the_detached_task():
    r = _reg()
    r.register("c1", "code_helper")
    sentinel = object()
    r.attach_task("c1", sentinel)
    assert r.get("c1").task is sentinel
    r.attach_task("unknown", sentinel)  # must not raise


# ── the soft-cancel contract: tombstones ─────────────────────────────────


def test_is_cancelled_survives_finish_so_a_late_worker_can_discard():
    """The whole point of soft cancel.

    A worker thread that cannot be interrupted returns up to 76s later
    (code_helper's measured max). It must still be able to learn that its
    result is unwanted, even though the token is long gone.
    """
    r = _reg()
    r.register("c1", "code_helper")
    r.cancel("c1", source="hud")
    r.finish("c1")
    assert r.get("c1") is None
    assert r.is_cancelled("c1") is True


def test_a_tombstone_expires():
    r = _reg()
    r.register("c1", "code_helper")
    r.cancel("c1", source="hud")
    r.finish("c1")
    # Reach past the TTL rather than sleeping 180s for it.
    r._recent[-1] = ("c1", time.monotonic() - 10_000)
    assert r.is_cancelled("c1") is False


def test_a_task_that_finished_normally_is_not_cancelled():
    r = _reg()
    r.register("c1", "web_search")
    r.finish("c1")
    assert r.is_cancelled("c1") is False


def test_is_cancelled_on_an_empty_cid_is_false():
    assert _reg().is_cancelled("") is False


# ── cross-thread behaviour ───────────────────────────────────────────────


def test_cancel_sets_the_event_synchronously_on_the_calling_thread():
    """A busy event loop must never be able to delay the machine stopping.

    The HUD's cancel arrives on an aiohttp handler thread (both
    websocket_server._spawn and HudApi._call spawn one), so the Event has to be
    set there and then -- not hopped onto the loop first.
    """
    r = _reg()
    token = r.register("c1", "spotify_control")
    done = threading.Event()

    def from_another_thread():
        r.cancel("c1", source="hud")
        done.set()

    threading.Thread(target=from_another_thread, daemon=True).start()
    assert done.wait(2.0), "the cancelling thread never ran"
    assert token.event.is_set() is True


def test_cancel_newest_picks_the_last_registered_live_task():
    r = _reg()
    r.register("old", "web_search")
    r.register("new", "spotify_control")
    assert r.cancel_newest(source="voice").cid == "new"
    # The next "cancel that" falls through to the one still running.
    assert r.cancel_newest(source="voice").cid == "old"
    assert r.cancel_newest(source="voice") is None


def test_cancel_all_flags_every_live_task_once():
    r = _reg()
    for cid in ("a", "b", "c"):
        r.register(cid, "web_search")
    r.cancel("b", source="hud")
    flagged = r.cancel_all(source="abort")
    assert sorted(t.cid for t in flagged) == ["a", "c"]  # "b" was already done
    assert r.active() == []


def test_the_cancel_reason_and_source_are_recorded():
    r = _reg()
    r.register("c1", "spotify_control")
    token = r.cancel("c1", source="voice", reason="no I meant the next track")
    assert token.source == "voice"
    assert token.reason == "no I meant the next track"
    assert token.cancelled_at is not None


def test_public_carries_exactly_what_the_hud_needs():
    r = _reg()
    r.register("c1", "spotify_control", label="Searching Spotify")
    assert set(r.register("c2", "web_search").public()) == {
        "cid",
        "tool",
        "label",
        "ageMs",
        "cancelled",
    }
    assert r.active_public()[0]["label"] == "Searching Spotify"


# ── bind / cancelled: the thread-local half ──────────────────────────────


def test_cancelled_fails_open_when_nothing_is_bound():
    """A tool called from a test, the scheduler or the CM lane is unaffected."""
    assert current() is None
    assert cancelled() is False


def test_bind_exposes_the_token_then_restores_the_previous_one():
    r = _reg()
    outer = r.register("outer", "a")
    inner = r.register("inner", "b")
    with bind(outer):
        assert current() is outer
        with bind(inner):
            assert current() is inner
        assert current() is outer
    assert current() is None


def test_bind_is_isolated_per_thread():
    r = _reg()
    a = r.register("a", "tool_a")
    b = r.register("b", "tool_b")
    r.cancel("a", source="hud")
    seen: dict[str, bool] = {}
    ready = threading.Barrier(2)

    def worker(name: str, token):
        with bind(token):
            ready.wait(2.0)
            seen[name] = cancelled()

    t1 = threading.Thread(target=worker, args=("a", a), daemon=True)
    t2 = threading.Thread(target=worker, args=("b", b), daemon=True)
    t1.start()
    t2.start()
    t1.join(3.0)
    t2.join(3.0)
    assert seen == {"a": True, "b": False}


def test_raise_if_cancelled_only_raises_when_cancelled():
    r = _reg()
    token = r.register("c1", "netflix_control")
    with bind(token):
        raise_if_cancelled()  # must not raise
        r.cancel("c1", source="hud")
        with pytest.raises(CancelledTask):
            raise_if_cancelled()


# ── sleep_cancellable ────────────────────────────────────────────────────


def test_sleep_cancellable_returns_early_when_cancelled_mid_sleep():
    r = _reg()
    token = r.register("c1", "netflix_control")
    threading.Timer(0.1, lambda: r.cancel("c1", source="hud")).start()
    with bind(token):
        started = time.monotonic()
        slept = sleep_cancellable(5.0)
    elapsed = time.monotonic() - started
    assert slept is False
    assert elapsed < 1.0, f"took {elapsed:.2f}s -- the cancel did not land promptly"


def test_sleep_cancellable_returns_false_immediately_if_already_cancelled():
    r = _reg()
    token = r.register("c1", "netflix_control")
    r.cancel("c1", source="hud")
    with bind(token):
        started = time.monotonic()
        assert sleep_cancellable(5.0) is False
    assert time.monotonic() - started < 0.2


def test_sleep_cancellable_sleeps_the_whole_time_when_not_cancelled():
    r = _reg()
    with bind(r.register("c1", "netflix_control")):
        started = time.monotonic()
        assert sleep_cancellable(0.25) is True
    assert time.monotonic() - started >= 0.2


def test_sleep_cancellable_unbound_is_a_plain_sleep():
    started = time.monotonic()
    assert sleep_cancellable(0.15) is True
    assert time.monotonic() - started >= 0.1


# ── offload: the run_in_executor contextvar trap ─────────────────────────


def test_offload_binds_the_token_inside_the_worker_thread():
    """The regression test for the nastiest constraint in this feature.

    `loop.run_in_executor` does NOT copy contextvars -- only asyncio.to_thread
    does. So a ContextVar token is invisible in exactly the worker threads that
    run ~45 of Era's tool branches. offload() must bind it explicitly.
    """
    r = _reg()
    token = r.register("c1", "spotify_control")
    r.cancel("c1", source="hud")

    async def go():
        return await offload(cancelled, token=token)

    assert asyncio.run(go()) is True


def test_offload_without_a_token_leaves_the_worker_unbound():
    async def go():
        return await offload(cancelled)

    assert asyncio.run(go()) is False


def test_offload_returns_the_workers_value_and_unbinds_afterwards():
    r = _reg()
    token = r.register("c1", "web_search")

    async def go():
        return await offload(lambda: "result", token=token)

    assert asyncio.run(go()) == "result"
    assert current() is None


# ── run_with_grace: the "receive loop is unblocked" contract ─────────────


def test_run_with_grace_returns_the_result_when_work_fits_in_the_grace():
    async def quick():
        return "done"

    finished, result, task = asyncio.run(run_with_grace(quick(), 1.0))
    assert (finished, result, task) == (True, "done", None)


def test_run_with_grace_hands_back_a_still_running_task_at_the_deadline():
    """It must RETURN at the deadline, not ack-then-await like await_with_ack.

    If this blocks, Gemini's socket goes undrained, the keepalive fails and the
    session dies with 1011 mid-call -- the exact bug this feature exists to fix.
    """

    async def slow():
        await asyncio.sleep(5.0)
        return "late"

    async def go():
        started = time.monotonic()
        finished, result, task = await run_with_grace(slow(), 0.2)
        elapsed = time.monotonic() - started
        task.cancel()
        return finished, result, task, elapsed

    finished, result, task, elapsed = asyncio.run(go())
    assert finished is False
    assert result is None
    assert task is not None
    assert elapsed < 0.7, f"resumed after {elapsed:.2f}s -- the loop stayed blocked"


def test_run_with_grace_propagates_an_exception_raised_inside_the_grace():
    async def boom():
        raise ValueError("nope")

    async def go():
        return await run_with_grace(boom(), 1.0)

    with pytest.raises(ValueError):
        asyncio.run(go())
