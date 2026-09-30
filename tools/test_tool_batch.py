"""Tests for app/era_live/tool_batch.py — one response per tool call, always.

THE INVARIANT
-------------
`len(responses) == len(calls)`, under every combination of state gate, turn
ending tool, and duplicate suppression. Gemini tolerated a short reply; Claude
rejects a turn whose tool_result blocks do not match its tool_use blocks, so a
batch that answers only some of its calls is unanswerable by half the models
Era is being taught to use.

This is also the layer where a model's *request* becomes a real action, so the
"did not run" cases are asserted on directly rather than inferred: a blocked
call must produce a response AND must not have reached `_execute_tool`.

No live session, no real tools — `_execute_tool` is recorded, not run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.era_live.tool_batch import HALTING_TOOLS, ToolBatchMixin  # noqa: E402
from core.llm.exec_ledger import ExecLedger  # noqa: E402


class FakeFunctionCall:
    """Stands in for google.genai's function-call object."""

    def __init__(self, name: str, args: dict | None = None, call_id: str | None = None) -> None:
        self.name = name
        self.args = args or {}
        self.id = call_id if call_id is not None else f"call-{name}-{id(self)}"


class FakeSessionManager:
    def __init__(self, **state) -> None:
        self.state = {
            "is_muted": False,
            "is_reconnecting": False,
            "standby_active": False,
            "wake_word_active": False,
        }
        self.state.update(state)
        self.snapshot_calls = 0

    def get_state_snapshot(self) -> dict:
        self.snapshot_calls += 1
        return dict(self.state)


class FakeEra(ToolBatchMixin):
    """Minimal host for the mixin: records what actually got executed."""

    def __init__(self, session_manager: FakeSessionManager | None = None, **state) -> None:
        self.session_manager = session_manager or FakeSessionManager(**state)
        self.executed: list[str] = []
        self.raise_on: str | None = None
        self._tool_ledger = ExecLedger()

    async def _execute_tool(self, fc):
        self.executed.append(fc.name)
        if self.raise_on == fc.name:
            raise RuntimeError(f"{fc.name} blew up")
        return {"name": fc.name, "id": fc.id, "executed": True}


def names(responses) -> list[str]:
    """Tool name for each response, real FunctionResponse or fake dict alike."""
    return [r["name"] if isinstance(r, dict) else r.name for r in responses]


def payload(response) -> dict:
    return response.response if hasattr(response, "response") else response


def is_blocked(response) -> bool:
    body = payload(response)
    return body.get("data", {}).get("executed") is False


# ── the invariant ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,tools",
    [
        ({}, ["volume_up", "web_search"]),
        ({"is_muted": True}, ["volume_up", "web_search"]),
        ({"is_reconnecting": True}, ["volume_up", "web_search"]),
        ({"standby_active": True}, ["volume_up", "web_search"]),
        ({"standby_active": True, "wake_word_active": True}, ["volume_up", "web_search"]),
        ({}, ["shutdown_era", "web_search", "volume_up"]),
        ({}, ["enter_standby", "web_search"]),
        ({}, ["volume_up", "shutdown_era", "web_search"]),
        ({"is_muted": True}, ["shutdown_era", "web_search"]),
        ({}, ["volume_up"]),
        ({}, ["volume_up", "volume_up", "volume_up"]),
    ],
)
async def test_every_call_gets_exactly_one_response(state, tools):
    """The core contract, across every gate/halt combination."""
    era = FakeEra(**state)
    calls = [FakeFunctionCall(t) for t in tools]

    responses = await era._run_tool_call_batch(calls)

    assert len(responses) == len(calls), (
        f"{len(calls)} calls produced {len(responses)} responses for {tools} in {state}"
    )
    assert names(responses) == tools


async def test_empty_batch_returns_empty():
    era = FakeEra()
    assert await era._run_tool_call_batch([]) == []


async def test_none_batch_returns_empty():
    era = FakeEra()
    assert await era._run_tool_call_batch(None) == []


# ── state gates ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state",
    [
        {"is_muted": True},
        {"is_reconnecting": True},
        {"standby_active": True},
    ],
)
async def test_gated_state_executes_nothing_but_still_answers(state):
    era = FakeEra(**state)
    calls = [FakeFunctionCall("volume_up"), FakeFunctionCall("send_message")]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == [], "a gated batch must not reach any tool"
    assert len(responses) == 2
    assert all(is_blocked(r) for r in responses)


async def test_fully_gated_batch_still_produces_sendable_responses():
    """Regression: the old loop returned [], and the send was behind
    `if fn_responses:` — so the model's turn waited forever on results that
    were never going to arrive."""
    era = FakeEra(is_muted=True)
    responses = await era._run_tool_call_batch([FakeFunctionCall("volume_up")])
    assert responses, "an all-gated batch must still answer the model"


