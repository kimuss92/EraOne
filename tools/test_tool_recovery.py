"""core/tool_recovery.py plus its wiring — board item 19, fallback before asking.

THE DEFECT THESE TESTS PIN
  `_execute_tool` used to call `self.speak_error(...)` the instant a tool failed,
  synchronously, mid-turn. On the Fish voice that reaches the speakers literally,
  so the user heard "Sir, X encountered an error" BEFORE the model had been handed
  the failure — and therefore before any workaround could be attempted. The
  load-bearing test here is `test_a_failed_tool_says_nothing_during_the_turn`:
  restore that call and it fails.

The store is driven by a FAKE CLOCK. The subject is bookkeeping over timestamps,
and a test that needs a real 90-second wait to check an expiry is a test that gets
deleted the first time someone is in a hurry.
"""

from __future__ import annotations

import pytest

from core.tool_recovery import MAX_AGE_S, MAX_NOTICES, TurnRecovery


class FakeClock:
    def __init__(self) -> None:
        self.t = 500.0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(clock):
    return TurnRecovery(clock=clock)


# ── the store ────────────────────────────────────────────────────────────────


def test_a_failure_is_armed_not_reported(store):
    """Arming is the whole mechanism: nothing leaves the store until the turn
    ends, which is what gives a workaround room to happen first."""
    store.note_failure("netflix_control", "play", "ActionFailed", "no player found")
    assert store.pending() == 1


def test_the_same_tool_succeeding_cancels_the_notice(store):
    """A retry or a re-route that landed back on the same tool worked. Reading
    the first attempt's failure out loud would be reporting a non-event."""
    store.note_failure("netflix_control", "play", "ActionFailed", "no player found")
    store.note_success("netflix_control", "play")
    assert store.pending() == 0
    assert store.take() == []


def test_a_different_tool_succeeding_does_not_cancel_it(store):
    """THE deliberate narrow rule. "Anything succeeding clears everything" would
    swallow a real failure inside a compound command — "close Photoshop and open
    Premiere", Photoshop fails, Premiere opens, user told nothing. Silence about
    a real failure is the worse error, so this stays armed."""
    store.note_failure("open_app", "close", "ActionFailed", "Photoshop not running")
    store.note_success("open_app", "open")  # same tool, DIFFERENT action
    store.note_success("premiere_control", "open")  # different tool entirely
    assert store.pending() == 1
    assert store.take()[0].tool == "open_app"


def test_every_notice_for_that_tool_clears_at_once(store):
    """A tool that failed twice before succeeding armed two notices, and the
    success forgives both — not just the most recent one."""
    for _ in range(3):
        store.note_failure("spotify_control", "play", "Timeout", "no response")
    assert store.pending() == 3
    store.note_success("spotify_control", "play")
    assert store.pending() == 0


def test_take_drains_the_turn_oldest_first(store, clock):
    store.note_failure("a", "x", "E", "first")
    clock.tick(1.0)
    store.note_failure("b", "y", "E", "second")
    got = store.take()
    assert [n.message for n in got] == ["first", "second"]
    assert store.pending() == 0, "take must drain, or the next turn inherits this one"


def test_a_stale_notice_is_dropped_and_counted_never_spoken(store, clock):
    """If a flush site is ever missed, the notice must not surface in a later,
    unrelated conversation. Counted rather than dropped silently, because a
    flush that keeps dropping means a flush site is missing."""
    store.note_failure("file_controller", "read", "FileNotFound", "D:\\gone.txt")
    clock.tick(MAX_AGE_S + 1.0)
    assert store.take() == []
    assert store.dropped_stale == 1


def test_a_fresh_notice_survives_alongside_a_stale_one(store, clock):
    store.note_failure("old", "x", "E", "ancient")
    clock.tick(MAX_AGE_S + 1.0)
    store.note_failure("new", "y", "E", "current")
    got = store.take()
    assert [n.tool for n in got] == ["new"]
    assert store.dropped_stale == 1


