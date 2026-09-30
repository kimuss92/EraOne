"""Tests for "abort mission" — core/panic_stop.py and its tool-batch enforcement.

WHAT THIS PINS
--------------
Two things, and the second is the one that failed on 2026-09-21:

  1. RECOGNITION. "abort mission" is an abort order; a bare "stop" is not.
     The exclusions are not stylistic — "stop" is a media command, "cancel" is
     a confirmation answer, and "cancel the shutdown" is the power-countdown
     abort. Any of those becoming a panic stop would be a regression the user
     would feel within a day.

  2. ENFORCEMENT. A tripped latch refuses tool calls the model ALREADY queued,
     mid-batch, without breaking tool_batch's one-response-per-call invariant.
     The real incident was five `gmail_control send` calls inside 77ms; the
     only defence that can beat that is a per-call re-read of the latch, so
     that is asserted directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.tool_batch import ToolBatchMixin  # noqa: E402
from core.llm.exec_ledger import ExecLedger  # noqa: E402
from core.panic_stop import PanicStop, is_abort_order, panic  # noqa: E402
from tools.test_tool_batch import FakeFunctionCall, FakeSessionManager, payload  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_latch():
    """The latch is a module singleton; never let one test arm another."""
    panic.clear("test-setup")
    yield
    panic.clear("test-teardown")


# ── 1. Recognition ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "abort mission",
        "Abort mission!",
        "abort the mission",
        "abort everything",
        "abort abort",
        "mission aborted",
        "stop everything",
        "halt everything",
        "cancel everything",
        "drop everything",
        "stop what you are doing",
        "stop what you're doing",
        "stop stop stop",
        "emergency stop",
        "panic button",
        "stand down",
        "freeze",
        "arrête tout",
        "arrete tout",
        "annule tout",
    ],
)
def test_abort_orders_are_recognised(text):
    assert is_abort_order(text)


@pytest.mark.parametrize(
    "text",
    [
        # Already mean something else — see core/panic_stop.py's exclusion note.
        "stop",
        "cancel",
        "abort",
        "pause",
        "stop the music",
        "stop playing netflix",
        "stop listening",
        "cancel the download",
        # The power countdown keeps its own abort.
        "cancel the shutdown",
        "abort the reboot",
        "reboot era one",
        # Conversation about an abort is not an abort.
        "why did you abort the mission",
        "the mission was aborted yesterday",
        # Not the whole utterance.
        "stop everything and open spotify",
        "",
        "   ",
    ],
)
def test_non_abort_text_is_left_alone(text):
    assert not is_abort_order(text)


def test_wake_word_prefix_is_allowed():
    assert is_abort_order("shazam, abort mission", wake_words=["shazam"])
    assert is_abort_order("hey era abort mission")


# ── 2. The latch ─────────────────────────────────────────────────────────────


def test_latch_trips_and_expires():
    latch = PanicStop(ttl_s=10.0)
    assert not latch.active(now=1000.0)
    latch.trip("voice", "abort mission", now=1000.0)
    assert latch.active(now=1005.0)
    # Auto-clears: a latch with no expiry is one lost clear() away from a
    # permanently deaf Era, which is worse than the loop it prevents.
    assert not latch.active(now=1011.0)


def test_repeat_trip_rearms_the_window():
    latch = PanicStop(ttl_s=10.0)
    latch.trip("voice", "abort mission", now=1000.0)
    latch.trip("voice", "abort mission", now=1008.0)
    assert latch.active(now=1015.0), "the second abort must not inherit the first's deadline"


def test_clear_releases_early():
    latch = PanicStop(ttl_s=10.0)
    latch.trip("typed", "abort mission", now=1000.0)
    assert latch.clear("user") is not None
    assert not latch.active(now=1001.0)
    assert latch.clear("user") is None


def test_snapshot_reports_refusals():
    latch = PanicStop(ttl_s=10.0)
    latch.trip("voice", "abort mission", now=1000.0)
    latch.note_refusal()
    latch.note_refusal()
    snap = latch.snapshot(now=1002.0)
    assert snap["active"] and snap["refused"] == 2 and snap["source"] == "voice"


# ── 3. Enforcement in the tool batch ─────────────────────────────────────────


class FakeEra(ToolBatchMixin):
    """Minimal host; records what actually reached execution."""

    def __init__(self, abort_after: int | None = None) -> None:
        self.session_manager = FakeSessionManager()
        self.executed: list[str] = []
        self._tool_ledger = ExecLedger()
        self._abort_after = abort_after

    async def _execute_tool(self, fc):
        self.executed.append(fc.name)
        # Stands in for the user speaking partway through a batch Era is
        # already running — the 2026-09-21 case exactly.
        if self._abort_after is not None and len(self.executed) == self._abort_after:
            panic.trip("voice", "abort mission")
        return {"name": fc.name, "id": fc.id, "executed": True}


@pytest.mark.asyncio
async def test_latched_abort_refuses_every_call_but_still_answers_them():
    era = FakeEra()
    panic.trip("voice", "abort mission")
    calls = [FakeFunctionCall("gmail_control", {"action": "send"}) for _ in range(5)]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == [], "a latched abort must let nothing through"
    # The one-response-per-call invariant still holds: Anthropic rejects a turn
    # whose tool_result blocks do not match its tool_use blocks one for one.
    assert len(responses) == len(calls)
    for r in responses:
        assert payload(r)["data"]["reason"] == "aborted"


@pytest.mark.asyncio
async def test_abort_mid_batch_stops_the_calls_still_queued():
    """The defect that mattered: five sends in 77ms, aborted after the second.

    Calls already executed cannot be undone — but everything the model queued
    behind them must die, which needs the latch re-read per call rather than
    once per batch.
    """
    era = FakeEra(abort_after=2)
    calls = [FakeFunctionCall("gmail_control", {"action": "send", "n": i}) for i in range(5)]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["gmail_control", "gmail_control"]
    assert len(responses) == 5
    assert [payload(r).get("data", {}).get("reason") for r in responses[2:]] == ["aborted"] * 3


@pytest.mark.asyncio
async def test_abort_outranks_the_other_state_blocks():
    """Muted/reconnecting/standby say what Era cannot do; abort is an order."""
    era = FakeEra()
    era.session_manager.state["is_muted"] = True
    panic.trip("typed", "abort mission")

    responses = await era._run_tool_call_batch([FakeFunctionCall("browser_control")])

    assert payload(responses[0])["data"]["reason"] == "aborted"


@pytest.mark.asyncio
async def test_no_abort_means_business_as_usual():
    era = FakeEra()
    calls = [FakeFunctionCall("spotify_control", {"action": "play"})]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["spotify_control"]
    assert len(responses) == 1
