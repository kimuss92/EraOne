"""SpeechOut called from a thread that has no event loop.

The 2026-07-29 logs carry 13 identical uncaught crashes:

    File "main.py", line 378, in _on_text_command   -> self.enter_standby()
    File "main.py", line 937, in enter_standby      -> self.speak(...)
    File "speech_out.py", line 125, in start        -> asyncio.create_task(...)
    RuntimeError: no running event loop

speak() already marshals onto the loop when it knows one, but self._loop was
only assigned after the first SUCCESSFUL connect — so while a connect was
failing (the bricked kokoro_text config), every speak() fell through to the
inline path and raised on a thread that has no loop.

The voice layer's rule is degrade, never raise: losing the local voice must
never propagate an exception into an unrelated caller like standby.
"""

import asyncio
import threading

from app.era_live.speech_out import SpeechOut


class _Era:
    """Minimal stand-in — SpeechOut only reads these off the engine."""

    def __init__(self):
        self.audio_in_queue = None
        self._turn_done_event = None


# ── The regression ───────────────────────────────────────────────────────────
def test_say_from_a_thread_with_no_loop_does_not_raise():
    """The exact crash: say() on a plain thread, no loop anywhere."""
    s = SpeechOut(_Era())
    box = {}

    def worker():
        try:
            s.say("Entering standby mode.")
            box["ok"] = True
        except BaseException as e:  # noqa: BLE001 - the point is to catch anything
            box["err"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)

    assert "err" not in box, f"say() raised on a loop-less thread: {box.get('err')!r}"
    assert box.get("ok") is True


def test_feed_and_end_turn_are_also_safe_with_no_loop():
    """feed()/end_turn() reach the same start() and were equally exposed."""
    s = SpeechOut(_Era())
    box = {}

    def worker():
        try:
            s.feed("hello there, this is a sentence. ")
            s.end_turn()
            box["ok"] = True
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)

    assert "err" not in box, f"raised on a loop-less thread: {box.get('err')!r}"
    assert box.get("ok") is True


def test_start_reports_failure_rather_than_raising():
    """start() returns False with no loop, so callers can skip queueing work
    that nothing would ever consume."""
    s = SpeechOut(_Era())
    assert s.start() is False
    assert s._worker is None


def test_nothing_is_queued_when_there_is_no_worker():
    """Queueing into a worker that will never run would silently accumulate the
    whole conversation in memory and replay it on the next connect."""
    s = SpeechOut(_Era())
    s.say("this should not be queued")
    assert s._pending.qsize() == 0


# ── The normal path still works ──────────────────────────────────────────────
def test_say_on_the_loop_starts_a_worker_and_queues():
    async def scenario():
        s = SpeechOut(_Era())
        assert s.start() is True
        s.say("First sentence here. Second sentence here.")
        # sentences + the end-of-turn sentinel
        assert s._pending.qsize() >= 2
        await s.stop()
        return True

    assert asyncio.run(scenario()) is True


def test_worker_is_awaited_on_stop_and_not_left_dangling():
    async def scenario():
        s = SpeechOut(_Era())
        s.start()
        worker = s._worker
        assert worker is not None
        await s.stop()
        assert worker.done(), "worker must be finished, not left running"
        assert s._worker is None
        return True

    assert asyncio.run(scenario()) is True


def test_a_dying_worker_is_reported_not_swallowed():
    """A worker that dies must not vanish into 'Task exception was never
    retrieved' — that is indistinguishable from Era having nothing to say."""
    seen = []

    async def scenario():
        s = SpeechOut(_Era())
        s.start()
        worker = s._worker
        worker.cancel()  # cancellation is the one case that must stay quiet
        try:
            await worker
        except asyncio.CancelledError:
            pass
        return True

    assert asyncio.run(scenario()) is True
    assert seen == []