def test_the_queue_is_bounded_and_the_overflow_is_counted(store):
    """A batch of twenty failing calls must not build a twenty-item queue. The
    bound is memory hygiene, not policy, so what it discards is counted."""
    for i in range(MAX_NOTICES + 4):
        store.note_failure(f"tool{i}", "x", "E", "boom")
    assert store.pending() == MAX_NOTICES
    assert store.overflow == 4


def test_reset_clears_without_reporting(store):
    store.note_failure("a", "x", "E", "boom")
    store.reset()
    assert store.pending() == 0 and store.overflow == 0 and store.dropped_stale == 0


def test_a_broken_clock_never_raises():
    """This sits in the tool funnel. Bookkeeping that can throw is bookkeeping
    that takes a tool call down with it."""

    def boom() -> float:
        raise RuntimeError("no clock")

    s = TurnRecovery(clock=boom)
    s.note_failure("a", "x", "E", "boom")
    s.note_success("b", "y")
    assert len(s.take()) == 1  # degraded timestamps, but no exception


def test_missing_fields_do_not_crash_the_store(store):
    store.note_failure(None, None, None, None)  # type: ignore[arg-type]
    assert store.pending() == 1
    store.note_success(None, None)  # type: ignore[arg-type]
    assert store.pending() == 0, "an unnamed tool must still be clearable"


# ── the wiring: the funnel arms, the turn boundary speaks ────────────────────


class _Host:
    """ToolDispatchMixin's own methods borrowed onto a bare object.

    The real mixin's __init__ needs a Live session, a HUD and a session manager;
    the four methods under test need none of them. Borrowing keeps this a test of
    the arming-and-flushing contract rather than of how much of EraLive can be
    faked.
    """

    def __init__(self, store):
        from app.era_live.tool_dispatch import ToolDispatchMixin as M

        self.tool_recovery = store
        self.spoken: list[tuple[str, str]] = []
        for name in (
            "_recovery",
            "_note_tool_failure",
            "_note_tool_success",
            "_flush_failure_notices",
        ):
            setattr(self, name, getattr(M, name).__get__(self))

    def speak_error(self, tool: str, message: str) -> None:
        self.spoken.append((tool, message))


@pytest.fixture
def host(store, monkeypatch):
    import app.era_live.tool_dispatch as td

    monkeypatch.setattr(td, "log_event", lambda *a, **k: None)
    return _Host(store)


def test_arming_through_the_funnel_speaks_nothing_yet(host):
    host._note_tool_failure("netflix_control", {"action": "play"}, "ActionFailed", "no player")
    assert host.spoken == [], "the announcement must wait for the turn to end"
    assert host.tool_recovery.pending() == 1


def test_a_recovered_failure_is_never_announced(host):
    """The payoff. A tool that failed and then worked produces silence — which is
    what made "try the workaround first" possible at all."""
    host._note_tool_failure("netflix_control", {"action": "play"}, "ActionFailed", "no player")
    host._note_tool_success("netflix_control", {"action": "play"})
    host._flush_failure_notices()
    assert host.spoken == []


def test_an_unrecovered_failure_is_announced_once_at_the_turn_boundary(host):
    """Deferring must not become losing: nothing recovered, so the user is still
    told — just after the attempt rather than before it."""
    host._note_tool_failure("file_controller", {"action": "read"}, "FileNotFound", "D:\\gone")
    host._flush_failure_notices()
    assert host.spoken == [("file_controller", "D:\\gone")]


def test_the_flush_empties_the_store(host):
    """A notice that survives its own turn is a failure announced into the middle
    of the next conversation."""
    host._note_tool_failure("a", {"action": "x"}, "E", "boom")
    host._flush_failure_notices()
    host._flush_failure_notices()
    assert len(host.spoken) == 1