async def test_standby_with_wake_word_is_not_gated():
    era = FakeEra(standby_active=True, wake_word_active=True)
    await era._run_tool_call_batch([FakeFunctionCall("volume_up")])
    assert era.executed == ["volume_up"]


async def test_blocked_response_is_marked_not_executed():
    era = FakeEra(is_muted=True)
    (response,) = await era._run_tool_call_batch([FakeFunctionCall("volume_up")])
    body = payload(response)
    assert body["success"] is False
    assert body["error_type"] == "BlockedAction"
    assert body["data"]["executed"] is False
    assert body["data"]["reason"] == "muted"


async def test_blocked_response_tells_the_model_to_stay_quiet():
    """Being muted is exactly the state where narrating the refusal is wrong."""
    era = FakeEra(is_muted=True)
    (response,) = await era._run_tool_call_batch([FakeFunctionCall("volume_up")])
    assert "Say nothing" in payload(response)["result"]


async def test_state_is_re_read_per_call():
    """A call earlier in a batch can change the state — entering standby is the
    obvious one — so the gate cannot be evaluated once for the whole batch."""
    sm = FakeSessionManager()
    era = FakeEra(sm)
    calls = [FakeFunctionCall("volume_up"), FakeFunctionCall("web_search")]

    await era._run_tool_call_batch(calls)
    assert sm.snapshot_calls == 2


async def test_a_tool_that_mutes_mid_batch_gates_the_rest():
    sm = FakeSessionManager()
    era = FakeEra(sm)

    original = era._execute_tool

    async def mute_then_execute(fc):
        result = await original(fc)
        if fc.name == "mute":
            sm.state["is_muted"] = True
        return result

    era._execute_tool = mute_then_execute
    calls = [FakeFunctionCall("mute"), FakeFunctionCall("send_message")]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["mute"]
    assert len(responses) == 2
    assert is_blocked(responses[1])


# ── turn-ending tools ─────────────────────────────────────────────────────


@pytest.mark.parametrize("halting", HALTING_TOOLS)
async def test_halting_tool_runs_and_blocks_the_rest(halting):
    era = FakeEra()
    calls = [FakeFunctionCall(halting), FakeFunctionCall("send_message")]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == [halting], "queued work must not run after the turn ends"
    assert len(responses) == 2
    assert is_blocked(responses[1]) is True
    assert is_blocked(responses[0]) is False


async def test_calls_before_a_halting_tool_still_run():
    era = FakeEra()
    calls = [
        FakeFunctionCall("volume_up"),
        FakeFunctionCall("shutdown_era"),
        FakeFunctionCall("send_message"),
    ]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["volume_up", "shutdown_era"]
    assert len(responses) == 3
    assert payload(responses[2])["data"]["reason"] == "turn_ended"


async def test_halted_response_names_the_tool_that_ended_the_turn():
    era = FakeEra()
    calls = [FakeFunctionCall("enter_standby"), FakeFunctionCall("send_message")]
    responses = await era._run_tool_call_batch(calls)
    assert "enter_standby" in payload(responses[1])["result"]


# ── duplicate suppression ─────────────────────────────────────────────────


async def test_replayed_batch_does_not_execute_twice():
    """The reason this exists: a replay must not send the message again."""
    era = FakeEra()
    calls = [FakeFunctionCall("send_message", {"to": "bob"}, call_id="c1")]

    first = await era._run_tool_call_batch(calls)
    second = await era._run_tool_call_batch(calls)

    assert era.executed == ["send_message"], "the replay re-executed the tool"
    assert len(second) == 1
    assert second[0] == first[0], "a duplicate must replay the original result"


async def test_a_fresh_generation_executes_again():
    """New call ids mean a genuine second ask — it must act."""
    era = FakeEra()
    await era._run_tool_call_batch([FakeFunctionCall("volume_up", call_id="c1")])
    await era._run_tool_call_batch([FakeFunctionCall("volume_up", call_id="c2")])
    assert era.executed == ["volume_up", "volume_up"]


async def test_two_identical_calls_in_one_batch_both_execute():
    """'Volume up, volume up' is two real actions, not a duplicate."""
    era = FakeEra()
    calls = [
        FakeFunctionCall("volume_up", call_id="c1"),
        FakeFunctionCall("volume_up", call_id="c2"),
    ]

    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["volume_up", "volume_up"]
    assert len(responses) == 2


async def test_replay_of_a_repeated_call_suppresses_both():
    era = FakeEra()
    calls = [
        FakeFunctionCall("volume_up", call_id="c1"),
        FakeFunctionCall("volume_up", call_id="c2"),
    ]

    await era._run_tool_call_batch(calls)
    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["volume_up", "volume_up"], "replay ran extra calls"
    assert len(responses) == 2


async def test_same_tool_different_args_is_not_a_duplicate():
    era = FakeEra()
    calls = [
        FakeFunctionCall("set_volume", {"level": 10}, call_id="c1"),
        FakeFunctionCall("set_volume", {"level": 20}, call_id="c2"),
    ]
    await era._run_tool_call_batch(calls)
    assert era.executed == ["set_volume", "set_volume"]