def test_a_stale_notice_is_never_spoken_by_the_flush(host, clock):
    host._note_tool_failure("a", {"action": "x"}, "E", "boom")
    clock.tick(MAX_AGE_S + 1.0)
    host._flush_failure_notices()
    assert host.spoken == []


def test_a_speak_failure_still_empties_the_store(store, monkeypatch):
    """If speaking throws, the notice must not survive to be retried next turn."""
    import app.era_live.tool_dispatch as td

    monkeypatch.setattr(td, "log_event", lambda *a, **k: None)

    class Boom(_Host):
        def speak_error(self, tool: str, message: str) -> None:
            raise RuntimeError("no voice")

    h = Boom(store)
    h._note_tool_failure("a", {"action": "x"}, "E", "boom")
    h._flush_failure_notices()
    assert h.tool_recovery.pending() == 0


def test_without_a_store_the_failure_is_announced_immediately(monkeypatch):
    """Degrade the right way. A stub or a half-built EraLive has no store, and a
    missing store must cost the DEFERRAL, never the announcement: premature is
    bad, silent is worse."""
    import app.era_live.tool_dispatch as td

    monkeypatch.setattr(td, "log_event", lambda *a, **k: None)
    h = _Host(None)
    h.tool_recovery = None
    h._note_tool_failure("a", {"action": "x"}, "E", "boom")
    assert h.spoken == [("a", "boom")]
    h._note_tool_success("a", {"action": "x"})  # must not raise either
    h._flush_failure_notices()


def test_missing_params_are_tolerated(host):
    host._note_tool_failure("a", None, "E", "boom")  # type: ignore[arg-type]
    host._note_tool_success("a", None)  # type: ignore[arg-type]
    assert host.tool_recovery.pending() == 0, "a call with no action must still pair up"


# ── the real call site: the branch inside _execute_tool itself ────────────────
#
# Everything above tests the helpers. These two drive the actual funnel, because
# the defect was never in a helper — it was one line in `_execute_tool` that
# spoke too early. Without these, restoring `self.speak_error(name, result.error)`
# there would leave every test above green.


class _Funnel:
    """Enough of EraLive for `_execute_tool` to run, and nothing more."""

    def __init__(self, store, result):
        from app.era_live.tool_dispatch import ToolDispatchMixin as M

        self.tool_recovery = store
        self.spoken: list[tuple[str, str]] = []
        self._standby_active = False
        self._result = result
        self.ui = type("UI", (), {"set_state": lambda *a: None, "muted": True})()
        self.session_manager = type("SM", (), {"transition_to": lambda *a, **k: None})()
        self.turn_latency = type("TL", (), {"note_tool": lambda *a: None})()
        for name in (
            "_execute_tool",
            "_recovery",
            "_note_tool_failure",
            "_note_tool_success",
            "_flush_failure_notices",
            "_crew_ask_should_background",
        ):
            setattr(self, name, getattr(M, name).__get__(self))

    async def _execute_tool_result(self, fc):
        return self._result

    def speak_error(self, tool: str, message: str) -> None:
        self.spoken.append((tool, message))


@pytest.fixture
def funnel(monkeypatch):
    """Builds a `_Funnel` around a given ToolResult, with the module's own
    collaborators stubbed out."""
    import app.era_live.tool_dispatch as td
    import core.task_trace as tt

    monkeypatch.setattr(td, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(td, "record_tool_result", lambda *a, **k: None)
    monkeypatch.setattr(td, "build_function_response", lambda *a, **k: "response")
    monkeypatch.setattr(tt, "trace", lambda *a, **k: None)
    monkeypatch.setattr(
        td,
        "hud",
        type(
            "HUD",
            (),
            {
                "tool_started": staticmethod(lambda *a: "call"),
                "tool_finished": staticmethod(lambda *a, **k: None),
            },
        )(),
    )

    def build(store, result):
        return _Funnel(store, result)

    return build


def _fc(name: str, **args):
    return type("FC", (), {"name": name, "id": "c1", "args": args})()


@pytest.mark.asyncio
async def test_a_failed_tool_says_nothing_during_the_turn(funnel, store):
    """THE load-bearing test for board item 19.

    Put `self.speak_error(name, result.error)` back where it was in
    `_execute_tool` and this fails: the whole defect was that the sentence
    reached the user mid-turn, before any workaround could be attempted.
    """
    from core.tool_result import ToolResult

    failed = ToolResult.fail(message="no player found", error_type="ActionFailed")
    host = funnel(store, failed)
    await host._execute_tool(_fc("netflix_control", action="play"))

    assert host.spoken == [], "the failure was announced before recovery could run"
    assert store.pending() == 1, "and it must actually be armed, not simply dropped"


@pytest.mark.asyncio
async def test_the_funnel_clears_the_notice_when_the_retry_lands(funnel, store):
    """The other half of the same branch: a success through the real funnel is
    what cancels the armed notice, so the pairing is tested where it happens."""
    from core.tool_result import ToolResult

    store.note_failure("netflix_control", "play", "ActionFailed", "no player found")
    ok = ToolResult.ok(message="playing")
    host = funnel(store, ok)
    await host._execute_tool(_fc("netflix_control", action="play"))

    assert store.pending() == 0
    host._flush_failure_notices()
    assert host.spoken == []


@pytest.mark.asyncio
async def test_a_gate_result_is_never_armed(funnel, store):
    """ConfirmationRequired and the other gate outcomes are the system working.
    They were excluded from the announcement before this change and must stay
    excluded from the store, or every confirmation prompt ends the turn with
    "encountered an error"."""
    from core.tool_result import ToolResult

    gated = ToolResult.fail(
        message="Shut the computer down, sir?", error_type="ConfirmationRequired"
    )
    host = funnel(store, gated)
    await host._execute_tool(_fc("computer_settings", action="shutdown"))

    assert store.pending() == 0 and host.spoken == []


# ── both flush sites exist ────────────────────────────────────────────────────


def test_the_flush_is_wired_at_both_turn_boundaries():
    """A structural check, deliberately, and the reason is the failure mode.

    Deferring an announcement makes the FLUSH load-bearing: drop it and failures
    stop being reported at all — a silent regression in the dangerous direction,
    where the premature announcement at least erred loudly. There are two
    boundaries (the cloud turn and the local-brain turn) and both sit inside long
    async receive loops that need a Live session, a mic and a speaker to reach,
    so no unit test here can execute them.

    Reading the source of those two functions is therefore the strongest check
    available that is still cheap. It cannot prove the call runs at the right
    moment; it does prove nobody deleted it, which is the regression actually
    worth catching.
    """
    import inspect

    from app.era_live.audio import AudioIOMixin
    from app.era_live.local_fallback import LocalFallbackMixin

    cloud = inspect.getsource(AudioIOMixin._receive_audio)
    local = inspect.getsource(LocalFallbackMixin._local_turn)
    assert "_flush_failure_notices()" in cloud, "the cloud turn no longer flushes failure notices"
    assert "_flush_failure_notices()" in local, "the local turn no longer flushes failure notices"


def test_the_known_false_negative_is_pinned_not_forgotten(store):
    """Pins a trade-off, not a desirable behaviour — see core/tool_recovery.py.

    The key is (tool, action), so one tool called twice with different targets
    inside a compound command forgives the first target's failure. Keying on the
    parameters as well would close this, and would simultaneously break the case
    the module exists for: a retry with the parameter the error named as wrong.

    Asserted so that whoever changes the key sees the decision instead of
    re-discovering it, and so the day the logs say this case is common, the test
    that has to change is easy to find.
    """
    store.note_failure("file_controller", "read", "FileNotFound", "D:\\a.txt")
    store.note_success("file_controller", "read")  # a DIFFERENT file succeeded
    assert store.pending() == 0, (
        "if this now holds the notice, the key gained parameters — check that a "
        "corrected-parameter retry still cancels its own notice"
    )