async def test_a_call_gated_on_the_first_pass_can_run_on_a_retry():
    """A blocked call never claimed the ledger, so unmuting and retrying the
    same batch must actually perform it."""
    sm = FakeSessionManager(is_muted=True)
    era = FakeEra(sm)
    calls = [FakeFunctionCall("send_message", call_id="c1")]

    await era._run_tool_call_batch(calls)
    assert era.executed == []

    sm.state["is_muted"] = False
    await era._run_tool_call_batch(calls)
    assert era.executed == ["send_message"]


# ── failure handling ──────────────────────────────────────────────────────


async def test_an_exploding_tool_propagates():
    era = FakeEra()
    era.raise_on = "web_search"
    with pytest.raises(RuntimeError):
        await era._run_tool_call_batch([FakeFunctionCall("web_search")])


async def test_a_failed_call_is_retryable():
    """The ledger claim must be reopened on the way out, or a retry of this
    batch would be refused as a duplicate of something that never ran."""
    era = FakeEra()
    era.raise_on = "web_search"
    calls = [FakeFunctionCall("web_search", call_id="c1")]

    with pytest.raises(RuntimeError):
        await era._run_tool_call_batch(calls)

    era.raise_on = None
    responses = await era._run_tool_call_batch(calls)

    assert era.executed == ["web_search", "web_search"]
    assert len(responses) == 1
    assert is_blocked(responses[0]) is False


# ── model transfer (an explicit request id) ───────────────────────────────


class RealResponseEra(FakeEra):
    """Returns a genuine FunctionResponse, so re-addressing can be observed."""

    async def _execute_tool(self, fc):
        self.executed.append(fc.name)
        return types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "done"})


async def test_an_explicit_request_id_dedupes_across_fresh_call_ids():
    """A request transferred to another model is a fresh generation with fresh
    ids. The derived default would not recognise it, so a router passes its own
    id — and the already-performed action must not run again."""
    era = RealResponseEra()

    await era._run_tool_call_batch(
        [FakeFunctionCall("send_message", {"to": "bob"}, call_id="gemini-1")],
        request_id="turn-42",
    )
    responses = await era._run_tool_call_batch(
        [FakeFunctionCall("send_message", {"to": "bob"}, call_id="claude-1")],
        request_id="turn-42",
    )

    assert era.executed == ["send_message"], "the transferred request re-sent the message"
    assert len(responses) == 1


async def test_a_replayed_result_is_readdressed_to_the_new_call():
    """A provider matches results to calls by id; handing back the first
    model's id would make the result unmatchable."""
    era = RealResponseEra()

    await era._run_tool_call_batch(
        [FakeFunctionCall("web_search", {"q": "x"}, call_id="gemini-1")],
        request_id="turn-42",
    )
    (response,) = await era._run_tool_call_batch(
        [FakeFunctionCall("web_search", {"q": "x"}, call_id="claude-1")],
        request_id="turn-42",
    )

    assert response.id == "claude-1"
    assert response.response == {"result": "done"}


async def test_rekey_leaves_a_matching_id_alone():
    era = RealResponseEra()
    original = types.FunctionResponse(id="c1", name="t", response={"result": "x"})
    call = FakeFunctionCall("t", call_id="c1")
    assert era._rekey_response(original, call) is original


async def test_rekey_passes_through_a_non_response_object():
    era = FakeEra()
    sentinel = {"not": "a FunctionResponse"}
    assert era._rekey_response(sentinel, FakeFunctionCall("t")) is sentinel


# ── plumbing ──────────────────────────────────────────────────────────────


async def test_ledger_is_created_lazily():
    class Bare(FakeEra):
        def __init__(self):
            super().__init__()
            self._tool_ledger = None

    era = Bare()
    assert isinstance(era._get_tool_ledger(), ExecLedger)
    assert era._get_tool_ledger() is era._get_tool_ledger()


async def test_responses_carry_the_original_call_id():
    """The provider matches results to calls by id; a blocked response that
    dropped it would be unmatchable."""
    era = FakeEra(is_muted=True)
    calls = [FakeFunctionCall("volume_up", call_id="abc-123")]
    (response,) = await era._run_tool_call_batch(calls)
    assert getattr(response, "id", None) == "abc-123"


async def test_a_nameless_call_still_gets_a_response():
    era = FakeEra(is_muted=True)
    call = FakeFunctionCall("volume_up")
    call.name = None
    responses = await era._run_tool_call_batch([call])
    assert len(responses) == 1


async def test_none_args_are_handled():
    era = FakeEra()
    call = FakeFunctionCall("volume_up")
    call.args = None
    responses = await era._run_tool_call_batch([call])
    assert len(responses) == 1
    assert era.executed == ["volume_up"]
